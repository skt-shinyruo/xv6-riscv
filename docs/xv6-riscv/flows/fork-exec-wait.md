# `fork -> exec -> wait`：从 shell 派生到回收

本文沿当前仓库的一条真实路径追踪：shell 读取普通命令，`fork()` 创建执行进程，子进程 `exec()` 装入目标 ELF，目标程序 `exit()`，父 shell 用 `wait()` 回收。完整机制分别见[进程与调度](../kernel/processes-and-scheduling.md)、[`exec`](../kernel/exec.md)、[系统调用](../kernel/system-calls.md)和[`init` 与 shell](../user/init-and-shell.md)。

## 1. 端到端总览

以 shell 输入 `echo hello` 为例：

```text
sh
  |- fork() -----------------------------------------.
  |   parent gets child pid                           |
  |   `- wait(0): reap now, or sleep if child lives   |
  |                                                   |
  `- child gets 0                                     |
      `- runcmd(EXEC)                                 |
          `- exec("echo", {"echo", "hello", 0})   |
              `- new echo image                       |
                  `- main -> exit(0)                  |
                      `- ZOMBIE + wake parent --------'

sh wait:
  copy optional status -> freeproc(child) -> return child pid
```

`fork` 复制一个进程，`exec` 替换调用者的用户地址空间，`exit` 只把它变成僵尸，`wait` 才释放进程槽、trapframe、页表和用户物理页。这四个动作的边界不能混为一体。

## 2. shell 为什么先 fork

除内建 `cd` 外，`user/sh.c` 为每一行创建一个临时执行进程：

```c
if (fork1() == 0)
  runcmd(parsecmd(cmd));
