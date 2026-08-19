# 全局不变量与资源边界

## 问题场景与本单元成果

一次 `pipe()` 失败可能同时碰到 per-process fd、global file object 和物理页；一次
filesystem write 又会跨 inode、buffer、log、VirtIO descriptor 与持久化边界。只在各章节里
分别记住“这里返回 `-1`”“那里会睡眠”不能回答失败后还剩下谁拥有资源，也不能指导容量修改。

本单元把前置单元已经验证的局部契约放进同一个资源账本。局部解释仍由[进程与内存](process-and-memory.md)、
[虚拟内存](virtual-memory.md)、[调度与同步](scheduling-and-synchronization.md)、
[通信与 I/O](communication-and-io.md)、[设备 I/O](device-io.md)、[文件系统](filesystem.md)、
[持久化](persistence.md)和 [恢复](recovery.md)分别拥有；本页唯一出口是“全局不变量与资源
边界报告包”：它必须列出 acquisition、transfer、rollback、release、exhaustion 和
post-failure state，并复用 `communication-and-io` 的 `FD_ROLLBACK` 有界实验，逐字段证明
`NOFILE` 耗尽时 fd 账本保持饱和、临时 file/pipe/page 全部回滚，关闭 duplicate 后整体账本
回到基线。报告只能陈述有界证据，不能把一次 QEMU 运行或一张静态表称为形式化证明。

## 前置单元与暂存黑盒

硬前置是[崩溃恢复与离线一致性](recovery.md)。进程/页、fd/file/pipe、inode/data、
buffer/log、device/interrupt 和 recovery 的局部算法分别由此前 verified 单元拥有；本单元只
拥有跨子系统的 invariant language 与 capacity consequence，不复制那些机制说明。

后续 evidence ticket 将建立 source-test traceability 与 fault/scheduling injection contract；预留的
综合单元再讨论 scalability 和三类 evidence project 的组合。尚未实现的 fault infrastructure、所有
可能调度交错、设备掉电持久性、scheduler fairness 和形式化 lock proof 是本单元明确记录的
evidence gap 与 non-goal，不是 manifest 中等待本单元解除的机制黑盒。

## 最小模型和关键不变量

### 一张账本、六个动作

对每个资源记录如下 tuple，而不是只写一个计数：

```text
(identity, capacity, owner, state, acquire, transfer,
 rollback, release, exhaustion, post_failure)
```

六个动作使用同一含义：

1. **acquire**：从 free/available 集合取得一个 identity 或 reservation；
2. **transfer**：释放责任转交给另一个 owner，原 owner 不再释放；
3. **rollback**：提交点前失败，撤销本次已取得但未发布的 owner；
4. **release**：最后 owner 归还 identity，或使其可复用；
5. **exhaustion**：没有可取得资源时，精确记录 return、sleep、kill、panic 或 partial result；
6. **post-failure**：失败后重新枚举存活 owner、允许副作用和仍待清理的 reservation。

只有固定且同质的池才可写 `free + owned + reserved + in_flight = capacity`。引用计数还要验证
identity 到 owner 的边；磁盘状态还要区分 cache、log data/header、home block 和 reboot 后的
owner，不能把它们压成一个整数。

### 跨子系统 ownership 表

