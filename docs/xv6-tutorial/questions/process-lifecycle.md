# 进程生命周期问题

本页迁移原 `BOOT-04` 与 `PROC-03/04/05/06/07/08/11/12`。它们由
`core.process-and-memory` 拥有，硬前置是[进程生命周期与回收](../core/process-and-memory.md)。
先完成状态/ownership 图和 `lifecycle` 报告，再查看[答案与证据标准](answers/process-lifecycle.md)。

## 问题

### LIFE-00

shell 启动一个前台命令直到 shell 回收它；若中间 parent 先退出，orphan 又怎样
转交给 `/init` 回收？进程槽、用户映像、fd/cwd 引用和 parent link 分别经过
哪些所有者与状态？哪些步骤由 #9 解释，哪些只作为调度或虚拟内存黑盒？

证据要求：先画 `sh fork -> child exec -> program exit -> parent wait` 的全景图，
再并列 `middle exit -> reparent -> init wait(0)`；把控制流与四类资源账本分开，
并给出 `proc.c/exec.c/init.c/sh.c` 的阅读入口。

### LIFE-01（原 BOOT-04）

当前分支为何让 `userinit()` 创建空地址空间，再由第一次 `forkret()` 执行
`fsinit()` 和 `kexec("/init", ...)`？为什么这两步不能简单放进 `main()`？

证据要求：从 `userinit -> scheduler -> forkret -> kexec -> /init` 画出首进程
控制链，并指出文件系统初始化可能 sleep 所需的进程上下文。
还要说明 `kexec()` 通过 `myproc()` 把新映像提交给当前 proc，不能从无进程上下文
的 `main()` 调用。

### LIFE-02（原 PROC-03）

`kfork()` 如果在 `uvmcopy/filedup/idup/parent` 完成前就把 child 设为
`RUNNABLE`，另一个 hart 可能看到哪些半初始化状态？

证据要求：按源码顺序列出 child 取得的资源和过早发布时可见的半成品，标出唯一
发布点；区分 `allocproc()` 内部分配/`uvmcopy()` 的可恢复失败，与当前基线中
`filedup()/idup()/parent` 没有失败返回的步骤。

### LIFE-03（原 PROC-04）

一次 `fork()` 为什么会在 parent 和 child 中分别返回 child pid 与 0？

证据要求：追踪 parent 的 `kfork()` 返回值怎样由 `kernel/syscall.c:syscall` 写入
parent trapframe，以及复制后 `np->trapframe->a0=0`；不能只用用户 API 语义回答。

### LIFE-04（原 PROC-05）

为什么 `exec()` 不创建新进程？成功后哪些对象被替换，哪些身份与引用必须
保留；失败为何还能从旧程序继续？

证据要求：比较 `lifecycle` 的 `EXEC_READY/EXEC_AFTER/EXECFAIL`；后者必须用
有效 ELF 和超出新用户栈容量的 argv 在临时映像构造后失败；同时追踪
`kernel/sysfile.c:sys_exec` 的 argv 预拷贝/释放，以及 `kernel/exec.c:kexec` 的
commit 或 `bad` 路径。

### LIFE-05（原 PROC-06）

`exit()` 为什么不能释放自己的 kernel stack、trapframe、用户页表和 proc
槽，而必须留下 `ZOMBIE`？

证据要求：区分 `kexit()` 已关闭的 fd/cwd 与仍由 zombie 持有的对象，再指出
哪个进程在何处调用 `freeproc()`。

### LIFE-06（原 PROC-07）

`wait(status)` 已找到 zombie 后，如果 `status` 是非法用户地址，为什么不能
先回收再返回 `-1`？

证据要求：用 `WAIT_BAD -> EXEC_REAP` 证明第一次失败后同一 pid/zombie 仍在，
第二次才交付 status=37 并回收。

### LIFE-07（原 PROC-08）

`parent` 是可复用 proc 槽的裸指针；`wait_lock`、reparent 和 wait 的锁序怎样
阻止 parent-child 关系在扫描/退出间变成悬空或 ABA？

证据要求：标出 `kfork/reparent/kexit/kwait` 对 parent 的访问边界，并解释为何
只拿 child `p->lock` 不足。完整 lost-wakeup 证明留给后续同步单元。

### LIFE-08（原 PROC-11）

为什么 `kill()` 不是同步销毁？一个已阻塞在空 pipe read 的进程，从
`SLEEPING` 到 parent 收到 `-1` status 要经过哪些检查点？

证据要求：触发 kill 前必须提交 target 已 `SLEEPING` 的快照，再从
`kernel/proc.c:kkill -> kernel/pipe.c:piperead -> kernel/trap.c:usertrap ->
kernel/proc.c:kexit/kwait` 追到 status=-1；仅用“最终 wait 返回”或 timeout 不通过。

### LIFE-09（原 PROC-12）

当前 `kill(0)` 为什么可能污染一个 `UNUSED` 槽？下一次 `allocproc()` 为什么
不会主动修正这个字段？

证据要求：比较 `kkill()` 的 pid 判定、`freeproc()` 的清零和 `allocproc()` 的
初始化顺序，再追踪成功发布后新进程会在哪个 `usertrap()` 安全点因继承的
`killed=1` 异常退出。只做源码推理；不要在共享或验收 guest 上执行这个有害命令。

### LIFE-10

middle 退出后，仍阻塞的 grandchild 怎样由 `reparent()` 转交给 `initproc`，
并由 `/init` 的 `wait(0)` 最终回收？为什么 controller 只能观察它消失，不能替
`/init` 取得该 child 的 status？

证据要求：把 `ORPHAN_REPARENT -> ORPHAN_REAP` 的 target pid、`SLEEPING`、
`parent_is_init` 和 absent 字段连回 `reparent/kexit/kwait` 与 `user/init.c:main`；
必须区分 controller 的 `wait(middle)` 与 init 的 `wait(0)`。

### LIFE-11

容量阶段 `fork() == -1` 时，怎样证明耗尽的是 NPROC 槽而不是物理页或 file
object？共享 gate pipe 上的 proc slots、free pages、file objects/refs 和 parent
links 应满足哪些关系，全部 child 回收后又应看到什么？

证据要求：推导 `used==NPROC && free>0`、`active_files=base+2`、
`refs=base+2+children*4`、`parents=base+children`，并用 `CAPACITY_REAP` 验证四类
账本都回到 BASE；仅报告 fork 失败不通过。

### LIFE-12

给定 `lifecycle` guest 的整次运行，怎样从同一份报告重建正常生命周期、提交与
回滚、失败重试、blocked kill、orphan 收养和容量释放的完整控制流与 ownership？

证据要求：只复用前面已经建立的概念，按 `BASE -> EXEC* -> EXECFAIL -> KILL*
-> ORPHAN* -> CAPACITY* -> FINAL` 串联所有字段与源码边；最后说明该证据为什么
不能推出多 hart 调度、lost wakeup 或 PTE 权限结论。

## 提交边界

整组问题共同引用一份生命周期与资源账本报告包，不另建同步副本。`PROC-01/02/09/10`
仍由 #10 的调度与同步单元拥有；虚拟内存问题仍留在原位置，等待 #11 晋级。
