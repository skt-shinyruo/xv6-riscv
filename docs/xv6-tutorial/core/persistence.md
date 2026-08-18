# Buffer cache、日志与事务

## 问题场景与本单元成果

一个 process 对已存在文件的同一 home block 连续写两次，再从 `write()` 返回。此时至少有五种
容易被混写成“已经落盘”的状态：新 bytes 只在 cache；block number 已加入 in-memory log；redo
payload 已写入 on-disk log data block；非零 log header 已完成；home block 已安装且 header 已
清零。若另一个 hart 同时请求相同 block，系统还必须先保证两条路径共享同一个 buffer identity，
否则 transaction pin 的可能不是其他 caller 看到的那份数据。

本单元的主要成果是把 buffer ownership、transaction admission、log absorption、commit、home
installation 和 clear 串成一条可复核状态机，并以确定性 parallel-cache 项目评价 identity、
collision 与并行，而不是以随机 stress 推断正确性。唯一出口是一个“buffer cache、日志与事务
证据报告包”；源码/runtime worksheet、candidate digest、raw events、resource ledger、回归和局限
都嵌在这个报告中，不另交第二份产物。

## 前置单元与暂存黑盒

硬前置是[文件系统命名、inode 与数据路径](filesystem.md)：那里已经把 syscall、file、inode、
logical/physical block 追到 `bread()/log_write()/brelse()` 边界。本单元接手
`buffer-cache-identity-and-eviction`、`log-admission-commit-and-clear` 与
`persistence-commit-boundary`。相关背景包括
[调度、同步与等待](scheduling-and-synchronization.md)的 lock/sleep contract，以及
[设备中断与 VirtIO 队列](device-io.md)的 request、completion 与 descriptor cleanup。

以下机制仍是显式黑盒 `crash-recovery-and-offline-consistency`：

- 不在 log data、commit header、home installation 或 clear 之间强制 crash；
- 不评价 `read_head()/recover_from_log()` 的 replay、第二次启动幂等或 mixed home blocks；
- 不执行 synthetic tear、orphan consistency 或 offline fsck；
- 不把 QEMU request completion 或 host 可重读的 image 写成 physical power-loss durability。

这些责任由 #17 接手。本单元的 `R` 只评价一个没有 crash 的 transaction 正常完成后，private
image 中 home bytes 可见且 on-disk header `n==0`。

## 最小模型和关键不变量

### Buffer identity、reference 与 eviction

`kernel/buf.h:struct buf` 同时承载 cache identity、data、同步和设备 ownership：

| 字段/状态 | owner 与保护 | 含义 |
|---|---|---|
| `dev/blockno` | `bcache.lock` | cache key；同一 key 最多一个 live buffer |
| `refcnt` | `bcache.lock` | ordinary holder/waiter 与 log pin 的合计，不是 lock owner 数 |
| `lock` | sleeplock | 当前允许读写 `data` 的唯一 caller |
| `valid` | 持有 sleeplock时使用 | data 是否已由 device fill；release 后仍可留在 cache |
| `disk` | `vdisk_lock`/request path | VirtIO 是否短暂拥有这个 buffer 的 request |
| `prev/next` | `bcache.lock` | baseline 的全局 recency list；ref 归零时移至 MRU head |

`bget()` 在同一个 `bcache.lock` 临界区完成 lookup 和 miss victim retag。hit 在释放 identity lock 前
`refcnt++`；miss 只选择 `refcnt==0` 的 LRU victim，设置新 key、`valid=0/refcnt=1` 后才发布。随后
才 acquire sleeplock。因此 one-buffer-per-block 依赖 lookup/publish 的共同 lock domain，不是依赖
sleeplock。若找不到 ref-zero victim，当前实现精确 `panic("bget: no buffers")`。

`brelse()` 先释放 sleeplock，caller 从此不能再使用 pointer/data；然后在 cache lock 下 ref--，
归零才更新 recency。`bpin()/bunpin()` 只改变 ref，并不持有 sleeplock。一个 holder、一个尚在等
sleeplock 的 caller 和一个 log pin 可以让 raw `refcnt==3`；报告必须用事件分别重算 ordinary ref
与 pin，而不是从终态整数猜来源。最终 refs/pins/owners/waiters/disk-owned 都为 0，但 valid cache
identity 可以保留。

parallel-cache candidate 可以把 global list/lock 改成 partitioned implementation，也可以采用
partition-local eviction；它不能改变以下不变量：

