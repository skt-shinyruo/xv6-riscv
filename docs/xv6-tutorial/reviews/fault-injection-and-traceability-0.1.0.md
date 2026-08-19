# 故障注入与源码测试追踪 0.1.0 非作者走查记录

- 教程版本：`0.1.0`；单元状态：`verified`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`9ae539db74cb5f1f7146fcb66829c0e71a01367f` 加 #19 候选 diff；
  本记录和晋级元数据在走查通过后纳入同一提交，最终提交由 GitHub #19 closure comment 记录
- 走查单元或连续路径：`core.fault-injection-and-traceability`、TRACE-00..TRACE-11、filesystem、
  scheduling、persistence 与 recovery publication seams
- 匿名入口能力：`TRACE-C24-R1`；已完成 `core.global-invariants`，能独立阅读 source、fixture、runner
  与 oracle，未参与本单元编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、Python 3.12.3、
  `riscv64-linux-gnu-gcc` 13.3.0、GNU Make 4.3；各 runner 使用其声明的 CPUS 配置

## 观察到的卡点

首轮审查发现退出报告的 forward records 使用 `TR-*`，reverse index 却省略前缀，且 status 没有使用
`baseline-current`、`verified-tutorial-fixture`、`proposed-learner-work` 三个规范字面值。修订后正反向
索引引用同一 stable ID，recovery 仍标为 tutorial fixture，但另行绑定使 no-op hook 生效的外部 candidate。

源码审计进一步发现 persistence bounded event array 溢出只设置 `error=1/gate_open=1`，没有
`wakeup(&audit)` 或 live overflow trigger；scheduling 的 32 槽 recorder 静默丢弃，当前精确 15/18-event
oracle 只能间接拒绝当前 case 的饱和 trace。最终正文把完整 error + wake/open all waiters + host reject
保留为 proposed contract，只把 current `ReplaySource` 的 `error=1` rejection 写成已验证能力。

persistence 的历史动态报告绑定 runner `cdcdfcd3...` 与 32 mutations，而 #19 当前 runner
`2af2de66...` 只新增 self-test mutation。最终历史记录恢复原 provenance，并单列当前 7 good/33 rejected
amendment；不把 self/static 冒充 fresh QEMU evidence。recovery 报告同样显式绑定 external candidate。

## 验收产物

- 最终走查前 staged candidate：SHA-256
  `862f39129df9ccaf4d039aedabdd1ebfb800a242666e8635023e6927f46272b9`
- 单元退出报告：`/tmp/xv6-fault-traceability-exit.md`，SHA-256
  `30f0dd8ec1eeee4e3b31fc9a8228f1db95874c4a63823f348bdf44df15fa65dc`
- filesystem author / non-author 报告：`/tmp/xv6-fi-filesystem.md` /
  `/tmp/xv6-fi-filesystem-nonauthor.md`，SHA-256 `f60d73ec...` / `cdcba1b3...`
- scheduling author / non-author 报告：`/tmp/xv6-fi-scheduling.md` /
  `/tmp/xv6-fi-scheduling-nonauthor.md`，SHA-256 `d6ab056b...` / `c4565c7c...`
- persistence current runner：`resources/persistence/run-lab.py`，SHA-256 `2af2de66...`；历史动态
  report `8f5a34b4...` 仍绑定 pre-#19 runner `cdcdfcd3...`
- recovery fixture/scenario/runner/candidate/verified report：`82ba3e78...` / `d6bdf3e6...` /
  `39d9654d...` / `5fcbe25c...` / `eb4edf2a...`

| 层级 | 命令 / 配置 | 结果 |
|---|---|---|
| filesystem | self/static/full runner | 1 good/23 rejected；6 cases、8 focused、quick/full PASS |
| scheduling | static/full runner | fixed/broken 各两轮；focused/related、quick/full PASS |
| persistence | self/static | 7 good/33 rejected；build/reverse/cleanup PASS；无 fresh dynamic 主张 |
| recovery | self/static | 2 good/15 rejected；fsck negative matrix、build/reverse/cleanup PASS |
| publication | normal/development validator、navigation、5 tests、JSON/diff check | PASS |

## 独立复算

filesystem `LINKFAIL` 为 `fail_at/eligible/fired=1/1/1`，`ALLOCFAIL=2/2/1`；后者 size 保持
12288，允许 empty indirect block，最终 ledger 回 BASE。scheduling fixed 顺序闭合
`WAIT_CHECK -> WAIT_PLOCK -> ATTEMPT -> release -> SLEEPING publish -> READY -> WAKE_MATCH`；broken
闭合 `WAIT_RELEASED -> READY -> WAKE_MISS -> publish -> BAD_ORACLE`，rescue 只用于 cleanup。

persistence self-test 将同一 good trace 的 META/LEDGER `error=0` 改为 1，`ReplaySource` 以 guest audit
error 拒绝；这不证明 live wake-all。recovery external candidate 把 ID 20 接到 nonzero `write_head()` 后；
verified report 的 crash header n=2、home=before、首启 replay/install=2、二启 replay=0，header clear 且
first/second image digest 相同。

反向查询 `kernel/log.c:commit()` 时，persistence transaction 与 recovery 10/20/30/40 是必跑 oracle；
filesystem/full usertests 只是 related regressions，lost-wakeup gate 不是 commit oracle。所有结果都保留
path coverage、formal proof、fairness、all interleavings 与 physical durability 缺口。

## 修正与复查

最终候选使用规范三态、完整 `TR-*` IDs、准确的 bounded event array 术语，并把 current/proposed、
历史/current runner、fixture/external candidate 清楚分层。两次独立动态 runner 均使用临时 pinned export、
私有 image 和进程组；结束后 patch reverse、源码指纹不变、共享 `fs.img` 仍 missing，无 QEMU/GDB 或
临时目录残留。

## 结果

| 单元 | 结果 | 晋级依据 |
|---|---|---|
| `core.fault-injection-and-traceability` | verified | 双向追踪、稳定 fault rule、gate/overflow 边界、四 seam 证据与 cleanup 均由非作者复核 |

学习者签名：`TRACE-C24-R1`。

非作者 reviewer 签名：`TRACE-NONAUTHOR-R1`。
