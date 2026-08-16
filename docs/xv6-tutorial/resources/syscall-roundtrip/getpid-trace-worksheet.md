# `getpid` 往返报告包

## 环境与静态身份

- 教程提交：
- pinned source baseline：
- QEMU / GDB / RISC-V toolchain：
- `CPUS` / 内存：
- `SYS_getpid` / 未知编号：
- stub / `ecall E` / `E+4`：
- runtime `uservec` / `userret`：

## 正常编号 checkpoint

| checkpoint | 模式 | `pc` | `satp` | `sp` | CSR / trapframe / dispatch / result |
|---|---|---:|---:|---:|---|
| STUB | | | | | |
| ECALL_BEFORE | | | | | |
| ECALL | | | | | |
| USERVEC | | | | | |
| SAVED | | | | | |
| KSTACK | | | | | |
| KPAGETABLE | | | | | |
| USERTRAP | | | | | |
| ADVANCED（`epc=E+4`、`SIE=0`） | | | | | |
| SYSCALL | | | | | |
| HANDLER | | | | | |
| PREPARE | | | | | |
| USERRET | | | | | |
| USER_SATP | | | | | |
| RESTORED | | | | | |
| AFTER | | | | | |

关系检查：

- `scause=8`、`SPP=0`，且原始 CSR 在 `intr_on()` 前取得：
- `$priv` 在 stub/AFTER 为 0、其余 checkpoint 为 1：
- stub `ra` 等于 `reparent` 首次 `jal getpid` 的返回地址：
- user/kernel `satp` 不同；切换前后 trampoline PC 保持可执行：
- user/kernel `sp` 不同；恢复后的 `sp` 等于进入前：
- `trapframe->a7=SYS_getpid`，handler 命中一次：
- 动态 `pid -> trapframe->a0 -> user a0`：
- `sepc E -> trapframe->epc E+4 -> AFTER pc E+4`：

## 未知编号 checkpoint

| checkpoint | 模式 | `pc` | `satp` | `sp` | CSR / trapframe / dispatch / result |
|---|---|---:|---:|---:|---|
| STUB | | | | | |
| ECALL_BEFORE (`a7=11`) | | | | | |
| ECALL (`a7=22`) | | | | | |
| USERVEC | | | | | |
| SAVED | | | | | |
| KSTACK | | | | | |
| KPAGETABLE | | | | | |
| USERTRAP | | | | | |
| ADVANCED（`epc=E+4`、`SIE=0`） | | | | | |
| SYSCALL | | | | | |
| PREPARE (`handler_hits=0`) | | | | | |
| USERRET | | | | | |
| USER_SATP | | | | | |
| RESTORED | | | | | |
| AFTER | | | | | |

边界检查：

- 精确一次 `<pid> usertests: unknown sys call 22`，pid 匹配：
- mutation 前后 `priv/pc/ra/sp/a0/satp` 相同，只有 `a7` 改变：
- `sys_getpid` 命中零次：
- `trapframe/user a0=UINT64_MAX`（C `int` 为 `-1`）：
- 失败路径仍只到达一次 `E+4`：

## Oracle、资源与限制

- `S/F/B` 结论：
- 原工作树状态与共享 `fs.img` 哈希前后值：
- 两个私有镜像、临时导出、端口、QEMU/GDB 清理：
- debugger timing 限制：
- 保留的 `full-trampoline-page-table-contract`：
- `C/R` 不适用的理由：
