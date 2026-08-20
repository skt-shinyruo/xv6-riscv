# xv6 tutorial 1.0.0 非作者发布审计

- 结果：`PASS`
- 审计日期：`2026-08-20`
- 匿名入口能力：`RELEASE-CLEAN-C24-R3`；完成全部 tutorial 前置，未参与 #21 候选编写
- 非作者 reviewer 签名：`RELEASE-1.0.0-NONAUTHOR-R3-PASS`
- pinned baseline：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 候选父提交：`fb6b5fb35b6d059a0c485646af987392039c1833`
- clean detached 审计提交：`21389c708b1852618eb6ec568b4a589aef3467bc`
- 审计提交 tree：`8420bc5c060035e8ff284893bae9aea1ac075274`
- 待签名冻结候选 patch：`/tmp/xv6-p21-candidate-v3.patch`，SHA-256
  `96f0097d3101efcb246b3f9bdb562de563aa175c663ee41a75ebc7ecb8fc4589`

本记录在上述冻结候选的独立 clean-checkout 审计通过后完成签名；包含签名更新的最终 #21 提交由
issue closure comment 绑定。审计环境为 WSL2 Linux x86-64、Python 3.12.3、GNU Make 4.3、
QEMU 8.2.2、GNU GDB 15.1 与 `riscv64-linux-gnu-gcc` 13.3.0。

首轮完整审计发现 `user/forphan.c` 与 `user/dorphan.c` 的 anchor 误用了运行时输出文本，因而以
`RELEASE-1.0.0-NONAUTHOR-R2-BLOCKER` 拒绝签名。候选随后只把两项改为各文件唯一的真实函数符号
`main(`；R3 在新 clean detached commit 上复核 pinned baseline、22/22、76/76 和 publication suite，
未重跑与不变 kernel/user/build/test code 绑定的长 QEMU regressions。R2 报告
`/tmp/xv6-release-1.0.0-final.md` SHA-256 为
`a1db06555ce232a794d62cc7fab3e69271ba933f2a20d1acfad7c7ed1d23b935`；R3 报告
`/tmp/xv6-p21-anchor-recheck.md` SHA-256 为
`d01f43c7d7120fee3cff148f74a78fc3f039fe1d1a49f3c75ebd4b85101131aa`。

## 发布矩阵

- `curriculum.json` 声明 `1.0.0`、`verified`、`coverage_complete=true`；`22/22` 单元均为
  `verified`，所有 `requires` 均解析到 verified 单元。
- 展开 `source_scope` 得到 76 个 scoped handwritten inputs；`source_areas` 对同一 76 个路径恰好
  各登记一次。每项有一个 verified primary owner、显式 `secondary` 数组，且该 owner 的
  `source_anchors` 含同一路径，结果为 `76/76`。
- 22 个单元页面均含固定八段 learning loop。新增 owner anchor 的正文有界解释对应真实符号，未以
  manifest 记账代替教学。
- `docs/questions/` 只保留 compatibility README；教程内 14 个问题文档及 generated question index
  是唯一权威集合，没有同步副本。
- navigation generator 负责 root、foundation、core、stages 与 questions 索引；单元顺序、状态统计、
  planned plain-path rendering、确定性重复生成和 stale `--check` rejection 均有测试。

## 独立执行

| 层级 | 命令 / 配置 | 结果 |
|---|---|---|
| publication | normal/development validator、navigation `--check`、13 tests、JSON、diff check | PASS |
| ownership | 独立 scope/owner/anchor/secondary/question/learning-loop assertions | PASS；22/22、76/76 |
| foundation | pointer-list smoke 与全部 shell syntax checks | PASS |
| seams | 16 个已发布 static/self-test seam | PASS；device 1+19、filesystem 1+23、persistence 7+33、recovery 2+15 |
| ABI | `resources/user-program-and-abi/run-lab.sh` | PASS；missing/present/missing、ABI echo、内部 full usertests |
| GDB/QEMU | syscall-roundtrip live normal + unknown number，`CPUS=1` | PASS；私有 image/port 与 cleanup |
| build | `make clean && make -j2`，再构建 `fs.img .gdbinit` 并复核 generator contracts | PASS |
| quick | `make clean && CPUS=2 python3 test-xv6.py -q usertests` | PASS；`ALL TESTS PASSED` |
| full | `make clean && CPUS=1 python3 test-xv6.py usertests` | PASS；`ALL TESTS PASSED` |

