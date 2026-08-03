# 系统调用：ABI、参数边界与资源事务

系统调用层是用户程序和内核实现之间的信任边界。它负责把 RISC-V 寄存器中的调用号和参数还原出来，拒绝非法调用号，把用户地址转换为受控的 `copyin`、`copyout` 或 `copyinstr` 操作，并把请求交给进程、虚拟内存、文件、管道和文件系统子系统。它不直接实现调度、ELF 装载、inode 缓存或日志提交；这些机制由下游模块完成。

本文以当前仓库为准。这里的实现与常见 xv6 版本有几个重要差异：内核入口使用 `kfork()`、`kexec()`、`kexit()`、`kwait()` 和 `kkill()`；`sbrk` 有 eager/lazy 两种策略；用户系统调用生成器把原始 `sbrk` 桩命名为 `sys_sbrk`。

## 1. 源码范围与核心符号

本篇直接讲解的源码如下。最后一列列出阅读和调试时最重要的入口或常量。

| 路径 | 职责 | 核心符号 |
|---|---|---|
| `kernel/syscall.c` | 参数提取、调用号校验、分派表与返回值写回 | `argraw(`、`syscalls[]`、`syscall(` |
| `kernel/syscall.h` | `SYS_fork` 到 `SYS_close` 的稳定调用号 | `SYS_fork`、`SYS_close` |
| `kernel/sysproc.c` | 进程、地址空间和时钟类包装函数 | `sys_sbrk(`、`sys_pause(`、`sys_uptime(` |
| `kernel/sysfile.c` | fd、文件、目录、`exec` 与管道类包装函数 | `argfd(`、`sys_link(`、`create(`、`sys_exec(` |
| `kernel/fcntl.h` | `open` 模式位 | `O_CREATE`、`O_TRUNC` |
| `kernel/vm.h` | `sbrk` 分配策略常量 | `SBRK_EAGER`、`SBRK_LAZY` |
| `user/usys.pl` | 生成用户态汇编桩 | `sub entry`、`ecall`、`entry("sbrk")` |

因此，本文所说的 `kernel/syscall.[ch]` 分别就是 `kernel/syscall.c` 和 `kernel/syscall.h`，不是另一个实际存在的文件。

理解完整往返还需要参考 `kernel/trap.c`、`kernel/trampoline.S` 和 `kernel/proc.h`；理解处理函数的下游效果还需要参考 `kernel/proc.c`、`kernel/vm.c`、`kernel/file.c`、`kernel/pipe.c`、`kernel/fs.c`、`kernel/log.c` 和 `kernel/exec.c`。这些文件由各自的子系统文档负责，本文只在系统调用边界处引用其行为。

## 2. ABI：调用号、寄存器与生成桩

### 2.1 用户态桩如何生成

`user/usys.pl` 的 `sub entry` 为每个系统调用输出一个很短的汇编函数：

```asm
.global open
open:
  li a7, SYS_open
  ecall
  ret
```

当前系统调用原型的参数由 C 编译器按 RISC-V C ABI 放入 `a0`、`a1` 等参数寄存器。xv6 的系统调用边界只提取 `a0` 至 `a5`，所以最多支持六个寄存器参数；`a6` 未被 `argraw()` 使用，`a7` 则由桩改写为调用号。桩随后执行 `ecall`，再用 `ret` 回到调用者。生成的 `user/usys.S` 是构建产物，不应手工修改；修改系统调用清单应改 `user/usys.pl`。

`entry("sbrk")` 是唯一的命名特例。生成器仍装入 `SYS_sbrk`，但导出标签 `sys_sbrk`，而不是 `sbrk`。用户库据此提供两个 C 包装：

```c
sbrk(n)     -> sys_sbrk(n, SBRK_EAGER)
sbrklazy(n) -> sys_sbrk(n, SBRK_LAZY)
```

这样常用 API 仍可叫 `sbrk()`，`sbrk()` 和 `sbrklazy()` 两个便捷包装分别代填 eager/lazy 策略。底层 `sys_sbrk(int, int)` 也在 `user/user.h` 中公开声明，用户程序可以直接调用并传入其他策略值；这个参数不是安全边界上的隐藏值。

### 2.2 寄存器契约

| 寄存器 | 进入 `ecall` 时 | 返回用户态时 |
|---|---|---|
| `a0` 至 `a5` | 最多六个整数、标量或用户虚拟地址参数 | `a0` 被返回值覆盖，其余不构成返回契约 |
| `a7` | `kernel/syscall.h` 中的 `SYS_*` 调用号 | 当前实现原样恢复该调用号，但它不是返回值契约 |
| `sepc` | trap 硬件记录 `ecall` 指令地址 | 内核改为原值加 4，或由成功的 `exec` 改为新入口 |

`kernel/trampoline.S` 把用户寄存器保存到当前进程的 `struct trapframe`。因此，进入 C 代码后，参数的权威副本是 `p->trapframe->a0` 至 `a5`，调用号是 `p->trapframe->a7`，不是已经被 C 调用约定自由使用的物理寄存器。

处理函数统一声明为 `uint64 sys_name(void)`。普通失败以 `(uint64)-1` 写回 `a0`，用户侧的 `int` 或指针原型再把低 32 位或完整指针值解释为 `-1` 或 `SBRK_ERROR`。反过来，`argint()` 也只保留寄存器参数的低 32 位。本实现没有 `errno`，不能区分“路径不存在”“fd 无效”“资源耗尽”等不同失败原因。

### 2.3 一次完整往返

```text
用户 C 函数
  -> user/usys.pl 生成的桩：a7 = SYS_xxx，执行 ecall
  -> trampoline 的 uservec：保存用户寄存器，切换页表和内核栈
  -> usertrap()：识别 scause == 8，epc += 4，打开中断
  -> syscall()：把 trapframe->a7 窄化为 int num，查 syscalls[]
  -> sys_xxx()：提取参数，调用下游实现
  -> syscall()：把 uint64 返回值写入 trapframe->a0
  -> prepare_return()/userret：恢复用户页表和寄存器，执行 sret
  -> 用户桩 ret：按 a0 返回给 C 调用者
```

`usertrap()` 在调用 `syscall()` 前开启中断。系统调用处理函数因而可以等待磁盘、管道、子进程、时钟 tick 或日志空间。分派器本身不持有任何锁；每个下游子系统必须建立自己的并发约束。

流程图画的是正常返回。`usertrap()` 实际会在分派前和处理函数返回后各检查一次 `killed(p)`；任一检查命中都会直接 `kexit(-1)`。因此处理函数可能已经修改文件、管道、用户内存或其他状态并计算出返回值，但后一次检查会让进程退出，用户桩看不到这个返回值。最后一次检查与真正 `sret` 之间也没有原子屏障：另一 CPU 在该窗口设置 `killed` 时，目标仍可能短暂返回用户态，到下一次 trap 才退出。

## 3. 调用号与全部系统调用

