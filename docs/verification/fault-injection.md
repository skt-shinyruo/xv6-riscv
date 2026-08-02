# 可重复故障注入与确定性交错

本文定义一套用于 xv6 的故障注入协议：在确定的第 N 次资源申请、确定的调度检查点或确定的日志写入边界触发失败，然后用返回值、进程状态、内核账本、磁盘镜像和重启结果共同判定。目标不是“多跑几次大概撞上”，而是让失败位置、前置状态、期望结果和清理动作都可复现。

本文必须先区分两类事实：

- **当前实现**：本仓库尚无通用 fault-control syscall、稳定 fault ID、调度 gate 或按提交阶段停机的钩子。现有 `test-xv6.py` 主要在 `logstress` 运行约两秒后向 QEMU 发送 `SIGKILL`；它可能命中任意阶段，日志测试最多重试五次。
- **建议测试设施**：下文以 `FI_*` 命名的接口、计数器、检查点、宿主 CLI 和测试程序都是应新增的测试专用设计，不是当前源码已经提供的 API。它们必须只在 `XV6_FAULT_INJECT` 构建中启用，普通内核不能暴露这些控制能力或承担额外时序扰动。

失败结果的统一词汇见[信任边界与失败模型](../architecture/trust-and-failure-model.md)，容量与当前耗尽行为见[资源失败矩阵](../reference/resource-failure-matrix.md)，日志提交协议见[一次文件系统事务](../flows/filesystem-transaction.md)。

## 1. 测试要证明什么

每个用例至少回答五个问题：

1. **命中了哪里**：稳定 fault ID、调用点标签、匹配范围和命中序号必须出现在记录中。
2. **失败如何暴露**：返回 `-1`、短计数、kill、panic、睡眠还是静默副作用，不能只检查“QEMU 还活着”。
3. **已取得资源归谁**：失败前取得的页、proc/file/fd/inode/buffer/log/descriptor 是否回滚，允许保留的副作用是什么。
4. **之后能否继续**：释放一个槽或撤销规则后，同类操作必须再次成功；预期 panic 的用例则必须在独立镜像和独立 QEMU 中运行。
5. **崩溃后是什么状态**：日志用例必须重启同一镜像，并把磁盘状态归类为事务前、事务后或明确的模型外损坏，不能只搜索 `recovering` 字样。

延时、随机 seed 和高压力可以补充覆盖，但不能代替确定性触发。若一个测试以“重复直到碰巧成功”为判定，它只能叫压力测试，不能成为某个 crash window 的证明。

## 2. 建议的测试控制面

### 2.1 固定大小、无分配的规则表

建议增加一个仅测试构建可见的固定数组，控制接口本身不得调用 `kalloc()`、打开文件或写磁盘：

```c
struct fi_rule {
  uint id;             // stable FI_* id
  uint action;         // FAIL, PARK, CRASH, HOLD, TRACE
  uint site;           // stable call-site tag, or FI_SITE_ANY
  int pid;             // 0 means any pid
  int hart;            // -1 means any hart
  uint64 nth;          // 1 means the next eligible hit
  uint64 eligible;     // scope-matched hits
  uint64 fired;
  uint one_shot;
  uint armed;
};
```

规则匹配顺序必须固定：先匹配 `id`，再匹配 `site/pid/hart`，最后递增 `eligible`。只有 `eligible == nth` 时触发；不匹配 scope 的调用不能消耗序号。默认规则触发一次后自动撤销。`nth==0`、重复 rule ID、未知 action 和溢出必须被控制接口拒绝。

规则和统计建议通过测试专用 `faultctl(op, arg)` syscall 管理，并提供用户程序 `fitest`。这不是安全边界：测试内核中的任意用户进程都可破坏系统，所以该 syscall 不得进入普通构建。最低接口包括：

```text
RESET                 清规则、gate、事件和 hold 状态；拒绝仍有 parked worker 时调用
ARM                   安装一条规则
DISARM                撤销一条规则
WAIT_EVENT            等待某事件计数达到目标值
RELEASE               释放指定 gate 或一个被 hold 的 completion
SNAPSHOT               复制固定大小统计，不进行动态分配
```

控制参数应优先放在寄存器可容纳的定长标量中；需要结构体时，用户程序必须在 arm 之前预先物化并校验该页。规则还应排除 controller pid。否则 `faultctl()` 自己的 `copyin/copyout` lazy fault 可能成为第 N 次 `kalloc`，使测试命中控制路径而不是目标路径。

`SNAPSHOT` 至少返回：每条规则的 eligible/fired、各 proc state 数量、已用 file/inode/buffer 槽数、`log.outstanding/committing/lh.n`、VirtIO free descriptor 数和 pending completion 数。统计读取必须按各子系统原锁顺序逐项采样，不能同时持有多把无既定顺序的锁；因此它是带时间戳的诊断快照，不应伪装成跨子系统原子快照。

### 2.2 稳定 ID，不用源码行号

故障计划不得使用 `__LINE__` 或 GDB 的临时地址作为长期 ID。建议显式标注：