1. 任一时刻每个 `(dev,blockno)` 只有一个 live `slot/generation`；
2. referenced、waiting 或 pinned buffer 不可成为 victim；
3. retag 原子发布并进入新 generation，旧 generation 不再可用；
4. collision 不能混淆不同 block 的 bytes；
5. 所有成功 acquire 恰好一个 release，首次 log append 恰好一个 pin/install-unpin。

### Transaction admission 与 group boundary

`struct log` 的 `lh.n` 是当前 group 已占用的 distinct home blocks；`outstanding` 是已经 admitted、
尚未执行 `end_op()` 的 filesystem operations；`committing` 阻止新 group 混入 commit。当前常量是：

```text
MAXOPBLOCKS = 10
LOGBLOCKS   = MAXOPBLOCKS * 3 = 30
NBUF        = MAXOPBLOCKS * 3 = 30
```

`begin_op()` 只在 `committing==0` 且下式成立时增加 outstanding：

```text
lh.n + (outstanding + 1) * MAXOPBLOCKS <= LOGBLOCKS
```

`lh.n=0` 时三个 operation 可进入，第四个因 40>30 睡在 `&log`。若 A 已 append 一个 block 且 A、
B outstanding，则 C 因 `1+3*10>30` 等待。非最后一个 `end_op()` 减少 outstanding 并 wake capacity
waiters；最后一个把 `committing=1`，释放 `log.lock` 后执行 commit。commit 会经 buffer/device path
sleep，所以不能持 `log.lock`。

`begin_op()` 必须早于 operation 可能长期持有的 inode 等锁：它会为 log space 睡眠；若 caller
带锁入睡，已有 reservation 的 operation 可能正等同一把锁才能到达 `end_op()`，于是没人能释放
reservation。reservation 是进入修改型文件系统临界路径前的资源门票，不是路径中途补领的配额。

这也是 operation 与 transaction 的分界：非最后一个 `end_op()` 可以在 group commit 前返回；
最后一个 caller 完成 commit/clear、把 committing 清零并 wake 后才返回。不能把每个 syscall 当成
一个独立 on-disk transaction。

### Log absorption 与五段持久化状态

`log_write()` 扫描 `lh.block[]`。首次看到 home block 才写 entry、`bpin()` 并增加 `lh.n`；同一
transaction 再次写它只 absorption，不增加 n 或 pin。commit 较晚从 cache 复制，因此最终 cache
bytes 成为 redo payload。状态机必须由 raw completion 复核：

```text
CACHED_DIRTY
  -> INTENT_PINNED
  -> LOG_PAYLOAD_COMPLETE
  -> COMMITTED
  -> HOME_INSTALLED
  -> CLEARED
```

`write_log()` 把每个 pinned home buffer 复制到 `log.start+tail+1` 并等待 write 完成；此时 payload
存在，但 transaction 尚未 committed。第一次 `write_head()` 把 `header.n>0` 和 block list 写到
`log.start`；该同步 `bwrite()` 返回才是源码声明的 true commit point。`install_trans(0)` 把 log
payload 写到各 home block，完成后 `bunpin()`。最后 `lh.n=0`，第二次 `write_head()` 完成才 cleared。

必须区分 submit 与 completion。`bwrite()` 经 `virtio_disk_rw()` 睡到 `b->disk` 被 interrupt handler
清零才返回，所以 QEMU trace 可以把 return 当逻辑 device completion；它仍看不到 host cache flush、
storage controller 或掉电介质保证。

### 一个可扩展的 tutorial audit seam

本项目不为七个 case 建七组 shallow syscall。runner 把 live transport 与共享 reducer 分开：

```text
QemuSource.command(command)                    -> transcript
QemuSource.panic_command(command)              -> transcript
ReplaySource.run(case, scenarios, panic=False) -> EvidenceBundle
```

解析、schema 或 oracle 失败统一抛出 `LabError`；candidate patch 只由外层隔离 export session 应用。

[`scenarios.json`](../resources/persistence/scenarios.json) 声明 actors、actions、gates、CPUS、budget
和 oracle ID。隔离 fixture 内部用一个语义 point interface 记录 closed、versioned tagged events；
`bio/log/virtio` owner-local adapters 隐藏 private fields。buffer 以
`dev:block:slot:generation` 标识，不发布指针。

