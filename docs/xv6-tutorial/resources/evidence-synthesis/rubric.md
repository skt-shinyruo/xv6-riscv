# 可扩展性与证据综合 rubric

## 交付边界

学习者提交一份 source-level impact report，不提交 kernel patch。固定 hypothetical change 是
`MAXOPBLOCKS 10 -> 11`；它不得应用到共享 worktree，也不得生成/复用 image 冒充新配置结果。
现有 verified reports 是 evidence 输入，不是要复制的答案。current ticket 只要求 fresh source/static/
publication checks；引用的 dynamic report 必须保留其原 fixture/runner/candidate/configuration provenance。

## 必须复算的影响链

| relation | pinned | hypothetical | acceptance |
|---|---:|---:|---|
| `MAXOPBLOCKS` | 10 | 11 | 明确标为未应用 |
| `LOGBLOCKS/NBUF` | 30/30 | 33/33 | `MAXOPBLOCKS*3` |
| `mkfs nlog` | 31 | 34 | header + data；metadata offsets 移动，data blocks 减少 3 |
| empty-log admitted operations | 3 | 3 | 由 `begin_op()` inequality 复算，不写成 throughput |
| `filewrite()` chunk | 3072 | 3072 | C integer division，`BSIZE=1024` |
| user `BUFSZ` | `12*BSIZE` | `13*BSIZE` | 只影响相应 test buffer/boundary |
| `NPROC/PHYSTOP/NUM/MAXFILE/FSSIZE` | unchanged | unchanged | 列出可能的 next bottleneck，不声称独立扩容 |

必须追踪 definition -> derived capacity -> owner/wait/serialization -> image/test -> gap，并从受影响
runner/test 反向找回同一 relation。旧 `fs.img` 只能作为原配置 artifact；新配置 image 写 `not built`。

## Scalability 与三类 evidence domain

报告至少覆盖：

- capacity：`NPROC/PHYSTOP`、file/inode、`NBUF/LOGBLOCKS`、`NUM`、`FSSIZE/MAXFILE`；
- coordination：locks、private/per-hart state、sleep predicate/producer、queue、cache pin、commit/recovery；
- concurrency/waiting：named scheduler/device/persistence evidence；
- memory/resource ownership：rollback/fault/cache/log ledger evidence；
- persistence/recovery：transaction、four crash points、offline/second boot evidence。

每行写 workload、first pressure point、exact outcome、cleanup/retry、next bottleneck 和 evidence limit。
不得把容量比值、一次 PASS 或 S/F/B/C/R 的数量写成 scalability score、formal proof 或 benchmark。

## Reproducibility 与 evidence boundary

报告必须记录 baseline、tutorial candidate、environment、toolchain/QEMU、CPUS/memory、configuration、image、
trigger/command、expectation、raw observation/digest、cleanup 和 limits。artifact 必须区分：

- `historical-dynamic`：只绑定原 runner/configuration；
- `fresh-self/static`：只证明 current parser/source/build contract；
- `fresh-publication`：validator/navigation/tests；
- `hypothetical-not-applied`：没有 runtime/image claim。

S/F/B/C/R 逐栏给 strongest bounded claim 与 remaining gap。禁止 path coverage、fairness、all interleavings、
new-config throughput/recovery、sector atomicity、FLUSH/FUA 或 physical durability 主张。

## Commands 与评定

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/xv6-tutorial/resources/persistence/run-lab.py --self-test
PYTHONDONTWRITEBYTECODE=1 python3 docs/xv6-tutorial/resources/persistence/run-lab.py --static-only
PYTHONDONTWRITEBYTECODE=1 python3 docs/xv6-tutorial/resources/recovery/run-project.py --self-test
PYTHONDONTWRITEBYTECODE=1 python3 docs/xv6-tutorial/resources/recovery/run-project.py --static-only
python3 docs/xv6-tutorial/tools/validate.py
python3 docs/xv6-tutorial/tools/validate.py --development
python3 docs/xv6-tutorial/tools/generate_navigation.py --check
python3 -m unittest docs.xv6-tutorial.tools.test_validate
```

- **通过**：影响链数值、三域 provenance、reproducibility、reverse index、S/F/B/C/R 和 cleanup 可由
  non-author 独立重算，所有 publication checks 通过。
- **退回**：实际改源码、遗漏 image layout、误算 unchanged relation、历史/当前 evidence 混写、缺任一
  evidence domain，或出现越界 proof claim。

non-author 至少独立复算 `30 -> 33`、`31 -> 34`、admission `3 -> 3`、chunk `3072 -> 3072`，并从
`commit()` 反向确认 mandatory persistence/recovery oracle 与 physical-durability gap。