```text
FI_KALLOC                 通用页面申请失败
FI_PROC_PUBLISH           proc 从 USED 发布到 RUNNABLE 前
FI_SLEEP_PUBLISHED        p->state/chan 已发布为 SLEEPING
FI_WAKE_MATCH             wakeup 已把目标改成 RUNNABLE
FI_LOG_ADMITTED           begin_op 已增加 outstanding
FI_LOG_DATA_DONE          一项或全部 log data bwrite 已返回
FI_LOG_HEADER_DONE        非零 header bwrite 已返回
FI_LOG_HOME_DONE          一项或全部 home bwrite 已返回
FI_LOG_CLEAR_DONE         n=0 header bwrite 已返回
FI_VIRTIO_COMPLETE        used entry 已观察但尚未唤醒请求者
```

一个 ID 可以有稳定的 `site` 子标签。页面分配至少应区分：

| `FI_KALLOC` site | 代表调用点 | 当前失败语义 |
|---|---|---|
| `KALLOC_PROC_TRAPFRAME` | `allocproc()` 的 trapframe | `fork()` 返回 `-1`；未发布 child 回滚 |
| `KALLOC_PGTABLE_ROOT` | `uvmcreate()` | fork/exec/lazy 的上层语义各自决定 |
| `KALLOC_PGTABLE_WALK` | `walk(..., alloc=1)` 中间层 | 上层失败；已建空中间层可保留到整个页表释放 |
| `KALLOC_UVM_LEAF` | eager 用户页/`uvmcopy()` | grow/fork/exec 返回失败并按各自范围回滚 |
| `KALLOC_LAZY_LEAF` | `vmfault()` 数据页 | 用户 fault 被 kill；copy helper 返回失败给调用者 |
| `KALLOC_PIPE_PAGE` | `pipealloc()` | 关闭已取得的两个 file，`pipe()` 返回 `-1` |
| `KALLOC_EXEC_ARG` | `sys_exec()` 临时参数页 | 释放此前参数页，旧映像保留，返回 `-1` |
| `KALLOC_KSTACK_BOOT` | `proc_mapstacks()` | 启动期 `panic("kalloc")` |
| `KALLOC_VIRTQ_BOOT` | VirtIO queue 三页 | 启动期 `panic("virtio disk kalloc")` |

调用点必须显式传标签，例如测试构建中的 `kalloc_site(KALLOC_PIPE_PAGE)`；用返回地址推断调用者在优化、内联或链接顺序变化后不稳定。运行期规则默认在 `init`/shell 已启动后才 arm，从而不会意外消耗在启动分配上；启动 OOM 必须使用独立的编译期 boot plan。

### 2.3 第 N 次页面失败的精确定义

`FI_KALLOC` 应在获取 `kmem.lock`、摘除 freelist 节点之前判断。触发失败时不改变 freelist，也不执行 allocator poison。这样“注入一次返回 0”与真实空链表的可观察结果一致，又不会把一页悄悄丢失。

推荐测试序列：

```text
RESET
记录 quiescent baseline
ARM(id=FI_KALLOC, site=S, pid=P, nth=N, one_shot=1)
执行唯一目标操作
SNAPSHOT：断言 eligible=N、fired=1
等待目标进程 exit/wait 或关闭全部临时资源
撤销规则并等待日志提交完成
再次执行同类操作，必须成功
比较 baseline 与 post-cleanup
```

不能在注入仍启用时用 `usertests` 的 `countfree()` 计数：它本身持续 `sbrk(PGSIZE)`，会消耗页面命中序号并故意把内存用尽。只有规则撤销、测试进程已回收、系统静止后，才可把 `countfree()` 作为丢页 oracle；更可靠的测试构建还应直接统计 freelist 长度，并同时记录页表中间页数量。

## 3. OOM 用例矩阵

对每个 site，至少扫描 `N=1..K`，直到覆盖一次成功路径所需的全部分配点，再额外运行“不触发”的 `N=K+1`。只测第 1 次失败无法发现中途构造的回滚错误。

| 场景 | 操作与触发 | 当前可接受结果 | 强 oracle |
|---|---|---|---|
| fork trapframe/root/walk/leaf | 小地址空间父进程执行 `fork()`；逐个 site 扫 N | 父进程得到 `-1`，无可 wait 的半成品 child | proc 槽回到 UNUSED；trapframe/页表/已复制叶页归还；父进程映像和 fd ref 不变；下一次 fork 成功 |
| eager `sbrk` | 用户 wrapper `sbrk(bytes)`（内部调用 `sys_sbrk(bytes,SBRK_EAGER)`）在叶页或 walk 失败 | 返回 `SBRK_ERROR`/`-1`，本轮扩展不发布 | `p->sz` 保持旧值；本轮叶页回滚；中间页最终随地址空间释放；下一次较小扩展成功 |
| lazy hardware fault | 先 lazy 增长，再在目标页执行 load/store | `vmfault()` 失败，目标进程被 kill 并以 `-1` 被 wait | 无目标叶映射；刚取得页未泄漏；内核和其他进程继续运行 |
| lazy `copyin/copyout` | syscall buffer 指向未物化 lazy 页 | helper 返回失败；上层可以是 `-1`、0 或部分长度 | 记录实际 caller 语义和前缀；失败页未映射；已物化的前置页属于允许副作用并在进程退出时回收 |
| exec 参数编组 | 在第 N 个 `KALLOC_EXEC_ARG` 失败 | `exec()` 返回 `-1`，旧映像继续执行 | 先前参数页全归还；旧 PC/SP/页表/fd/cwd 不变；随后合法 exec 成功 |
| exec 新映像 | ELF 段、stack、walk 的不同 N 失败 | `exec()` 返回 `-1`，尚未提交的新页表销毁 | 旧映像仍能校验其数据；新页表无可达页；参数临时页归还 |
| pipe | 在 pipe 页面失败，或分别耗尽第一个/第二个 file | `pipe()` 返回 `-1` | 两个用户 fd 均未发布；已取得 file ref 回到 0；无 pipe page；下一次 pipe 成功 |
| boot kstack/VirtIO | boot plan 在固定序号失败 | 精确 panic 字符串，系统不进入 shell | panic 前的 fault 记录通过同步 UART 输出；该用例独立 QEMU 运行，不做“恢复继续”断言 |

