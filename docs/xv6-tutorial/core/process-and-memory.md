# 进程生命周期与回收

## 问题场景与本单元成果

shell 执行一个命令时，`fork()`、`exec()`、`exit()` 和 `wait()` 不是四个互不
相关的系统调用。它们共同转移进程槽、用户页、文件引用、当前目录和 parent
关系的所有权。把 `exit()` 误解成“立即删除进程”，或把 `exec()` 误解成
“创建新进程”，都会在 zombie、失败回滚和孤儿回收处得到矛盾模型。

本单元的唯一出口是一个可独立复核的“进程生命周期与资源账本报告包”。报告
必须包含状态机、`fork -> exec -> wait` 的前后快照、有效 ELF 的后期失败
`exec`、错误 `wait` 地址、blocked `kill`、orphan 收养/回收、NPROC 容量边界、三层回归和
清理证明。这些分节共同构成一个出口产物，不是多份互相独立的日志。

## 前置单元与暂存黑盒

硬前置：[启动、陷阱、中断与汇编边界](boot-traps-and-interrupts.md)。相关单元：
[用户程序如何成为可运行镜像](user-program-and-abi.md)与[一次系统调用如何往返](syscall-roundtrip.md)。

本单元解除 `first-user-process`，并完整拥有进程身份、parent、zombie、wait、
reparent、delayed kill 和生命周期资源交接。两个机制只在边界出现：

- scheduler 为什么跨 `swtch()` 交接 `p->lock`，以及 `sleep/wakeup` 为什么不
  丢唤醒，由后续调度与同步单元解释；这里仅在快照确认目标已经 `SLEEPING`
  后触发 `kill`。
- PTE、lazy hole、trampoline/trapframe 映射、permission fault 与 TLB 由后续
  虚拟内存单元解释；这里仅追踪“新映像临时拥有 -> commit 后进程拥有 ->
  旧映像释放”的进程级事务边界。

## 最小模型和关键不变量

### 状态机不是生命周期阶段名称列表

当前 `kernel/proc.h:enum procstate` 的可达转换如下：

```text
UNUSED --allocproc------------------------------------------> USED
USED   --userinit/kfork 完成全部初始化并发布---------------> RUNNABLE
USED   --构造失败/freeproc---------------------------------> UNUSED
RUNNABLE --scheduler----------------------------------------> RUNNING
RUNNING  --yield--------------------------------------------> RUNNABLE
RUNNING  --sleep--------------------------------------------> SLEEPING
SLEEPING --wakeup/kkill-------------------------------------> RUNNABLE
RUNNING  --kexit--------------------------------------------> ZOMBIE
ZOMBIE   --kwait/freeproc-----------------------------------> UNUSED
```

`SLEEPING` 不能直接变成 `RUNNING`，`ZOMBIE` 不能再次运行，未完成的 `USED`
不能被 scheduler 选择。`RUNNABLE` 是发布边界：child 的页表、trapframe、
`ofile[]`、`cwd`、name 和 parent 必须在它之前可用。

| 状态 | 必须仍拥有 | 已经不能声称拥有 |
| --- | --- | --- |
| `UNUSED` | 按槽预映射的固定 kernel stack | pid、trapframe、用户页表、parent、fd/cwd 引用 |
| `USED` | 正在构造的槽，以及已经成功取得的局部资源 | scheduler 可见的完整进程 |
| `RUNNABLE/RUNNING/SLEEPING` | pid、kernel stack、trapframe、用户映像、fd/cwd、parent | 未经锁保护的共享状态修改权 |
| `ZOMBIE` | pid、`xstate`、槽、trapframe、页表、用户页、parent link | `ofile[]` 和 cwd 引用；它们已由 `kexit()` 关闭 |

