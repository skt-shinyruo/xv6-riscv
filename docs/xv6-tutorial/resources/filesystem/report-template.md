# 文件系统命名与数据路径报告包

## 1. 身份与环境

- tutorial commit：
- pinned baseline（来自 `curriculum.json`）：
- fixture SHA-256：
- runner SHA-256：
- 机器证据附录 SHA-256：
- toolchain/QEMU/host：
- `CPUS`：fixture / quick / full：

## 2. Image、seam 与允许副作用

- `Makefile:fs.img -> mkfs/mkfs.c:main -> superblock/root/bitmap` layout：
- patch path 与 test-only `fsaudit` snapshot/fault interface：
- private image/path 与 before digest：
- 允许副作用：临时导出、build artifacts、private image、QEMU/process group、外部 `/tmp` 报告。
- 禁止副作用：共享 checkout/index、共享 `fs.img`、production baseline。

## 3. 内嵌 worksheet 与 raw evidence

用一张表记录 image、pathname、directory、inode、file、buffer 与 disk block 的 owner 和状态；
这张表就是报告包内的 worksheet。

| 步骤/marker | `path:symbol` | owner/ref/lock | `inum/nlink/size` | logical/physical block | 结果/cleanup |
|---|---|---|---|---|---|
| | | | | | |

粘贴完整 `FS BASE`、六个场景、六个 `FS AFTER` 与唯一 `FS PASS`，不要只贴摘要。逐项记录 host
对 closed fields、bytes/checksum、block identity、event sequence、link counts、same inum、fault
delta 和七项 resource ledger 的重算。

## 4. 六个场景与证据维度

| 场景 | 输入/trigger | 观察字段 | 支持的结论 | 不可推出 |
|---|---|---|---|---|
| RW | | | | |
| LINK | | | | |
| TRUNC | | | | |
| FDFAIL | | | | |
| LINKFAIL | | | | |
| ALLOCFAIL | | | | |

| 维度 | 结论与证据 |
|---|---|
| S | |
| F | |
| B | |
| C=N/A | 不声称 concurrent pathname/cache correctness、scalability 或 fairness。 |
| R=N/A | private image 只隔离副作用；不声称 commit、durability、tear 或 recovery。 |

## 5. 回归、资源账本与退出

- self-test/static 命令、exit status、digest：
- focused filesystem 命令、exit status、transcript digest：
- quick/full 命令、`ALL TESTS PASSED` 计数、digest：
- 每个 AFTER 是否逐字段回到 BASE：
- reverse patch / `make clean` / source snapshot：
- private image 删除与 before/after 范围：
- 共享 worktree/index before/after：
- 共享 `fs.img` before/after：
- QEMU/process-group cleanup：

## 6. Walkthrough signatures

- learner capability/entry contract（匿名）：
- non-author walkthrough record/path：
- author review：
- open corrections and disposition：

在 non-author walkthrough 与修正完成前，本报告只能支持 `draft`，不能支持 `verified` 晋级。