OOM 后“操作返回失败”不等于零副作用。例如 lazy copy 已物化的前置页保留；页表 walk 可留下空中间页到整个地址空间销毁；文件写入可在后续页 copyin 失败前已经分配块和修改前缀。测试必须按源码契约断言这些状态，而不是要求不存在的事务回滚。

## 4. 固定槽位的自然耗尽

优先通过持有真实对象把表填满；直接把 allocator 改成返回失败只能覆盖错误分支，不能证明容量边界、等待通道和归还时机。所有自然耗尽用例都应查询内部统计来区分“目标表已满”与“更早 OOM/其他表已满”。

### 4.1 `NPROC=64`

建议用一个极小父进程创建子进程；每个 child 先关闭继承的 write 端，再立即阻塞在共享 pipe 的 read 端；父进程只保留 write 端且暂不 wait。这样 child 保持 SLEEPING，而不是快速 exit 后被回收，也不会由 child 自己意外维持 `writeopen`。循环 fork 到第一次 `-1`：

1. 断言 `proc_used == NPROC`，且 fault 统计显示本轮没有 `FI_KALLOC` 失败；否则不能声称命中了 proc 槽上限。
2. 再 fork 一次仍返回 `-1`，shell/控制进程和已有 child 仍可运行。
3. 向 pipe 写一个 token，使一个 child exit，并 `wait()` 回收它。仅 exit 到 ZOMBIE 还不释放槽；必须 wait 后才可复用。
4. 新 fork 必须成功；新 pid 可以跳号，但所有活跃 pid 必须唯一。
5. 释放所有 child 并 wait，`proc_used` 回到用例前基线。

现有 `forktest` 和 `usertests forktest/forkfork/forkforkfork` 会验证 fork 能优雅失败，但不稳定地区分 NPROC 与物理内存耗尽，也不提供“释放一个槽后立即恢复”的精确 oracle。

### 4.2 `NOFILE=16`

在单一进程中反复 `open("README", O_RDONLY)`，记录返回的 fd，直到 `-1`。不要使用 `O_CREATE`，以免把目录副作用混入基础用例。

- `fd_used` 必须恰为 `NOFILE`；已有标准 fd 会占用若干槽，所以不要把成功 open 次数硬编码成 16。
- 全局 `file_used` 必须仍小于 `NFILE`，证明失败来自 per-process 表。
- `dup(0)` 和另一次 open 返回 `-1`，已有 fd 仍可读。
- 关闭任意一个已打开 fd 后，下一次 open 返回该最低空槽；全部关闭后 `file_used` 回到基线。

`sys_dup()` 在 `fdalloc()` 成功后才 `filedup()`，所以 NOFILE 失败不应增加 file ref。该瞬态顺序值得用 ref 统计单独断言。

### 4.3 `NFILE=100`

一个进程会先撞到 `NOFILE`，因此需要多个 holder。父进程依次创建 child；每个 child 在启动 gate 后独立打开已有文件到自身预定额度，再阻塞。fork 继承的 fd 只增加已有 file 的 ref，不会创建新 `struct file`，所以必须让 child 自己调用 open。

当 `file_used == NFILE` 时，启动一个仍有空 fd 的 contender：

1. `open("README", O_RDONLY)` 必须返回 `-1`，contender 的 `fd_used < NOFILE`。
2. 命令一个 holder 关闭一个独立 open，等待 `file_used == NFILE-1`。
3. contender 的下一次 open 必须成功，随后关闭并回到 `NFILE-1`。
4. 释放所有 holder，验证每个 file ref 恰好归零，表回到基线。

另设一个“允许部分副作用”用例：在 NFILE 已满时执行 `open("fi-created", O_CREATE|O_RDWR)`。当前 `sys_open()` 先完成 `create()`，再做 `filealloc()`；因此返回 `-1` 后名字可以已经存在并持久化。释放一个 file 槽后只读打开该名字，应看到空文件。此结果不是回滚 bug，而是当前失败模型的一部分。

### 4.4 `NINODE=50` 内存 inode cache

`NINODE` 不是磁盘 `mkfs` 的 `NINODES=200`。用例先创建足够多的不同 inode，再由多个 holder 分别打开不同路径并保持引用，直到内部统计显示所有 inode cache 槽都满足 `ref>0`。打开已缓存 inode仍可增加 ref；必须请求一个此前未缓存的 inum 才会进入无 victim 路径。

当前 `iget()` 的预期是 `panic("iget: no inodes")`，不是 `-1` 或等待。因此该用例必须：

- 使用独立的临时镜像副本和独立 QEMU；
- 在触发前记录 50 个槽对应的 `(dev,inum,ref)`，证明没有重复 inode 冒充满表；
- 对第 51 个不同 inode 请求，只接受精确 panic；
- 不把“QEMU 卡住”视为通过，必须匹配 panic 且宿主超时终止实例。

