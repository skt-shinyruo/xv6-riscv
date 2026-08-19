# 可扩展性与证据综合

## 问题场景与本单元成果

局部单元可以分别证明一个 waiter 被唤醒、一个资源失败后回收、一次 transaction 在 crash 后重放；
它们不能自动回答“提高容量后，瓶颈移到哪里，哪些证据必须重做”。容量、锁、per-hart state、队列、
cache 和存储串行化共同约束 workload，一处常量变化可能扩大一个池、延长一次扫描、移动 image layout，
却不提高最终吞吐。

本单元不新增 kernel 实现或通用 benchmark。唯一出口是一份**可扩展性与证据综合报告**：它选择一个
假设变更，沿 source -> invariant -> test/resource -> remaining gap 建立双向影响链，并把并发与等待、
内存与资源 ownership、持久化与恢复三类 verified evidence 汇入同一份可复现记录。报告使用
`S/F/B/C/R` 描述证据种类，不把它们合并成形式化证明。

## 前置单元与暂存黑盒

硬前置是[故障注入与源码测试追踪](fault-injection-and-traceability.md)。capacity/owner 词汇由
[全局不变量与资源边界](global-invariants.md)拥有；scheduler、VM、device、filesystem、log 与 recovery
机制仍由各自单元拥有。本页只拥有 workload bottleneck 与 evidence 的跨域综合，不复制局部机制。

以下内容保持为明确 gap/non-goal，而不是当前能力：

- 没有 workload-independent 的“scalability score”、完整 lock graph 或 scheduler fairness proof；
- 没有通用 fault/gate framework，也没有把所有容量池动态耗尽的单一 runner；
- QEMU logical order、host image bytes 与 synthetic tear 仍不能推出真实介质的 FLUSH/FUA 或掉电语义；
- 假设变更只做 source-level impact review，不应用 patch，不生成兼容 image，也不冒充 benchmark 结果。

## 最小模型和关键不变量

### 从 workload 到 bottleneck

先记录 workload 向量，而不是先改常量：

```text
W = (runnable processes, resident pages, open objects, unique dirty blocks,
     in-flight disk requests, crash/restart phase)
K = (NPROC, PHYSTOP, NOFILE/NFILE/NINODE, NBUF, MAXOPBLOCKS/LOGBLOCKS,
     NUM, FSSIZE/MAXFILE)
```

`K` 只给出显式容量。实际 progress 还取决于 owner transfer、锁的竞争区、per-hart scheduler state、
wait predicate、queue descriptor、cache pin、log admission、commit 和 recovery 串行化。扩容审查必须同时
说明：首先饱和的 identity、exact outcome、等待者/producer、回收路径，以及扩容后可能出现的下一个瓶颈。

| workload 压力 | 直接容量/状态 | 串行化或等待边界 | 不能从常量单独推出 |
|---|---|---|---|
| runnable process 增加 | `proc[NPROC]`、`PHYSTOP`、每槽 kernel stack | 每 hart `scheduler()` 扫描、`p->lock`、`wakeup()` 扫描 | wall-clock slowdown、公平性、线性加速 |
| open/create 增加 | `NOFILE`、`NFILE`、`NINODE`、physical pages | private `ofile[]`、`ftable.lock`、inode/cache locks | 所有失败都返回 `-1`，或 fork refs 必然先耗尽 `NFILE` |
| unique dirty blocks 增加 | `NBUF`、`MAXOPBLOCKS/LOGBLOCKS` | `bcache.lock`、buffer sleeplock、`log.lock`、last-`end_op()` commit | `NBUF==LOGBLOCKS` 就有足够 headroom |
| disk request 增加 | `NUM=8`、每请求三个 descriptors | `disk.vdisk_lock`、descriptor wait、IRQ completion/reclaim | 两笔在途等于设备吞吐上限或 DMA ordering proof |
| file/image 增加 | `MAXFILE`、`FSSIZE`、`mkfs` metadata layout | `filewrite()` chunk、log commit、home install | 扩大 transaction 会扩大单文件上限 |
| crash/restart | nonzero header、home blocks、orphan state | `recover_from_log()` 后再 `ireclaim()` | logical crash point 等于物理 sector tear |

### 一处假设变更的完整影响链

本单元固定审查一个**未应用**的变更：`MAXOPBLOCKS 10 -> 11`。pinned source 可静态重算：

1. `LOGBLOCKS` 与 `NBUF` 都从 `10*3=30` 变为 `11*3=33`；这扩大 log header array 和 buffer array。
2. `mkfs/mkfs.c:nlog = LOGBLOCKS + 1` 从 31 变为 34；`inodestart`、`bmapstart`、`freeblock` 随之移动，
   `FSSIZE=2000` 不变，因此旧 `fs.img` 不能作为新配置的 evidence。