`kernel/syscall.h` 当前连续定义 1 到 21。`kernel/syscall.c` 使用指定下标初始化 `syscalls[]`，所以表项在源码中的书写顺序不是 ABI；数值才是 ABI。

| 号 | 用户入口 | 内核处理函数 | 类别 | 成功结果 |
|---:|---|---|---|---|
| 1 | `fork()` | `sys_fork()` | 进程 | 父进程得到子 pid，子进程得到 0 |
| 2 | `exit(status)` | `sys_exit()` | 进程 | 不返回 |
| 3 | `wait(statusp)` | `sys_wait()` | 进程 | 被回收子进程的 pid |
| 4 | `pipe(fdarray)` | `sys_pipe()` | fd/管道 | 0，并写出读端和写端 fd |
| 5 | `read(fd, buf, n)` | `sys_read()` | fd/I/O | 读取字节数；返回值可以为 0 |
| 6 | `kill(pid)` | `sys_kill()` | 进程 | 0 |
| 7 | `exec(path, argv)` | `sys_exec()` | 进程映像 | 成功后从新程序入口开始，不回到旧调用点 |
| 8 | `fstat(fd, st)` | `sys_fstat()` | fd/元数据 | 0，并写出 `struct stat` |
| 9 | `chdir(path)` | `sys_chdir()` | 命名空间 | 0 |
| 10 | `dup(fd)` | `sys_dup()` | fd | 新 fd |
| 11 | `getpid()` | `sys_getpid()` | 进程 | 当前 pid |
| 12 | `sys_sbrk(n, policy)` | `sys_sbrk()` | 虚拟内存 | 修改前的 `p->sz` |
| 13 | `pause(ticks)` | `sys_pause()` | 时钟/等待 | 0 |
| 14 | `uptime()` | `sys_uptime()` | 时钟 | 启动以来的 tick 快照 |
| 15 | `open(path, mode)` | `sys_open()` | fd/命名空间 | 新 fd |
| 16 | `write(fd, buf, n)` | `sys_write()` | fd/I/O | 写入字节数 |
| 17 | `mknod(path, major, minor)` | `sys_mknod()` | 命名空间/设备 | 0 |
| 18 | `unlink(path)` | `sys_unlink()` | 命名空间 | 0 |
| 19 | `link(old, new)` | `sys_link()` | 命名空间 | 0 |
| 20 | `mkdir(path)` | `sys_mkdir()` | 命名空间 | 0 |
| 21 | `close(fd)` | `sys_close()` | fd | 0 |

每新增一项，至少要同步四处：调用号 `kernel/syscall.h`、用户桩清单 `user/usys.pl`、处理函数声明与 `syscalls[]` 表项 `kernel/syscall.c`，以及用户原型。漏掉任何一处可能表现为链接错误、调用错误编号，或合法编号落入空表项。

## 4. 分派与参数提取

### 4.1 `syscall()` 的防御边界

`syscall()` 先把 64 位 `p->trapframe->a7` 赋给有符号 `int num`，再检查：

1. 调用号必须大于 0；
2. 调用号必须小于 `NELEM(syscalls)`；
3. 对应函数指针必须非空。

三项检查针对窄化后的 `num`，不是原始 64 位 `a7`。在当前 RV64 工具链上，这一步保留低 32 位并按 `int` 解释，因此高 32 位非零并不必然被拒绝：例如低 32 位为 1 的原始值可以别名到 `SYS_fork`。通过检查后才间接调用，并把结果写入 `p->trapframe->a0`。失败时打印 pid、进程名和窄化后的调用号，把 `p->trapframe->a0` 置为 `-1`，然后从 `void syscall()` 返回。指定下标数组允许以后出现空洞；边界检查不能仅验证最大调用号。

### 4.2 四层参数助手

| 助手 | 输入来源 | 做什么 | 不做什么 |
|---|---|---|---|
| `argraw(n)` | trapframe 的 `a0` 至 `a5` | 取第 `n` 个 64 位原始值 | 不校验含义；`n` 不在 `[0, 5]` 会 `panic` |
| `argint(n, &v)` | `argraw` | 保存为 32 位 `int` | 不做范围、符号或枚举校验 |
| `argaddr(n, &va)` | `argraw` | 保存 64 位用户虚拟地址 | 故意不检查映射和权限 |
| `argstr(n, buf, max)` | 用户虚拟地址 | 经 `fetchstr()`/`copyinstr()` 拷贝 NUL 结尾字符串 | 不接受缺少 NUL、未映射页或超过 `max` 的字符串 |

`argraw()` 的索引由内核处理函数写死，用户只能控制对应寄存器的值，不能直接用一个系统调用参数触发索引越界。延迟校验地址仍然必要，因为合法性取决于方向、长度和跨过的每一页。`argaddr()` 只保留数值，真正访问时由 `copyin()`、`copyout()`、`copyinstr()` 或它们的下游包装完成，内核不能把用户虚拟地址当作内核指针直接解引用。

这里的“可读/可写”必须按源码理解：`walkaddr()` 只要求 PTE 具有 `PTE_V|PTE_U`，所以 `copyin()` 和 `copyinstr()` 不显式检查 `PTE_R`；`copyout()` 在同样的映射检查后还明确要求 `PTE_W`。这些助手按页映射判断，也不统一检查整个范围是否严格落在字节级的 `p->sz` 内；已映射末页中位于逻辑末端之后的字节仍可能被复制。

### 4.3 `fetchaddr()` 与 `fetchstr()`

`fetchaddr(addr, &word)` 用于读取用户内存中的一个 64 位指针，当前主要服务于 `exec` 的 `argv[]`。它先要求：

```text
addr < p->sz
addr + sizeof(uint64) <= p->sz
```

两项都需要：第一项也挡住加法溢出后回绕到小地址的情况。随后 `copyin()` 再验证实际页表映射。

`fetchstr(addr, buf, max)` 调用 `copyinstr()`，成功后返回不含 NUL 的长度。与 `fetchaddr()` 不同，它不先检查 `addr` 或扫描终点是否小于 `p->sz`，只依赖逐页的 `PTE_V|PTE_U` 映射；因此已映射末页中位于逻辑 `p->sz` 之后的 NUL 也可能结束字符串。路径通常使用 `MAXPATH` 大小的内核缓冲区，所以 NUL 必须出现在复制窗口的前 `MAXPATH` 个字节内；恰好有 `MAXPATH` 个非 NUL 字节再跟一个 NUL 仍会失败。

### 4.4 lazy 地址带来的非对称性

当前 `kernel/vm.c` 的行为使不同参数类型存在有意的差别：