wait(0);
```

若 shell 自己直接 `exec` 普通命令，它的地址空间会被永久替换，命令结束后就没有 shell 可读取下一行。fork 让父 shell 保留解析循环、cwd 和控制台 fd；子进程继承这些状态并可自由重定向或 exec。

## 3. 从用户 `fork()` 到 `kfork()`

用户 stub 把 `SYS_fork` 放入 `a7` 并执行 `ecall`。trap 路径保存寄存器后，`syscall()` 分派到 `sys_fork()`，后者只调用本分支命名的 `kfork()`。

`kfork()` 先通过 `allocproc()` 取得一个仍持有 `np->lock` 的 `USED` 槽：

- 分配唯一 pid；
- 分配独立 trapframe 物理页；
- 建立空用户页表并映射 trampoline 与该 trapframe；
- 把第一次内核上下文的 `ra` 设为 `forkret`，`sp` 设为永久内核栈顶。

如果没有 `UNUSED` 槽，`allocproc()` 在释放每个检查过的槽锁后直接返回 0；如果认领槽后 trapframe 或页表分配失败，它才调用 `freeproc()` 回滚并把槽恢复为 `UNUSED`。两种情况最终都让父进程得到 -1，且不会发布半成品子进程。

## 4. 复制地址空间

`uvmcopy(parent, child, p->sz)` 按页扫描父地址空间：

```text
no page-table page       -> continue
PTE exists but invalid   -> continue
valid leaf               -> kalloc + copy 4096 bytes + same PTE flags
```

因此本实现不是 copy-on-write：地址范围内的每个有效叶映射都立即获得独立物理副本，包括清除了 `PTE_U` 的用户栈 guard 映射。对普通可写用户页，父子随后写相同虚拟地址互不影响。

lazy allocation 留下的未映射页洞会被跳过。子进程继承相同逻辑 `sz`，以后触及自己的洞时独立 fault、独立分配。复制中途内存不足时，`uvmcopy` 释放已经建好的子页，`kfork` 再回滚整个进程。

## 5. 复制 trapframe 和资源引用

地址空间成功后，`kfork()` 整体复制父 trapframe。里面保存的是父进程刚进入 fork 系统调用时的用户寄存器，特别是 `epc` 已由 `usertrap()` 推进到 `ecall` 后一条指令。

随后只修改：

```c
np->trapframe->a0 = 0;
```

这让子进程从 fork 返回 0。父进程的 `sys_fork()` 返回 child pid，`syscall()` 把它写入父 trapframe 的 `a0`。同一次逻辑调用由此出现两个返回值。

其他继承项：

| 资源 | fork 行为 | 后果 |
|---|---|---|
| fd 槽 | 每项 `filedup()` | 父子指向同一全局 `struct file`，共享 offset |
| cwd | `idup()` | 同一 inode 的独立内存引用 |
| name | 字符串复制 | 先沿用父调试名，exec 后更新 basename |
| pid | 新分配 | 不继承 |
| parent | 指向调用者 | 受全局 `wait_lock` 保护 |
| killed | 不复制；正常情况下来自 BSS 初值或上一次 `freeproc()` 写入的 0 | 不继承父进程的 kill 标志，但见下述 `kill(0)` 缺陷 |
| xstate | 不复制；来自 BSS 初值或上一次 `freeproc()` 写入的 0 | 只有进程进入 ZOMBIE 后才承载本次退出状态 |

这里的零初值是当前调用链维持的结果，不是 `allocproc()` 自己提供的保证：`allocproc()` 会写 pid/state、分配 trapframe/页表并重置 context，却不会主动清零 `killed` 或 `xstate`。静态 `proc[]` 首次由内核 BSS 清零；正常回收时 `freeproc()` 再清零这两个字段，所以通常看不到旧值泄漏到新进程。

当前 `kkill(pid)` 只比较 `p->pid == pid`，没有同时要求槽状态不是 `UNUSED`。由于空槽的 pid 为 0，用户执行 `kill(0)` 可能命中第一个空槽、把它的 `killed` 写成 1 并错误返回成功；下一次 `allocproc()` 若认领该槽，又不会覆盖这个字段，新进程便带着 kill 标志发布。`forkret()` 本身不检查 `killed`，所以该进程可能短暂进入用户态，到下一次用户 trap 时才由 `usertrap()` 执行 `kexit(-1)`。这个污染不涉及 `xstate`。要修源码，应在 `kkill()` 排除空槽/非正 pid，或让 `allocproc()` 显式建立全部标量初值；本文记录的是未修源码的实际行为。

## 6. 发布顺序防止半初始化运行

子进程在复制过程中保持 `USED`，调度器不会选择它。初始化结束后：

1. 保存 pid 并释放 `np->lock`。
2. 在 `wait_lock` 下设置 `np->parent = p`。
3. 重新获取 `np->lock`，把状态改为 `RUNNABLE`。

parent 关系在 runnable 之前可见，因此子进程即使立刻在另一 CPU 运行并退出，`kexit()` 也能唤醒正确父进程。父子资源未复制完整前不会暴露给 scheduler。

## 7. `forkret()` 的两条首次调度路径

### 7.1 普通 fork 子进程

新进程不是从内核 `kfork()` 调用栈继续执行。scheduler 的 `swtch()` 恢复 `allocproc()` 设置的 context，第一次进入 `forkret()`：

```text
release child p->lock inherited from scheduler
prepare_return()
compute child user satp
jump trampoline:userret(satp)
```

只有全系统第一个进程会在 `forkret()` 的 `first` 分支执行 `fsinit()` 和 `kexec("/init")`；普通 fork child 直接走返回用户态路径。

`userret` 切到子页表、恢复复制的寄存器并 `sret`，从 `ecall` 后一条用户指令继续，`a0 == 0`。shell 子进程因而进入 `if (fork1() == 0)` 分支。

### 7.2 首进程不是普通 fork child

当前仓库的启动路径与经典的内嵌 `initcode` 版本不同：

```text
main()
  -> userinit()
       allocproc()                         state: UNUSED -> USED
       initproc = p
       p->cwd = namei("/")
       p->state = RUNNABLE                 发布空首进程
  -> scheduler()
       p->state = RUNNING
       swtch(..., p->context)
  -> forkret()                             继承 scheduler 持有的 p->lock
       release(p->lock)
       fsinit(ROOTDEV)                     仅 first==1 时
       first = 0 + 全顺序 fence
       p->trapframe->a0 = kexec("/init", {"/init", 0})
       prepare_return() -> trampoline:userret
  -> user/ulib.c:start(argc=1, argv)
       -> user/init.c:main()
