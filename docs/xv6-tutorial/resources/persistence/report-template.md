# Buffer cache、日志与事务证据报告包

## 1. 身份与环境

- tutorial commit：
- pinned baseline（来自 `curriculum.json`）：
- scenario SHA-256：
- fixture SHA-256：
- runner SHA-256：
- parallel-cache candidate SHA-256：
- 机器证据附录 SHA-256：
- toolchain/QEMU/host：
- `CPUS`：transaction / concurrency / quick / full：

## 2. Interface、隔离与允许副作用

- `QemuSource.command/panic_command` -> transcript；`ReplaySource.run` -> `EvidenceBundle`，失败为 `LabError`：
- event schema version、point capability table 与 event budget：
- `QemuSource`/`ReplaySource` adapters：
- baseline/candidate cache topology adapter：
- private export/image 与 before digest：
- candidate/fixture path allowlist：
- 允许副作用：临时源码/build、private image、QEMU process group、外部 `/tmp` 报告。
- 禁止副作用：共享 checkout/index、共享 `fs.img`、production baseline、host oracle。

## 3. 内嵌 worksheet 与 raw events

该表就是单一报告包中的 worksheet；使用稳定 ID，不记录指针。

| `seq`/point | actor/hart | buffer `dev:block:slot:generation` | ref/lock/pin/disk | log `n/outstanding/committing` | IO/结果 |
|---|---|---|---|---|---|
| | | | | | |

附上完整 closed-schema raw evidence 或其外部机器附录 SHA-256。记录 host 对 schema、连续 seq、
state transition、identity、capacity relation 和 ledger 的独立重算；guest `PASS` 只作为 raw field。

## 4. Transaction state machine

| epoch/home block | cached dirty | append/absorb + pin | payload complete | nonzero header complete | home installed/unpin | zero header complete |
|---|---:|---:|---:|---:|---:|---:|
| | | | | | | |

- operation `end_op()` returns 与 group commit 的区别：
- `COMMITTED` 判定使用的 raw completion：
- orderly stop 后 home bytes：
- offline header `n`：
- QEMU logical order、host-visible image 与不能推出的 power-loss claim：

## 5. Admission 与 cache scenarios

| case | deterministic trigger/gate | observed wait/overlap | host relation | cleanup |
|---|---|---|---|---|
| log-capacity-empty | | | | |
| log-capacity-used | | | | |
| cache-same-block | | | | |
| cache-collision | | | | |
| cache-parallel | | | | |
| cache-full | | | | isolated panic run |

- discovery 得到的 same/different partition blocks：
- same-block live identity count 与 peak refs：
- collision 的两个 slot/generation 与内容：
- different-partition spin gate 的两个 hart/partition：
- eviction victim eligibility/generation：
- `bget: no buffers` 前 ledger 与 exact panic：

## 6. Evidence 与资源总账

| 维度 | 触发器 | 支持的结论 | 不能推出 |
|---|---|---|---|
| S | | | |
| F | | | |
| B | | | |
| C | | | |
| R | orderly completion only | | crash/tear/recovery/fsck |

| checkpoint | ordinary refs | pins | owners/waiters | disk owned | IO submit/complete | `n/outstanding/committing` | gates/overflow |
|---|---:|---:|---|---:|---|---|---|
| BASE | | | | | | | |
| AFTER transaction | | | | | | | |
| AFTER admission | | | | | | | |
| AFTER cache | | | | | | | |

解释 `valid identity` 可保留而 refs 必须归零，以及为什么 `NBUF==LOGBLOCKS` 不能代替上表。

## 7. Regression、cleanup 与 evidence limits

- self-test/static 命令、exit status、digest：
- focused `writebig/bigwrite/bigfile/manywrites`：
- `logstress f0` `MAXFILE` boundary（恰好一次 `write failed -1`）：
- quick `CPUS=2` / full `CPUS=1`：
- audit-disabled baseline/candidate build：
- QEMU/process-group cleanup：
- reverse candidate/fixture、`make clean`、source snapshot：
- private image/temp tree 删除：
- shared worktree/index/fs.img before/after：
- instrumentation、bounded schedule 与 non-adversarial adapter 的局限：

## 8. Walkthrough signatures

- learner capability/entry contract（匿名）：
- non-author walkthrough record/path：
- author review：
- open corrections and disposition：

在 runnable fixture、完整机器证据、non-author walkthrough 与修正完成前，本报告只能支持
`draft`，不能支持 `verified` 晋级。