- 对当前进程的 `p->pagetable` 做普通用户复制时，`copyin()` 和 `copyout()` 遇到页基址小于 `p->sz` 的尚未映射页，可以调用 `vmfault()` 分配零页；它们传入向下取整的页基址，所以原始指针即使已落在字节级 `p->sz` 之外，只要仍在最后一个逻辑页中，也可能触发补页并完成复制；`vmfault()` 固定把新页映射到当前进程页表，不能把这一行为泛化到任意临时页表；
- `copyinstr()` 不触发 lazy 分配，字符串所在页必须已经映射；
- 因而 `read` 目标、`write` 源、`wait` 状态、`fstat` 结果、`pipe` 的 fd 数组以及 `exec` 的 `argv[]` 指针数组，都可能在 `copyin/out` 或 `fetchaddr()` 时补页；即使整个系统调用后来失败，这些物化页和已经完成的部分复制也不会回滚；
- `open`、`link`、`unlink`、`mkdir`、`chdir`、`mknod` 的路径、`exec` 的路径以及每个参数字符串都经 `copyinstr()`，不能靠它补页。

这是本 fork 的实际语义，不应按其他 xv6 分支的用户拷贝行为推断。

## 5. 进程与时钟类调用

### 5.1 `fork`

`sys_fork()` 直接调用 `kfork()`。下游先分配并锁住新进程；`uvmcopy()` 按页扫描 `[0, p->sz)`，跳过尚无页表项或 PTE 无效的 lazy hole，复制每个有效叶映射的物理页并保留原权限。这也包括 `exec` 栈中有效但已清除 `PTE_U` 的 guard page，不只是用户可访问页。随后它复制独立的 trapframe，把子 trapframe 的 `a0` 改为 0；打开文件通过 `filedup()` 增加共享 `struct file` 的引用，cwd 通过 `idup()` 增加 inode 引用。成功后设置父子关系和 `RUNNABLE` 状态，父进程从处理函数得到子 pid。

若进程槽、trapframe、页表页或被复制的物理页分配失败，`kfork()` 清理尚未发布的子进程并返回 `-1`。回滚不会撤销 `allocpid()` 已经对全局 `nextpid` 的递增，因此失败可能造成 pid 跳号；父进程拥有的页、fd 和 cwd 引用不变。

### 5.2 `exit` 与 `wait`

`sys_exit()` 用 `argint()` 取得低 32 位状态后调用 `kexit()`，其后的 `return 0` 不可达。状态以原始 `int` 保存在 `xstate`，`wait` 也只复制这 4 字节，不做 POSIX 式退出状态编码。`kexit()` 先关闭 fd 并在事务中释放 cwd 引用，再持有 `wait_lock` 转交子进程、唤醒父进程；随后还取得自身 `p->lock`，发布 `xstate` 和 `ZOMBIE`，释放 `wait_lock` 后带着 `p->lock` 进入调度器。此时 pid、进程槽、trapframe、页表和用户页仍保留到父进程 `wait()`，没有普通错误回滚路径。init 退出会触发 `panic`，这是内核不变量而不是普通错误返回。

`sys_wait()` 只取一个原始地址并调用 `kwait()`：

- `statusp == 0` 表示调用者不需要退出状态；
- 扫描由 `wait_lock` 保护；检查某个候选子进程、复制状态和回收时还同时持有该子进程的 `pp->lock`；
- 找到僵尸后，若地址非零，必须先把原始 32 位 `xstate` `copyout()` 成功才调用 `freeproc()`；
- 跨页 `copyout()` 失败可能已写入部分状态字节或物化父进程的 lazy 页，这些用户地址空间副作用不会撤销，但子进程仍是僵尸，父进程可用合法地址或 0 重试；
- 没有任何子进程，或者本轮没有找到可回收僵尸且当前父进程的 killed 标志已被观察到时，返回 `-1`；已经找到的僵尸优先于循环末尾的 killed 检查；
- 没有可回收目标时在当前父进程地址上睡眠，`sleep()` 原子地交接并暂时释放 `wait_lock`。

“先写状态、后回收”是重要的失败原子性：错误的用户指针不能使子进程在状态丢失后被回收。

### 5.3 `kill` 与 `getpid`

`sys_kill()` 把低 32 位 pid 交给 `kkill()`。后者逐个锁住进程槽，只比较 `p->pid == pid`；匹配后设置 `killed`，若目标正在 `SLEEPING` 则改为 `RUNNABLE`。找不到匹配值才返回 `-1`。`kill` 不在调用瞬间销毁目标，也不等待目标退出；目标通常在系统调用等待循环或 `usertrap()` 的检查点执行 `kexit(-1)`，而最后检查到 `sret` 的竞态窗口意味着这种终止不是严格即时的。

目标若就是调用者自己，`sys_kill()` 内部会算出 0，但紧接着的 `usertrap()` killed 检查会退出当前进程，因此 `kill(getpid())` 的用户代码看不到成功返回。

当前实现没有验证 `pid > 0`、进程状态或 `p != initproc`，因而还有几个必须按源码保留的边界：

- `ZOMBIE` 槽仍保留正 pid，`kill` 可以对它返回 0 并设置一个不会再次驱动退出的标志；
- `UNUSED` 槽的 pid 为 0，所以 `kill(0)` 可能命中空槽、返回 0 并留下 `killed = 1`；`allocproc()` 复用该槽时不主动清零 killed，新进程可能继承这个标志；
- 对 init 的 `kill` 也可先返回成功，init 在后续退出检查点进入 `kexit(-1)` 时会触发 `panic("init exiting")`。

因此当前可靠的调用前提是传入正的、已知仍存活且不是 init 的 pid；这些约束并未由内核强制执行。

`sys_getpid()` 直接读取当前进程的 pid。它没有失败路径，也不分配资源。

### 5.4 `pause` 与 `uptime`

`sys_pause()` 读取有符号 tick 数：负数被规范化为 0；零值不进入等待循环，但仍会短暂获取并释放 `tickslock`。正数路径持有 `tickslock` 读取起点，然后循环检查无符号差值 `ticks - ticks0`。未到期时执行 `sleep(&ticks, &tickslock)`；`sleep` 在睡眠和释放 `tickslock` 之间完成锁交接，避免错过时钟中断的唤醒。被 kill 时必须先释放 `tickslock` 再返回 `-1`，但 `usertrap()` 随后的 killed 检查通常会让进程直接退出，用户态看不到这个 `-1`。

循环而不是单次睡眠很重要，因为唤醒不等于目标 tick 数已经满足。无符号减法可自然跨越 `uint` 的 tick 回绕，但接口的 `int n` 仍把单次请求限制在有符号 32 位范围。

`sys_uptime()` 只在 `tickslock` 下复制 32 位无符号全局 `ticks`，随即释放锁，并把它零扩展成处理函数的 `uint64` 返回值。用户原型却是 `int uptime(void)`；bit 31 置位后，低 32 位按用户 C 类型解释为负数，而且桩本身没有执行 ABI 所期望的 32 位有符号返回值扩展。计数器还会在 `2^32` tick 后回绕，这些都是教学接口的长期运行限制。

## 6. `sbrk` 的 eager/lazy 策略