执行路径使用 transport/reducer 分层：`QemuSource` 从 private QEMU 收集 live transcript，
`ReplaySource` 把 live 或 mutated transcript 交给同一 host reducer。replay 只证明 parser/oracle 能拒绝坏
证据，不构成 kernel runtime evidence。cache topology 也有 baseline single-partition 与 learner
candidate 两个 adapter；runner 先 discovery，按 opaque stable partition 选择 collision 和 parallel
blocks，不把 bucket count/hash 写死。

point capability 是 interface 的一部分：observe-only 永不阻塞；sleep gate 只能位于可调度位置；
持 buffer sleeplock 的 gate 其 release condition 不得取该 buffer；lock-held spin gate 只允许
`CPUS=2`、两个 actor 和有限 iteration budget。schema mismatch、非法 gate、event overflow、seq
gap/duplicate、candidate 越界、oracle failure 与 cleanup failure 都是显式错误；timeout 只打印最后
事件。

## 源码追踪计划

先画 cache、log 和 device 三张局部图，再按一次 write 串联：

```sh
rg -n '^struct buf' kernel/buf.h
rg -n '^binit\(|^bget\(|^bread\(|^bwrite\(|^brelse\(|^bpin\(|^bunpin\(' kernel/bio.c
rg -n 'struct logheader|struct log \{' kernel/log.c
rg -n '^initlog\(|^begin_op\(|^end_op\(|^log_write\(' kernel/log.c
rg -n '^write_log\(|^write_head\(|^install_trans\(|^commit\(' kernel/log.c
rg -n 'MAXOPBLOCKS|LOGBLOCKS|NBUF' kernel/param.h
rg -n 'struct superblock|nlog|logstart' kernel/fs.h
rg -n '^filewrite\(' kernel/file.c
rg -n '^writei\(' kernel/fs.c
rg -n '^virtio_disk_rw\(|^virtio_disk_intr\(' kernel/virtio_disk.c
rg -n '^main\(' user/logstress.c
rg -n 'writebig\(|bigwrite\(|bigfile\(|manywrites\(' user/usertests.c
```

先回答[持久化问题链](../questions/persistence.md)的 PERSIST-01..03 建立 breadth-first 地图，再用
PERSIST-04..10 深入 identity、ref/pin、admission 与 commit，随后用 PERSIST-11..14 复核资源、
并发和证据边界；PERSIST-15 只综合前面已经建立的概念。

## 观察任务

先记录 manifest baseline 与教程提交，并检查 publication seam 的静态合同：

```sh
rg -n '"baseline_commit"|"id": "core.persistence"' docs/xv6-tutorial/curriculum.json
git rev-parse HEAD
python3 -m json.tool docs/xv6-tutorial/resources/persistence/scenarios.json >/dev/null
python3 docs/xv6-tutorial/tools/validate.py
python3 docs/xv6-tutorial/tools/generate_navigation.py --check
```

在 worksheet 中记录两条 trace：

1. cache：同一未缓存 block 的 `bget -> bread -> virtio -> brelse`，逐步写 identity、ref、lock、
   valid、disk owner 与 eviction eligibility；
2. transaction：`filewrite -> begin_op -> writei/log_write -> end_op -> write_log -> nonzero header ->
   install -> zero header`，逐步写 `lh.n/outstanding/committing`、pin 与 IO completion。

再静态计算两个 admission 边界，并解释为什么 `NBUF==LOGBLOCKS` 不是 resource proof。runtime marker
只接受 runner 在 pinned baseline 临时副本中加载 audit fixture 后采集的原始输出；手写、重放或只看最终
PASS 都不能替代 publication evidence。

## 有界修改任务

学习者在 runner 导出的 pinned baseline 临时副本上提交一个 parallel-cache candidate patch。
candidate 改变 cache implementation，不改 production shared tree，也不得修改 scenarios、audit
fixture 或 host oracle。项目允许新的 partition lock、per-partition list 和 test-only topology
adapter；不要求保留 baseline 的 global LRU，但必须保留前述五条 identity/resource 不变量。

声明式 suite 固定七个 case：

1. `transaction-flow`：同一 home block 写两次，复核 absorption 与五段状态；
2. `log-capacity-empty`：三个 operation admitted，第四个真实等待，释放后进入；
3. `log-capacity-used`：`lh.n=1/outstanding=2` 时第三个真实等待；
4. `cache-same-block`：两个 actor overlap，始终共享一个 slot/generation；
5. `cache-collision`：两个不同 block/同 partition 共存且内容不混；
6. `cache-parallel`：不同 partition 在两个 hart 的 lock-held point 同时到达；
7. `cache-full`：独立只读 panic run 在 NBUF refs 后精确观察 `bget: no buffers`。