关键 retained artifacts：

- ABI：`/tmp/final-user-abi.log`，SHA-256
  `c33c00837a56da4e17c1f94de3bf8a34e8af73bd38b1cfc50669efc732929601`
- live GDB：`/tmp/final-syscall-live.md`，SHA-256
  `9fa0109a4883aa4dac9d4f95a811cd87995581f3c5f84ca11c343df22bff0e91`
- build/provenance：`/tmp/final-build-provenance.log`，SHA-256
  `a9291e449ebc13daccf16cb2cd9983689fa4788f1be9b0e0a03bc36a035b7868`
- quick：`/tmp/final-quick.log`，SHA-256
  `29f971b348de71638e92da0b62afb7b812dd55311eb134a221c00b89c6a33f6e`
- full：`/tmp/final-full.log`，SHA-256
  `515cecef23b4db22a2712f2a2a41d9677466b3b7715d2316520a9ee6e45b6d62`
- 审计 manifest：SHA-256
  `6b7a6ce3a2842c9aa5c2c882ba0f495699235ac9dc693d50d82038592990533d`

clean build 还独立复核了 `user/usys.S <- user/usys.pl`、`.gdbinit <- .gdbinit.tmpl-riscv`、
kernel/user ELF、asm、sym 与 `fs.img <- mkfs + UPROGS`。其中 `kernel/kernel`、`user/_usertests`、
`fs.img` 的 SHA-256 分别为
`abc7ad40c1b7090823207404389acdf4057322f17db007fd17bbbd5cd5b26c65`、
`f88cb5045332f7c87dd6311342fb2f78a84072d7dcaa9ea2253e9ddc9e12aa96`、
`27fc56fab33ad288e8698fa6e35de1da4bc6f0ba59800a2ea06f670ce68ba9fe`。

## Crash smoke 边界

原生 `make clean && CPUS=1 python3 test-xv6.py crash` 只作为 supplemental regression，进程随机
kill 不能代替确定性 `R` oracle。R1、R2 都在五次固定两秒时序尝试内未观察到 `recovering`，因此按原
driver 退出 1；日志 SHA-256 分别为
`5441e36f98f9c902b62166a349b6ddcbee58b2775107df1dd29cab2b5f435101` 与
`6fcf46fd47002e8e647864f577ced18f415fd407f8dd65fafff238aac6240f3c`。R3 的同一原命令通过 log
recovery、`forphan` 与 `dorphan`，`/tmp/final-crash-r3.log` SHA-256 为
`74e7da6b4756d87809d5fa36eb16853dc924b7aa1e83855f1151cc14f10040e2`。

发布判据依赖 #17 已提交的四个 stable crash points、六个 synthetic tear profiles、first/second boot
关系和 offline fsck；本次 fresh recovery self/static 继续通过。#21 未修改 kernel、user、mkfs、
Makefile 或 `test-xv6.py`，后者在 baseline、父提交与候选中的 SHA-256 均为
`958707b485c2e6072eb20ea655c749aa1036afa6b2a6e7885c25753ab8ae8682`。

## Cleanup 与结论

最终 `make clean` 后 detached checkout 的 `git status --porcelain` 为空；无 `fs.img`、QEMU、GDB、
private export、临时 image 或 runner 残留。随后再次执行 publication checks，结果仍为 PASS，checkout
保持干净。

非作者确认本候选满足 #21 的 source ownership、generator provenance、question authority、固定学习
循环、publication pipeline 与 clean-checkout release audit 条件。证据仍有界，不声称形式正确性、全部
交错、物理持久性、公平性或未枚举 fault space。
