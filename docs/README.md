# xv6-riscv 实现文档

本目录描述的是当前仓库中的实际实现，而不是对 xv6 book 或上游仓库的泛化摘要。文档以“能够从机制追到源码、从源码回到机制”为目标：每个手写实现文件都有明确归属，每个核心子系统都说明控制流、数据结构、资源生命周期、并发约束、失败路径和验证方法。

## 文档范围

当前教学文档范围包括：

- `kernel/` 下全部手写 C、汇编、头文件和链接脚本；
- `user/` 下全部用户程序、运行库、系统调用生成器和链接脚本；
- `mkfs/mkfs.c`、`Makefile`、`test-xv6.py`；
- GDB 和 VSCode 调试配置；
- 生成文件 `user/usys.S` 的来源、生成过程和 ABI 作用。

这些文档不逐行转述源码。简单包装函数和常量集中说明；状态机、跨特权级边界、锁、资源所有权、恢复协议和非平凡算法则展开说明。

## 学习路线与阅读顺序

不建议按 `kernel/` 的文件名顺序从头读到尾。先运行系统，再沿真实控制流建立骨架，最后进入各专题核对数据结构、锁和失败路径：

```sh
make qemu
make qemu-gdb
```

这两个目标用于不同的观察方式，应先退出当前 QEMU 实例再启动另一个，不能让它们同时以可写方式占用同一个 `fs.img`。

第一条主线是 [QEMU 到 shell](architecture/boot-and-init.md)：`_entry -> start -> main -> scheduler -> forkret -> /init -> sh`。第二条主线是 [`fork -> exec -> wait`](flows/fork-exec-wait.md)：从 shell 启动普通命令，串起用户 ABI、系统调用、进程、页表与回收。完成这两条主线后，再按下面的顺序系统阅读：

1. [总体架构](architecture/overview.md)：先建立分层、地址空间和运行实体的全局模型。
2. [平台与外部规范契约](architecture/platform-contracts.md)：先明确 RISC-V、QEMU、PLIC、UART、VirtIO、ELF 和 psABI 的实现边界。
3. [启动与初始化](architecture/boot-and-init.md)：跟随 QEMU、`_entry`、`start()`、`main()`、`/init` 到 shell 提示符。
4. [进程与调度](kernel/processes-and-scheduling.md)：理解进程状态、上下文切换、休眠与唤醒。
5. [虚拟内存](kernel/memory.md)：理解 Sv39、内核/用户页表和 lazy allocation。
6. [Trap 与中断](kernel/traps-and-interrupts.md)以及[系统调用](kernel/system-calls.md)：连接用户态和内核态。
7. [文件系统](kernel/filesystem.md)、[存储栈](kernel/storage-stack.md)和[文件与管道](kernel/files-and-pipes.md)：沿文件描述符一路走到 VirtIO，再用[资源上界证明](kernel/resource-bounds.md)核对日志容量。
8. [调用上下文契约](kernel/call-context-contracts.md)：横向复核锁、睡眠、分配、日志与 IRQ 前提。
9. [用户 ABI 与运行库](user/runtime-and-abi.md)、[`init` 与 shell](user/init-and-shell.md)：回到用户态观察系统如何被使用。
10. 最后阅读 `flows/` 中的端到端路径，并结合[用户程序与测试](user/programs-and-tests.md)选择静态检查、运行测试或强杀恢复实验。

阅读每个机制时采用同一循环：先写出它要解决的问题和预期不变量，再沿源码记录状态变化，最后用[VSCode/GDB 调试指南](build/vscode-debug.md)中的方法观察实际控制流。适合逐步开展的小实验包括：

- 在 `fork`、`exec`、`usertrap`、`sleep` 设置断点，核对父子 pid、trapframe、进程状态和调用顺序；
- 比较 `CPUS=1` 与默认多核下的调度和启动输出，不把单次打印顺序当作并发保证；
- 新增一个最小系统调用，贯穿用户声明、stub、系统调用号、分派和 handler；
- 修改 `NPROC` 等固定资源参数，结合[资源上界](kernel/resource-bounds.md)观察失败和回收边界；
- 锁实验只在可丢弃的工作树和磁盘镜像中进行，并用重复压力测试判断竞态，不能用一次未失败证明正确。

## 架构与内核

