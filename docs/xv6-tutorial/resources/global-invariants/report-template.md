# 全局不变量与资源边界报告模板

## Provenance

- 教程提交 / candidate diff：
- pinned baseline：
- `communication.patch` SHA-256：
- `communication-and-io/run-lab.py` SHA-256：
- 机器报告路径与 SHA-256：
- 环境、QEMU/toolchain、CPUS：
- non-author reviewer / walkthrough：

## 全局 ownership 账本

| resource | identity/capacity | owner/state | acquire/transfer | rollback/release | exhaustion/post-failure | source anchor |
|---|---|---|---|---|---|---|
| process | | | | | | |
| page | | | | | | |
| fd/file | | | | | | |
| inode | | | | | | |
| buffer | | | | | | |
| log | | | | | | |
| device | | | | | | |
| interrupt | | | | | | |
| persistence | | | | | | |

## 容量与联动

| constant | value/source | first exhausted resource | exact result | coupled resources | cleanup/retry |
|---|---|---|---|---|---|
| `NPROC` | | | | | |
| `PHYSTOP` / physical-page pool | | | | | |
| `NOFILE/NFILE` | | | | | |
| `NINODE` | | | | | |
| `NDEV` | | | | | |
| `NBUF` | | | | | |
| `MAXOPBLOCKS/LOGBLOCKS` | | | | | |
| `NUM` | | | | | |
| `MAXFILE` | | | | | |

## Bounded `FD_ROLLBACK` oracle

粘贴两轮完整 `IO BASE`、`IO FD_ROLLBACK`、对应 `IO AFTER` 和 `IO PASS` raw lines。公开 marker
不包含 failure-point ledger；报告必须同时给出 fixture source line / exit result，明确哪些字段
由 guest 内部 `same_ledger()` 断言，哪些字段由 host 从 raw marker 复算。

| relation | result / evidence kind |
|---|---|
| `NOFILE - BASE.fd == filled` | |
| `NOFILE=16`, `BASE.fd=3`, `filled=13` | |
| first `fdalloc(rf)` has no slot; no new fd installed | |
| next `pipe == -1` | |
| fixture 内部 `same_ledger(before, after)` source/assertion（非 raw 字段） | |
| published `FD_ROLLBACK AFTER == BASE` | |
| duplicates/endpoints/children released | |
| second independent BASE relation | |

## Regression 与 cleanup

记录 static/build、两次 `ioflow`、focused、CPUS=2 quick、CPUS=1 full、patch reverse、
`make clean`、共享 worktree/`fs.img` fingerprints、process group 与临时目录结果；保存 transcript
digest，不只写 `PASS`。

## Evidence boundary

| S/F/B/C/R | 本报告建立的结论 | 未覆盖 gap / 禁止的 proof claim |
|---|---|---|
| S | | |
| F | | |
| B | | |
| C | | |
| R | | |

明确写出该 trigger 未执行 `fd0` 已安装而 `fd1` 失败的 branch；该分支只能由本报告的源码
ownership 分析支持。也记录 `copyout()`/`vmfault()` 可能留下的 process-owned page side effect，
不要把它误写成 fixture 已回滚。