若将来把 `iget()` 改成可等待/可失败 API，测试期望必须随接口迁移，并增加释放一个 inode ref 后恢复的用例。在当前实现上，`usertests outofinodes` 主要耗尽磁盘 dinode，不能替代本测试。

### 4.5 `NBUF=30` buffer cache

普通用户接口不能稳定地把 30 个不同 buffer 都保持 `refcnt>0`。建议提供测试 fixture `FI_PIN_BLOCK(blockno)`：`bread()` 后执行 `bpin()`、`brelse()`，保留 identity 和 pin，但不长期持有 sleeplock；`FI_UNPIN_ALL` 逐项 `bunpin()`。fixture 只能选择已验证范围内、互不相同且不会被本测试修改的 block。

填满全部 NBUF 后对第 31 个 uncached block 执行 `bread()`。当前唯一可接受结果是 `panic("bget: no buffers")`。这是独立 panic 用例。另一个非 panic 用例只 pin `NBUF-1`，读一个新块、释放，再读另一块，验证 LRU victim 可复用且 `(dev,blockno)` identity 不重复。

还需一个联合压力用例：让日志 pin 若干 home buffers，同时 fixture 占用剩余 victim，停在 commit 需要 log data/header/home 临时 buffer 之前。它应在精确的 `bget()` 上 panic，从而验证 [resource-bounds](../kernel/resource-bounds.md) 中“NBUF==LOGBLOCKS 不是安全证明”的边界。测试记录必须区分普通 holder ref、日志 pin、I/O owner 和 sleeplock waiter。

### 4.6 日志容量与 admission

合法压力的期望是睡眠，不是 `-1`。在 `log.lh.n==0` 时，三个 worker 分别通过 `begin_op()` 并停在 `FI_LOG_ADMITTED`；此时 `outstanding==3`。第四个 worker调用 `begin_op()` 时：

```text
0 + (3 + 1) * MAXOPBLOCKS = 40 > LOGBLOCKS(30)
```

所以它必须 SLEEPING 在 `&log`，且尚未修改任何 buffer。释放一个已 admitted worker执行 `end_op()` 后，`outstanding` 变为 2，`end_op()` 会唤醒等待者；第四个 worker可以进入，使 outstanding 再到 3。最后释放全部 worker，只有 outstanding 降到 0 的那个 `end_op()` 触发 commit；结束后断言 `outstanding=0`、`committing=0`、`lh.n=0`、所有 log pin 解除。

第二类用例故意违反单 operation 不超过 `MAXOPBLOCKS` 的证明前提：测试 fixture 在一个 begin/end 区间登记互不相同的 home blocks。它可证明 `log_write()` 达到 `LOGBLOCKS` 后的当前行为是 `panic("too big a transaction")`。注意检查发生在 absorption 之前；`lh.n==LOGBLOCKS` 时即使下一次登记的是重复 block 也会 panic。该用例不是合法文件系统操作的期望路径，只用于防止把预算破坏静默化。

### 4.7 VirtIO `NUM=8` descriptor

每笔块请求固定使用三个 descriptor，所以最多两笔在途，剩余两个 descriptor 无法组成第三条链。QEMU 通常完成太快，单靠并发 read 不能确定命中等待。建议测试模式在 `virtio_disk_intr()` 观察 used entry 后，将 completion 放入固定 pending 数组并推进 `used_idx`，但暂不设置 `b->disk=0`、不 wake 请求者。控制进程调用 `RELEASE_COMPLETION` 时再执行正常完成动作；descriptor仍由醒来的请求线程在 `free_chain()` 中归还。

确定性步骤：

1. 两个 worker 对不同 uncached block 发起请求，等待 `desc_free==2` 且 `pending_completion==2`。
2. 第三个 worker 发起请求；`alloc3_desc()` 必须失败并睡在 `&disk.free[0]`。断言它不是卡在 `vdisk_lock` 或 buffer sleeplock。
3. 释放一个 completion；对应请求者醒来并释放三个 descriptor，`free_desc()` 唤醒第三个 worker。
4. 第三个请求成功入队；随后释放全部 completion。
5. 断言每个请求数据正确、`desc_free==NUM`、`pending_completion==0`、所有 `disk.info[].b==0`，没有重复 free 或丢 wakeup。

不能简单在 interrupt handler 持有 `vdisk_lock` 时 park：那只会让第三个线程等锁，并未验证 descriptor wait channel。测试 hold 必须允许 handler释放锁且允许第三个请求真正进入 `alloc3_desc()`。

## 5. 确定性调度检查点

### 5.1 gate 语义

普通 gate 使用固定槽和 condition lock：worker 到达安全检查点后递增 event counter、唤醒 controller，并在 gate channel 睡眠；controller等待精确计数后释放指定数量。只有在调用点允许睡眠、释放调用点现有锁不会改变目标协议，并且未持有不允许跨 sleep 的锁时才能使用这种 gate。特别是“已持条件锁检查 predicate、尚未调用目标 `sleep(chan,lk)`”的窗口不能用普通 park gate，因为 gate 自己会释放该锁，让 producer 抢先改变条件并 wake。

内核锁内的关键事件采用**只记录、不 park**的 tracepoint，或采用测试专用的无锁原子自旋 gate，并限制 `CPUS>=2`。自旋 gate 必须列出当前持有哪些锁、由哪个 hart 解除、超时后如何报告；不得在单 hart 上启用。绝不能为了测试在持 spinlock 时调用普通 `sleep()`。

每条 trace 记录固定序号，而不是墙钟时间：