same-block 与 collision 使用可 sleep rendezvous；different-partition 的 critical-section overlap 使用
有限双 hart spin gate，且只在两个 actor 已 runnable 后 arm。spin budget 耗尽是明确失败，不能把
watchdog timeout 当并发证据。full-cache panic 单独运行，允许内存 ledger 不归零，但 private image
不得变化且 QEMU/process group 必须由 host 回收。

项目不发布 authoritative complete cache solution。资源
[`rubric.md`](../resources/persistence/rubric.md)约束行为和证据，
[`report-template.md`](../resources/persistence/report-template.md)约束一个出口产物；candidate 的实现、
取舍和局限由学习者负责。只有 fixture/runner 的完整机器证据、独立 non-author walkthrough 与修正
记录都闭合后，报告才能支持 `verified`；缺少任一项仍只支持 `draft`。

## Oracle、证据、失败路径和局限

| 维度 | 触发器 | 必须观察到 | 允许副作用与资源结果 |
|---|---|---|---|
| S | pinned source + scenario/candidate scope | cache/log 状态机、point capability、常量/admission 与 anchor 全部成立 | 只写临时 export；reverse 后逐文件相同 |
| F | transaction-flow、顺序 cache case | one append/pin + absorption；payload -> nonzero header -> home -> zero header；bytes exact | normal AFTER 全部 ledger 回零，可保留 valid identities |
| B | 两个 admission case、closed-schema mutations、cache-full | 原生 capacity wait/recheck；非法 schema/gate 被拒绝；exact panic precondition | panic 独立只读；其余 case cleanup 闭合 |
| C | `CPUS=2` same/collision/parallel | one identity、collision distinct identities、different-partition controlled overlap | gate waiters/owners/refs/pins 清零，event ring 未 overflow |
| R | 无 crash transaction + orderly stop | private image home bytes exact、on-disk header `n==0` | 只支持正常完成；不支持 crash/tear/replay/fsck claim |

每个 normal AFTER 必须满足：

```text
ordinary_refs=0 pins=0 buffer_owners=0 buffer_waiters=0 disk_owned=0
io_submitted=io_completed outstanding=0 committing=0 lh_n=0
gate_waiters=0 event_overflow=0
```

host reducer 还要拒绝 swapped commit/install/clear、把 submit 当 completion、同 key 两个 live
generation、retag referenced victim、duplicate pin、capacity inequality、seq/schema mutation 和
candidate/fixture 越界。guest PASS、一次终态、stress 时长或 `ALL TESTS PASSED` 都只是补充信号。

instrumentation 和 gates 会改变时序；atomic seq 给出 audit calls 的观测全序，不是所有内存访问的
formal happens-before。topology adapter 按教学中的 non-adversarial candidate 处理，仍需独立 source
review。有限 case 不能穷尽 eviction policy、fairness、所有 interleaving、DMA memory model 或硬件
durability。

## 退出产物与后续单元

提交一份完成的[报告包](../resources/persistence/report-template.md)，至少包含：

1. cache identity/ref/lock/pin/disk owner 图，以及 eviction generation 规则；
2. 一个 epoch 的 raw event 与 cached/logged/committed/installed/cleared 状态表；
3. 两个 reservation boundary 和 same-block/collision/parallel/full-cache 的 trigger、oracle 与结果；
4. candidate/fixture/runner/scenario hashes，focused `writebig/bigwrite/bigfile/manywrites`、单文件
   `logstress f0` 的 `MAXFILE` 边界（恰好一次 `write failed -1`），quick `CPUS=2`、full `CPUS=1`
   与 audit-disabled usertests regressions；原生六 writer crash/recovery 路径留给 #17；
5. refs/pins/locks/IO/log/gates 总账，reverse patch、private image、process group 和共享树 cleanup；
6. S/F/B/C/R 的支持范围，以及不能推出 crash atomicity、recovery 或 offline consistency 的说明。

旧问题 `FS-04..FS-07` 与 `SYNC-07` 已收缩为本问题链的兼容入口，不再维护同步答案副本。#17 从本
单元 cleared 状态之前的命名 crash points 接手 `crash-recovery-and-offline-consistency`，加入
before/after、replay、幂等重启、synthetic tear、orphan 与 offline fsck。
