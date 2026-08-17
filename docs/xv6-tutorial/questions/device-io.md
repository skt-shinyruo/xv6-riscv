# 设备中断与 VirtIO 队列问题

学习目标：从外部 console byte 或 disk request 建立两条端到端流程，随后解释对象状态、锁、
内存发布、容量失败与回收。硬前置是[设备中断与 VirtIO 队列](../core/device-io.md)。每题从
给定 symbol 开始，沿调用和状态变化阅读，不以函数名释义作答。

## 高层总览

### DEVICE-01 两条设备路径如何共享 interrupt 骨架？

从 `kernel/main.c:main`、`kernel/trap.c:devintr` 和 `kernel/memlayout.h:UART0_IRQ` 开始，画出
console RX 与 VirtIO completion 的共同 PLIC 骨架，以及分叉后的 owner、输入和 caller 结果。

## 流程骨架

### DEVICE-02 一个 host 输入行如何到达 `read(0, ...)`？

从 `kernel/plic.c:plic_claim` breadth-first 追到 `kernel/uart.c:uartintr`、
`kernel/console.c:consoleintr`、`kernel/proc.c:wakeup` 和 `kernel/console.c:consoleread`。先列阶段，
暂不深入 ring 的每个分支。

### DEVICE-03 一个未缓存 block read 如何完成？

从 `kernel/bio.c:bread` 追到 `kernel/virtio_disk.c:virtio_disk_rw`、`kernel/trap.c:devintr`、
`kernel/virtio_disk.c:virtio_disk_intr`、caller sleep/wakeup 与 `kernel/bio.c:brelse`，区分
completion 和 reclamation，并判断 CPUS=2 下 sleep 与 IRQ 是否必须全序。

## 局部深入

### DEVICE-04 PLIC 的 global 与 per-hart 配置分别保证什么？

比较 `kernel/plic.c:plicinit`、`kernel/plic.c:plicinithart`、claim register 与 complete write；
说明 priority、enable、threshold、claim/complete 分别属于哪个状态，为什么设备 ACK 不能替代
PLIC complete。

### DEVICE-05 console RX/TX 为什么不是一个 wait channel？

比较 `kernel/console.c:consoleread` 的 `&cons.r`、`kernel/console.c:consoleintr` 的 ring indices，
以及 `kernel/uart.c:uartwrite` 的 `tx_busy/&tx_chan`。给出每把 lock 保护的字段与 sleeper/waker。

### DEVICE-06 queue 初始化后，哪些地址可供设备访问？

从 `kernel/virtio_disk.c:virtio_disk_init` 和 `kernel/virtio.h:struct virtq_desc` 追踪三页分配、
MMIO address register、queue ready 和 driver ready。区分虚拟代码引用、物理地址登记与运行时
DMA 可见性主张。

### DEVICE-07 三段 descriptor 如何编码一次 read？

从 `kernel/virtio_disk.c:virtio_disk_rw` 记录 header/data/status 的 addr、len、flags、next，说明
read/write 时 `VRING_DESC_F_WRITE` 的方向为何不同，并定位 `disk.info[head].b` 与 `b->disk`。

## 横切机制

### DEVICE-08 两个 fence、avail idx 和 notify 建立了什么，没建立什么？

沿 `kernel/virtio_disk.c:virtio_disk_rw` 的 ring write、两个
`__atomic_thread_fence(__ATOMIC_SEQ_CST)`、idx increment 和 MMIO notify 排序。说明可由源码与本
runner 复核的 CPU 发布顺序，以及不能由此推出的任意 DMA memory model。

### DEVICE-09 used ring 到 free descriptor 之间谁仍拥有资源？

比较 `kernel/virtio_disk.c:virtio_disk_intr` 与 caller 在 `virtio_disk_rw` 醒来后的清理，记录
status、`b->disk`、`disk.info`、descriptor free bitmap 和两个 wake channel 的提交/回收时点。

### DEVICE-10 为什么 8 个 descriptor 只能支持两笔三段请求在途？

从 `kernel/virtio.h:#define NUM`、`kernel/virtio_disk.c:alloc3_desc` 与
`kernel/virtio_disk.c:free_desc` 计算容量。给出部分分配回滚、第三 requester 的 state/channel、
确定性触发和最终 ledger；timeout 不得充当 oracle。

## 全流程串联

### DEVICE-11 如何复核一次外部输入和三笔磁盘请求？

只复用 DEVICE-01..10 的概念，分别重建两个具体场景：`D14-input\n` 唤醒 console reader；
blocks 1996..1998 在两笔 deferred completion 下制造 descriptor 等待。把 IRQ、hart、lock、
channel、owner、偏序 seq、allowed side effects、cleanup 和证据限制放进同一报告包。

答案与证据标准见[配套答案](answers/device-io.md)。