```text
seq, event_id, pid, hart, object_key, old_state, new_state
```

环形 trace 满时必须标记 overflow 并让测试失败，不能覆盖早期证据后继续判定。

### 5.2 sleep/wakeup 交错

验证不丢 wakeup 的最小剧本：

1. consumer 在持有条件锁、检查 predicate 为 false 后，到达测试专用的原子自旋 call-site gate；它在 gate 期间保持该条件锁，且测试要求 `CPUS>=2`，由另一 hart 的 controller 释放。另一种实现必须同时 gate producer，直到观察到 `FI_SLEEP_PUBLISHED`，不能只 park consumer。
2. 释放 consumer；它立即进入 `sleep(chan, lk)`，在获得 `p->lock`、释放条件锁并发布 `chan/SLEEPING` 时记录 `FI_SLEEP_PUBLISHED`。producer 在此之前无法取得条件锁。
3. producer 获得同一条件锁，改变 predicate，然后调用 `wakeup(chan)`；匹配目标时记录 `FI_WAKE_MATCH`。
4. consumer恢复并重新取得条件锁，再次观察 predicate 为 true。

oracle 不是“两个进程最后都退出”而是事件偏序：

```text
predicate=false
  < FI_SLEEP_PUBLISHED
  < predicate=true under same condition lock
  < FI_WAKE_MATCH
  < consumer observes predicate=true
```

再运行另一顺序：producer先把 predicate 置真，consumer随后取得条件锁并跳过 sleep；此时不应出现 `FI_SLEEP_PUBLISHED`。两者共同覆盖“睡前 wakeup”和“睡后 wakeup”，而不是靠 `pause()` 猜窗口。

### 5.3 kill 阻塞进程

让 worker 阻塞在空 pipe read 或满 pipe write，等待 trace 确认 `state==SLEEPING` 且 `chan` 是预期地址，再由 controller 调用 `kill(pid)`：

- `kkill()` 必须在持 `p->lock` 时设置 `killed=1` 并把 SLEEPING 改为 RUNNABLE；
- scheduler 运行它后，pipe 循环检查 killed 并返回 `-1`；
- parent `wait()` 得到退出状态 `-1`；
- pipe/file/page 引用最终回到基线。

这比“发 kill 后等一会”更强，因为它证明 kill确实命中了阻塞状态和对应 wakeup 路径。

### 5.4 scheduler 选择与跨 hart

多 hart 用例建议增加测试专用 allow matrix：`scheduler()` 看到 RUNNABLE 进程后，只有 `(pid,hart,epoch)` 被 controller允许时才能选择。它不改变普通构建。用它可固定：worker 先在 hart 0 运行并 yield/sleep，随后只允许 hart 1 选择同一 pid。

断言事件序列包含：

```text
RUNNING(pid,hart0) -> RUNNABLE/SLEEPING -> RUNNING(pid,hart1)
```

并检查 `c->proc`、`p->state` 和 `p->lock` 的 handoff。不要要求用户寄存器或内核 context 依赖 hart 固定；迁移后的进程必须从保存的 context/trapframe继续，而 `tp`/`mycpu()` 应反映 hart 1。调度 interleaving 测试固定 `CPUS=2`；资源计数和单线程回滚测试默认 `CPUS=1`，避免无关调用消耗第 N 次规则。

## 6. 日志的精确 crash point

### 6.1 停机握手

精确日志窗口默认使用 `CPUS=1`。建议 `FI_ACTION_CRASH` 不直接调用 panic，也不执行优雅 shutdown。它应：

1. 用同步 `uartputc_sync()` 输出唯一短标记和 fault sequence；
2. 关闭当前 hart 中断，设置 crash-reached 标志并停在不做磁盘 I/O 的 loop；
3. 不再获取文件系统锁、不再发 VirtIO 请求；
4. 宿主看到完整标记后立即向**确切 QEMU pid**发送 `SIGKILL`。

若专门测试多 hart group，controller 必须在启动目标事务前把所有非 owner hart 停在显式 quiescent gate，并断言它们不持锁、没有 outstanding block request；不能指望 crash-reached 标志抢占一个已在内核或设备路径中的 hart。只有 owner hart 进入目标事务并触发 crash point，宿主才可把该镜像归因于记录的写入边界。

宿主不得用固定延时，也不应取 `make` 的“第一个子进程”猜测 QEMU。建议直接启动构建后解析出的 QEMU argv，使用独立 process group，并保存 pid/start-time/cwd；kill 前核对 pid 仍属于本工作目录。标记后若超时未 kill，本轮失败，不能继续当成有效 crash image。

这里的“DONE”表示相应 `bwrite()` 已收到 VirtIO completion 并返回，只在当前测试模型中当作写完成。它不自动等价于真实介质 durable；第 7 节单独限定这一点。

### 6.2 稳定 crash ID

对一个 `lh.n=m` 的 group，至少提供：

| crash ID | 精确位置 |
|---|---|
| `LOG_BEFORE_DATA` | `commit()` 已确认 `lh.n>0`，尚未写第一个 log data |
| `LOG_AFTER_DATA(i)` | 第 i 个 `bwrite(log.start+1+i)` 返回；`0 <= i < m` |
| `LOG_AFTER_ALL_DATA` | `write_log()` 完成，尚未写非零 header |
| `LOG_AFTER_HEADER` | 非零 header 的 `bwrite(log.start)` 返回，即逻辑 commit point |
| `LOG_AFTER_HOME(i)` | 第 i 个 home `bwrite(lh.block[i])` 返回 |
| `LOG_AFTER_ALL_HOME` | `install_trans(0)` 完成，尚未把内存 `lh.n` 置 0 |
| `LOG_BEFORE_CLEAR` | 内存 `lh.n=0`，尚未写清零 header |
| `LOG_AFTER_CLEAR` | 清零 header 的 `bwrite()` 返回 |