```

`userinit()` 不分配普通用户页，也不设置用户入口；其页表最初只有 supervisor-only 的 trampoline 和 trapframe 映射。`namei("/")` 在此时不会读盘，因为 `/` 没有待遍历的分量，路径解析只用 `iget(ROOTDEV, ROOTINO)` 取得 inode cache 引用。

这条根路径分支不会以 0 表示查找失败：`iget()` 要么命中/占用一个 inode cache 槽并返回非空指针，要么在 `NINODE` 个槽全部有引用时 `panic("iget: no inodes")`。正常启动时 inode table 刚初始化，后者不应发生；但这里也尚未读取或验证磁盘上的 root dinode，首次 `ilock()` 才装入它，若其 `type == 0` 会走 `panic("ilock: no type")`。因此不能把 `userinit()` 未检查返回值解释成会发布 `cwd == 0` 的恢复分支。

`fsinit()` 可能因磁盘 I/O 调用 `sleep()`，所以必须等 scheduler 和一个普通进程 context 已存在后执行，不能从 `main()` 直接调用。首进程在 `forkret()` 中直接调用 `kexec()`，没有经过 `sys_exec()` 或系统调用分派，因此由 `forkret()` 自己把返回的 `argc` 写入 trapframe `a0`。普通 fork 子进程后来进入 `forkret()` 时看到全局 `first==0`，不会重复初始化文件系统或再次装入 `/init`。

## 8. shell 子进程执行 AST

简单命令的 AST 类型为 `EXEC`。`runcmd()` 调用：

```c
exec(ecmd->argv[0], ecmd->argv);
```

重定向会在此前关闭/重新打开 fd，pipe 会再创建端点进程。无论拓扑多复杂，最终某个进程调用 exec；已经建立的 fd 引用由 exec 保留，所以管道和重定向自然进入新程序。

## 9. `sys_exec()` 先复制不可信 argv

系统调用层不能在装载过程中长期信任旧用户地址空间。`kernel/sysfile.c:sys_exec()`：

1. `argstr()` 把路径复制到内核 `path[MAXPATH]`。
2. 从用户 argv 数组逐个 `fetchaddr()` 读取 64 位指针。
3. 为每个字符串分配一整页内核缓冲区，并 `fetchstr()` 复制。
4. 内核数组只有 `argv[0]` 到 `argv[MAXARG-1]`；其中必须有一个槽保存终止空指针，所以最多接受 `MAXARG-1` 个非空参数字符串。循环在读取下标 `MAXARG` 前失败，换言之最后允许的终止符位置是 `argv[MAXARG-1]`。
5. 调用 `kexec(path, kernel_argv)`。
6. 无论成功失败，释放所有 argv 内核页。

这保证 `kexec()` 替换旧页表后仍能访问参数内容。坏指针、字符串不终止或内存不足都在页表提交前返回 -1，但“旧映像未被新映像替换”不等于没有任何可观察副作用：`fetchaddr()` 使用的 `copyin()` 会为位于 `p->sz` 内的合法 lazy hole 调用 `vmfault()`，所以读取用户 `argv[]` 指针槽时可能给旧页表物化并清零一页。路径和参数字符串走 `copyinstr()`，它不会补 lazy 页。exec 随后即使失败，旧页表身份、逻辑地址范围以及未被 loader 提交改写的 `sp/a1` 仍保留，刚物化的旧 lazy 页也会保留到以后退出或再次 exec；但这仍是一次正常返回的系统调用，trap 入口已经把 `epc` 推进到 `ecall` 后一条指令，分派层还会把 `a0` 写成 -1，不能概括成“全部寄存器不变”。

`sys_exec()` 的 31 参数上界还遮住了 `kexec()` 自身的一个 off-by-one 前置条件。`kexec()` 的 `ustack[MAXARG]` 循环只在处理非空参数的循环体内检查 `argc >= MAXARG`，终止指针则在循环后执行 `ustack[argc] = 0`。内部调用者若直接传入恰好 `MAXARG` 个非空参数再加 NULL，会越界写 `ustack[MAXARG]`；系统调用编组层不会构造这种输入，当前 `forkret()` 也只传一个 `/init` 参数，但任何新增的内核调用者都必须把非空参数限制为 `MAXARG-1`。

## 10. `kexec()` 的准备阶段

`kexec()` 在事务中 `namei` 并锁 ELF inode，验证 ELF magic 和每个 loadable program header：

- `memsz >= filesz`；
- `vaddr + memsz` 不溢出；
- segment virtual address 页对齐；
- `uvmalloc()` 返回非零，且 `loadseg()` 按其 32 位 `uint` 长度参数完整读到截断后的字节数。

它创建全新页表，而不是先破坏旧表。`uvmalloc()` 为 segment 申请页并按 ELF flags 映射，`loadseg()` 从 inode 读入文件字节；`loadseg()` 的 `offset/sz` 形参是 32 位 `uint`，而 ELF header 的 `off/filesz` 是 64 位，所以这条“完整读取”只适用于当前小型文件系统和 32 位范围内的正常构建产物，不能外推为任意 ELF64 大文件支持。`memsz - filesz` 部分因新页清零形成 BSS。

文件数据读完即解锁/释放 inode并 `end_op()`。后续用户栈构建不再依赖文件系统。

这些不是完整的敌对 ELF 校验。当前代码没有验证段按虚拟地址递增且互不重叠、段终点避开 `MAXVA/TRAPFRAME`，也不验证 `elf.entry` 确实落在可执行用户页。本文的正常流程针对本仓库 linker 生成并打入 `fs.img` 的 ELF；异常布局可能在现有内部前置条件处 panic，或者 exec 先成功、随后在首次取指时被 kill，不能一概视为会干净返回 -1。完整限制见 [`exec`](../kernel/exec.md)。

## 11. 新栈和参数 ABI

程序段顶部向上页对齐后，exec 分配一页 guard 加 `USERSTACK` 页用户栈。guard 页保留有效映射但清除 `PTE_U`，使实际命中该页的向下越界触发权限 fault；一次跳过整页 guard 后落入更低有效映射的访问不在其保护范围内。

参数从栈顶向下放置：

```text
high address / initial stack top
  argument strings, each placement aligned to 16 bytes
  argv[0..argc-1] user pointers
  null pointer
