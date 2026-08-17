# 设备 I/O 报告包

## 1. 身份与环境

- tutorial commit：
- pinned baseline（来自 `curriculum.json`）：
- fixture SHA-256：
- runner SHA-256：
- 机器附录 SHA-256（生成后由本报告外部记录）：
- toolchain/QEMU/host：
- `CPUS`：设备 trace `2`；quick `2`；full `1`

## 2. 变更与 seam

- patch 相对路径和 15 个 path：
- test-only `devprobe`/snapshot/gate 接口：
- 允许副作用：临时导出、私有 fs.img 只读 block、QEMU/driver 进程、外部 `/tmp` 报告。
- 禁止副作用：共享 checkout、共享 `fs.img`、生产 baseline。

## 3. 内嵌 worksheet 与 raw evidence

先用一张表记录 console/disk/queue 的 trigger、owner、lock-before/after、channel、hart、
program/publish/notify/complete/reclaim 与 cleanup；这张表就是报告包内的 worksheet。

随后粘贴完整 `DEV CONSOLE_WAIT`、`DEV CONSOLE`、`DEV DISK`、`DEV QUEUE`、三个 `DEV AFTER` 和
`DEV PASS`，不要只贴摘要。记录每个字段的 host 重算关系：IRQ、hart mask、channel、descriptor
flags/length/next、queue address、avail/used delta、唯一 seq、lock handoff、free/info/deferred/buf-ref ledger。

## 4. 证据维度与结论

| 维度 | 输入/触发 | 观察字段 | 结论 | 不可推出 |
| --- | --- | --- | --- | --- |
| S |  |  |  |  |
| F |  |  |  |  |
| B |  |  |  |  |
| C |  |  |  |  |
| R=N/A |  |  |  |  |

## 5. 回归与退出

- focused 命令、exit status、transcript digest：
- quick/full 命令、`ALL TESTS PASSED` 计数、digest：
- reverse patch / `make clean`：
- 每个 AFTER ledger 是否回到 `active=0 waiters=0 deferred=0 free_desc=8 info=0 buf_refs=0`：
- valid cache data 与零 buffer refcnt 的区别：
- 共享 worktree/index 状态与 digest before/after：
- 共享 `fs.img` before/after：
- QEMU/driver process-group cleanup：

## 6. Walkthrough signatures

- learner capability/entry contract（匿名）：
- non-author walkthrough record/path：
- author review：
- open corrections and disposition：