`LOG_AFTER_DATA(i)` 和 `LOG_AFTER_HOME(i)` 的 `i` 必须进入记录；若要覆盖顺序，应对 `i=0..m-1` 每个位置单独运行。crash hook 放在循环外只测“全部完成”会漏掉部分 log/home 写窗口。

### 6.3 每个窗口的预期磁盘状态

测试事务应修改一组可离线识别的 home blocks：数据内容包含 case ID/epoch/checksum，目录/inode/bitmap变化也保存事务前和事务后镜像。重启前先离线读取 on-disk header，再启动恢复；不能只用 `ls` 推断所有 home block一致。

| crash point | kill 时 header | 重启动作 | 最终唯一允许状态 |
|---|---|---|---|
| `LOG_BEFORE_DATA` | 旧的 `n=0` | 不 redo | 事务前 |
| 任意 `LOG_AFTER_DATA(i)` | `n=0` | 不 redo，即使 log data 是前缀 | 事务前 |
| `LOG_AFTER_ALL_DATA` | `n=0` | 不 redo | 事务前 |
| `LOG_AFTER_HEADER` | `n=m` | redo 全部 m 项，再 clear | 事务后 |
| 任意 `LOG_AFTER_HOME(i)` | `n=m` | 对所有项 redo；已安装前缀被幂等覆盖 | 事务后 |
| `LOG_AFTER_ALL_HOME` | `n=m` | 再 redo 全部 | 事务后 |
| `LOG_BEFORE_CLEAR` | `n=m` | 再 redo 全部 | 事务后 |
| `LOG_AFTER_CLEAR` | `n=0` | 不 redo | 事务后 |

每次恢复后必须断言：header 为 0；所有目标 home blocks恰好组成事务前或事务后集合，不允许混合；bitmap、dinode、目录和数据通过离线一致性检查；第二次重启不再打印 recovery 且镜像语义不变。检查规则见[文件系统一致性](../filesystem/filesystem-consistency.md)。

`recovering` 输出只证明 `install_trans(1)` 进入过循环。它不能证明 header 范围合法、所有目标正确、home 内容一致或 clear 已持久化。

### 6.4 group 与系统调用返回

精确 crash 测试必须记录 group 中有哪些 operation。一个 operation 的 `end_op()` 在其他 outstanding operation 尚未结束时可以先返回，此时 group 尚未 commit。大 file write 又可能拆成多个 begin/end 区间。oracle 应围绕该次 header 中的 unique home block 集合，而不是笼统要求“整个 write 系统调用全有或全无”。

最小基准使用一个 worker、一个受控日志区间和 `CPUS=1`。并发扩展再显式组成两个 operation 的 group，并验证两者共同出现在同一个 header；不要让 shell、orphan reclaim 或后台测试意外加入该 group。

## 7. sector tearing 与持久化边界

### 7.1 两种不同的 tear 模型

`BSIZE=1024`，VirtIO sector 是 512 字节。确定性 tear 不应依赖宿主恰好在一次 write 中间杀进程；可在保存事务前/后 block 副本后，对 crash image 定点合成：

```text
T0: block[0:512]=old, block[512:1024]=new
T1: block[0:512]=new, block[512:1024]=old
TB: 在一个 512-byte sector 内按指定 byte offset 拼接或翻转
```

T0/T1 假定单 sector 原子、只允许 1024-byte block 的两 sector 不同时完成；TB 则模拟 sector 内部也会 torn/corrupt。每个合成镜像应记录目标 block number、旧/新 SHA-256、tear mode 和拼接 offset。

日志 header 的有效内容是 `n` 加最多 30 个 `int`，共 124 字节，位于第一个 512-byte sector。因此在“512-byte sector 原子”的 T0/T1 模型下，header语义通常是完整旧值或完整新值；第二个 sector 新旧混合本身不会拆开有效字段。只有 TB、bit corruption 或未来更大的 header 才会制造字段级破裂。数据、inode、目录和 bitmap block 则可能在两个 sector都有有效内容。

### 7.2 预期结果与模型缺口

| tear 位置 | header 状态 | 当前预期 |
|---|---|---|
| 未提交 log data | 0 | recovery忽略它；home 保持事务前 |
| 已提交 header 对应的 log data | 非零有效 | 当前没有 checksum；recovery会把新旧混合 log block静默装到 home，可能一致性失败或数据静默损坏 |
| home block，header仍非零 | 非零有效且 log data完整 | recovery应以完整 log copy 重写 home，最终事务后 |
| home block，header已清零 | 0 | 无 recovery；混合 home永久可见 |
| header 第一 sector 内部 | 可能任意 `n/list` | `read_head()`不校验范围/目标；可 panic、越界复制、错误覆盖或静默损坏，均属于当前信任模型外 |
| clear header | 旧非零或新零 | 若 sector原子，分别是重复 redo或无需 redo；二者都应收敛到事务后 |

