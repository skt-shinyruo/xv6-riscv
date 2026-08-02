# 汇编边界专题

本目录不把汇编文件当作若干 `sd`/`ld` 的清单，而是说明它们与硬件 trap 语义、RISC-V psABI、C 编译器、页表和调度器共同建立的契约。阅读时应始终区分三类状态：硬件 CSR、活的通用寄存器、内存中的保存区。

| 文档 | 入口 | 核心问题 |
|---|---|---|
| [`entry.S` 与 `start()`](entry-and-start.md) | `_entry` | 无 C 运行环境时怎样选栈、建立 hart 身份并从 M-mode 降到 S-mode |
| [`swtch.S` 上下文切换](context-switch.md) | `swtch(old, new)` | 为什么只保存 callee-saved 寄存器，以及一次 `ret` 为什么返回到另一条控制流 |
| [`kernelvec.S` 内核 trap 向量](kernel-trap-vector.md) | `kernelvec` | 如何在当前内核栈保存易失寄存器并允许 handler 内调度 |
| [`trampoline.S` 用户 trap 边界](trampoline.md) | `uservec` / `userret` | 未换页表、未换栈时如何保存用户态，再原子式重建返回环境 |

四条路径的保存集合不同，不能互换：

```text
entry       没有旧控制流要恢复，建立首个 C 调用环境
swtch       保存 C continuation：ra, sp, s0..s11
kernelvec   显式保存 caller-saved GPR 与 gp；sp 可推导，s0..s11 由 C ABI 保持，tp 保留恢复 hart
uservec     保存完整用户 GPR 状态到每进程 trapframe
```

配套背景见[平台契约](../architecture/platform-contracts.md)、[启动流程](../architecture/boot-and-init.md)、[进程与调度](../kernel/processes-and-scheduling.md)和[Trap 与中断](../kernel/traps-and-interrupts.md)。修改任一汇编保存区时，必须同时核对对应 C 结构体偏移、链接布局、编译器 ABI 和至少一个反汇编结果。

## 共用验证清单

1. 用 `riscv64-unknown-elf-objdump -d kernel/kernel` 确认伪指令实际展开和重定位结果。
2. 用 `offsetof`/编译期断言核对 `struct context`、`struct trapframe` 与硬编码偏移。
3. 在 GDB 中分别断在 `_entry`、`swtch`、`kernelvec`、`uservec`、`userret`，记录 `sp/tp/satp/stvec/sstatus/sepc/sscratch`。
4. 分别在 `CPUS=1` 与多 hart 下观察；一次单核成功不能证明 hart 身份和迁移路径正确。
5. 对 trap 测试同时覆盖系统调用、用户页故障、内核 timer、UART 和 VirtIO 完成中断。