`kernel/vm.h` 定义 `SBRK_EAGER == 1`、`SBRK_LAZY == 2`。`sys_sbrk()` 先用 `argint()` 读取低 32 位的 `n` 和策略 `t`，再把当前 `p->sz` 保存为 `addr`。各分支成功时都返回旧末端 `addr`；可报告的失败返回 `(uint64)-1`，用户指针 API 将其解释为 `SBRK_ERROR`。

### 6.1 实际分支规则

| 条件 | 行为 |
|---|---|
| `n < 0` | 不看策略，调用 `growproc(n)`；正常范围内收缩，越过 0 的请求具有下述下溢缺陷 |
| `n >= 0 && t == SBRK_EAGER` | 调用 `growproc(n)`，立即分配、清零并映射需要的新页 |
| `n >= 0 && t != SBRK_EAGER` | 只增长 `p->sz`，不建立物理映射 |

最后一项意味着当前代码并没有严格拒绝非法策略值；只要不是 `SBRK_EAGER`，非负增长都按 lazy 处理。两个便捷包装只传合法常量，但公开的 `sys_sbrk()` 用户符号允许普通 C 程序直接到达这个分支。

### 6.2 eager 分配

`growproc()` 拒绝新末端超过 `TRAPFRAME` 的增长，并通过 `uvmalloc()` 逐页分配。若中途耗尽物理页或页表分配失败，`uvmalloc()` 回收本轮已经映射的数据页，`p->sz` 保持旧值，`sys_sbrk()` 返回 `-1`；本轮新建但已经变空的中间页表页不会在这里立即回收，要到整棵页表最终 `freewalk()` 时释放。只有本次真正新分配的物理页保证全页清零。

收缩也经过 `growproc()` 和 `uvmdealloc()`。后者允许 lazy 区域存在未映射空洞，只释放跨过页边界后仍位于新末端之上的实际映射页。若 `|n| > p->sz`，`sz + n` 按无符号规则下溢到一个大值，`uvmdealloc()` 因 `newsz >= oldsz` 直接返回旧大小；当前调用会成功返回旧末端、地址空间不变，而不是返回 `-1`。

收缩到页中间时，包含新末端的整个物理页仍映射给用户，`[p->sz, PGROUNDUP(p->sz))` 中的旧字节不会被清除，硬件页表也不会阻止进程访问这段页尾。以后无论 eager 还是 lazy 地增长回同一页，都不会重新分配或清零，旧内容会重新成为逻辑 heap 的一部分。

### 6.3 lazy 分配

lazy 增长只做两个上界检查：

1. `addr + n < addr` 拒绝 64 位加法回绕；
2. `addr + n > TRAPFRAME` 拒绝覆盖 trapframe/trampoline 保留区。

通过后只执行 `p->sz += n`。第一次访问尚未映射的新增页时，用户 load/store 触发页故障，或者内核的普通 `copyin()`/`copyout()` 主动调用 `vmfault()`；后者分配清零页，并忽略 `read` 形参，统一映射为 `PTE_R|PTE_W|PTE_U`。如果新增区间仍落在已有的末页，则不会 fault，也不会清零上段所述的旧页尾。物理内存耗尽因而可能推迟到首次需要新页时，而不是发生在 `sbrklazy()` 返回时。

末端可以恰好等于 `TRAPFRAME`，因为逻辑地址区间是半开区间 `[0, p->sz)`，且 `TRAPFRAME` 本身页对齐；再增长一个字节必须失败。`n` 来自 `argint()`，一次调用最多只能按 32 位有符号正数增长，测试靠多次约 1 GiB 的 lazy 增长接近上界。

`p->sz` 和页表是当前进程私有状态，处理函数不取得 `p->lock`。这一点依赖 xv6 没有同一进程内的多内核线程并发修改地址空间；若将来加入线程，该假设必须重新设计。

## 7. fd、数据 I/O 与管道

### 7.1 `argfd()` 和 `fdalloc()` 的所有权

`argfd()` 取得整数 fd，要求 `0 <= fd < NOFILE` 且 `myproc()->ofile[fd] != 0`，然后按需返回编号和 `struct file *`。它只验证“当前打开”，不检查可读、可写或文件类型；这些条件由 `fileread()`、`filewrite()` 和 `filestat()` 检查。

`fdalloc(f)` 线性寻找当前进程 `ofile[]` 的空槽并直接写入 `f`。它不调用 `filedup()`，语义是“成功时接管调用者已经持有的一个 file 引用”。因此在处理函数的稳定入口/出口处必须遵守以下不变量：

```text
每个非空 p->ofile[fd] 恰好对应一个 struct file 引用；
fdalloc() 失败时引用仍归调用者；
fdalloc() 成功后不能再把同一引用当作未转移资源释放。
```

`NOFILE` 当前是每进程 16 个 fd；全局 `struct file` 表另有 `NFILE == 100` 的限制。两种耗尽都以 `-1` 表示，但发生在不同层。

### 7.2 `dup` 与 `close`

`sys_dup()` 先验证旧 fd，再用 `fdalloc(f)` 占新槽，最后 `filedup(f)` 增加引用。两步之间暂时有两个槽共享一个引用，但当前进程没有并发内核线程，且 `filedup()` 不会以普通错误返回；处理函数返回前引用数会恢复到每槽一个。若没有空 fd，原 fd 和引用计数完全不变。

新旧 fd 指向同一个 `struct file`，所以普通 inode 文件共享 `off`；它们不是两个独立打开实例。`fork` 复制 fd 时也保持这种共享关系。

`sys_close()` 先把 `p->ofile[fd]` 清零，再调用 `fileclose(f)` 丢掉引用。若这是最后一个引用，`fileclose()` 才关闭管道端或在文件系统事务中 `iput()` inode。先清槽保证当前进程不会在可能睡眠的最终关闭路径上继续把该 fd 视为打开。

### 7.3 `read`、`write` 与 `fstat`

三个处理函数都先取用户地址/长度，再用 `argfd()` 验证 fd，实际方向检查留给文件层：

- `sys_read()` 调用 `fileread(f, dst, n)`；inode、设备和管道各有自己的读路径；
- `sys_write()` 调用 `filewrite(f, src, n)`；inode 大写入会切成多个受日志预算约束的操作区间，设备和管道由各自实现处理；
- `sys_fstat()` 调用 `filestat(f, staddr)`；只有 inode 和设备文件支持，并以 `copyout()` 写回用户结构。

`filestat()` 在 inode sleeplock 下调用 `stati()` 生成快照，解锁后复制给用户。当前 RV64 下 `struct stat` 占 24 字节，字段 `nlink` 与 `size` 之间有 4 字节对齐填充；局部变量 `struct stat st` 没有先清零，`stati()` 也不写填充区，但 `copyout(sizeof(st))` 会把它一起交给用户。这会泄漏 4 字节未初始化内核栈内容，是当前实现缺陷，不是有效元数据字段。