low address / stackbase
  guard page below (not PTE_U)
```

每一步检查不低于 `stackbase`。最终 `trapframe->a1 = argv_array_address`；`kexec()` 返回 argc，外层 syscall 分派把它放入 `a0`，正好满足新程序 `main(argc, argv)` ABI。

## 12. exec 的提交点

所有验证和分配成功后才执行：

```text
oldpagetable = p->pagetable
p->pagetable = new pagetable
p->sz = new size
p->trapframe->epc = elf.entry
p->trapframe->sp = new sp
free old page table and user pages
```

这是地址空间替换的逻辑提交区。此前失败只销毁临时新页表，旧 shell child 可以从 `exec()` 得到 -1、打印错误并退出。从写入 `p->pagetable` 开始，后续语句没有返回失败的分支，最终释放旧页表；提交后旧用户代码、AST、heap 和 stack 都消失，成功 exec 不会返回到旧 wrapper。

这里的“提交”表示错误处理边界，不是单条硬件原子事务：`pagetable`、`sz`、`epc` 和 `sp` 是依次写入的。它们由当前进程私有，timer 中断即使让当前进程暂时 `yield()`，同一进程也不会在另一个 hart 上并发执行这段代码；恢复后会从原内核栈继续完成剩余提交步骤。

不会被替换的进程身份包括 pid、parent、内核栈、trapframe 物理页、打开 fd 和 cwd。进程调试名改为路径 basename。

## 13. 进入新程序

`syscall()` 把 `kexec()` 返回的 argc 写入当前 trapframe `a0`。之后走普通 `prepare_return()`/trampoline `userret`，但 trapframe 的 `epc`、`sp`、`a0`、`a1` 已指向新映像。

对普通用户 ELF，`sret` 后从用户运行库 `start()` 开始。`start()` 调用 `main(argc, argv)`；main 返回时运行库调用 `exit(ret)`。目标程序从未执行旧 exec 调用后的下一条指令。

`user/_forktest` 是 Makefile 中唯一的入口特例：它用 `-e main -Ttext 0` 链接，只包含 `forktest.o + ulib.o + usys.o`，所以 `epc` 直接指向 `main()`，不会经过 `start()` 的返回包装。其错误分支在 `forktest()` 内显式 `exit(1)`，成功路径由 `main()` 显式 `exit(0)`；若把普通程序“main 可以直接 return”的结论套到该特例，返回后将没有有效的调用者。

## 14. `kexit()`：释放运行资源但保留僵尸

`sys_exit(status)` 调用不返回的 `kexit(status)`。它禁止 `initproc` 退出，然后：

1. 关闭所有 fd；最后 file 引用可能关闭 pipe 或在事务中 `iput` inode。
2. 在事务中释放 cwd inode 引用。
3. 获取 `wait_lock`，把所有 `parent == p` 的直接孩子 reparent 给 `initproc`，其中也包括尚未被旧父进程回收的 ZOMBIE。
4. `wakeup(p->parent)`，防止父进程仍睡在 wait。
5. 获取自己的 `p->lock`，写 `xstate` 并设 `ZOMBIE`。
6. 释放 `wait_lock` 后 `sched()`，永不再运行。

此时用户页、trapframe、pid 和 proc 槽都仍存在。这使父进程能读取退出状态，也避免父进程在 child 仍使用内核栈时提前释放它。

## 15. `kwait()`：查找、睡眠和回收

父 shell 的 `wait(0)` 分派到 `kwait(addr=0)`。它持 `wait_lock` 扫描整个 proc table，只考虑 `pp->parent == current` 的槽；对候选再获取 `pp->lock`，与 child 的 exit/swtch 串行。

发现 ZOMBIE 时：

```text
remember pid
if status address != 0:
  copyout child xstate