| 资源 | identity / owner | acquire -> transfer -> release | exhaustion 与稳定边界 |
|---|---|---|---|
| process | `proc[NPROC]` slot；构造者、scheduler、current parent | `allocproc()` 得到 `USED`；`RUNNABLE` 发布；`ZOMBIE` 把回收权交当前 parent（必要时是 `initproc`）；`kwait()->freeproc()` | `kfork()` 返回 `-1`；未发布 slot 必须回滚，已发布 zombie 必须等可见 parent 回收 |
| page | physical page；freelist 或唯一 kernel/PTE/pipe/queue owner | `kalloc()`；完整初始化后发布；最终 `kfree()` | `kalloc()==0`，由 caller 决定 return/kill/panic；不能 double-free 或泄露旧 bytes |
| fd/file | `p->ofile[fd]` entry 与 `ftable.file[NFILE]` object | `fdalloc()` 安装 entry；`filedup()` 增 ref；最后 `fileclose()` 回收 object | `NOFILE/NFILE` 通常返回 `-1`；必须区分 entry、object 与 ref，保留已提交的用户可见副作用 |
| inode | `(dev,inum)` cache identity、`ref` 与 disk `nlink` | `iget()` 取得 ref；file/dir transfer；`iput()` 或 boot `ireclaim()` | `NINODE` 无 victim 会 panic；cache slot、disk inode 和 link count 不是同一容量 |
| buffer | `(dev,blockno)` identity、`refcnt`、sleeplock、log pin | `bget()/bread()`；`log_write()` pin；`brelse()/bunpin()` | `NBUF` 无 `refcnt==0` victim 会 panic；identity lock 与 data lock 必须分别成立 |
| log | aggregate `log.outstanding` admission、`lh.block[]` entry、pinned home buffer | `begin_op()` 在 `log.lock` 下按保守不等式增加 aggregate count；`log_write()`登记；`commit()` install/clear | capacity wait 使用 `&log`；错误预算可 panic；`outstanding==0` 才移交 commit owner；没有 per-operation reservation object |
| device | VirtIO descriptor chain、request buffer 与 completion status | requester 取三个 descriptor；device owns in-flight chain；IRQ 发布 completion；requester reclaim | `NUM=8` 只容两笔三-descriptor 请求，第三笔睡眠；IRQ 不归还 caller 的 buffer owner |
| interrupt | PLIC claim 返回的 source token；当前 hart handler | `plic_claim()`；handler ACK device；`plic_complete(irq)` | `irq==0` 表示无 claim；同一 token 只 complete 一次，IRQ context 不得进入可睡眠路径 |
| persistence | log block/header/home block bytes；normal commit 或 recovery | data -> nonzero header -> home -> zero header；reboot 后 `recover_from_log()` 接管 | logical completion 不等于 host power-loss durability；before/after、non-mixed 与二启幂等分别验收 |

### 容量与行为不是一个数字

| source anchor | 当前值 | 直接后果 | 联动审查 |
|---|---:|---|---|
| `kernel/param.h:NPROC` | 64 | process slot 满时 `kfork()` 可返回 `-1` | 每槽永久 kernel stack、scheduler/wakeup 扫描、最大 inherited refs |
| `kernel/param.h:NOFILE` | 16 | 当前进程 `fdalloc()` 返回 `-1` | `open/pipe/dup` rollback 与 `NFILE/NINODE` 的下一层压力 |
| `kernel/param.h:NFILE` | 100 | `filealloc()` 返回 0 | global ref peak、pipe 两端与 open file 的竞争 |
| `kernel/param.h:NINODE` | 50 | `iget()` 无空槽时 panic | 与 disk dinode 数、`nlink`、open refs 分开 |
| `kernel/param.h:NDEV` | 10 | 越界 major 或空 callback 使 open/I/O 返回 `-1` | `devsw[]` 静态槽与 PLIC source、VirtIO queue identity 分开 |
| `kernel/param.h:NBUF` | 30 | `bget()` 无 victim 时 panic | log pins、并发 readers、commit 临时 buffers 与 VirtIO backpressure |
| `MAXOPBLOCKS/LOGBLOCKS` | 10 / 30 | `begin_op()` wait；错误登记可 panic | `NBUF`、filesystem chunking、image log layout |
| `kernel/virtio.h:NUM` | 8 | 每请求三个 descriptor，第三笔 request wait | ring layout、in-flight buffer 与 completion/reclaim 顺序 |
| `kernel/fs.h:MAXFILE` | 268 blocks | `writei()` 越界失败或 short/partial result | direct/indirect mapping、log chunk 与 cleanup |

物理页没有独立的“空闲页常量”：可用集合由 `PHYSTOP`、kernel end、永久映射、`NPROC`
kernel stacks 和运行时 owner 共同决定。提高一个常量只能移动瓶颈；它不自动改变下一层容量或
失败语义。

### 锁、发布与失败的共同约束

