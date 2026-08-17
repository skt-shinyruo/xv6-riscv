# 设备中断与 VirtIO 队列：从外部事件到完成回收

## 问题场景与本单元成果

上一单元已经把 `read`/`write` 追到 console 或 buffer cache，但把真实设备完成留作黑盒。
本单元沿两条可观察流程继续向下：host 输入字节经 PLIC、UART 和 console ring 唤醒
reader；只读磁盘请求经 buffer、三段 VirtIO descriptor、avail/used ring 和完成中断返回
caller。完成后你应能：

- 区分 programmed、driver-published、device-owned、DMA-visible address、notified、completed
  和 reclaimed，而不把 `b->disk=1`、一次 fence 或一次中断混成同一事件；
- 对 UART IRQ 10 和 VirtIO IRQ 1 记录 claim/handler/complete、hart、锁与 wait channel；
- 用 `NUM=8`、每请求三个 descriptor 构造两笔在途、第三笔等待的确定性容量 oracle；
- 提交一份设备 I/O 报告包，由 host 从 raw marker 重算事件顺序和资源归零。

## 前置单元与暂存黑盒

硬前置是[文件描述符、管道、控制台与设备](communication-and-io.md)。它给出 file/console/buffer
边界；[启动、陷阱、中断与汇编边界](boot-traps-and-interrupts.md)给出 trap/interrupt 入口；
[调度与同步](scheduling-and-synchronization.md)给出 spinlock、sleep/wakeup 和 per-hart 中断状态。

本单元把 QEMU 的 16550A UART 与 virtio-mmio 当作外部实现，不解释设备模型内部线程，也不把
CPU fence 当成形式化 DMA 一致性证明。磁盘内容如何成为 inode、日志提交如何变成持久状态、
crash 后如何恢复，继续留给后续文件系统与持久化单元。

## 最小模型和关键不变量

### PLIC 只路由外部中断

`plicinit()` 为 UART0_IRQ=10 与 VIRTIO0_IRQ=1 设置非零 priority；每个 hart 的
`plicinithart()` 独立写 enable bits 和 threshold。supervisor external interrupt 到达
`devintr()` 后，`plic_claim()` 返回 IRQ，handler 消费设备状态，最后 `plic_complete(irq)`
解除 PLIC 对该 source 的占用。设备自己的 ACK 与 PLIC complete 是两个不同动作：VirtIO
先写 `VIRTIO_MMIO_INTERRUPT_ACK`，`devintr()` 返回前才 complete PLIC。

不变量：每个非零 claim 最终恰有一个同 IRQ complete；handler 不能用 timeout 代替设备状态
消费；hart mask 只说明本次运行在哪些 hart 处理过事件，不证明固定 affinity。

### console RX 与 UART TX 使用不同状态

RX 正常路径是：host byte -> UART RHR -> IRQ 10 -> `uartintr()` -> `consoleintr(c)` ->
`cons.buf`。newline 推进 `cons.w` 并 `wakeup(&cons.r)`；`consoleread()` 在空 ring 上以
`cons.lock` 调用 `sleep(&cons.r, &cons.lock)`，恢复后复制字节并返回 caller。

TX 的 `tx_lock/tx_busy/&tx_chan` 是另一套状态。`uartwrite()` 可睡眠等待 TX-ready interrupt；
`uartputc_sync()` 则自旋，供 printk 与 input echo 使用。不能从“都是 UART”推出 RX reader
睡在 `&tx_chan`，也不能从 console fd 推出 driver wait channel 身份。

### VirtIO 请求的所有权阶段

`virtio_disk_init()` 分配并清零 descriptor、avail、used 三页，把物理地址写入 MMIO，再把
queue 0 标为 ready。一次 block request 使用三个不必连续的 descriptor：

1. request header：type/sector，`NEXT`；
2. 1024-byte `b->data`：read 时由设备写，故 `WRITE|NEXT`；
3. one-byte status：由设备写，故 `WRITE`。