这些用例不是要求当前 xv6 在所有 tear 下通过。它们应把已知保护范围编码成测试：受日志协议保护的完整 block write窗口必须通过；committed log data/header 自身 torn 的用例应标为“预期暴露缺口”。刻意破坏 inode/bitmap/目录关系的 metadata fixture 应由离线 checker 检出；普通文件数据 tear 可能保持结构 clean，只能由测试预置的内容 checksum/golden data发现，fsck 必须允许报告“内容完整性不可证明”，而不能把启动成功或 fsck clean 当成安全。

### 7.3 completion 不等于掉电持久化

当前驱动没有 FLUSH/FUA，也不协商或控制宿主 write cache。CPU fence只约束 CPU/编译器可见顺序，不能把设备缓存写入稳定介质。QEMU 对 raw `fs.img` 的 completion、宿主 page cache 和真实存储控制器断电是三个不同模型。

因此报告必须写明测试 profile：

- **QEMU logical-order profile**：把 VirtIO completion 当作本次模拟器实验的完成点，只证明 driver/log 调用顺序。
- **host persistence profile**：记录完整 QEMU `-drive` cache/aio 参数、QEMU 版本、宿主文件系统和存储类型；即使采用更强同步选项，也只声称该配置的经验结果。
- **synthetic tear profile**：离线合成指定 sector/block 混合，证明恢复算法在明示故障模型下的结果。

不得把 `SIGKILL` QEMU 的通过结果写成真实掉电保证。

## 8. oracle 与失败判定

### 8.1 返回和进程级 oracle

每个用户测试输出一条机器可解析记录：

```text
FI_RESULT case=<id> step=<n> ret=<value> status=<value> errno=none
```

xv6 没有 errno，不能根据同一个 `-1` 区分 NPROC、OOM、NOFILE 或 NFILE；必须结合 fault hit 和内部资源统计。对于 killed 场景，parent必须 `wait(&status)` 并断言 `status==-1`。对于短 I/O，记录完成字节数和内容前缀。

### 8.2 资源账本 oracle

在 quiescent baseline 与 cleanup 后比较：

| 资源 | 必查字段 |
|---|---|
| pages | freelist 数、各活跃页表页/叶页、pipe pages；`countfree()` 只作外部交叉检查 |
| proc | 各 state 数、pid、parent、trapframe/pagetable 是否随 UNUSED 清零 |
| fd/file | 每进程非空 fd 数、全局 `ref>0` 数、引用总和与 fd/pipe/inode owners 对应 |
| inode | `(dev,inum)` identity唯一、ref/valid/nlink、无意外 orphan |
| buffer | identity唯一、refcnt/pin/disk、所有测试 pin 已解除 |
| log | outstanding=0、committing=0、lh.n=0、on-disk header n=0 |
| VirtIO | 8 个 descriptor均 free、info.b 全 0、pending 0、used index追平 |

允许的差异要逐项白名单，例如 pid 跳号、`O_CREATE` 后 file 表失败留下的空文件、lazy copy 已物化页、完整 write chunk 已提交前缀。任何未列出的资源差异都算失败。

### 8.3 panic、hang 与等待

- 预期 panic：必须匹配精确 panic 文本、fault fired=1 和触发前状态；其他 panic 不通过。
- 预期等待：先证明 waiter 的 state/channel 和资源条件，再解除条件并要求在有界事件数内完成。墙钟 timeout只是防卡死，不是等待已发生的证明。
- 非预期 hang：保存 proc dump、fault trace、log/VirtIO统计和宿主 QEMU pid；不能仅重跑到通过。

## 9. 可重复运行协议

每个正式记录至少包含：

```text
git commit + dirty diff hash
kernel config and FI plan hash
compiler/binutils/QEMU versions
CPUS, QEMU RAM, full QEMU argv
base fs.img SHA-256 and per-case working-copy SHA-256
test case, rule ids/sites/nth/pid/hart
scheduler gate script and event trace
expected result class and observed oracle
post-run image SHA-256 and offline checker report
```

推荐顺序：

1. 构建普通内核并运行定向 `usertests`，建立无注入基线。
2. 构建 `XV6_FAULT_INJECT=1` 内核；确认未 arm 规则时同一基线仍通过。
3. 停止所有使用镜像的 QEMU，为每个破坏性用例从只读基准复制独立 working image；禁止并行共享可写 `fs.img`。
4. 单资源/回滚用例使用 `CPUS=1`；明确测试多 hart 的用例使用固定 `CPUS=2` 和 gate script。
5. `RESET` 后确认规则表为空、统计在基线；arm 后才启动目标 worker。
6. 只接受一次精确 fired；0 次表示未覆盖，2 次以上表示 scope 或 one-shot 有错。
7. 收集 cleanup 后账本；crash 用例则先离线读取镜像，再重启同一 working image两次。
8. 关闭测试实例，保留失败 case 的镜像、trace 和控制台；通过 case 可按保留策略删除 working copy。

建议实现后的宿主 CLI 采用显式参数，例如：

```sh
./test-xv6.py fi --case kalloc-fork --site KALLOC_UVM_LEAF --nth 3 --cpus 1
./test-xv6.py fi --case log-crash --point LOG_AFTER_HOME --index 1 --cpus 1
./test-xv6.py fi --case virtio-descriptors --cpus 2
```

这些命令是建议接口；当前 `test-xv6.py` 尚不接受 `fi` 参数。实现时脚本必须在未知 case/site/point 时失败，不能回退到普通 usertests 后给出假阳性。

## 10. 清理与隔离

