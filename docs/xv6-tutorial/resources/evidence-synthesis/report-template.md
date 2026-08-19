# 可扩展性与证据综合报告模板

## Provenance 与 reproducibility

- pinned baseline / tutorial commit or candidate diff：
- manifest / core / questions / answers / rubric SHA-256：
- environment、toolchain、QEMU、`CPUS`、memory：
- configuration：
- image path/hash/state（本练习应为 `not built; hypothetical-not-applied`）：
- trigger/commands：
- expected relations：
- raw observations/artifact digests：
- cleanup：
- limits：
- non-author reviewer / walkthrough：

## Workload 与 bottleneck 地图

| workload | capacity / identity | lock/private/per-hart state | wait/queue/cache/storage boundary | exact outcome | next bottleneck | evidence / gap |
|---|---|---|---|---|---|---|
| runnable processes | | | | | | |
| open/create | | | | | | |
| unique dirty blocks | | | | | | |
| disk requests | | | | | | |
| crash/restart | | | | | | |

## Hypothetical impact chain

变更：`MAXOPBLOCKS 10 -> 11`；状态：`hypothetical-not-applied`。

| source relation | pinned | hypothetical | changed/unchanged + invariant | owner/wait/layout consequence | mandatory test/resource | related-only | remaining gap |
|---|---:|---:|---|---|---|---|---|
| `LOGBLOCKS/NBUF` | | | | | | | |
| `mkfs nlog/metadata/data` | | | | | | | |
| empty-log admission | | | | | | | |
| `filewrite()` chunk | | | | | | | |
| user `BUFSZ` | | | | | | | |
| `NPROC/PHYSTOP/NUM/MAXFILE/FSSIZE` | | | | | | | |

明确记录旧 image 为什么无效，以及新配置没有 build/boot/QEMU/recovery claim。

## 三类 evidence domain

| domain | artifact status | fixture/scenario/runner/candidate/report SHA-256 | trigger/configuration | expectation -> raw observation | cleanup | strongest claim | gap |
|---|---|---|---|---|---|---|---|
| concurrency/waiting | | | | | | | |
| memory/resource ownership | | | | | | | |
| persistence/recovery | | | | | | | |

`artifact status` 只使用 `historical-dynamic`、`fresh-self/static`、`fresh-publication` 或
`hypothetical-not-applied`。不要把 current self/static 写成 fresh dynamic。

## Reverse source-test index

| changed source/semantic edge | mandatory oracle | related regression | unrelated | remaining gap |
|---|---|---|---|---|
| `MAXOPBLOCKS/LOGBLOCKS/NBUF` | | | | |
| `kernel/log.c:commit()` | | | | |
| `mkfs/mkfs.c:nlog` | | | | |

## S/F/B/C/R boundary

| dimension | established conclusion | evidence identity | remaining gap / prohibited claim |
|---|---|---|---|
| S | | | |
| F | | | |
| B | | | |
| C | | | |
| R | | | |

## Final bounded claim

- strongest supported sentence：
- statements deleted as unsupported：
- reruns required before applying the hypothetical change：