在 `disk.vdisk_lock` 下，driver 填好链、令 `b->disk=1`、登记 `disk.info[head].b`，再把 head
写入 avail ring。第一个 seq_cst fence 位于 ring entry 与 `avail->idx` 之间；第二个在 idx
与 `QUEUE_NOTIFY` 之间。这里可以观察 CPU 发布顺序和设备可访问的物理地址，不能仅凭一次运行
证明任意设备实现的 DMA memory model。

device 把 head 写入 used ring 并更新 used idx 后触发 IRQ 1。`virtio_disk_intr()` ACK 设备，
检查 status，令 `b->disk=0` 并 `wakeup(b)`；等待 caller 恢复后才清 `info`、`free_chain()`
并把三个 descriptor 归还。因此 completed 不等于 reclaimed。

### 容量边界

`NUM=8` 且每请求需要三个 descriptor，所以两笔请求占六个后只剩两个；第三笔的
`alloc3_desc()` 必须回滚其部分获取并在 `&disk.free[0]` 上睡眠。任一完成 caller 释放 chain
时，`free_desc()` 唤醒该 channel。边界 oracle 必须先观察 free=2、两笔 deferred completion
和第三进程 `SLEEPING`，再释放 gate；单纯“最后三个 read 都返回”不能证明容量窗口出现过。

## 源码追踪计划

先从 manifest 的 path:symbol anchors 建图：

```sh
rg -n 'plicinit\(|plicinithart\(|plic_claim\(|plic_complete\(' kernel/plic.c
rg -n 'devintr\(|UART0_IRQ|VIRTIO0_IRQ|uartintr\(|virtio_disk_intr\(' kernel/trap.c kernel/memlayout.h
rg -n 'uartinit\(|uartwrite\(|uartputc_sync\(|uartintr\(' kernel/uart.c
rg -n 'consoleread\(|consoleintr\(|sleep\(&cons.r|wakeup\(&cons.r' kernel/console.c
rg -n 'struct virtq_desc|struct virtq_avail|struct virtq_used|#define NUM' kernel/virtio.h
rg -n 'virtio_disk_init\(|alloc3_desc\(|free_desc\(|free_chain\(|virtio_disk_rw\(|virtio_disk_intr\(' kernel/virtio_disk.c
rg -n 'b->disk|bread\(|brelse\(' kernel/buf.h kernel/bio.c kernel/virtio_disk.c
```

在 worksheet 中分别画 console 与 disk 两条 breadth-first 骨架，再补每一步的 owner、锁、
channel、hart 和提交/回收点。不要按文件目录逐个抄函数。

## 观察任务

先运行 host oracle 的 mutation self-test：

```sh
python3 docs/xv6-tutorial/resources/device-io/run-lab.py --self-test
```

它接受一份 worked trace，并拒绝未知/重复/缺失/非整数字段、额外 `DEV FAIL/PASS`、channel
不同、descriptor 与锁账本错误、重复/倒序事件、hart ledger 不同、容量错误和伪造 PASS。
再阅读 `device-audit.patch`，定位下列 seam：

```sh
rg -n 'devaudit_(plic|uart|console|disk|desc)|virtio_disk_audit_' \
  docs/xv6-tutorial/resources/device-io/device-audit.patch
```

记录 fixture 与 baseline 的边界：生产函数仍由真实 QEMU MMIO/interrupt 驱动；fixture 只增加
观测、两笔完成的有界 defer/release gate 和一个 audit syscall。它不是新的生产 driver 接口。

## 有界修改任务

完整 runner 把 patch 只应用到 pinned baseline 的临时导出，构建私有 fs.img，并执行三个竖切片：

```sh
python3 docs/xv6-tutorial/resources/device-io/run-lab.py \
  --report /tmp/xv6-device-io-report.md
```

