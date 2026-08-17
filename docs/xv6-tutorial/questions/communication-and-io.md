# 通信与设备 I/O 问题

本页由 [`core.communication-and-io`](../core/communication-and-io.md) 拥有。答案必须
使用同一份 `ioflow` 报告，不把 `PASS` 或超时当作正确性证明。

### IO-00

区分 `p->ofile[fd]`、`struct file`、`struct pipe` 和 `struct proc` 的 ownership。
`fork`、`dup`、`close` 分别改变哪一层？

### IO-01

沿 `user/sh.c:runcmd` 画 `cmd1 | cmd2` 的 fork/dup/close/wait 图。遗漏哪一个端点会
分别造成永不 EOF 或无法得到 broken-end？

### IO-02

当 pipe 满或空时，`pipewrite`/`piperead` 的谓词、channel、条件锁和 wakeup 是什么？
为什么 wakeup 不是字节的所有权转移？

### IO-03

证明 writer 关闭后空读返回 `0`，reader 关闭后写返回 `-1`。请指出
`pipeclose` 唤醒的相反 channel。

### IO-04

如何用 `iosnapshot(fd,pid,addr)` 构造“先看到 child 为 `SLEEPING` 且 channel/occupancy
符合谓词，再 kill，最后 wait”的 deterministic oracle？为什么 timeout 或 kill 后的任意
退出不能证明 blocked-waiter 行为？

### IO-05

console read 的 `cons.r/cons.w`、UART transmit busy、PLIC claim/complete 各自拥有
什么状态？为什么 console file descriptor 不等于 UART wait channel？

### IO-06

一次 inode read 怎样经过 `fileread -> readi -> bread`，并在 cache miss 时进入
`virtio_disk_rw -> interrupt -> brelse`？为什么普通 read 不持有 log reservation，
哪些资源仍属于后续 persistence 单元？

### IO-07

复核 `ioflow` 的 raw ledger：`IO BASE` 与每个阶段的 fd slots、active files/refs/pipes、
procs/free pages、child state/channel、occupancy/open flags 各是什么？正常、满/空、断端、
killed waiter 后如何证明 cleanup 回到 BASE？

### IO-08

报告中的 S/F/B/C/R 各自建立了什么有限结论？为什么 fixture、CPUS=1 和串口 marker
不能推出调度公平性、DMA ordering 或 crash recovery？