- 共享 identity/count 必须在所属锁下改变；`fdalloc()` 的当前进程 `ofile[]` 和 VM/page-table
  构造路径依赖单线程或 private-owner 排他，而不是内部锁。对象只有完整初始化后才 transfer 给
  scheduler、hart、IRQ 或 device。
- `sleep(chan, lock)` 前必须有由同一 `lock` 保护的谓词和 producer；wakeup 不等于资源转移。
- IRQ handler 可 ACK、drain/process device input、publish completion 与 wake；但不得 sleep、取得
  sleeplock、调用 `begin_op()` 或发起同步 disk I/O。
- rollback 只撤销尚未越过提交点的 owner。directory entry、write prefix 或 nonzero log header
  一旦发布，失败后可能是允许副作用或 recovery obligation，而不是“全部没发生”。

## 源码追踪计划

先从容量定义进入分配与归还点，再沿一个失败调用图连接局部账本：

```sh
rg -n '^#define (NPROC|NOFILE|NFILE|NINODE|NDEV|MAXOPBLOCKS|LOGBLOCKS|NBUF)' kernel/param.h
rg -n '^#define PHYSTOP' kernel/memlayout.h
rg -n '^#define (MAXFILE|NDIRECT|NINDIRECT)' kernel/fs.h
rg -n '^#define NUM' kernel/virtio.h
rg -n '^proc_mapstacks\(|^allocproc\(|^freeproc\(|^kfork\(|^kexit\(|^kwait\(|^scheduler\(|^sleep\(|^wakeup\(' kernel/proc.c
rg -n '^kalloc\(|^kfree\(' kernel/kalloc.c
rg -n '^uvmalloc\(|^uvmcopy\(|^vmfault\(' kernel/vm.c
rg -n '^fdalloc\(|^sys_open\(|^sys_pipe\(' kernel/sysfile.c
rg -n '^filealloc\(|^filedup\(|^fileclose\(' kernel/file.c
rg -n '^pipealloc\(|^pipeclose\(' kernel/pipe.c
rg -n '^ialloc\(|^iget\(|^iput\(|^ireclaim\(' kernel/fs.c
rg -n '^bget\(|^brelse\(|^bpin\(|^bunpin\(' kernel/bio.c
rg -n '^begin_op\(|^end_op\(|^log_write\(|^write_head\(|^install_trans\(|^commit\(|^recover_from_log\(' kernel/log.c
rg -n '^alloc3_desc\(|^free_chain\(|^virtio_disk_rw\(|^virtio_disk_intr\(' kernel/virtio_disk.c
rg -n '^devintr\(' kernel/trap.c
rg -n '^plic_claim\(|^plic_complete\(' kernel/plic.c
rg -n '^acquire\(|^release\(' kernel/spinlock.c
```

对 `sys_pipe()` 先 breadth-first 画 `pipealloc -> filealloc x2 -> kalloc -> fdalloc x2 ->
copyout`，再分别进入每个 `bad`/failure branch。每条边标记当前 owner、锁、提交点和 cleanup；
不要先逐行抄函数。

## 观察任务

1. 从前置单元各取一份报告，选择 process、page、file、inode、buffer、log、device、interrupt、
   persistence 各一个 raw observable，填入[报告模板](../resources/global-invariants/report-template.md)。
2. 对 `sys_pipe()` 的每个失败位置写出“已取得 -> 是否已发布 -> 谁回收”。特别区分内核资源
   rollback、user `fdarray` 的 partial `copyout`，以及 `copyout()` 触发 `vmfault()` 后可能留下的
   process-owned lazy page。
3. 对容量表中的每一项写出 exact outcome；不得用“失败”同时代替 return、sleep、kill、panic
   和 partial result。
4. 运行静态门，确认复用 fixture 仍适用于 pinned baseline：

```sh
python3 docs/xv6-tutorial/resources/communication-and-io/run-lab.py --static-only
python3 docs/xv6-tutorial/tools/validate.py
python3 docs/xv6-tutorial/tools/generate_navigation.py --check
```

## 有界修改任务