1. `devtrace console` 的 child 先 arm 后 `read(0, ...)`；parent 只有在 raw state 显示
   `SLEEPING` 与非零 channel 后才打印 `CONSOLE_WAIT`。host 收到该 marker 后注入唯一
   `D14-input\n`。
2. `devtrace disk` 读未缓存高位 block 1999。fixture 暂存一次已到达的 used completion，不立即
   清 `b->disk`/wakeup；controller 等到 completion 与 caller sleep 都已观察到后再 release。
   CPUS=2 上 sleep 与 IRQ 的先后不固定，报告只要求二者都在 release/wake 之前。
3. `devtrace queue` 让三个 child 读取 1996..1998。前两笔完成暂存但不回收 descriptor，第三笔
   在 free=2 时睡眠；parent 观察完整边界后一次 release。

patch 不写这些 blocks，只读私有镜像。runner 随后运行 `pipe1`、`writebig`、`bigfile`、
`manywrites`，再运行 quick CPUS=2 与完整 CPUS=1 usertests。最后 `make clean`、reverse patch，
临时源码 snapshot 必须与 baseline 完全相同，共享 worktree 与共享 fs.img digest 必须不变。

## Oracle、证据、失败路径和局限

| 维度 | 可执行 oracle |
| --- | --- |
| S | 15-path patch scope、manifest anchors、descriptor flags/length、MMIO queue address 与 fence/notify 源码顺序可重查 |
| F | console 的 10 bytes、IRQ 10 claim/complete、同一 wait/wake channel；disk 的 8->5->8 descriptor、avail/used +1、status=0，以及 notify 先于 sleep/IRQ、二者均先于 wake/reclaim 的偏序 |
| B | 两笔 deferred 后 free=2；第三 pid 在 descriptor channel 上 state=2；release 前五个事件 seq 均已发生，最终 avail/used +3 |
| C | CPUS=2 下记录 submit hart、相同的 claim/complete hart mask 与 controller hart；gate 顺序而非随机 delay 建立进度关系 |
| R | N/A；没有 crash、host flush 或重启恢复 oracle |

每条 marker 使用精确 schema；duplicate/unknown/missing/non-integer field、额外 `DEV` marker
和重复 seq 都失败。runtime lock ledger 还必须证明 console publish 在 `cons.lock` 内、VirtIO
program/publish/notify/complete/reclaim 在 `disk.vdisk_lock` 内，以及 `sleep()` 从 condition lock
held 交接为 `p->lock` held/condition lock released；wakeup 扫描持有目标 `p->lock`。每个场景之后必须看到
`active=0 waiters=0 deferred=0 free_desc=8 info=0 buf_refs=0`。缓存项可以继续保持 valid，但
没有 fixture、descriptor、request-info 或 buffer 引用仍被占有。QEMU watchdog 只负责终止挂起进程，不是
坏路径判据。

证据限制：instrumentation 和 deferred completion 改变时序；pointer/PA 只在同一次 boot 内比较；
hart mask 不证明中断 affinity；seq_cst fence 加 raw address 只支持当前源码发布顺序，不构成对
所有设备实现的 DMA happens-before 证明；只读高位 block 会在私有 buffer cache 留下 valid data，
但 `buf_refs=0` 证明没有活跃引用；它不验证写回、持久化或恢复。

## 退出产物与后续单元

唯一退出产物是一份按 `report-template.md` 完成的设备 I/O 报告包；源码/runtime worksheet
直接嵌在报告内，不另交第二份文档。`run-lab.py --report /tmp/xv6-device-io-report.md` 生成的是
机器证据附录，报告包引用其 digest 与 raw evidence。机器附录不能稳定自包含自己的 SHA；最终
tracked review record 在外部记录该 digest、author/non-author 签名和修正结论。rubric 与报告
模板位于同名 resource 目录。

下一单元进入文件系统与持久化：从本单元已经完成并回收的 buffer 开始，解释 inode、log、
transaction、disk write、crash tear 与 recovery；不要把本单元的 interrupt completion 误写成
持久化完成。