系统调用层不拒绝负的 `n`，也不预先验证整个缓冲区。零长度复制通常完全不检查地址；数值 0 也不会被 `argaddr()` 一律视为非法，只有 `wait(statusp == 0)` 明确定义了空指针语义。负长度更没有统一行为：它传给接受 `uint` 的 `readi()` 时会发生 32 位无符号转换，而管道、设备和 inode 写路径又有各自的循环/错误结果。合法程序必须传非负长度，不能把这些偶然结果当作接口。

逐页复制还使错误具有部分副作用：

- inode `readi()` 的 `copyout()` 即使已经写出前缀，只要后续失败就把总结果改成 `-1`；`fileread()` 因结果不大于 0 而不推进共享 file offset，但用户缓冲区前缀和已物化的 lazy 页保留；
- `fstat()` 的结构写回同样可能部分完成后返回 `-1`，元数据和 fd 本身不变；
- 管道读在已成功交付若干字节后失败会返回短计数并只消费已交付字节；管道写的源地址失败会返回已写短计数，但 broken reader 或 killed 检查即使发生在部分写入之后仍返回 `-1`；
- console 读在取出输入字符后才 `copyout()`，失败可消费一个未交付字符；不同设备回调不共享统一的部分 I/O 规则。

inode 写也不是“任意长度的一次原子事务”。当前日志预算把 `filewrite()` 的每个区间限制为 3072 字节，并为每个区间调用一次 `begin_op()`/`end_op()`；并发操作可让相邻区间进入同一个日志组，所以这些区间不是彼此独立的磁盘 commit。后面的区间失败时，前面区间的文件数据和 `f->off` 仍可能已经改变，而 `filewrite()` 最终向用户返回 `-1`。经 `dup` 或 `fork` 共享同一 `struct file` 的并发写者也只在每个 inode 锁区间内串行，大写入在区间之间可互相穿插，不能保证整次调用的数据连续出现。

还有一个更细的失败边界：`writei()` 先用 `bmap()` 分配目标块，再把用户数据复制进 buffer cache，完整复制成功后才对该数据 buffer 调用 `log_write()`。跨页 `copyin()` 可先改写 buffer 前缀再失败，使缓存内容已经变化却没有由这个数据复制点登记日志；若该 buffer 因新块清零或同组其他写入已经在日志中，组提交还可能包含这些变化。即使 `tot == 0`，`writei()` 也无条件 `iupdate()`，所以 `bmap()` 新增的块指针可能被持久化。调用者不能从最终 `-1` 推断文件、offset、缓存或 lazy 页完全未变。

### 7.4 `pipe`

`sys_pipe()` 的资源顺序是：

```text
pipealloc() 创建 rf/wf 和 pipe 页
  -> fdalloc(rf)
  -> fdalloc(wf)
  -> copyout(fd0)
  -> copyout(fd1)
```

`pipealloc()` 若在两个 file 槽或 pipe 页的部分构造中失败，会按实际取得程度自行关闭/释放。只有它成功后，后续 `fdalloc()` 或 `copyout()` 失败才由 `sys_pipe()` 清除已经安装的 `ofile[]` 槽，并对读写两端各执行一次 `fileclose()`；两端都关闭后 pipe 页被释放。任一次 `copyout()` 都可能先写部分整数或物化 lazy 页再失败；若第二次失败，第一次写出的完整 `fd0` 也不会被擦除。内核中的两个 fd 会全部撤销并返回 `-1`，所以内核资源回滚完整，用户地址空间写出却不是全有或全无；失败后的数组内容无效。

## 8. 路径、目录与 `open` 模式

### 8.1 `kernel/fcntl.h` 的模式位

当前定义为：

| 标志 | 值 | `sys_open()` 中的效果 |
|---|---:|---|
| `O_RDONLY` | `0x000` | 不设置写位；由于值为 0，不是独立 bit |
| `O_WRONLY` | `0x001` | `readable = 0`，`writable = 1` |
| `O_RDWR` | `0x002` | `readable = 1`，`writable = 1` |
| `O_CREATE` | `0x200` | 不存在时创建普通文件 |
| `O_TRUNC` | `0x400` | 打开普通文件时截断内容 |

实现不做完整的模式枚举校验。未知位通常被忽略；同时设置 `O_WRONLY | O_RDWR` 时按位表达式计算，不提供 POSIX 式错误。尤其是 `O_RDONLY | O_TRUNC` 仍会截断普通文件，随后返回只读 fd。目录只有在 `omode == O_RDONLY` 完全相等时才允许打开，所以给目录附加 `O_CREATE`、`O_TRUNC` 或未知位会失败。这里没有权限、用户身份、`O_APPEND`、`O_EXCL` 或 close-on-exec 语义。

路径还有两类容易被 POSIX 直觉掩盖的边界。目录分量恰好 14 字节时可以没有 NUL；超过 `DIRSIZ == 14` 不会报错，而会静默只取前 14 字节，因此不同长名字可能别名。空相对路径的 `namei("")` 直接返回 cwd，所以 `open("", O_RDONLY)` 会打开当前目录，`chdir("")` 也成功但保持同一 cwd；`nameiparent("")` 则失败，所以空路径不能用于创建、unlink 或新硬链接名。

### 8.2 `create()` 的契约

`create(path, type, major, minor)` 是 `kernel/sysfile.c` 的内部助手，调用者必须已经进入 `begin_op()`：

1. `nameiparent()` 取得父目录引用并提取末级名字；
2. 锁住父目录，检查同名项；
3. 对 `T_FILE` 请求，已有普通文件或设备 inode 可直接作为成功结果返回；目录创建和设备创建遇到已有项失败；
4. 新建时 `ialloc()` inode，设置类型、设备号和 `nlink = 1`；
5. 目录还要建立 `.`、`..`，再把新 inode 链入父目录；
6. 只有全部链接成功后，才为目录的 `..` 增加父目录 `nlink`。

成功返回的是仍持有 sleeplock 且带引用的 inode。失败分支把新 inode 的 `nlink` 设为 0，登记 inode 更新，再通过 `iunlockput()` 紧接着执行的 `iput()` 回收新 inode 及其块，并释放父目录。调用者必须负责成功结果的解锁/引用转移。

这仍不是逐块状态完全回到入口：父目录第一次扩展到间接区时，`bmap()` 可能先成功分配间接块，随后因数据块耗尽而让 `dirlink()` 失败；父目录中这个空的间接块指针不会回滚。新 inode 或调用者预增的链接数会清理，但失败的 `create()`/`link()` 仍可能消耗一个父目录块。

### 8.3 `open`

`sys_open()` 先把路径拷入 `MAXPATH` 内核缓冲区，再 `begin_op()`：

- 有 `O_CREATE` 时调用 `create(path, T_FILE, 0, 0)`；
- 否则 `namei()` 后锁住 inode，并拒绝非纯只读方式打开目录；
- 设备 inode 的 major 必须处于 `[0, NDEV)`；`mknod` 可以先创建其他 major，但这样的节点不能成功打开；
- 依次取得全局 file 槽和进程 fd 槽；任一失败都关闭已分配 file、释放 inode，并 `end_op()`；
- 初始化 file 类型、inode、偏移和读写位；必要时在 inode 锁下 `itrunc()`；
- 解锁 inode、结束事务，返回 fd。