本单元不新增 kernel seam。复用已验证的
[`communication.patch`](../resources/communication-and-io/communication.patch) 与
[`run-lab.py`](../resources/communication-and-io/run-lab.py)，因为它们已经在临时 pinned export
中提供只读 ledger，并包含 ticket 要求的 exact resource-limit oracle。运行：

```sh
python3 docs/xv6-tutorial/resources/communication-and-io/run-lab.py \
  --report /tmp/xv6-global-invariants.md
```

host 必须从源码确认 `NOFILE=16`，从 BASE 确认 inherited fd=3，再验证 13 次 `dup()` 填满
fd 3..15、随后 `pipe()` 精确返回 `-1`。该 trigger 让第一个 `fdalloc(rf)` 失败，所以没有新 fd
被安装；`pipealloc()` 临时取得的两个 file objects 与一个 pipe page 必须回滚。fixture 在 failure
前后由 fixture 内部 `same_ledger()` 断言饱和状态的 `fd/files/refs/pipes/procs/free`；这些
failure-point 字段不在公开 marker 中。关闭 13 个 duplicate
后，host 再逐字段确认 `IO AFTER phase=FD_ROLLBACK` 回到 BASE；第二次独立 `ioflow` 重复同一
关系。`fd0` 已安装而 `fd1` 失败的分支由源码 ownership 表覆盖，不属于这个动态 trigger。

runner 同时执行 fixture build、两次 `ioflow`、focused tests、CPUS=2 quick、CPUS=1 full、
patch reverse、`make clean`、共享 worktree/`fs.img` fingerprint 和 process-group cleanup。学习者
不修改复用 fixture；有界工作是用 raw marker 重建跨资源 owner 图，并按本单元 rubric 解释
为什么这个 oracle 比 `pipe==-1` 更强。

## Oracle、证据、失败路径和局限

| 维度 | 可接受证据 | 明确不能推出 |
|---|---|---|
| S | pinned symbols、capacity definitions、acquire/release/failure branches | 所有运行时路径都到达、形式化 invariant proof |
| F | 正常 pipe/process/I/O 与前置单元 raw reports | arbitrary workload 或 scheduler fairness |
| B | exact `NOFILE` exhaustion、`pipe=-1`、饱和 failure ledger equality、最终 BASE 与容量 outcome matrix | 已安装 fd0 的 rollback branch，或 `NFILE/NINODE/NBUF` 也被动态耗尽 |
| C | 复用实验先观察 named waiter，device 单元的 queue gate 与 lock ledger | 所有 interleaving、完整 lock graph 或 DMA ordering |
| R | recovery 单元的 four-point before/after、offline checker 与二启幂等 | 真实介质 FLUSH/FUA、任意 torn write 或宿主掉电语义 |

失败也必须是验收对象：marker 缺字段、常量漂移、13 与 `NOFILE-3` 不一致、任一 AFTER 不等于
BASE、child 未 wait、patch 不可逆、QEMU/临时目录残留、focused/quick/full 回归失败，都使报告
无效。panic 类容量实验只能在独立 QEMU 中执行；本单元不为了“覆盖表格”再次触发已有的
`NINODE/NBUF` panic。

## 退出产物与后续单元

按[报告模板](../resources/global-invariants/report-template.md)提交一份报告，并用
[rubric](../resources/global-invariants/rubric.md)复核。报告至少包含：

- 九类资源的 identity/owner/acquire/transfer/rollback/release/exhaustion/post-failure 表；
- 容量常量、直接行为、联动假设和 source anchor；
- 完整 `FD_ROLLBACK` raw marker、fixture failure ledger assertion、BASE/AFTER 逐字段复算、两次运行和回归 digest；
- fixture/runner/baseline/tutorial identity、临时 export 与 process cleanup；
- S/F/B/C/R 的已建立结论、未覆盖 gap 和禁止的 proof claim。

下一单元把这些 invariant 和 evidence gap 映射回 source/test，并约束 deterministic fault 与
scheduling injection；最终综合单元再讨论 capacity、lock、per-hart state、queue、cache 与
storage serialization 在 workload 下的 scalability。后续单元引用本页的统一账本，不复制局部
机制。