`kernel/proc.c:proc_mapstacks()` 在启动时为全部 `NPROC` 槽分配 kernel stack；
所以一次 fork 的动态 free-page delta 不包含“新建 kernel stack”。进程自己也
不能释放当前正在使用的 kernel stack、trapframe 或页表；`kexit()` 只能留下
`ZOMBIE`，由另一个进程的 `kwait()` 调用 `freeproc()`。

### 从第一个进程到 fork 发布

当前分支没有从嵌入式 `initcode` 开始。`userinit()` 建立空地址空间和 cwd，
将首槽发布为 `RUNNABLE`；第一次 `forkret()` 在普通进程上下文中执行
`fsinit()`，再 `kexec("/init", ...)`。`/init` 打开 console、fork shell，
并持续 `wait()`，这也是它之后回收孤儿的用户态循环。

`allocproc()` 在 child `p->lock` 下依次取得槽/pid、trapframe 页、含特殊映射
的空页表和初始 kernel context。`kfork()` 再执行：

```text
uvmcopy(user pages)
copy trapframe, child a0 = 0
filedup(each ofile), idup(cwd), copy name
wait_lock: child.parent = current process
p->lock: child.state = RUNNABLE
```

父进程从 `kfork()` 得 child pid；复制的 trapframe 令 child 恢复时从同一次
`fork()` 得到 0。已有 `struct file` 不因 fork 复制成新 object；`filedup()`
增加共享 object 的 ref。任何页复制失败都必须 `freeproc()` 并保持 child 从未
发布。

### exec 是原进程内的两阶段替换

`kernel/sysfile.c:sys_exec()` 先把用户 argv 逐项复制到 kernel buffers，再调用
`kernel/exec.c:kexec()`；后者先独占一个临时页表，在其中装载 ELF、guard page、
用户栈和 argv。失败跳到 `bad`，释放临时映像并返回 `-1`；旧映像仍可继续
执行。所有构造都完成后才 commit：

```text
oldpagetable = p->pagetable
p->pagetable = new pagetable
p->sz = new size
p->trapframe->epc/sp = new entry/stack
proc_freepagetable(oldpagetable, oldsz)
```

成功替换用户页表、用户页、`sz`、用户 PC/stack 和调试 name；它保留 pid、
parent、kernel stack、trapframe 物理页、仍打开的 `ofile[]` 与 cwd。实验在
`exec` 前后让同一个 pid 阻塞，必须看到 pagetable/`sz` 改变而 trapframe、
cwd 和继承 fd 仍有效。PTE 怎样构造属于下一内存单元，不在这里重复。

### exit、wait、orphan 和 kill 各自只完成一段交接

`kexit(status)` 先逐个 `fileclose()`，再在文件系统事务中 `iput(cwd)`；随后
持有 `wait_lock` 执行 `reparent()`、唤醒 parent，最后持有自身 `p->lock` 写
`xstate`、发布 `ZOMBIE` 并进入 `sched()`。`freeproc()` 不负责关闭 fd/cwd；
它只适用于尚未取得这些引用的构造失败，或已经由 `kexit()` 清理外部引用的
zombie。

`kwait(status)` 按 `wait_lock -> child p->lock` 扫描。若向 parent 的
`status` 地址 `copyout()` 失败，它返回 `-1`，但必须保留该 zombie；parent
可用正确地址重试并取得同一 pid/status。成功后 `freeproc()` 才把槽、
trapframe 和用户映像归还。

`reparent()` 在 `wait_lock` 下把退出进程的所有 child 指向 `initproc`，不按
child 是 live、sleeping 还是 zombie 过滤；`/init` 的 wait 循环最终回收它们。
`kkill(pid)` 也不立即销毁目标：它只写 `killed=1`，并把 `SLEEPING` 目标改为
`RUNNABLE`。目标在可取消等待或返回用户态的安全点观察 flag，才执行
`kexit(-1)`，之后仍需 parent `wait()`。