成功时，`namei()`/`create()` 提供的 inode 引用转移给新 `struct file`，而 file 引用转移给 `p->ofile[fd]`。失败时这两级内存引用都必须归还，但“引用回滚”不等于撤销此前的命名空间修改：若 `O_CREATE` 已经新建并链接文件，随后 `filealloc()` 或 `fdalloc()` 才因资源耗尽失败，新文件仍会随 `end_op()` 保留下来，尽管 `open()` 返回 `-1`。`O_TRUNC` 位于 file 和 fd 都取得之后，执行后没有普通可返回失败点。

### 8.4 `mkdir` 与 `mknod`

两者都在 `begin_op()`/`end_op()` 内调用 `create()`，成功后 `iunlockput()` 返回的 inode。

- `sys_mkdir()` 创建 `T_DIR`，包括 `.`、`..` 和父目录链接计数更新；
- `sys_mknod()` 创建 `T_DEVICE`；`argint()` 先保留 major/minor 的低 32 位，传入 `create()` 的 `short` 形参和 inode 字段时再窄化为低 16 位。普通 C 原型本来就声明两个参数为 `short`，但内核仍不验证这种窄化。创建阶段不检查 major 是否可分派；`open` 才要求符号解释后的 major 位于 `[0, NDEV)`，具体 `read`/`write` 又会检查相应的设备方法是否存在。通用 `devsw` 回调只按 major 分派，并不接收或使用 inode 的 minor。

### 8.5 `link`

`sys_link()` 在进入事务前复制 `old`、`new` 两个路径。事务内先找到旧 inode，拒绝目录，然后在 inode 锁下乐观执行 `nlink++` 和 `iupdate()`。接着查找新路径的父目录：

- 新旧 inode 必须在同一设备；
- `dirlink()` 必须成功，不能覆盖已有名字；
- 成功后释放父目录和旧 inode 引用并结束事务；
- 失败则走 `bad`，重新锁旧 inode，把 `nlink` 减回去并登记更新，再释放引用和结束事务。

增加和回退位于同一日志操作范围，因此普通错误不会留下只有计数、没有目录项的硬链接；两次 `iupdate()` 是把缓存块登记到当前日志组，真正持久化要等 group commit。目录硬链接被禁止，以避免形成遍历环和破坏父子计数规则。

`nlink` 是有符号 `short`，而 `sys_link()` 在递增前没有上界检查。极端数量的硬链接在当前工具链下可能使它绕成负值，后续 `unlink()` 会因 `ip->nlink < 1` 触发 `panic`；这不是一个会干净返回 `-1` 的容量限制。

### 8.6 `unlink`

`sys_unlink()` 先复制路径，再在事务中锁住父目录。它拒绝 `.` 和 `..`，要求目标存在；目标若是目录，还必须除 `.`、`..` 外为空。成功顺序是：

1. 把父目录中的目标 `dirent` 清零；
2. 若目标是目录，减少父目录因目标 `..` 产生的 `nlink`；
3. 释放父目录；
4. 减少目标 inode 的 `nlink` 并释放目标；
5. 结束事务。

在实际清除目录项之后，`writei()` 短写被视为内核不变量破坏并 `panic`，不是尝试普通错误回滚。目标的 `nlink` 变为 0 并不一定立即释放数据：仍被打开的 file 引用可继续访问 inode，最终 `iput()` 才完成回收。

“目录为空”的判断依赖磁盘格式正常：`isdirempty()` 直接从第三个 dirent 开始扫描，并不验证前两个确实是 `.`、`..`。路径遍历和空目录扫描还通过分配型 `bmap()` 读取数据；损坏 inode 在 `[0, size)` 内出现块洞时，校验阶段本身就可能尝试分配块或触发日志断言。因此上述普通错误顺序不是对任意损坏文件系统映像的恢复保证。

### 8.7 `chdir`

`sys_chdir()` 在事务中解析并锁住新 inode，只接受 `T_DIR`。成功时解锁但保留新 inode 引用，在事务内 `iput(p->cwd)` 释放旧 cwd，再结束事务并把新引用赋给 `p->cwd`。旧 cwd 可能已从目录树 unlink，最后一次 `iput()` 可能修改并回收磁盘 inode，所以不能把它移到日志事务外。

失败时新 inode 被 `iunlockput()`，旧 cwd 完全不变。

## 9. `exec` 的两层回滚

`sys_exec()` 负责把不可信的用户 `argv` 转换成内核拥有的字符串数组，`kexec()` 负责建立新地址空间。

### 9.1 参数编组

处理函数先复制 `path`，然后循环：

1. 用 `fetchaddr(uargv + i * 8)` 读取第 `i` 个用户指针；
2. 读到空指针时写入内核 `argv[i] = 0` 并停止；
3. 为每个非空参数分配一整页；
4. 用 `fetchstr(uarg, argv[i], PGSIZE)` 复制参数字符串。

内核数组大小是 `MAXARG == 32`，而其中必须留一个空指针哨兵，所以当前编组层最多接受 31 个非空参数。每个参数字符串必须在单个 `PGSIZE` 缓冲区内包含 NUL；路径必须在 `MAXPATH` 内包含 NUL。

路径的 `argstr()` 失败时尚未分配参数页，处理函数直接返回 `-1`。之后读取 `argv[]` 槽失败、参数过多、`kalloc()` 失败或参数 `fetchstr()` 失败才进入 `bad`，释放此前为每个参数分配的页。调用 `kexec()` 返回后，无论成功还是失败，`sys_exec()` 也释放全部参数页；`kexec()` 在调用期间只借用这些字符串，不取得所有权。

这些临时参数页能完整回收，但读取用户 `argv[]` 的 `fetchaddr()` 内部使用 `copyin()`，可能已经为旧地址空间物化 lazy 页；指针槽跨页失败还可能只把一个 64 位指针的前缀写入内核临时变量。后续编组或装载失败不会撤销旧地址空间中新分配的页。每个参数字符串走不补页的 `copyinstr()`，与指针数组并不对称。

### 9.2 新映像提交点

`kexec()` 自己用 `begin_op()`/`end_op()` 包围可执行文件的查找和读取。它在独立的新页表中校验 ELF、装载段、建立 guard page 和用户栈、复制参数；所有这些步骤成功后才交换 `p->pagetable`、`p->sz`、`epc` 和 `sp`，再释放旧页表。

因此：