正常返回用例的 cleanup 顺序：

```text
停止创建新 worker
释放所有 scheduler gates / pending completions
等待并 wait 全部 child
关闭 fd、解除 inode/buffer pin
等待 outstanding==0 且 commit完成
DISARM all, RESET trace
比较 post-cleanup 账本
运行一次同类成功操作
```

若 cleanup 自身需要被注入的资源，必须先 disarm，再清理；否则会把“故障路径泄漏”和“清理也被故障拦截”混在一起。`RESET` 不得强制把 still-owned 引用计数改零，那会掩盖泄漏；它只能撤销控制状态，资源仍须走正常 release path。

panic/crash/tear 用例不能在同一 kernel 实例中清理。它们使用独立 working image，宿主终止确切 QEMU pid，确认没有进程仍打开镜像后再归档或丢弃副本。不要在失败时直接覆盖基准镜像；保留触发前/触发后副本和 SHA-256 才能复核。

最后必须回到普通构建，确保测试专用 syscall、fault table、scheduler filter 和 completion hold 均未链接进生产 kernel，并运行完整回归。

## 11. 与现有测试的映射

| 风险 | 当前测试/入口 | 当前能证明 | 确定性补充用例 |
|---|---|---|---|
| 页面 OOM | `usertests mem/sbrkfail/execout`，完整 usertests 的 `countfree()` | 压力下常见回滚、最终丢页检查 | 每个 `FI_KALLOC` site 的 N 扫描、scope/fired 证明、直接页账本 |
| proc 槽 | `forktest`、`usertests forktest/forkfork/forkforkfork` | fork 最终失败且系统继续 | 持满 NPROC、证明非 OOM、wait 后单槽复用 |
| NOFILE | fd/pipe/open 常规测试 | 局部错误路径 | 单进程精确填满、dup ref 不增长、最低 fd 复用 |
| NFILE | 无独立精确测试 | 并发测试间接使用 file table | 多进程 holder、fresh contender、释放一槽恢复、O_CREATE 副作用 |
| NINODE cache | 无；`outofinodes` 是磁盘 dinode | 磁盘 inode耗尽可清理 | 50 个不同活跃 identity 后第 51 个精确 panic |
| NBUF | `manywrites`/`bigwrite` 间接压力 | 正常 workload 未明显死锁 | fixture pin、精确第 31 个 miss panic、日志 pin 联合压力 |
| log admission | `logstress`、`manywrites` | 并发下反复提交 | 3 个 admitted + 第 4 个 waiter、释放后有界唤醒 |
| log crash | `./test-xv6.py log`/`crash` | 延时 SIGKILL 偶尔命中非零 header并恢复 | 每个 data/header/home/clear 精确点，事务前后块级 oracle |
| orphan recovery | `forphan`、`dorphan`、`./test-xv6.py crash` | unlink-open/cwd 后强杀可由 ireclaim处理 | 在 unlink commit 后、close/iput 前精确 crash；验证块与 dinode全部归还 |
| VirtIO descriptor | `manywrites` | 并发 I/O 压力 | hold 两个 completion、第三个明确睡 descriptor channel、全部归还 |
| sleep/wakeup/kill | `pipe1`、`preempt`、`killstatus` | 宏观活性和 kill 结果 | event/gate 固定 predicate、SLEEPING、RUNNABLE 偏序 |
| filesystem capacity | `diskfull`、`outofinodes`、`badwrite` | 耗尽后清理的若干路径 | 同故障点重复、精确归还点、离线一致性和下一次成功 |

最小回归集合：

```sh
./test-xv6.py -q usertests
./test-xv6.py usertests
./test-xv6.py crash
```

`./test-xv6.py crash` 会重建并修改 `fs.img`，而且仍是非确定性 crash 基线；运行前必须确认当前镜像可丢弃且没有另一个 QEMU 使用它。确定性 suite 实现后应保留这个旧入口作为压力补充，但精确 crash-point 用例才承担提交窗口证明。

## 12. CI 分层与验收标准

建议按成本分层：

- 每次提交：普通 quick usertests；无规则的 fault build；N=1 的 fork/exec/pipe 回滚；NOFILE；sleep/wakeup 两种顺序。
- 每日：所有 kalloc site 的 N 扫描；NPROC/NFILE；log admission；VirtIO descriptor；完整 usertests。
- 破坏性/隔离 runner：NINODE/NBUF/oversized-log 的预期 panic；所有精确 crash point；orphan；synthetic tear；`grind`。

一个用例只有同时满足以下条件才通过：

1. 计划中的 fault 恰好 fired 一次，命中正确 site/pid/hart/N；
2. 返回、kill、panic或等待类别与当前源码契约一致；
3. 允许副作用全部在白名单内，资源账本无额外差异；
4. 撤销故障后同类操作恢复，或预期 panic/crash 的独立实例按计划终止；
5. crash 用例的重启状态满足块级前/后 oracle，header 清零且第二次启动幂等；
6. trace未 overflow，宿主没有超时、猜 pid、复用脏镜像或重试到碰巧通过；
7. 普通非注入内核的定向测试和完整回归通过。

这套协议把“容量耗尽”“故障返回”“等待背压”“内核不变量 panic”和“存储崩溃”分成不同实验。只有明确 fault point、事件偏序、资源归还和持久化模型，测试结果才能反向支撑全局不变量与源码到测试追踪矩阵，而不只是提供一次看似正常的运行日志。