当前分支还有两个不可推广的边界：`kkill(0)` 可能匹配 `pid==0` 的 `UNUSED`
槽并污染 `killed`，而 `allocproc()` 不主动清它；杀 init 也没有入口拒绝，
最终会触发 `panic("init exiting")`。本实验验证并记录前者的源码事实，但不会
执行会污染槽或 panic 的两个命令。

## 源码追踪计划

用稳定 `path:symbol` 分开状态、身份和资源边：

```sh
rg -n '^enum procstate|^struct proc \{' kernel/proc.h
rg -n '^proc_mapstacks\(|^procinit\(|^allocpid\(|^allocproc\(|^freeproc\(' kernel/proc.c
rg -n '^proc_pagetable\(|^proc_freepagetable\(|^userinit\(|^forkret\(' kernel/proc.c
rg -n '^kfork\(|^reparent\(|^kexit\(|^kwait\(|^kkill\(|^killed\(' kernel/proc.c
rg -n '^kexec\(|^loadseg\(|oldpagetable|^bad:' kernel/exec.c
rg -n '^sys_exec\(' kernel/sysfile.c
rg -n '^sys_fork\(|^sys_exit\(|^sys_wait\(|^sys_kill\(' kernel/sysproc.c
rg -n '^uvmcopy\(|^uvmfree\(' kernel/vm.c
rg -n '^filealloc\(|^filedup\(|^fileclose\(' kernel/file.c
rg -n '^idup\(|^iput\(' kernel/fs.c
rg -n '^main\(' user/init.c
rg -n '^runcmd\(|^main\(' user/sh.c
rg -n '^#define (NPROC|NOFILE|NFILE|MAXARG|USERSTACK)' kernel/param.h
rg -n '^#define PGSIZE' kernel/riscv.h
rg -n '^exitwait\(|^reparent\(|^reparent2\(|^killstatus\(|^forktest\(' user/usertests.c
```

把一次 shell child 的控制边和所有权边分别画出：

```text
control: sh fork -> child exec -> program exit -> parent wait
identity: one pid/parent/kernel stack/trapframe/cwd/ofile set survives exec
image: parent image --uvmcopy--> child old image --commit--> child new image
reclaim: child files/cwd --exit--> closed; slot/pages --wait--> free
orphan: middle child --reparent--> init child --init wait--> free
```

## 观察任务

先读取 manifest 的 pinned baseline 和当前教程提交，再运行静态门：

```sh
rg -n '"baseline_commit":' docs/xv6-tutorial/curriculum.json
git rev-parse HEAD
python3 docs/xv6-tutorial/resources/process-lifecycle/run-lab.py --static-only
```

静态门从 pinned baseline 导出临时源码，验证上述状态/回滚锚点、实验 patch
scope、snapshot 字段、syscall 生成链和 guest phase；随后构建临时 kernel、
`lifecycle`、`lifeexec` 与私有镜像，并逆向 patch/比较快照。

完整运行把唯一报告写到 `/tmp`：

```sh
python3 docs/xv6-tutorial/resources/process-lifecycle/run-lab.py \
  --report /tmp/process-lifecycle-report.md
```

runner 使用 `CPUS=1`。focused QEMU 先运行 `exitwait/reparent/killstatus` 控制，
要求零 `LIFE` marker；再按 gate 和快照状态运行 `lifecycle`，最后运行
`exectest/reparent2/forktest`。每个 blocked trigger 都以“快照已经显示
`SLEEPING`/`ZOMBIE`/init parent”为前置，`pause()` 和 timeout 只给调度机会并
限制等待，不是成功条件。随后 quick 和完整 `usertests` 在新实例中运行。

## 有界修改任务

资源 [`lifecycle.patch`](../resources/process-lifecycle/lifecycle.patch) 和
[`run-lab.py`](../resources/process-lifecycle/run-lab.py) 构成 tutorial-only
audit seam。patch 只在临时导出中添加编号 22 的只读 `lifesnapshot`、两个 guest
fixture，以及三类短锁计数：