| 文档 | 主要问题 |
|---|---|
| [总体架构](architecture/overview.md) | 系统分层、依赖方向、执行实体、全局限制和仓库特有实现 |
| [平台与外部规范契约](architecture/platform-contracts.md) | RISC-V、psABI、ELF、QEMU `virt`、PLIC、16550A 与 VirtIO 的版本和实现边界 |
| [启动与初始化](architecture/boot-and-init.md) | 多 hart 如何从 machine mode 进入 supervisor mode 并安全汇合 |
| [内核基础运行时](kernel/kernel-runtime.md) | 基础类型、公共接口、字符串原语、内核格式化输出和 panic |
| [虚拟内存](kernel/memory.md) | 物理页分配、Sv39、页表生命周期、用户拷贝、lazy fault |
| [进程与调度](kernel/processes-and-scheduling.md) | `proc` 生命周期、调度、`fork/exit/wait`、`sleep/wakeup` |
| [Trap 与中断](kernel/traps-and-interrupts.md) | trampoline、trapframe、异常/中断入口和返回 |
| [系统调用](kernel/system-calls.md) | ABI、参数校验、分派以及进程/文件类系统调用 |
| [`exec`](kernel/exec.md) | ELF 校验、两阶段地址空间替换、栈和参数布局 |
| [文件系统](kernel/filesystem.md) | 磁盘格式、inode、目录、路径和系统调用更新协议 |
| [存储栈](kernel/storage-stack.md) | buffer cache、redo log、VirtIO 请求和完成中断 |
| [文件系统与日志资源上界](kernel/resource-bounds.md) | `create/mkdir/link/unlink/itrunc/write` 的 unique block 推导及 `MAXOPBLOCKS/LOGBLOCKS/NBUF` 条件证明 |
| [文件与管道](kernel/files-and-pipes.md) | fd、全局 file、inode/设备/pipe 分派和引用计数 |
| [同步与锁](kernel/synchronization.md) | spinlock、sleeplock、锁交接、锁顺序和等待通道 |
| [调用上下文契约](kernel/call-context-contracts.md) | helper 的前置锁、内部锁、可睡眠性、分配、日志区间和 IRQ 可调用性 |
| [设备与控制台](kernel/devices.md) | PLIC、UART、console 输入编辑和控制台输出 |

## 用户态、构建与测试

| 文档 | 主要问题 |
|---|---|
| [用户 ABI 与运行库](user/runtime-and-abi.md) | 程序入口、系统调用 stub、C 辅助函数、`malloc` |
| [`init` 与 shell](user/init-and-shell.md) | 第一个用户进程、孤儿回收、shell AST、管道和重定向 |
| [用户程序与测试](user/programs-and-tests.md) | 工具程序算法、压力测试、`usertests` 测试矩阵、宿主测试驱动 |
| [构建、链接与文件系统镜像](build/build-link-and-fs-image.md) | 编译参数、链接地址、用户镜像、`mkfs`、QEMU 和 GDB |
| [VSCode/GDB 调试](build/vscode-debug.md) | WSL 环境、QEMU GDB stub、工作区配置、断点流程与故障排查 |

## 端到端执行链

| 文档 | 起点到终点 |
|---|---|
| [`fork -> exec -> wait`](flows/fork-exec-wait.md) | shell 创建子进程到父进程回收僵尸 |
| [一次系统调用往返](flows/syscall-round-trip.md) | 用户 stub 的 `ecall` 到返回下一条用户指令 |
| [一次 lazy page fault](flows/lazy-page-fault.md) | `sbrklazy()` 扩展逻辑大小到首次访问分配物理页 |
| [一次文件读写](flows/file-read-write.md) | 用户缓冲区到 inode、缓存、日志和磁盘 |
| [一次文件系统事务](flows/filesystem-transaction.md) | `begin_op()` 到提交、安装和崩溃恢复 |
| [QEMU 到 shell](architecture/boot-and-init.md) | 从 reset stub、内核初始化和首进程装载到 `$ ` 提示符 |

## 当前分支必须注意的差异

阅读外部 xv6 资料时，不能假定它与本仓库完全一致：

- 内核实现使用 `kfork()`、`kexec()`、`kexit()`、`kwait()`、`kkill()` 等名称与用户 API 区分。
- `userinit()` 只建立空的首进程；第一次进入 `forkret()` 时才初始化文件系统并执行 `/init`。
- `sbrk` 带第二个策略参数，用户库分别暴露 eager 的 `sbrk()` 和 lazy 的 `sbrklazy()`。
- 用户缺页以及部分 `copyin()`/`copyout()` 路径可调用 `vmfault()` 补页；`copyinstr()` 不会自动补页。
- `fsinit()` 在日志恢复后调用 `ireclaim()`，回收崩溃前已经失去目录链接但仍有内存引用的 inode。
- UART 发送采用“每字节写入后等待 THRE（发送保持寄存器可再接收字节）中断”的阻塞协议，而不是上游一些版本中的软件发送环形队列；该中断不代表字节已在串行线路上发送完毕。

各子系统文档会在相关位置再次说明这些差异及其后果。
