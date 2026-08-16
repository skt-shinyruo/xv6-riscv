# 启动、陷阱、中断与汇编边界：答案与证据标准

先完成[问题页](../boot-traps-and-interrupts.md)和报告包中的源码图、控制
transcript、timer 事件表。本页给出判定标准，不提供可以替代运行记录的固定
地址清单。

## BOUNDARY-01

QEMU 从 M mode 的 `_entry`/`start()` 开始。`mret` 根据 `mstatus.MPP` 进入
S mode 的 `main()`；hart 初始化后，用户进程在 U mode 运行。用户 timer trap
先到同址 trampoline 的 `uservec`（CPU 仍为 S mode，用户 `satp/sp` 尚未由硬件
自动切换），再由 C 路径进入 `clockintr()`/`yield()`；返回经过 `userret` 和
`sret` 回到 U mode。kernelvec 只处理已经在 kernel 页表/栈上的 supervisor
trap。通过答案必须把页表和栈切换归给相应汇编入口，不能把 `mret` 或硬件 trap
描述成自动保存全部 GPR。

## BOUNDARY-02

`_entry` 先从 `mhartid` 算出 `stack0 + (hartid + 1) * 4096`，再设置 `sp`
并调用 `start`。这是每 hart 进入 C 前唯一可靠的栈来源；若配置的 hart 数
超过静态栈数组，地址计算不会自动产生友好的错误，错误 hart 可能在保存
`ra`、调用 C 或执行 fence 前就写出分配范围。通过答案必须给出源码顺序和
边界推理，而不是声称 CPUS=1 已覆盖越界。

## BOUNDARY-03

hart 0 先初始化全局 trap/PLIC 状态，再以 `__ATOMIC_SEQ_CST` fence 发布
`started=1`；其他 hart 循环等待并执行同样的 seq_cst fence 后才继续。普通变量的可见值不能单独建立前置
初始化写入的 happens-before；没有 fence 时，其他 hart 可能读到旧的内核页表
根、锁/进程表或全局 PLIC 配置，即便一次运行恰好通过。当前 runner 使用 CPUS=1，故只能静态复核
该发布契约，不能宣称多 hart 运行已验证。

## BOUNDARY-04

`trapframe` 是每个进程的用户寄存器和返回信息（包括用户 `epc`、`a0`、`a7`
以及 kernel_satp/kernel_sp 等入口字段）；`context` 是调度器交换的 kernel
callee-saved `ra/sp/s0-s11`。`kernelvec` 保存 `ra/gp/t0-t6/a0-a7`，不恢复
hart-local 的 `tp`；`uservec` 先填 trapframe；用户态 timer 经
`usertrap -> devintr -> clockintr`，内核态 timer 才经
`kernelvec -> kerneltrap -> devintr -> clockintr`。`yield()` 通过 `swtch`
保存当前 context 并恢复另一个 context；被抢占进程的 trapframe 仍保留用户
返回状态。恢复后
才由 `userret` 读取 trapframe 并 `sret`。通过答案必须区分“同时存在”与“同一
结构保存全部状态”，并说明单 hart trace 不建立锁图或跨 hart 顺序。

## 证据边界

S 证据来自 pinned baseline 的稳定锚点和静态契约；B 证据来自未 arm 的控制
命令零 marker；F 证据来自 `USERTRAP -> CLOCK -> YIELD -> RESUME` 的精确
顺序、同 pid 和单 tick 增量。timeout 只是 watchdog。由于实验是 CPUS=1、带
instrumentation 和有界用户循环，不能由此推出实时延迟、公平性、PLIC 队列
公平、DMA 顺序、完整 trampoline 映射安全性或恢复性质。