- 提交点之前的普通可返回错误会释放新页表，旧程序仍可从 `exec()` 得到 `-1`；当前 ELF 校验并不覆盖所有会触发页表辅助函数 `panic()` 的恶意布局，这类内核崩溃不属于可恢复回滚保证；
- 提交点之后，旧地址空间已经不存在，不能再回到原 `ecall` 后的指令；
- `kexec()` 返回的 `argc` 经分派器写入新 trapframe 的 `a0`，而新 `argv` 地址已放入 `a1`，恰好成为新程序 `main(argc, argv)` 的参数。

参数页回滚和新页表回滚是两层不同的所有权协议，不能只检查其中一层。更完整的 ELF 和栈布局见 `docs/xv6-riscv/kernel/exec.md`。

## 10. 文件系统事务、锁与可睡眠性

### 10.1 哪些路径进入日志事务

| 系统调用 | 事务位置 | 原因 |
|---|---|---|
| `open` | `sys_open()` 直接 `begin_op/end_op` | create、truncate、最终 inode put 都可能改磁盘 |
| `mkdir`、`mknod` | 处理函数直接包围 `create()` | 分配 inode、目录项和链接计数 |
| `link`、`unlink` | 处理函数直接包围整个命名空间变更 | 多个 inode/目录块必须属于同一逻辑操作 |
| `chdir` | 处理函数直接包围查找和旧 cwd 的 `iput` | 最后一个旧引用可能触发 inode 回收 |
| `exec` | `kexec()` 包围可执行文件读取 | 保护 inode 生命周期；地址空间提交在读取事务之后 |
| `write` | inode 的 `filewrite()` 拆成多个 `begin_op/end_op` 范围 | 每个范围最多写 3072 字节，以控制日志预算；范围不等于独立 commit |
| `close` | 最后一个 inode file 引用由 `fileclose()` 开事务 | `iput()` 可能回收已 unlink inode |

`read`、`fstat`、`dup`、`pipe` 本身不修改文件系统命名空间，不由 `sysfile.c` 开日志操作。它们仍可能等待 inode sleeplock、磁盘、pipe 或内存。普通 inode `read` 依赖文件在 `[0, size)` 内没有块洞；因为 `readi()` 复用了“缺块就分配”的 `bmap()`，损坏 inode 中的洞会在无事务的读取路径尝试分配。没有其他 outstanding 操作时会在 `log_write()` 触发 `panic("log_write outside of trans")`；恰有并发操作时还可能未经预留地借用其日志组，并留下块分配或泄漏副作用。

`begin_op()`/`end_op()` 标记的是一个尚未提交的文件系统操作，并为最坏情况预留日志空间。`end_op()` 不保证每个系统调用单独形成磁盘提交：多个并发操作可组成同一批次，最后一个 outstanding 操作离开时才提交。这里提供的是 xv6 redo log 的崩溃一致性，不是完整 POSIX 持久化或全系统调用数据原子性。

参数复制与日志等待的顺序也不统一：`open`、`link`、`unlink` 在 `begin_op()` 之前复制路径；`mkdir`、`mknod`、`chdir` 则先进入操作范围，再执行 `argstr()`。因此后三者即使用户字符串无效，也可能先在 `begin_op()` 等待日志空间，但错误出口仍必须执行配对的 `end_op()`。`exec` 的 path 和 argv 编组发生在 `kexec()` 开始其读取事务之前。

### 10.2 锁顺序和等待约束

- 分派器不持锁进入处理函数；处理函数可睡眠。
- 文件系统变更通常先 `begin_op()`，再获取 inode sleeplock。不能拿着 inode 锁等待日志空间，否则可能阻塞负责完成当前事务的线程。
- `create()` 持父目录锁后再持新 inode 锁；`unlink()` 持父目录锁再持目标锁；`link()` 更新旧 inode 后先释放它，再锁新父目录，避免长期同时持有无固定全局顺序的两个任意 inode 锁。
- `ticks` 只在 `tickslock` 下读取或用于 `pause` 的睡眠交接。
- `ofile[]`、`cwd`、`p->sz` 对当前单线程进程是私有字段，系统调用包装层通常不取 `p->lock`；共享 file、inode、pipe、日志和进程表由各自的锁保护。
- 任何普通错误返回和用户态返回前都不得遗留 inode sleeplock、spinlock 或未配对的 `begin_op()`；显式 `panic` 路径不属于可恢复返回。

## 11. 失败回滚与所有权清单

| 路径 | 已取得资源或已改变状态 | 失败时的处理 |
|---|---|---|
| `fork` | pid、子进程槽、trapframe、页表和有效叶页面 | `freeproc()` 回收未发布子进程；父资源不变，但 pid 计数不回退 |
| `wait` | 找到且锁住僵尸子进程 | 状态 `copyout` 失败时不回收僵尸；部分用户写入/lazy 补页不撤销 |
| eager `sbrk` | 本轮部分新页 | `uvmalloc()` 回收本轮页面，保留旧 `p->sz` |
| `dup` | 新 fd 槽 | 只有 `fdalloc` 会失败，失败前引用计数不变 |
| `open` | inode 引用、可能的 file/fd、可能的新目录项 | 归还引用和槽；后续 file/fd 耗尽不撤销已完成的 `O_CREATE` |
| `pipe` | 两个 file 引用、pipe 页、零到两个 fd | 清 fd 槽并关闭两端；已写用户数组的整数不保证撤销 |
| inode `read`/`write` | 用户缓冲区、offset、cache、块映射或日志项 | `-1` 允许前缀和分配副作用；写失败可已推进 offset，读失败通常不推进 |
| `create` | 新 inode、可能的目录/间接块 | 回收新 inode；父目录已分配的空间接块可能保留 |
| `link` | 已修改缓存并登记日志的 `nlink++` | `bad` 分支在同一操作范围执行 `nlink--`；父目录块分配可能保留 |
| `unlink` | 父和目标引用 | 正常文件系统上的普通校验错误在修改前；修改后短写是 `panic` |
| `chdir` | 新目录 inode 引用 | 失败释放新引用，成功才替换 cwd |
| `exec` 编组 | 每个参数一页，可能物化旧地址空间 lazy 页 | 临时参数页逐页 `kfree()`；旧地址空间补页不撤销 |
| `exec` 装载 | 新页表和若干新页 | 普通错误在提交前释放新页表；旧映像保持有效 |

审查 `goto bad` 时，应按这张表逐项核对“谁拥有资源”和“所有权何时转移”，而不能只看最终是否返回 `-1`。

## 12. 重要边界和教学实现限制