3. 空日志时 `begin_op()` 的不等式从 `(outstanding+1)*10 <= 30` 变为
   `(outstanding+1)*11 <= 33`，两者最多都接纳 3 个 outstanding operations；非空 `log.lh.n` 下必须重新
   枚举边界，不能写成“并发度提高”。
4. `filewrite()` 的 chunk 公式从 `((10-4)/2)*BSIZE` 变为 `((11-4)/2)*BSIZE`；C 整数除法使两者都为
   `3*BSIZE=3072`。`user/usertests.c:BUFSZ` 则从 `12*BSIZE` 变为 `13*BSIZE`。
5. `NUM`、`NPROC`、`PHYSTOP`、`MAXFILE` 没有直接变化；更大的 cache/log 仍可能把压力移到 physical
   pages、VirtIO descriptor queue 或 commit serialization。

这五项同时包含 changed/unchanged 关系。遗漏“不变项”会让 reviewer 无法区分真实影响与关键词匹配。

### 三类 evidence domain 与 S/F/B/C/R

| domain | 现有 strongest evidence | 仍保留的边界 |
|---|---|---|
| 并发与等待 | scheduling fixed/broken named events；device 两笔占六 descriptors、第三笔等待；persistence gate capability | 固定 interleaving 不是 fairness/all-interleavings proof；QEMU device 不是硬件 timing model |
| 内存与资源 ownership | `FD_ROLLBACK` 的 `NOFILE-3=13` 与 final AFTER==BASE；filesystem nth fault ledger；cache-full/admission | 未动态耗尽的池、隐藏 fixture assertion、partially-installed fd branch 仍需 source analysis |
| 持久化与恢复 | transaction payload/header/home/clear；crash IDs 10/20/30/40；offline checker 与 second boot | synthetic tear/host bytes 不是 physical durability；外部 recovery candidate 必须单独绑定 |

`S` 是静态 source/invariant；`F` 是正常功能；`B` 是容量、失败或错误输入边界；`C` 是受控并发关系；
`R` 是持久化、crash 或 recovery。字母可以共同支持一条有限 claim，但不能相加成“证明等级”。

`kernel/defs.h` 是跨 C 文件的内部 declaration surface；影响审查要确认改动是否改变 prototype/caller
contract，本练习的常量变化不改变 `filewrite()`、`begin_op()` 或 `log_write()` prototype。
`user/grind.c:go()` 随机混合 process、VM、fd、pipe 与 filesystem 操作，`user/stressfs.c:main()` 让多个
process 重复读写文件。它们适合作为 broad F regression/workload pressure，却没有 stable event、nth fault、
资源账本或 crash point，不能替代本报告的 deterministic B/C/R oracle。

## 源码追踪计划

先 breadth-first 追踪容量派生和 workload path，再进入局部锁或失败 branch：

```sh
rg -n '^#define (NPROC|NOFILE|NFILE|NINODE|MAXOPBLOCKS|LOGBLOCKS|NBUF|FSSIZE)' kernel/param.h
rg -n '^#define PHYSTOP' kernel/memlayout.h
rg -n '^#define (MAXFILE|NDIRECT|NINDIRECT)' kernel/fs.h
rg -n '^#define NUM' kernel/virtio.h
rg -n 'filewrite\(|begin_op\(|log_write\(' kernel/defs.h
rg -n '^proc_mapstacks\(|^scheduler\(|^sleep\(|^wakeup\(' kernel/proc.c
rg -n '^kalloc\(' kernel/kalloc.c
rg -n '^filewrite\(' kernel/file.c
rg -n '^bget\(|^bpin\(|^bunpin\(' kernel/bio.c
rg -n '^begin_op\(|^end_op\(|^log_write\(|^commit\(' kernel/log.c
rg -n '^alloc3_desc\(|^free_chain\(|^virtio_disk_rw\(|^virtio_disk_intr\(' kernel/virtio_disk.c
rg -n 'nlog = LOGBLOCKS|inodestart|bmapstart|freeblock' mkfs/mkfs.c
rg -n 'BUFSZ|MAXOPBLOCKS' user/usertests.c
rg -n '^go\(|^main\(' user/grind.c user/stressfs.c
```

对假设变更建立 forward row：definition -> derived capacity -> owner/wait/serialization -> image/test -> gap；
再从每个 runner/report 反向找回受影响 row。只命中同名常量而没有语义边的 test 是 related-only，不能列为
mandatory oracle。

## 观察任务

