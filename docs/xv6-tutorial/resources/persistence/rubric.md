# Buffer cache、日志与事务验收 rubric

## 交付边界

学习者提交一个“buffer cache、日志与事务证据报告包”。源码/runtime worksheet、事务状态表、
parallel-cache candidate digest、raw event、offline image 结果、回归与局限都嵌在这一个产物中。
`scenarios.json` 是声明式 trigger/gate 合同；后续 publication fixture 只应用于 manifest pinned
baseline 的临时导出，不是 production kernel 修改，也不是 parallel-cache authoritative answer。

candidate 只能修改项目允许的 cache implementation 路径，不能修改 scenario、fixture、runner、
host oracle 或 raw event。guest 只发布 closed-schema facts；host 必须独立重算状态机、顺序、
identity 和资源账本，不能接受 guest 自报 `PASS` 作为结论。

## Evidence dimensions

- `S`：manifest `path:symbol` anchors、cache/log/interface contract、patch/candidate scope、
  `NBUF/MAXOPBLOCKS/LOGBLOCKS` 关系和 point capability table。
- `F`：cache hit/miss/eviction、同一 home block 的 log absorption，以及
  cached -> logged -> committed -> installed -> cleared 完整事务。
- `B`：空 log 与已有一项 log 的 reservation 边界、event/gate/schema 拒绝，以及独立
  `bget: no buffers` panic run。
- `C`：`CPUS=2` same-block identity、same-partition collision 和 different-partition 受控重叠。
- `R`：仅限无 crash 的正常完成；orderly stop 后 private image 的 home bytes 正确且
  on-disk header `n==0`。不声称 power-loss durability、tear atomicity 或 recovery。

## Event 与 gate contract

`EVENT` envelope 必须包含连续且唯一的 `generation/seq`，以及闭集字段
`point/pid/hart/domain/slot/ref/dev/block/buffer_generation/log_n/outstanding/committing/aux`。
buffer 身份使用 `dev/block/slot/buffer_generation`，不得用地址。host 拒绝
unknown/extra/missing field、schema mismatch、seq gap/duplicate、未 ready 的槽、ring overflow 和
额外 PASS。

每个 point 的能力来自 `scenarios.json`，不能由 caller 临时声明：

- `observe-only` 不得阻塞；
- `sleep-ok` 只能在没有 spinlock/interrupt ownership 的位置使用；
- `sleep-ok-with-buffer-sleeplock` 只允许持有目标 buffer sleeplock，release 条件不得依赖该锁；
- `spin-2cpu-ok` 只允许 `CPUS=2`、恰好两个 actor 和有限 iteration budget。

iteration budget 耗尽会写入 `META error=2`；QEMU timeout 只是 watchdog，不能替代 arrival、wait、release 和
postcondition event。

## Scenario oracles

### Transaction flow

同一 home block 的两次 `log_write()` 必须只有一次 append/pin，第二次为 absorption。host 要求：

```text
CACHE_DIRTY
  < LOG_APPENDED/LOG_ABSORBED
  < all LOG_PAYLOAD IO_COMPLETE
  < HEADER_COMMIT_COMPLETE(header_n > 0)
  < all HOME_INSTALL_COMPLETE
  < HEADER_CLEAR_COMPLETE(header_n == 0)
```

`COMMITTED` 只从非零 header write completion 开始；write submit、`log_write()` 或 guest label
都不够。commit 开始时 `outstanding==0 && committing==1`；clear 后才允许
`committing:1->0`。非最后一个 `end_op()` 可以早于 group commit 返回，报告必须区分 operation
return 和 transaction commit。

### Admission boundaries

每次 successful admission 都满足：

```text
lh.n + (outstanding + 1) * MAXOPBLOCKS <= LOGBLOCKS
```

当前 pinned values 为 `10/30`。`log-capacity-empty` 必须先观察三个 admitted actor，第四个出现
真实 `BEGIN_WAIT_SPACE`，释放一个后才 admitted。`log-capacity-used` 必须在 `lh.n=1`、两个
outstanding 时让第三个等待，因为 `1+3*10>30`；减少 outstanding 后才可进入。

### Cache identity and concurrency

- same block：两个 overlapping reference 始终映射同一 `slot/generation`，第二 actor 的 lock
  acquire 晚于第一 actor release，peak ordinary refs=2，live identity count=1；
- collision：两个不同 block 的 reported partition 相同，但必须使用不同 live identity，内容不混；
- parallel：discovery 选择不同 partition，两个 hart 都到达 `CACHE_PARTITION_HELD` 后才释放；
- eviction：victim 必须 `refcnt==0`，retag 增加 generation；candidate 可改变全局 LRU 为局部
  policy，但不能回收 referenced/pinned buffer；
- full cache：`NBUF` 个 held refs 后第 `NBUF+1` 次 request 精确 panic，且 panic run 只读、隔离。

## Resource ledger

每个非 panic case 的 quiescent AFTER 必须满足：

```text
ordinary_refs=0 pins=0 buffer_owners=0 buffer_waiters=0 disk_owned=0
io_submitted=io_completed outstanding=0 committing=0 lh_n=0
gate_waiters=0 event_overflow=0
```

`struct buf.valid` 和 identity 可以留在 cache；这不是资源泄漏。host 以 acquire/release 与
pin/unpin 事件重算 ref composition，因为 raw `refcnt` 本身不区分普通引用、waiter 与 log pin。
`NBUF==LOGBLOCKS` 也不能推出 buffer 一定够用：home buffers、log buffers、普通 references 和
pins 的生命周期会重叠。

## Commands、regressions 与 cleanup

目标 publication seam 是：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/xv6-tutorial/resources/persistence/run-lab.py --self-test
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/xv6-tutorial/resources/persistence/run-lab.py --static-only
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/xv6-tutorial/resources/persistence/run-lab.py \
  --candidate /tmp/parallel-cache.patch \
  --report /tmp/xv6-persistence-report.md
```

fixture 已作为 publication-only patch 落盘；它和 candidate 都只能应用到 runner 的临时 baseline
export。完整 runner 必须执行 fixed scenarios、focused `writebig/bigwrite/bigfile/manywrites`、单文件
`logstress f0` 的 `MAXFILE` 边界（恰好一次 `write failed -1`），以及 quick `CPUS=2` 与 full
`CPUS=1` usertests regressions。原生六 writer crash/recovery 必须留给 #17，不得用本 runner 的
orderly-completion 证据代替。结束时回收 QEMU process group、`make clean`、reverse
candidate/fixture、比较导出
源码 snapshot、删除 private image/temp tree，并确认共享 checkout/index 与共享 `fs.img` digest
不变。

## 评定等级

- **通过**：closed event schema、七个 scenario oracle、五类证据、完整 ledger、focused/quick/full
  regression 与 cleanup 全部成立；报告包含 external machine appendix SHA-256。
- **退回**：只有 boot、stress、timeout、最终 PASS/终态、地址比较，或者把 QEMU completion 写成
  host/power-loss durability；duplicate identity、非法 gate、资源不归零、candidate 越界修改也退回。

`read_head()/recover_from_log()`、named crash point、before/after、synthetic tear、幂等重启与 offline
fsck 属于 #17，不得用本 rubric 推断。
