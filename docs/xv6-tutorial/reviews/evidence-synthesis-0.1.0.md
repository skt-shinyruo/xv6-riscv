# 可扩展性与证据综合 0.1.0 非作者走查记录

- 教程版本：`0.1.0`；单元状态：`verified`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`84bbc4bce170be01a8d516b03ff9f386d1641516` 加 #20 候选 diff；
  本记录与晋级元数据在走查通过后纳入同一提交，最终提交由 GitHub #20 closure comment 记录
- 走查单元或连续路径：`core.evidence-synthesis`、SYNTH-00..SYNTH-10、三类 verified evidence domain，
  以及 `MAXOPBLOCKS 10 -> 11` 的未应用 impact review
- 匿名入口能力：`SYNTH-C24-R1`；已完成 foundation/core 前置，能独立阅读 source、manifest、runner、
  raw report 与 oracle，未参与 #20 候选和资源编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、Python 3.12.3、
  `riscv64-linux-gnu-gcc` 13.3.0、GNU Make 4.3；本票 fresh checks 未启动 QEMU，历史报告保留原 CPUS 配置

## 观察到的卡点

首轮 Standards review 只发现题目中的一处中文错字，修订为 `NPROC`“增加”。最终 release source-scope
预审还指出 `kernel/defs.h`、`user/grind.c`、`user/stressfs.c` 应先进入真实教学边界，再由 manifest 声明
ownership。最终正文明确 declaration compatibility，并把 `grind`/`stressfs` 分类为 broad F regression；
它们没有 stable event、nth fault、资源账本或 crash point，不能代替 deterministic B/C/R oracle。

非作者初审发现退出报告的三域 artifact 使用缩写 hash，不能从保留路径独立重算。最终报告逐项记录
fixture/scenario/runner/candidate/report 的完整路径、完整 SHA-256、原 CPUS/configuration 和
`historical-dynamic` / `fresh-self/static` / `hypothetical-not-applied` 状态。persistence 历史报告继续绑定
pre-#19 runner；current runner 只声明 fresh self/static。recovery 继续绑定仓库外 candidate。

## 验收产物

- 最终走查前 staged candidate：SHA-256
  `3acb0b02945f7e006aaca8c4d70d5887350972b00b1c0a8b66d18331a4eb73ac`
- 单元退出报告：`/tmp/xv6-evidence-synthesis-exit.md`，加入 reviewer profile 后 SHA-256
  `a6d95224341884084e73445cd523dd39ffbc57f670211faea5d07b6cff54e513`
- core / questions / answers：SHA-256 `5a6c2c1047cd8850a5047ce5106c6c1fec67248a30752c2478557a634248f6e0` /
  `5a3c9d434c8c269c65079fdb898ba4e6125525f80be293642e68529883f48f73` /
  `6fb4df1bfac4d0de7ad4dda00deb6ac785bbb8e65c03995ece9f6d4ef39ae4c0`
- rubric / report template：SHA-256
  `b2d2867c3a7c9069c8959970cd902521591fcbb0bee26b599b407456fcaee917` /
  `ad5f5b085b13a9f411aec90be5848cc2fe84b82029c3b481a50ead1ac940947f`
- 候选 manifest：SHA-256
  `12ec011a6e16435fe7b49c2689de1e584d924e252d7a9f0910bd5f1164133cba`

| 层级 | 命令 / 配置 | 结果 |
|---|---|---|
| impact | pinned source assertions | `30 -> 33`、`31 -> 34`、admission `3 -> 3`、chunk `3072 -> 3072` |
| persistence | current self/static | 7 good/33 rejected；manifest/patch/anchor/build/reverse/cleanup PASS |
| recovery | current self/static | 2 good/15 rejected；fsck mutation/build/reverse/cleanup PASS |
| publication | normal/development validator、navigation、5 tests、JSON/diff check | PASS |

## 独立复算

非作者从 pinned source 独立确认 `LOGBLOCKS/NBUF 30 -> 33`、`mkfs nlog 31 -> 34` 且 data blocks
减少 3；空日志 admission 仍为 3，`filewrite()` chunk 因 C 整数除法仍为 3072，user `BUFSZ` 从
`12*BSIZE` 变 `13*BSIZE`。`NPROC/PHYSTOP/NUM/MAXFILE/FSSIZE` 不直接改变；旧 image 的 metadata
layout 不能作为 hypothetical 配置 evidence。

三域分别绑定 scheduling/device concurrency、global/filesystem ownership、persistence/recovery reports。
从 `commit()` 反向查询时，transaction 与 four crash point oracle 是 mandatory，filesystem/general tests
是 related regression，physical durability 仍是 gap。S/F/B/C/R 只描述 bounded evidence，不构成形式证明、
fairness、all-interleavings、benchmark speedup、sector atomicity、FLUSH/FUA 或 physical durability。

## 修正与复查

走查期间 staged candidate 从初稿修正为上述冻结 SHA；最终只加入本 review、manifest 晋级和 generated
navigation。hypothetical change 从未应用，没有 kernel/user/mkfs/Makefile diff，没有新 image 或 QEMU。
shared `fs.img` 始终 missing；static runners 完成临时 build/reverse/cleanup，无 QEMU 或 runner 残留。

## 结果

| 单元 | 结果 | 晋级依据 |
|---|---|---|
| `core.evidence-synthesis` | verified | workload/bottleneck、changed/unchanged impact chain、三域 provenance、reverse index 与 evidence limits 均由非作者复核 |

学习者签名：`SYNTH-C24-R1`。

非作者 reviewer 签名：`SYNTH-NONAUTHOR-R1`。
