# Buffer cache、日志与事务 0.1.0 非作者走查记录

- 教程版本：`0.1.0`；单元状态：`verified`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`8f3d18e77efe33ecc477960a1e45be272adbb565` 加 #16 候选 diff；
  本记录、迁题和晋级元数据在走查通过后纳入同一提交，最终提交由 GitHub #16 closure comment 记录
- 走查单元或连续路径：`core.persistence`、PERSIST-01..PERSIST-15、隔离 `persisttrace` fixture
- 匿名入口能力：`PERSIST-C22-R1`；已完成 `core.filesystem`，能独立阅读 cache/log/virtio C 路径，
  未参与本单元编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、GNU Make 4.3、Python 3.12.3、
  `riscv64-linux-gnu-gcc` 13.3.0；scenario/focused/full `CPUS=1`，并发/quick `CPUS=2`

## 观察到的卡点

首轮走查发现 collision 声称两个 actor 在持锁状态会合，但旧 fixture 只在 lookup gate 同步，
串行 trace 也会被接受。最终 fixture 在 `CACHE_SLEEP` 使用真实 sleep rendezvous，host 要求两个
不同 buffer identity 的 acquire 都早于首次 release。transaction oracle 也从只计四次 I/O 收紧为
按目标 block、相邻序号和 identity 分别绑定 payload、header commit、home install 与 header clear；
交换 commit/install 或把语义点移到 completion 前的 mutation 均被拒绝。

后续独立复核发现声明式 scenario 含未执行的 entry gate、`select-stable-home-block` 被误写成
prepare-existing-file、教程和 rubric 使用不存在的 runner 类与 event 字段，以及 full usertests 被
误称为六 writer crash/recovery。修订后 JSON 只保留真实 gate/action，文档逐项对应
`QemuSource`/`ReplaySource`、`EvidenceBundle`、`LabError` 和 `MARKER_SCHEMAS`；full 只证明
普通 usertests，crash、tear、replay、幂等重启和 offline fsck 留给 #17。

最后一轮发现 cache adapter 在释放保护锁后读取 `refcnt`，以及报告只写 marker、没有保留
cache-full 的原始 panic。最终 candidate 与 baseline adapter 都在 cache/bucket lock 下采样并发出
`CACHE_SLEEP`/`CACHE_RELEASE`，机器附录原样保留且只保留一次
`panic: bget: no buffers`。最终哈希上的七个 raw block 均由 reducer 无补写 replay 通过。

## Source worksheet

| slice | `path:symbol` | owner / state | 可复核关系 | 边界 |
| --- | --- | --- | --- | --- |
| cache | `kernel/bio.c:bget/bread/brelse` | cache identity、ref 与 sleeplock 分层 | one-buffer-per-block、reuse、30-buffer 上界 | candidate 可用局部 eviction |
| admission | `kernel/log.c:begin_op/end_op` | `outstanding` 与 `committing` 受 log lock 保护 | `n + (outstanding + 1) * MAXOPBLOCKS > LOGBLOCKS` 时等待 | 不等于 on-disk commit |
| transaction | `kernel/log.c:log_write/commit` | header slot 与 pin 共同保留 dirty identity | append/absorb、payload/header/home/clear 严格排序 | crash 后 replay 属于 #17 |
| device | `kernel/virtio_disk.c:virtio_disk_rw/virtio_disk_intr` | virtio descriptor 与 buffer disk flag | 四个 write submit/complete 绑定四个逻辑阶段 | completion 不等于 host durability |
| boundary | `kernel/file.c:filewrite`、`user/logstress.c:main` | filewrite 分片受 MAXOPBLOCKS 预算约束 | `logstress f0` 恰有一次 `write failed -1` | 非 crash/recovery 测试 |

## 最终 raw evidence

```text
tx: append=9 absorb=10 payload=25 header=34 home=46 clear=56 writes=4/4 ledger=0
admission-empty: 0 + (3 + 1) * 10 > 30; wait=4 readmit=8
admission-used: 1 + (2 + 1) * 10 > 30; wait=6 readmit=10
same: one identity; ref peak=2; first release=10; second acquire=11
collision: blocks=(2,4); domain=0; acquire=(4,8); first release=9
parallel: blocks=(2,3); domains=(0,1); harts=(0,1); guard=(2,4)
cache-full: holders=30; next=1030; panic: bget: no buffers
orderly image: logstart=2; header_n=0; home_block=1990; home_bytes=5152
```