1. xv6 最多提取六个寄存器参数；`argraw()` 对任何不在 `[0, 5]` 的内核索引直接 `panic`，不是用户错误返回。
2. 原始 64 位 `a7` 先窄化为 `int`；调用号 0、窄化后的越界号和空表项统一打印诊断并把 `a0` 置为 `-1`，但高位非零值可能别名合法号。
3. `argint()` 只保留低 32 位且不验证高位是否为规范符号扩展；处理函数必须自己验证符号、范围和枚举。`argaddr()` 则保留完整 64 位数值。
4. 用户复制主要验证逐页的 `PTE_V|PTE_U`，输入不额外检查 `PTE_R`，输出另需 `PTE_W`；零长度可跳过地址检查，已映射末页的 `p->sz` 外字节也可能被访问。
5. 路径缓冲区是 `MAXPATH == 128`，NUL 必须出现在复制窗口内；目录分量超过 `DIRSIZ == 14` 会静默截断而不是失败，空路径还有非 POSIX 的 cwd 语义。
6. `exec` 最多 31 个非空参数，每个参数必须在 `PGSIZE` 容量内包含 NUL；argv 指针页可 lazy 补页，参数字符串页不可以。
7. fd 必须处于 `[0, NOFILE)` 且当前槽非空；无 `errno`，无法从 `-1` 判断是哪一级资源耗尽。
8. 用户地址不是在 `argaddr()` 时一次性验证；跨页访问可能产生部分用户内存、lazy 页、文件、offset、缓存或管道状态变化。
9. `copyinstr()` 不为 lazy 页补页，针对当前页表的普通 `copyin/out` 可以；相同数值地址因参数方向和类型不同可能得到不同结果。
10. `open` 模式没有完整合法性检查，`O_TRUNC` 不要求最终 fd 可写，失败的 `O_CREATE` 也可能留下新文件。
11. `mknod` 创建时不验证 major/minor，参数最终窄化为 16 位；`open` 只拒绝越界 major，设备方法到 I/O 时才检查，minor 未传给回调。
12. `kill` 是不校验状态的标志设置和唤醒，不同步等待退出；pid 0、僵尸和 init 暴露额外缺陷，正 pid 子进程仍需 `wait` 才确认回收。
13. `fstat` 当前把 `struct stat` 的 4 字节未初始化 padding 一并复制给用户，构成内核栈信息泄漏。
14. 日志组保证受支持文件系统操作的崩溃一致性，但大写入有多个操作范围且可能共享 group；用户复制、缓存前缀和某些失败分配不具备回滚能力。

## 13. 可复述的核心不变量

审阅者应能从源码验证以下陈述：

- `SYS_*` 数值、用户桩中的装载值和 `syscalls[]` 指定下标必须一一对应。
- 分派器只从当前进程 trapframe 取参数，并只把返回值写入该 trapframe 的 `a0`。
- 内核不直接解引用用户虚拟地址；地址最终必须经过用户拷贝或路径拷贝助手。
- 在处理函数稳定入口/出口处，每个非空 `ofile[]` 槽拥有一个 file 引用；`fdalloc()` 通常转移引用，`dup` 安装槽后立即补引用，`close` 清槽并减少引用。
- 每个 `begin_op()` 的普通返回分支都有且只有一个匹配的 `end_op()`。
- inode/目录的持久状态只能在相应 inode sleeplock 下修改，可能释放 inode 的路径必须处于日志事务中。
- `wait` 只有在成功交付退出状态后才回收目标；`exec` 只有在新映像完整建立后才替换旧映像。
- lazy `sbrk` 先扩大逻辑边界，物理页可在用户缺页或 `copyin/out` 时才分配；地址空间末端绝不能越过 `TRAPFRAME`。
- 所有普通返回或切走路径都释放自己取得的锁和临时资源；`exit` 是唯一在内核处理函数层正常不返回的路径，成功 `exec` 则会返回处理函数但不回到旧用户调用点。

## 14. 验证与调试

### 14.1 静态一致性

在仓库根目录核对编号、表项和桩清单：

```sh
rg -n '^#define SYS_' kernel/syscall.h
rg -n '^  \[SYS_|extern uint64 sys_' kernel/syscall.c
rg -n '^entry\(' user/usys.pl
```

查看生成结果而不编辑生成文件：

```sh
perl user/usys.pl | sed -n '1,100p'
```

应看到每个桩包含 `li a7, SYS_name`、`ecall`、`ret`，并且 `sbrk` 导出 `sys_sbrk`。

### 14.2 运行测试

完整构建并进入 xv6 shell 后，先运行快速集合：

```text
usertests -q
```

定位参数边界时可单独运行：

```text
usertests copyin
usertests copyout
usertests copyinstr1
usertests copyinstr2
usertests validatetest
usertests argptest
usertests badarg
```

定位 eager/lazy 地址空间行为时运行：

```text
usertests rwsbrk
usertests sbrkfail
usertests sbrkbugs
usertests sbrklast
usertests sbrk8000
usertests lazy_alloc
usertests lazy_unmap
usertests lazy_copy
usertests lazy_sbrk
```

定位 fd、事务和回滚时运行：

```text
usertests openiput
usertests iput
usertests pipe1
usertests sharedfd
usertests truncate1
usertests linktest
usertests unlinkread
usertests createdelete
usertests linkunlink
```

预期每个单项最后显示 `ALL TESTS PASSED`；测试后还应留意控制台是否出现 `panic`、未知系统调用或意外 page fault。

### 14.3 GDB 观察点

使用 `make qemu-gdb` 启动后，推荐按问题选择断点：

```gdb
break usertrap
break syscall
break sys_open
break sys_exec
break sys_sbrk
break sys_pipe
break begin_op
break end_op
```

在 `syscall()` 中应检查 trapframe，而不是假设当前硬件 `a*` 寄存器仍保存用户值：

```gdb
p myproc()->pid
p/x myproc()->trapframe->a7
p/x myproc()->trapframe->a0
p/x myproc()->trapframe->a1
```

调试 lazy 分配时，记录 `sys_sbrk()` 前后的 `myproc()->sz`，再在 `vmfault()` 观察首次访问地址是否严格小于 `p->sz`、是否已有映射。调试文件系统错误路径时，同时观察 inode 的 `ref/nlink`、进程 `ofile[]`、`log.outstanding` 和 `log.committing`；单看系统调用的 `-1` 无法判断资源是否已完整回滚。

## 15. 修改系统调用时的审查顺序

1. 确定公开 C 原型、寄存器参数宽度和成功/失败返回语义。
2. 分配稳定的 `SYS_*` 号，并同步用户生成桩和 `syscalls[]`。
3. 对每个整数参数明确符号、范围和枚举策略；不要把 `argint()` 当作验证。
4. 对每个用户地址明确方向、长度、是否允许空指针，以及使用 `copyin`、`copyout` 还是 `copyinstr`。
5. 列出取得的 fd、file、inode、页、进程槽和锁，标明所有权转移点。
6. 若可能修改 inode 或释放最后一个 inode 引用，先设计完整的 `begin_op/end_op` 配对和锁顺序。
7. 为每个部分失败点写回滚路径，并检查用户内存或 I/O 是否可能已产生不可撤销的部分效果。
8. 增加正常、非法地址、资源耗尽、并发和崩溃边界测试，再运行 `usertests -q` 复核整体行为。

这套顺序的目标不是让包装函数变复杂，而是确保真正的复杂度——跨特权级 ABI、不可信内存、引用所有权、睡眠和持久化——都有明确、可验证的归属。
