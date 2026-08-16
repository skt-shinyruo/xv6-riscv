# 系统调用往返问题

本页迁移自原问题集的 `BOOT-05`、`BOOT-07` 和 `BOOT-08`。它们由
`core.syscall-roundtrip` 拥有，硬前置是[一次系统调用如何往返](../core/syscall-roundtrip.md)。
先提交自己的 checkpoint 表，再查看[答案与证据标准](answers/syscall-roundtrip.md)。

## 问题

### SYSCALL-01（原 BOOT-05）

用户在 `getpid` stub 执行 `ecall` 时，RISC-V 硬件自动改变哪些 CSR、PC 和
特权状态？哪些用户通用寄存器、页表和栈不会自动改变，必须由 `uservec`
处理？

证据要求：并列记录 `ecall` 前与 runtime `uservec` 入口的 `pc/sp/satp/a0/a7`
和 `sepc/scause/sstatus/stvec`，将每个变化归属给硬件或 `uservec`。

提示 1：先找 `kernel/trampoline.S:uservec` 的第一条指令；在它尚未执行时，
已经发生的变化才可能由 trap 硬件完成。

提示 2：`sstatus.SPP` 描述前一模式，不等于当前模式；检查 `sscratch`、
`TRAPFRAME` 和第一次 `ld sp` 的顺序。

### SYSCALL-02（原 BOOT-07）

为什么系统调用路径必须令 `trapframe->epc += 4`，而可修复的 page fault
不能照搬这个动作？当前 stub 中 `li` 可能只有 2 字节，为什么仍然加 4？

证据要求：从反汇编取得本次 `ecall` 地址 `E` 和下一条指令地址，在正常与
未知编号两个实例中都证明只到达一次 `E+4`；再从异常语义解释 page fault
为何需要重试原指令。

提示 1：推进的是 `ecall` 的地址，不是整个 stub 的起点。

提示 2：把未知编号返回路径想成一个反例；若仍回到 `E`，控制台会出现什么？

### SYSCALL-03（原 BOOT-08）

`usertrap()` 为什么先把 `stvec` 改到 `kernelvec`、保存 `sepc/scause/sstatus`
相关状态并推进 `epc`，之后才允许中断？如果在 `syscall()` 断点才读取硬件
`scause`，为何不能断言它仍是原用户 `ecall` 的原因？

证据要求：标出 `kernel/trap.c:usertrap` 中开中断前后的操作，并说明一次
supervisor timer/device interrupt 会使用哪套 vector、栈和保存位置。

提示 1：区分进程 trapframe 中已持久化的用户 PC 与会被下一次 trap 改写的
硬件 CSR。

提示 2：进入 C 后已经在 kernel page table 与 kernel stack；此时若仍把
`stvec` 指向 `uservec`，会违反哪项入口假设？

## 提交边界

三题共同使用一份 `getpid` 报告包，不另造同步 trace。答案必须引用稳定的
`path:symbol` 和实际寄存器关系；只复述调用链或抄本次绝对地址不算完成。
`BOOT-06` 的完整 trampoline 映射契约、`BOOT-09` 的阻塞 context 和
`BOOT-10` 的跨 hart `tp` 仍由后续单元负责。
