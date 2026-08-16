# 进程生命周期问题：答案与证据标准

先提交[问题页](../process-lifecycle.md)要求的状态机和隔离报告。本页给出判定
标准，不用固定 pid、地址或 free-page 绝对数替代运行证据。

## LIFE-00

合格的全景图先区分控制流和 ownership：shell fork 发布 child；child 在同一 pid
内以 exec 事务替换用户映像；exit 关闭 fd/cwd 并发布 zombie；正常前台 child 由
shell 的 `wait(0)` 回收。若它的 parent 先退出，`reparent()` 改由 init 持有，
`/init` 也以 `wait(0)` 回收，不接收 status。实验中 controller 的 `wait(&status)`
是另一条显式交付状态的路径。调度选择、sleep/wakeup happens-before 与具体 PTE
结构仍是黑盒；只列系统调用名称、没有分支和资源边界，不通过。

## LIFE-01

`userinit()` 只分配第一个 proc、设置 cwd 并发布 `RUNNABLE`。第一次由 scheduler
选中后，`forkret()` 仍接住 scheduler 交来的 `p->lock`，先释放它；其一次性
分支执行可能 sleep 的 `fsinit()`，发布 `first=0`，再把 `/init` 装入同一进程。
`main()` 不是普通进程上下文，不能承担需要 sleep/sched 的初始化调用；
`kexec()` 还用 `myproc()` 取得提交目标。通过答案必须同时解释“为何是进程上下文”
和“为何仍是同一个首进程”。

## LIFE-02

`allocproc()` 持 child lock 取得槽/pid、trapframe、空页表和初始 context；
`kfork()` 再复制用户页/trapframe、增加 `ofile[]/cwd` 引用并在 `wait_lock` 下
设置 parent。它最后重新取得 child lock 才写 `RUNNABLE`。过早发布可暴露空或
部分映像、未复制 trapframe、缺失 fd/cwd 或尚无 parent。`allocproc()` 内部页
分配和 `uvmcopy()` 失败会 `freeproc()` 回到 `UNUSED`；当前基线的
`filedup()/idup()/parent` 步骤没有失败返回。通过答案必须把 parent publication
与 state publication 分开，不能虚构不存在的 rollback 分支。

## LIFE-03

parent 的 `sys_fork()->kfork()` 得到新 pid，`kernel/syscall.c:syscall()` 把 handler
返回值写入 parent 的 `p->trapframe->a0`；child 复制原 trapframe 后，内核单独把
`np->trapframe->a0` 写成 0。child 第一次恢复用户寄存器时读取这个 a0，所以同一
调用点出现两个结果。只说“fork 返回两次”而没有指出两次写入不通过。

## LIFE-04

`sys_exec()` 先把用户 argv 指针数组和字符串复制到逐页 kernel buffer，调用
`kexec()` 后再逐页释放；`kexec()` 在临时页表中完成 ELF/stack/argv，失败由
`bad` 释放临时映像；成功
才替换 `p->pagetable/sz` 和 trapframe 的用户 `epc/sp`，随后释放旧映像。
pid、parent、kernel stack、trapframe 物理页、仍打开的 fd 和 cwd 都属于进程
身份而保留。通过证据需同时看到同 pid、name/pagetable/`sz` 改变、trapframe/
cwd 保持、继承 fd 可读，以及有效 ELF 已建立临时页表和程序段、却因 argv 超出
新用户栈而失败后，pid/`sbrk(0)`/fd 与完整资源账本仍有效。

## LIFE-05

`kexit()` 正在使用自己的 kernel stack，并需要保留 pid、`xstate`、trapframe/
页表供 parent 识别和回收；它先关闭 fd/cwd，再发布 `ZOMBIE` 并永不返回。
`kwait()` 是另一个执行上下文，持 `wait_lock -> child lock` 调用 `freeproc()`，
此时才能释放 trapframe、用户页表/页并把槽变回 `UNUSED`。`freeproc()` 本身不
关闭 fd/cwd，这是重要的调用前置。