freeproc(child)
return pid
```

`freeproc()` 才释放 trapframe、用户页表/物理页并把状态改为 `UNUSED`。因此 ZOMBIE 是“运行资源已关闭但身份/地址空间尚待父回收”的状态。页表回收会逐页扫描 `[0, child->sz)`；child 即使只有少量物化 lazy 页，成功 wait 仍可能在持有 `wait_lock` 和 child lock 时花费 `O(child->sz/PGSIZE)`。

若 status 用户指针无效，copyout 失败后 wait 返回 -1，但不调用 `freeproc()`；zombie 保留，父进程可用合法指针或 0 再试。这避免因无法交付状态而永久丢失 child。

扫描 zombie 的动作先于 `killed(p)` 判断。已经被 kill 的父进程若扫描到 zombie，`kwait()` 仍会先尝试复制状态并回收一个 child；成功返回内核系统调用层后，`usertrap()` 的统一 killed 检查会让父进程 `kexit(-1)`，所以用户态通常看不到这个 wait 返回值。只有完整扫描没有发现 zombie 时，`kwait()` 才根据“没有 child”或“父进程已 killed”返回 -1。

## 16. wait 的无丢失唤醒

没有 zombie 但仍有 child 时，父进程：

```c
sleep(p, &wait_lock);
```

`sleep` 在设置 SLEEPING/channel 时以 `p->lock` 接替条件锁，child 的 `wakeup(parent)` 扫描时也获取相同进程锁，因此“检查无 zombie”与“进入睡眠”之间不会丢失退出通知。醒来后 sleep 重新取得 `wait_lock`，循环再次扫描条件，而不是假设某个特定 child 已退出。

若完整扫描没有 zombie：没有任何 child 时立即返回 -1；仍有 child 但父本身 killed 时也返回 -1；否则才睡眠。接口不能指定 pid，多个 child 时回收 proc table 扫描顺序中第一个 zombie，而不是按退出时间排序。

## 17. orphan 路径

父进程退出时，`reparent()` 在 `wait_lock` 下把其所有直接 children 的 parent 指向 `initproc`，不按 child 状态过滤，并对每个匹配项调用 `wakeup(initproc)`。仍在运行/睡眠的 child 将来退出后由 init 回收；已经是 ZOMBIE、只是旧父尚未 wait 的 child 则可由 init 立即回收。后一个分支正是 reparent 不能只处理“活着的孩子”的原因，也解释了为何改写 parent 时必须唤醒 init：该 zombie 不会再执行一次 exit 来发送新通知。

shell 后台命令常走这条路：临时 command runner 派生后台 child 后立即退出，后台 child 被 init 收养。init 会忽略不是当前 shell pid 的 wait 返回值并继续等待。

## 18. 锁和所有权时间线

| 阶段 | 关键锁 | 所有权变化 |
|---|---|---|
| alloc child | `np->lock` | 新槽/trapframe/pagetable 属于 child |
| copy fd/cwd | ftable/itable 内部锁 | child 新增共享引用 |
| attach parent | `wait_lock` | parent 指针可被 exit/wait观察 |
| publish runnable | `np->lock` | scheduler 可选择 child |
| exec load | inode sleeplock + 活跃日志操作预留 | 临时页表属调用进程但未发布 |
| exec commit | 当前进程私有状态，无额外进程锁 | 进入不再失败的提交区，依次换页表/寄存器并释放旧页；不是硬件原子操作 |
| exit/reparent | `wait_lock` 后 `p->lock` | child 运行权结束，parent 获回收权 |
| wait reap | `wait_lock` + child lock | proc 槽归系统复用 |

## 19. 失败路径

| 位置 | 用户可见结果 | 必须保持 |
|---|---|---|
| 无 proc/trapframe/page | fork 返回 -1 | 无半发布 child、无页泄漏 |
| uvmcopy 中途失败 | fork 返回 -1 | 已复制 child 页全部释放 |
| 路径不存在、ELF magic/已实现约束失败或短读 | exec 返回 -1 | 不提交新映像的 entry PC、sp 或 a1，fd/cwd 不变；正常 syscall 返回仍已推进旧 `epc` 并把 a0 写成 -1，导入 argv 指针时物化的旧 lazy 页不会回滚 |
| exec 新页/栈/argv不足 | exec 返回 -1 | 临时页表完整回滚 |
| 内核直接向 `kexec()` 传 32 个非空参数 | 未定义行为风险 | `ustack[32] = 0` 越界；当前 `sys_exec()` 上界和 `/init` 调用不会触发 |
| ELF 通过现有检查但布局/entry 异常 | 可能稍后被 kill，部分内部前置条件还可能 panic | loader 不是面向敌对 ELF 的完整验证器 |
| `userinit()` 中 `allocproc()` 返回 0 | 内核启动失败 | 当前代码随即解引用空 `p`，没有回滚或错误返回 |
| `userinit()` 获取 root 时 inode cache 无空槽 | 内核 `panic("iget: no inodes")` | `namei("/")` 的该分支不会返回 0；正常冷启动的空 table 避免此错误 |
| 首次 `kexec("/init")` 失败 | 内核 `panic("exec")` | 不返回空地址空间，也没有备用 init |
| wait status 地址坏 | wait 返回 -1 | zombie 尚未回收，可重试 |
| parent 被 kill 且已有 zombie | wait 先回收一个 zombie，随后 trap 路径 exit | killed 检查位于扫描之后；其余 children reparent 给 init |
| parent 被 kill 且没有 zombie | wait 返回 -1，随后 trap 路径 exit | children reparent 给 init |
| 用户调用 `kill(0)` | 可能错误返回成功并污染一个 UNUSED 槽 | 后续新进程继承 `killed == 1`；`allocproc()` 不主动清零 |
| `initproc` 调用 exit | 内核 `panic("init exiting")` | 检查发生在关闭 fd/cwd 和 reparent 之前，不存在正常的 init 退出路径 |

## 20. 验证与调试

先在单 hart 和默认多 hart 下分别验证，前者便于观察确定顺序，后者覆盖真实锁竞争：

```sh
make qemu CPUS=1
make qemu
```

在 xv6 shell 中运行：

```text
echo hello
usertests exectest
usertests bigargtest
usertests exitwait
usertests reparent
usertests reparent2
usertests forktest
usertests -q
```

`exectest` 覆盖正常 ELF 替换，`bigargtest` 覆盖参数页和栈边界，`exitwait/reparent/reparent2` 覆盖退出、回收和锁顺序，`forktest` 覆盖资源耗尽后的回滚。`usertests -q` 还会比较测试前后空闲页数，能发现 fork/exec 失败路径中的物理页泄漏。

GDB 可设置以下断点：

```gdb
b kfork
b forkret
b sys_exec
b kexec
b kexit
b kwait
b reparent
```

观察一次命令时记录 pid、`state`、`parent`、`trapframe->epc/a0/a1/sp`、`pagetable`、`ofile[]` 与 cwd。最容易混淆的是：fork child 第一次内核入口是 `forkret`；exec 成功仍从同一次 trap 返回，但返回到全新 ELF；exit 后页表直到 wait 才由 `freeproc` 释放。

## 21. 核心结论

这条链靠三次“延迟发布/释放”保证正确性：fork 完整复制后才置 RUNNABLE，exec 完整构建后才替换页表，exit 完成关闭后只置 ZOMBIE、等 parent wait 才释放进程容器。理解这三个提交边界，就能系统分析资源耗尽、并发退出、坏指针和 orphan 等失败场景。