transaction 的四个写 `IO_COMPLETE` 为 seq 24/33/45/55，分别紧邻 payload/header/home/clear
的 25/34/46/56。collision 中两个不同 identity `(10,2)`、`(12,2)` 的 acquire 均先于 release；
parallel 则以两个 hart 和两个 domain 独立证明跨域重叠。cache-full 前后 private image SHA-256
同为 `c21417c7f51f054d7f0db7eff6f1e20aa7f5e4eb49f4e279abd7c9d8efec19bd`。

## 验收产物

- baseline adapter：`resources/persistence/baseline-cache-adapter.patch`，SHA-256
  `2afba7587bca91e11b8fa70cbf57aa142d733cf3c46b7934c907aaa271ea9b0f`
- fixture：`resources/persistence/persistence-audit.patch`，SHA-256
  `916f82771b03ddefc52c2c974203eb5663af8fd9eee163c7a1dd31ad5001e9b9`
- scenario：`resources/persistence/scenarios.json`，SHA-256
  `a90d6a9dc8e0462793c263c8ce922777f4ca6f5e5e8cf88167d1b45e55b8c6db`
- runner：`resources/persistence/run-lab.py`，SHA-256
  `cdcdfcd358715b8aad47ae1fcfa5be01d398c3edf9ed81203738d94e634ddd00`
- 走查 candidate：`parallel-cache.patch`，SHA-256
  `8063a2890d8ad69011c4047e990add5842f6a44ed884a2f5365c8325488124f0`
- 最终机器附录：`/tmp/xv6-persistence-report-final.md`，SHA-256
  `8f5a34b47183f8eefecfe10dfc5fb35c1624d07da628db512a3644cb0237ffd5`

| 层级 | 命令 / 配置 | 结果 |
| --- | --- | --- |
| static | `run-lab.py --static-only` | manifest/published patches/anchor/build/reverse/snapshot PASS |
| oracle | `run-lab.py --self-test` | 7 good traces accepted；32 mutations rejected |
| focused | `writebig/bigwrite/bigfile/manywrites/logstress f0` | 每项通过；无 marker 泄漏 |
| quick | `test-xv6.py -q usertests`，CPUS=2 | PASS，SHA-256 `d4af859f...` |
| full | `test-xv6.py usertests`，CPUS=1 | PASS，SHA-256 `6df7ab73...` |
| publication | normal/development validator、navigation、5 tests、JSON/diff check | PASS |

focused transcript SHA-256 依次为 `eaff7910...`、`48d00bdc...`、`7520cde1...`、
`fefed0a8...`、`371c5f30...`。`logstress f0` 只接受一次预期的 `write failed -1`，无 panic。

#19 只在 `ReplaySource` self-test matrix 新增 `error=1` event-overflow mutation；runtime parser、guest
fixture、scenario 和 candidate 均未改变。上列历史机器附录绑定的 runner 是
`cdcdfcd358715b8aad47ae1fcfa5be01d398c3edf9ed81203738d94e634ddd00`，不是当前
`2af2de66...`；它仍是未变动态路径的历史证据，但不得称为当前 runner 的 fresh QEMU report。当前
runner 另以 7 good/33 rejected 的 self-test 和 static/build/reverse/cleanup 验收该增量。

## 修正与复查

临时 baseline export 在 `make clean` 后按 candidate/fixture 逆序恢复并逐文件比较；QEMU/driver
process group 消失，共享 `fs.img` 前后均为 `missing`，共享 repository digest 未变。
S/F/B/C/R 分别由源码合约、transaction、admission/capacity、确定性双 hart gate 和 orderly
private-image 检查支持。R 只证明无 crash 停机后的 home bytes 与 cleared header，不外推 host
掉电、sector tear、recovery 或 fsck。

走查 candidate 固定两个各 15-buffer 的局部 eviction domain。若 16 个 held block 全落在同一
domain，另一 domain 尚有可用 buffer 时也会 panic；rubric 允许局部 eviction，所以这不是本票
correctness blocker，但它是该设计的容量偏斜上限，不能被教程描述成全局 30-buffer replacement。

旧 FS-04..FS-07 与 SYNC-07 在本记录、review metadata 和 verified 状态同一变更中收缩为
PERSIST 兼容入口；正文不保留同步副本。

## 结果

| 单元 | 结果 | 晋级依据 |
| --- | --- | --- |
| `core.persistence` | verified | cache/log/device anchors、闭合 raw oracle、bounded concurrency、回归与隔离清理由非作者复核 |

学习者签名：`PERSIST-C22-R1`。

非作者 reviewer 签名：`PERSIST-NONAUTHOR-R1`。