## LIFE-06

`kwait()` 在 `copyout(xstate)` 成功之后才 `freeproc()`。非法 status 地址时，
第一次 wait 返回 `-1`，child 必须仍为 zombie 且 fd 已关闭；正确地址重试获得
同一 child pid 和 37 后才消失。若先回收，status 不但丢失，槽复用还会让重试
错误地观察另一个进程。

## LIFE-07

parent 指针只在 `wait_lock` 下建立、遍历和重写。`kexit()` 持该锁把 child
转给 `initproc`、唤醒原 parent 并发布自身 zombie；`kwait()` 同锁扫描后再取
候选 child lock。这样 proc 槽不能在关系仍被扫描时由另一路 wait 回收并复用。
通过答案需保持 `wait_lock -> p->lock` 顺序；完整 sleep/wakeup happens-before
不属于本题。

## LIFE-08

`kkill()` 只在 target lock 下置 `killed=1`；若 target 为 `SLEEPING`，把它改成
`RUNNABLE`。空 pipe read 被唤醒后在可取消点看到 killed 并返回，`usertrap()`
的安全点再执行 `kexit(-1)`；parent 的 wait 最后回收。通过报告必须先显示
`target_sleeping=1`，再显示 wait status=-1 和完整账本恢复。

## LIFE-09

`UNUSED` 槽的 `pid` 为 0。`kkill(0)` 只比较 pid，没有先排除 `UNUSED`，所以
可写 `killed=1`；`allocproc()` 设 pid/state 并分配资源，却不显式清 killed。
正常回收时 `freeproc()` 会清零；若污染槽被选中并成功发布，新进程会在后续
`usertrap()` 安全点观察 `killed=1` 并异常 `kexit(-1)`。该问题是绕过正常身份
前置后的当前分支边界，只做静态推理，不执行危险命令。

## LIFE-10

middle 的 `kexit()` 在 `wait_lock` 下把 grandchild 的 parent 改为 `initproc`；
controller 只能 wait middle。`ORPHAN_REPARENT` 必须显示 grandchild 仍存在、已
`SLEEPING` 且 parent 为 init。gate 放行后 grandchild exit，`/init` 自己的
`wait(0)` 回收它，controller 以 `ORPHAN_REAP target_present=0` 观察结果；它不
拥有该 child，因此不能取得其 status。

## LIFE-11

只有 `fork_fail=-1`、`used=NPROC` 且 `free>0` 同时成立，才能排除先耗尽物理页。
一个共享 gate pipe 只新增两个 global file objects；每个 child 继承 0/1/2 和
gate read 四个 refs，所以满载关系为 `active_files=base+2`、
`refs=base+2+children*4`，parent link 增加 children。关闭 gate、wait 所有 child
后，`CAPACITY_REAP` 的 used/free/active_files/refs/parents 必须逐项等于 BASE。

## LIFE-12

综合答案从 BASE 建立账本，经 EXEC_READY/AFTER 说明 fork ownership 与 exec
commit，经 WAIT_BAD/REAP 说明 status 交付和延迟回收，再以 EXECFAIL 说明临时
映像 rollback，以 KILL、ORPHAN、CAPACITY 各自的确定性前置和回收关系收束到
FINAL。每段都必须连接触发器、源码状态、身份字段和基线恢复；不能用最终 PASS
替代中间关系。CPUS=1 snapshot 不证明多 hart lock order、lost wakeup、PTE
权限或持久化恢复。

## 证据边界

S/F/B 共同支持本单元；CPUS=1 snapshot 不支持跨 hart lock order 或 lost-
wakeup 结论，free-page freelist 也不能单独归因每个物理页类型。capacity 只有在
`used==NPROC` 且仍有 free pages 时才能称为 slot exhaustion；所有 phase 最终
必须把 slot/page/file/link 账本恢复到同一个 baseline。
