# xv6-riscv 从 QEMU 到 shell 的调用链

这份笔记按真实执行顺序记录 xv6 启动过程：从 QEMU 把内核交给 `_entry`，一直到屏幕上出现 shell 提示符 `$ `。

## 1. QEMU 直接跳到内核入口

`Makefile` 里使用：

```text
-machine virt -bios none -kernel kernel/kernel
```

这表示 QEMU 不走传统 BIOS/OpenSBI 引导，而是把内核直接加载到 `0x80000000`，然后让每个 hart 从那里开始执行。

这个地址由 `kernel/kernel.ld` 固定：

```ld
. = 0x80000000;
kernel/entry.o(_entry)
```

所以 xv6 的最早入口就是 `kernel/entry.S` 里的 `_entry`。

## 2. `_entry` 只做栈初始化

`kernel/entry.S` 的核心工作很少：

- 根据 `mhartid` 为当前 CPU 选择一段内核栈
- 设置 `sp`
- 跳转到 `start()`

换句话说，`_entry` 的任务就是把 C 代码跑起来所需的最小环境搭好。

## 3. `start()` 切到 supervisor mode

`kernel/start.c` 里，`start()` 仍然运行在 machine mode。

它做的关键事情是：

- 把 `mepc` 设为 `main()`
- 把 `mstatus.MPP` 设为 supervisor
- 关闭分页
- 配置异常/中断委托
- 开启 timer 中断
- 记录 hart id 到 `tp`
- 执行 `mret`

`mret` 之后，CPU 进入 supervisor mode，并从 `main()` 开始执行。

## 4. `main()` 初始化内核

`kernel/main.c` 中，CPU 0 会顺序完成这些初始化：

- 物理内存分配器 `kinit()`
- 内核页表 `kvminit()`
- 当前 hart 的页表 `kvminithart()`
- 进程表 `procinit()`
- trap 向量 `trapinit()` / `trapinithart()`
- 中断控制器 `plicinit()` / `plicinithart()`
- 缓冲区缓存、inode 表、文件表、磁盘驱动
- `userinit()`

最后所有 CPU 都进入 `scheduler()`。

## 5. `userinit()` 只创建第一个进程

这一版代码和经典 xv6 有个重要差异：

`userinit()` 并没有直接把一个内核内嵌的 init 程序装进内存，而只是：

- 分配一个进程
- 设为 `initproc`
- 设置 cwd 为 `/`
- 把它标记成 `RUNNABLE`

真正的用户程序是在后面第一次调度到这个进程时才装载的。

## 6. 第一次调度到 `forkret()`

`allocproc()` 把新进程的上下文设置成：

```c
p->context.ra = (uint64)forkret;
```

所以 `scheduler()` 第一次切到这个进程后，不会直接回到普通用户代码，而是先进入 `forkret()`。

`forkret()` 做两件关键事：

- 第一次运行时调用 `fsinit(ROOTDEV)`
- 之后执行 `kexec("/init", (char *[]){"/init", 0})`

也就是说，第一个真正的用户进程来自文件系统里的 `/init` ELF 文件。

## 7. `kexec()` 装载 `/init`

`kernel/exec.c` 的 `kexec()` 负责：

- 打开 ELF 文件
- 读 ELF 头和程序头
- 创建新的用户页表
- 把程序段映射并装入内存
- 分配用户栈
- 把 `argc/argv` 放到栈上
- 设置：

```c
p->trapframe->epc = elf.entry;
p->trapframe->sp = sp;
```

这意味着下一次返回用户态时，CPU 会从 ELF 入口开始执行 `/init`。

## 8. 从内核返回用户态

`forkret()` 之后会走到 `prepare_return()` 和 `userret()`。

关键点是：

- `prepare_return()` 把 trap 入口切回 `uservec`
- 填好 trapframe 里的 `kernel_sp`、`kernel_trap`、`kernel_hartid`
- 设置 `sstatus` 和 `sepc`
- `userret()` 切换到用户页表
- 恢复寄存器
- `sret`

这时 CPU 才真正进入 `/init` 的用户态代码。

## 9. `init` 启动 shell

`user/init.c` 做的事情很直接：

- 打开或创建 `console`
- 把标准输入/输出/错误重定向到控制台
- `fork()`
- 子进程 `exec("sh", argv)`
- 父进程 `wait()`

所以屏幕上先看到的是 `init: starting sh`，随后 shell 被拉起来。

## 10. shell 打出提示符

`user/sh.c` 的 `getcmd()` 每次读命令前会执行：

```c
write(2, "$ ", 2);
```

因此你看到的 `$ `，就是整条启动链最终成功跑通的标志。

## 11. 这一条链最该记住的结论

这条路径真正值得记住的是：

- QEMU 只是把 CPU 扔到 `_entry`
- `_entry` 只负责把栈搭好
- `start()` 把机器态切到 supervisor mode
- `main()` 只做系统初始化
- 第一个用户程序是在 `forkret()` 里 `exec /init`
- `/init` 再启动 `sh`
- shell 最后打印 `$ `

