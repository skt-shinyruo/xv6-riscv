# 设备中断与 VirtIO 队列答案与证据标准

## DEVICE-01

hart 0 在 `main()` 初始化 console、trap、PLIC、buffer cache 与 VirtIO；其他 hart 等待全局发布
后执行 per-hart trap/PLIC 初始化。两条路径都以 supervisor external interrupt 进入
`devintr()`，claim 后按 IRQ 10/1 分派，handler 返回后 complete。分叉处分别由 UART/console
ring 与 VirtIO queue/buffer 拥有状态；caller 结果分别是 console bytes 与仍持有 sleeplock 的
buffer，消费完成后才由 `brelse()` 解锁并减少引用。

## DEVICE-02

IRQ 10 的 handler 从 RHR 取 byte，`consoleintr()` 在 `cons.lock` 下处理编辑字符并推进 `e/w`；
newline 把 `w=e` 并 `wakeup(&cons.r)`。空 ring reader 先在同一 lock 契约下睡眠，恢复后逐 byte
copyout，遇 newline 返回。可接受证据必须先观察目标 pid state=2 与非零 channel，再注入输入，
并令 wait/wake raw channel 相等。

## DEVICE-03

`bread()` 的 cache miss 得到持有 sleeplock 的 buffer，`virtio_disk_rw()` 在 vdisk spinlock 下
分配和发布 descriptor，并准备在 `b` 上睡眠。CPUS=2 下 IRQ 可在 caller 真正进入 `sleep()` 前
到达；baseline handler 会提交 status、`b->disk=0` 与 wakeup，fixture 则暂存该提交以建立可观察
窗口。caller 恢复后清 info、free chain，返回 `bread()`，最后 `brelse()` 释放 buffer
sleeplock/ref。因此 used entry 可见不是 descriptor 已回收，sleep 与 IRQ 也不是固定全序。

## DEVICE-04

`plicinit()` 写两 source 的 global priority；`plicinithart()` 写当前 hart 的 enable bitmap 与
threshold。读取 claim register 取得并占用最高优先级 pending source；向同一 hart 的 claim/
complete register 写 IRQ 才允许该 source 再次递送。VirtIO ACK 清设备 interrupt status，PLIC
complete 清 controller 路由状态，二者对象不同。

## DEVICE-05

`cons.lock` 保护 console input ring 的 `r/w/e`；reader 睡在 `&cons.r`，newline/EOF 唤醒它。
`tx_lock` 保护 `tx_busy`，writer 睡在 `&tx_chan`，TX-ready interrupt 清 busy 并唤醒。RX 与 TX
可由同一次 `uartintr()` 检查，但 channel、owner 与进度条件仍不同；console fd 只是 file-table
入口，不拥有任一 driver channel。

## DEVICE-06

driver 用 `kalloc()` 得到 descriptor/avail/used 三页并写入 virtio-mmio 的 low/high address
register，再设置 queue size/ready 与 DRIVER_OK。源码证明这些物理地址被登记并按 fence 顺序发布；
runtime marker 可验证非零页对齐地址与 ready=1。它看不到 QEMU 设备内部 load 顺序，故不能把
“地址已登记”写成所有平台上的 DMA happens-before 证明。

## DEVICE-07

head 指向 `virtio_blk_req`，len=16、flags=NEXT；data descriptor len=1024，read 时设备写内存，
flags=WRITE|NEXT；status len=1、flags=WRITE。三个 index 不必连续但必须互异并由 next 串起。
`disk.info[head].b` 让 completion 从 used id 找回 buffer，`b->disk=1` 表示 driver 正把 buffer
视为设备拥有。write 时 data 由设备读取，所以不设 WRITE。

## DEVICE-08

driver 先写 descriptor 与 avail ring head，fence 后递增 avail idx，再 fence 后写 notify MMIO。
这给 CPU/compiler 发布动作建立明确源码顺序，并避免 idx/notify 越过先前普通内存写。证据没有
观察任意真实设备内部缓存、总线或 IOMMU，不能独立证明通用 DMA memory model；本分支与 QEMU
的具体契约仍是必要前提。

## DEVICE-09

设备写 used entry/status 后，handler ACK、检查 status、清 `b->disk` 并 wake buffer sleeper；
此刻 `disk.info` 和三个 descriptor 仍被该 caller 占有。caller 退出 sleep loop 后清 info 并
`free_chain()`，每个 `free_desc()` 又唤醒 `&disk.free[0]` 上的容量 waiter。最终 free=8、
info=0 才是 reclaimed。

## DEVICE-10

两笔请求使用 6/8 descriptor，第三次 `alloc3_desc()` 最多暂取剩余两个，然后必须逐个
`free_desc()` 回滚并返回失败；outer loop 睡在 `&disk.free[0]`。可信 B oracle 是：两笔真实
completion 已进入 deferred set，free=2，第三 pid state=SLEEPING 且 channel 等于 descriptor
channel；release 前这些 raw seq 均存在，之后 avail/used 各增 3、free=8、deferred/info=0。

## DEVICE-11

console 报告应串起 wait -> IRQ10 claim -> UART RX -> ring publish -> wake -> read return，并复核
PLIC claim/complete ledger 与实际 hart mask。disk/queue 报告应串起 program -> avail publish ->
notify，随后把 buffer sleep 与 IRQ1/used/status 作为可并发分支；两者汇合后才允许 controller
release -> wake -> reclaim。
`DEV AFTER` 是两个故事共同的资源终点。允许副作用只有临时 patch、私有 fs.img 读取、QEMU
进程和外部 `/tmp` 报告；R=N/A，地址仅在一次 boot 内比较，gate 改变时序且不证明所有交错。
