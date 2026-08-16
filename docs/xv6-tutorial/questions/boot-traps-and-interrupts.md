# 启动、陷阱、中断与汇编边界问题

本页迁移自原问题集的 `BOOT-01`、`BOOT-02`、`BOOT-03` 和 `BOOT-09`。它们由
`core.boot-traps-and-interrupts` 拥有，硬前置是[启动、陷阱、中断与汇编边界](../core/boot-traps-and-interrupts.md)。
先提交自己的源码图和事件表，再查看[答案与证据标准](answers/boot-traps-and-interrupts.md)。

## 问题

### BOUNDARY-01（原 BOOT-01）

从 QEMU reset 到第一个用户态 timer preemption，哪些阶段处于 M mode、S mode
和 U mode？每个阶段使用哪一个栈、页表和 trap vector？

证据要求：用 `_entry`、`start()`、`main()`、`trapinithart()`、`uservec`、
`kernelvec` 和 `userret` 的稳定锚点画一条控制线，并在 runner 报告中标出
`mret`、timer cause、`sret` 的边界。不要用某次构建的绝对地址代替关系。

提示：`mret` 只完成 M->S 的特权交接；用户 trap 的第一条汇编指令仍在 S mode，
`SPP=0` 描述的是 trap 前的 U mode。

### BOUNDARY-02（原 BOOT-02）

`_entry` 为什么在进入 C 代码前用 `mhartid` 计算栈地址？如果 `CPUS > NCPU`，
为什么可能先破坏内存而不是得到一个干净的配置错误？

证据要求：引用 `kernel/entry.S:_entry` 的栈基址、hart stride 和 `call start`
顺序，并说明启动配置边界；不要把实验的 `CPUS=1` 结果推广成任意 hart 数量。

提示：数组边界检查若不存在，错误的 hart id 会影响栈指针本身，后续故障可能
发生在任意保存寄存器或 C 调用处。

### BOUNDARY-03（原 BOOT-03）

为什么其他 hart 必须等待 hart 0 发布全局初始化？只使用普通变量、没有
`__atomic_thread_fence` 会造成什么可观察风险？

证据要求：从 `kernel/main.c:main` 标出 `started` 的发布、两个 fence 和等待
顺序；把“看到 1”与“看到已发布的初始化写入”区分开。说明本单元没有用
`CPUS=1` runner 证明多 hart 的完整 happens-before。

提示：volatile 读写不等于跨 hart 的 release/acquire；即便最终输出正常，也不
能排除其他 hart 使用旧的内核页表根、锁/进程表或全局 PLIC 配置。

### BOUNDARY-04（原 BOOT-09）

`trapframe` 和 `context` 分别保存什么？timer trap 触发 `yield()` 时，两者在
哪一段控制流中同时有效？

证据要求：对照 `kernel/proc.h:struct trapframe`、`kernel/proc.h:struct context`、
`kernel/trampoline.S:uservec`、`kernel/kernelvec.S:kernelvec` 和
`kernel/swtch.S:swtch`，说明用户寄存器、内核 callee-saved 寄存器、栈和返回
地址的所有权；报告中必须指出 trace 只证明单 hart 的一次交接。

提示：`yield()` 保存的是可继续运行的 kernel context；它不会取代
`uservec` 已写入的 trapframe，也不会自行完成 `sret`。

## 提交边界

四题共同使用一份启动、陷阱、中断与汇编边界报告包，不另造同步 trace。答案
必须引用稳定 `path:symbol`、寄存器/栈关系和 runner 的 S/B/F 结果；只复述
调用链或抄固定地址不算完成。后续单元再迁移 `BOOT-04`、`BOOT-06`、`BOOT-10`
等尚未拥有的问题。