1. 从 `W` 选三种 workload：runnable processes、unique dirty blocks、in-flight disk requests。分别标出首先
   触及的 capacity、owner、锁/queue/cache/storage serialization 和 exact exhaustion outcome。
2. 用上面的 command 重算 `MAXOPBLOCKS 10 -> 11` 的五项 changed/unchanged 关系；不得修改源码或 image。
3. 从 verified review 各选一份 concurrency、ownership、recovery raw report，写出 trigger、expectation、
   observation、cleanup 与 strongest bounded claim。
4. 从 `kernel/log.c:begin_op()` 或 `kernel/log.c:commit()` 做反向查询：哪些 oracle 必跑，哪些只是 related
   regression，哪些 gap 在现有 seam 下仍不可验收。
5. 检查 `kernel/defs.h` 的 declaration surface，并把 `grind`/`stressfs` 分类为 related F regression；解释
   为什么它们的随机/压力行为没有提供 stable fault、resource ledger 或 recovery oracle。
6. 运行 publication/static gates，确认报告绑定 current tutorial 而不重跑无关 QEMU：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/xv6-tutorial/resources/persistence/run-lab.py --self-test
PYTHONDONTWRITEBYTECODE=1 python3 docs/xv6-tutorial/resources/persistence/run-lab.py --static-only
PYTHONDONTWRITEBYTECODE=1 python3 docs/xv6-tutorial/resources/recovery/run-project.py --self-test
PYTHONDONTWRITEBYTECODE=1 python3 docs/xv6-tutorial/resources/recovery/run-project.py --static-only
python3 docs/xv6-tutorial/tools/validate.py
python3 docs/xv6-tutorial/tools/generate_navigation.py --check
```

## 有界修改任务

kernel change 为 `N/A`。本单元的有界工作是填写
[报告模板](../resources/evidence-synthesis/report-template.md)中的 `MAXOPBLOCKS 10 -> 11` impact review。
不得应用这项修改，也不得复用旧 `fs.img` 声称新配置通过。报告只复用已经 verified 的 publication seam：

- concurrency/waiting：scheduling 与 device reports；
- memory/resource ownership：global-invariants、filesystem 与 persistence reports；
- persistence/recovery：persistence 与 recovery reports；
- source-test reverse index：fault-injection-and-traceability report。

每个引用记录路径、SHA-256、原配置和 fresh/historical 状态。当前 ticket 只 fresh 运行上面的 self/static 与
publication checks；历史 QEMU report 仍绑定产生它的 fixture/runner/candidate，不能写成 fresh run。

## Oracle、证据、失败路径和局限

| 维度 | 本单元接受的证据 | 明确不能推出 |
|---|---|---|
| S | pinned definition、派生公式、整数除法、source-owner-test 影响链 | runtime path coverage 或性能曲线 |
| F | verified normal reports 与 current self/static compatibility | 假设配置已经 build/boot，或 workload 吞吐提高 |
| B | 现有 exact exhaustion/fault/cache/log oracles 与假设边界表 | `MAXOPBLOCKS=11` 的 live exhaustion outcome |
| C | verified named order、queue wait 与 gate capability | fairness、所有交错、跨 hart 线性扩展 |
| R | verified crash matrix、image bytes、offline/second boot | 新 layout 的 recovery compatibility 或物理 durability |

以下任一情况使综合报告失败：把假设改动写成 applied/current；遗漏 `mkfs` layout 或旧 image 失效；把
`filewrite()` chunk 或空日志 admission 误写为增加；没有三类 evidence domain；缺 baseline/environment/
configuration/image/trigger/expectation/observation/limits；把 historical run 冒充 fresh；缺 source-test reverse
index；或声称 formal proof、fairness、all interleavings、benchmark speedup、FLUSH/FUA/physical durability。

## 退出产物与后续单元

提交一份按[报告模板](../resources/evidence-synthesis/report-template.md)填写并由
[rubric](../resources/evidence-synthesis/rubric.md)复核的报告。至少包含：

- baseline、environment、configuration、image、trigger、expectation、observation、limits；
- workload/capacity/lock/per-hart/queue/cache/storage serialization 表；
- `MAXOPBLOCKS 10 -> 11` 的 source -> invariant -> tests/resources -> gaps changed/unchanged 影响链；
- concurrency、ownership、recovery 三域的 verified artifact provenance；
- S/F/B/C/R strongest bounded claims、reverse rerun index、cleanup 与禁止的 proof claim。

这是当前课程图的最终核心综合单元。完成只表示当前 pinned baseline 路径的增量单元均有 verified 走查；
release/version、coverage completeness 与 parent issue 的最终发布门仍由后续 release ticket 决定。
