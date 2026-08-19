# 全局不变量与资源边界 0.1.0 非作者走查记录

- 教程版本：`0.1.0`；单元状态：`verified`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`615b47b` 加 #18 候选 diff；本记录、迁题和晋级元数据在走查通过后
  纳入同一提交，最终提交由 GitHub #18 closure comment 记录
- 走查单元或连续路径：`core.global-invariants`、INVARIANT-00..INVARIANT-10、复用
  `communication-and-io` 的 `FD_ROLLBACK` publication seam
- 匿名入口能力：`INVARIANT-C24-R1`；已完成 `project.crash-recovery`，能阅读 process、VM、
  filesystem、log 与 device owner 路径，未参与本单元编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、Python 3.12.3、
  `riscv64-linux-gnu-gcc` 13.3.0、GNU Make 4.3；fixture/focused/full `CPUS=1`，quick `CPUS=2`

## 观察到的卡点

首轮审查发现正文把 `ZOMBIE` 回收者写成原 parent，但 `reparent()` 可把已退出 child 移交
`initproc`；又把所有 identity/count 更新都归入 lock，漏掉 current-process `ofile[]` 与 private
page-table construction 的排他 owner。最终模型分别改成 current parent/init owner，以及
“所属锁或 private/exclusive owner”。

`FD_ROLLBACK` 的公开 marker 只包含 `filled/pipe/cleanup` 与 duplicates 关闭后的最终账本；饱和
failure point 的 `same_ledger(before, after)` 是 guest 内部断言。走查据此删除了“host 独立复算
隐藏账本”的越界主张，并明确满 `NOFILE` 使第一个 `fdalloc(rf)` 失败，不动态覆盖 fd0 已安装、
fd1 失败的 branch。

边界复核还补入 `copyout()` 触发 `vmfault()` 后可能保留的 process-owned page、`begin_op()` 的
aggregate admission、fork refs 不新占 `NFILE` slot、IRQ 可 drain/process input，以及 `NDEV` 和
physical-page capacity。未来故障注入单元作为 planned manifest owner 插在本单元与综合单元之间，
不把拟议基础设施写成当前黑盒。

## 验收产物

- 复用 fixture：`resources/communication-and-io/communication.patch`，SHA-256
  `741ac9f2f2de62952b715613c5b37b24a40a12081d0bd5a56b355a6ed79e64f5`
- 复用 runner：`resources/communication-and-io/run-lab.py`，SHA-256
  `dbd8062296aa5d0f946a7c7b370ddb9aa92cd74e56c125c2eb4bf522f81cdce3`
- core：`core/global-invariants.md`，SHA-256
  `f437fc74343a7217da89d65927c73fba353d7829ff1dc2d8de5eb9d80a448e5b`
- questions：`questions/global-invariants.md`，SHA-256
  `e7d5e102c8efe3d9a3422e9a7670874d79282083e39c6d6c0883d1c15152327d`
- answers：`questions/answers/global-invariants.md`，SHA-256
  `06332481fa97e966c43f8b086e677a7b78886d1b0829ff8a62a197cac66de840`
- rubric：`resources/global-invariants/rubric.md`，SHA-256
  `f2ae67044d26b78301afd66731a0b1797ee7733fe2031fbd23ab45fed156fd3e`
- report template：`resources/global-invariants/report-template.md`，SHA-256
  `19dd677b5bab382cfc2e73bc5463f148a0386257a5ec5c9d17e47efeccb1350b`
- 单元退出报告：`/tmp/xv6-global-invariants-exit.md`，SHA-256
  `43a3def0525174841d5f3777fc24708be90d4effc60cfc8d84be95220844edab`
- author 机器附录：`/tmp/xv6-global-invariants-author-final.md`，SHA-256
  `c58d6784055562e26691af668634865dad0dc3f11db269ddae3d6411ed0f35d5`
- non-author 机器附录：`/tmp/xv6-global-invariants-nonauthor.md`，SHA-256
  `29f5779c1e865f91542321bdfd512190ef4572a96b90e09ac0feaaadfa4a0a72`

| 层级 | 命令 / 配置 | 结果 |
| --- | --- | --- |
| static | `run-lab.py --static-only` | fixture scope/source/apply/build/reverse PASS |
| bounded | 两次独立 `ioflow` | `NOFILE=16`、BASE fd=3、filled=13、pipe=-1、最终账本回到 BASE |
| focused | `pipe1 preempt killstatus sharedfd copyout exectest` | PASS |
| quick/full | `test-xv6.py`，CPUS=2/1 | PASS；non-author digest 分别为 `7d77dd...` / `d4b447...` |
| publication | normal/development validator、navigation、5 tests、JSON/diff check | PASS |

## 独立复算

作者报告的两轮 raw BASE 都是
`fd=3 files=1 refs=9 pipes=0 procs=3 free=32530`。non-author 必须从 `NOFILE=16` 独立复算
`16-3=13`，确认 `IO FD_ROLLBACK filled=13 pipe=-1 cleanup=1`，再逐字段比较对应
`IO AFTER phase=FD_ROLLBACK` 与 BASE。fixture source 的 `same_ledger(before, after)` 只证明 guest
内部 failure-point assertion 被执行；它不是 raw failure ledger，也不能支持 partially-installed-fd
branch 或其他资源池 exhaustion。

QEMU 证据只覆盖该固定配置与 trigger。NPROC/NFILE/NINODE/NBUF/log/NUM 的 capacity consequence
来自 stable source 与前置单元，不因这次运行变成动态证明；一次成功运行也不能推出 formal proof、
fairness、all interleavings 或 physical durability。

退出报告另以排除本 review 文件的可重算 candidate-content diff 绑定教程内容，避免自引用摘要；
它逐项记录九类 resource、九项 capacity，以及 `p->lock`/`wait_lock`、`kmem.lock`、`ftable.lock`、
`itable.lock`/`ip->lock`、`bcache.lock`/`b->lock`、`log.lock` 和 `disk.vdisk_lock` 的保护边界。

## 修正与复查

教程、题目、答案、rubric 和模板统一使用同一 acquisition/transfer/rollback/release/exhaustion/
post-failure vocabulary。旧 SYNC-05/08 只在本记录、review metadata 与 `verified` 状态同一变更中
收缩为兼容入口。临时 pinned export、QEMU process group、私有 image 与 patch reverse/clean 由
复用 runner 负责；最终 non-author full rerun 已确认无残留。

## 结果

| 单元 | 结果 | 晋级依据 |
| --- | --- | --- |
| `core.global-invariants` | verified | 统一账本、容量边界、精确 FD exhaustion、回归与 cleanup 均由非作者复核 |

学习者签名：`INVARIANT-C24-R1`。

非作者 reviewer 签名：`INVARIANT-NONAUTHOR-R1`。