- `wait_lock -> each p->lock`：状态、parent link 与指定 pid 的身份/资源字段；
- `kmem.lock`：freelist 中的 free physical pages；
- `ftable.lock`：active `struct file` object 与所有 ref 总数。

它先释放 `wait_lock/p->lock`，再分别取得 allocator/file 锁，不建立新的反向
锁序。这个 syscall 不会修改进程状态、页表、fd 或 parent；真正的变化仍只由
baseline 的 fork/exec/wait/kill/pipe 路径产生。

容量 fixture 只建一个 gate pipe，并持续 fork；每个 child 关闭 writer 后阻塞
在共享 read end。只有同时观察 `used == NPROC`、所有 child 已 `SLEEPING` 且
`free_pages > 0`，才能把 `fork() == -1` 归因于槽耗尽。两个新增 file objects
由所有 child 共享；每个 child 最终保留 `0/1/2 + gate read` 四个引用，所以
关系是 `refs = baseline + 2 + children * 4`，不是“每次 fork 新建四个文件”。

允许副作用仅为临时源码/build、私有 `fs.img` 和短寿命 QEMU/driver 进程。
所有运行后执行 `make clean`、逆向 patch、再清理并比较源码快照；原工作树状态
与共享 `fs.img` digest 必须不变。

## Oracle、证据、失败路径和局限

| 维度 | 触发器 | 必须观察到 | 资源结果与不能推出 |
| --- | --- | --- | --- |
| S | pinned baseline + applied patch 静态门 | 状态转换、publish/commit/rollback 顺序、wait 锁序、13-path patch scope 和生成链成立 | 只写临时导出；不证明动态时序 |
| F | gate 控制的 fork/exec/wait | 同 pid，name/页表/`sz` 改变，trapframe/cwd/fd 保留；status=37；wait 后 slots/pages/files/links 回 baseline | `filedup` 增 ref 而不增 object；不证明 PTE 权限 |
| B | 有效 `lifeexec` + 超出新用户栈容量的 argv、bad wait pointer、blocked kill、live orphan、NPROC capacity | 临时页表/程序段已构造后走 `bad`，old image/fd 继续；zombie 保留再重试；SLEEPING 后 kill 得 -1；parent=init 后消失；64 槽满且页未耗尽 | 每段清理回 baseline；不执行 kill(0)/kill(init) |

free-page 数是全局 allocator freelist，不是某种页面的精确 owner API；本实验用
阶段 delta、槽容量归因和最终恢复建立有界证据。固定 kernel stacks 在 baseline
快照前已经分配。snapshot instrumentation、console 输出和 CPUS=1 都改变观察
条件，因此不能推出多 hart lock order、调度公平、lost wakeup、TLB/PTE 安全或
实时延迟。

`C` 为 N/A：#10 才用确定性 interleaving 建立 happens-before；本单元刻意在
单 hart 上只观察已经发布的状态。`R` 为 N/A：没有 crash point、持久化写入或
恢复主张。timeout 在所有阶段都只是 watchdog。任一 pid/status/identity 关系
不符、`fork` 失败时未填满 NPROC、账本未恢复、回归失败或进程/临时树残留，
都使报告无效。

## 退出产物与后续单元

提交一份填好的进程生命周期与资源账本报告包，必须包含：pinned baseline 与
教程提交、状态机/ownership 表、F/B phase transcript、exec 身份字段、错误
wait 后 zombie、blocked kill 前置、orphan parent/reclaim、capacity 关系、
focused/related/full 回归、patch digest、临时资源和 cleanup，以及 `C/R` 局限。

本单元拥有的迁移题在[进程生命周期问题](../questions/process-lifecycle.md)。
随后可并行进入调度/同步单元与虚拟内存单元：前者展开 scheduler、锁、sleep/
wakeup 与 lost-wakeup evidence，后者展开地址翻译、lazy fault、NX 和完整
trampoline mapping；二者都不需要重新定义本单元的 pid/parent/zombie 所有权。
