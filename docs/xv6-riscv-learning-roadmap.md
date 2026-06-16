# xv6-riscv 代码学习路线

学习 xv6 不建议从 `kernel/` 目录顺序读。更有效的方式是按“运行路径 + 关键机制”拆开学：先让系统跑起来，再追踪一个真实执行链路，最后分模块深入。

## 1. 先跑起来

先用下面两个命令建立基本运行和调试环境：

```sh
make qemu
make qemu-gdb
```

xv6 适合边跑边看。很多控制流跨 C、汇编、用户态、内核态，如果只静态阅读，很容易被细节打散。

## 2. 追启动主线

第一条主线是从 QEMU 加载内核到 `main()`：

```text
kernel/entry.S -> kernel/start.c -> kernel/main.c
```

这一阶段重点理解：

- CPU 如何从最早的 `_entry` 开始执行。
- machine mode 如何切到 supervisor mode。
- 内核栈什么时候设置。
- 页表什么时候打开。
- 多个 hart，也就是多个 RISC-V CPU 核，如何启动。

目标不是记住每一行，而是搞清楚启动阶段每一步解决什么问题。

## 3. 追一个用户程序的完整生命

第二条主线建议追 `init -> sh -> ls`：

```text
user/init.c
user/sh.c
user/ls.c
kernel/proc.c
kernel/exec.c
```

围绕这条线理解：

- 第一个用户进程如何创建。
- shell 如何启动和等待命令。
- `fork` 如何复制进程。
- `exec` 如何替换进程地址空间。
- `wait` 如何回收子进程。

这条线能把用户态、系统调用、进程管理、地址空间切换串起来。

## 4. 分模块深入

完成前两条主线后，再按模块阅读核心机制。

### 进程和调度

主要文件：

```text
kernel/proc.c
kernel/proc.h
kernel/swtch.S
```

重点问题：

- `struct proc` 里每个字段什么时候被修改。
- `scheduler()` 和 `sched()` 如何配合。
- 上下文切换为什么只保存部分寄存器。
- 进程状态如何在 `UNUSED`、`USED`、`SLEEPING`、`RUNNABLE`、`RUNNING`、`ZOMBIE` 之间变化。

### 虚拟内存

主要文件：

```text
kernel/vm.c
kernel/vm.h
kernel/memlayout.h
kernel/riscv.h
kernel/trampoline.S
```

重点问题：

- xv6 如何建立内核页表。
- 用户页表和内核页表有什么区别。
- `walk()`、`mappages()`、`uvmalloc()` 分别解决什么问题。
- `trampoline` 为什么要同时映射在用户页表和内核页表中。

### Trap 和系统调用

主要文件：

```text
kernel/trap.c
kernel/trampoline.S
kernel/syscall.c
kernel/sysproc.c
kernel/sysfile.c
user/usys.pl
user/user.h
```

重点问题：

- 用户态执行 `ecall` 后如何进入内核。
- `uservec`、`usertrap`、`usertrapret`、`userret` 各自负责哪一段。
- 系统调用号和参数如何传递。
- 返回用户态前为什么要恢复寄存器和切换页表。

### 文件系统

主要文件：

```text
kernel/fs.c
kernel/log.c
kernel/bio.c
kernel/file.c
kernel/pipe.c
kernel/virtio_disk.c
```

重点问题：

- inode、目录项、文件描述符之间是什么关系。
- buffer cache 如何减少磁盘访问。
- log 为什么能保证文件系统崩溃恢复的一致性。
- `open/read/write/link/unlink` 这些系统调用如何落到磁盘数据结构。

### 锁和并发

主要文件：

```text
kernel/spinlock.c
kernel/spinlock.h
kernel/sleeplock.c
kernel/sleeplock.h
```

重点问题：

- spinlock 和 sleeplock 的使用场景有什么区别。
- 为什么持有 spinlock 时要关中断。
- 哪些共享结构必须受锁保护。
- 锁顺序不当会怎样导致死锁。

## 5. 每学一个机制就做一个小实验

不要只读代码。每学完一个机制，做一个小改动验证理解：

- 给 `fork` 打印父子进程 pid。
- 给 `scheduler()` 打印进程状态切换。
- 新增一个简单系统调用。
- 故意删掉某个锁，观察 race、panic 或异常行为。
- 修改 `NPROC` 等参数，观察资源上限变化。
- 在 `exec`、`usertrap`、`sleep` 这些关键路径加日志，确认实际调用顺序。

有效的读法是：读代码之前先问“这段代码解决什么操作系统问题”，读完之后用调试器或日志验证自己的理解。

