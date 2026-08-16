# 系统调用往返问题：答案与证据标准

先完成[问题页](../syscall-roundtrip.md)和 `getpid` checkpoint 表。本页给出
判定标准，不提供可替代运行记录的固定地址清单。

## SYSCALL-01

`ecall` 从 U mode 进入 S mode，把当前 `pc` 写入 `sepc`，把原因写入
`scause=8`，按 `sstatus` 规则保存前一特权/中断状态，并从 `stvec` 取得新
PC。它不保存全部 GPR，也不切 `satp` 或 `sp`。因此 `uservec` 入口仍使用
user page table 与 user stack；汇编必须先经 `sscratch` 保存原 `a0`，把
用户寄存器写入 `TRAPFRAME`，再加载四个 `kernel_*` 字段并切换栈、hart id、
页表和 C 入口。

通过答案必须把上述归属与实际入口寄存器并列，并明确 `SPP=0` 表示 trap
来自 U mode；不能把它误读成当前仍在 U mode。

## SYSCALL-02

`sepc` 指向触发 trap 的 `ecall`。若普通或未知系统调用返回到同一地址，就会
重复进入同一 trap；所以 `usertrap` 把保存的 `epc` 推进到下一条指令。
当前 `ecall` 的机器码是 32 位 `0x00000073`，故推进 4 字节；前一条 `li`
是否压缩不改变 `ecall` 长度。可修复 page fault 则必须重试发生 fault 的
load/store，提前推进会跳过尚未完成的内存操作。

通过答案必须同时给出正常与未知编号的 `E -> E+4` 运行关系。只引用
`epc += 4` 源码属于 `S` 证据，不足以替代 `F/B`。

## SYSCALL-03

进入 `usertrap` 后已经使用 kernel stack/page table。把 `stvec` 改成
`kernelvec` 可保证随后 supervisor trap 使用内核入口，而不会错误执行假定
user page table/TRAPFRAME 入口条件的 `uservec`。原 `sepc/scause/sstatus`
必须在 `intr_on()` 前消费；开中断后新的内核中断可以覆盖硬件 CSR，但已写入
`trapframe->epc` 的用户返回 PC 保持独立。`prepare_return` 反向执行：关中断，
把 `stvec` 指回 runtime `uservec`，设置 `SPP/SPIE/sepc`，再交给 `userret`。

通过答案必须指出“在 `syscall()` 读取原始 `scause`”的时间漏洞，并区分
kernel interrupt 的内核栈现场与进程 trapframe，不能用一次单 hart 调试结果
声称并发时序已被证明。
