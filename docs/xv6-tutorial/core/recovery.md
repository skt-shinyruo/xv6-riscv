# 崩溃恢复与离线一致性

## 问题场景与本单元成果

一次 transaction 修改两个 home blocks。若 QEMU 分别停在 log payload 完成、非零 header 完成、
home installation 完成和 header clear 完成之后，重新启动只能看到完整的 before-state 或完整的
after-state，不能看到 mixed home blocks；再次启动还必须不再 replay。单次 `SIGKILL`、重新进入 shell
或一份最终文件内容都不足以支持这个结论。

本单元接续[Buffer cache、日志与事务](persistence.md)，把 commit boundary 扩展为一个可复核的
recovery evidence project。唯一出口是一份“崩溃恢复与离线一致性报告包”：它同时包含源码追踪、
仓库外 learner candidate digest、四点 crash matrix、两次启动、host image inspection、synthetic tears、
offline consistency、回归、资源清理和证据局限。

## 前置单元与暂存黑盒

硬前置是 `core.persistence`。该单元已经解释 `write_log()`、第一次 `write_head()`、
`install_trans()` 和第二次 `write_head()` 的 logical order，本单元不重复 buffer identity、reservation
或 log absorption。相关背景来自[文件系统命名、inode 与数据路径](filesystem.md)中的 inode/block
ownership，以及[设备中断与 VirtIO 队列](device-io.md)中的 request completion。

本单元解除 `crash-recovery-and-offline-consistency`，但仍不声称：

- QEMU 的 `bwrite()` return 等于真实硬件已越过 volatile host/controller cache；
- 六种 synthetic mutation 穷尽 sector tearing、write reordering 或所有介质故障；
- audit hook、fixture 或 `fsck.py` 是 production recovery design；
- 有限 image 和 crash epoch 构成形式化 crash-consistency proof。

这些不是本单元可偷偷扩大范围的 black boxes；报告必须把它们保留为 evidence limits。

## 最小模型和关键不变量

### 四个稳定 crash point

point ID 是 versioned logical ABI，不是源码行号。learner candidate 必须把 hook 放在同步写返回之后，
且 marker 发布后冻结当前 QEMU；host 只 kill 已验证 PID 的那个 QEMU process。

| `logical_id` | `point` | 精确边界 | crash header | crash home | phase |
|---:|---|---|---:|---|---|
| 10 | `LOG_DATA_COMPLETE` | `write_log()` 已返回，第一次 `write_head()` 尚未开始 | 0 | before/before | precommit |
| 20 | `COMMIT_HEADER_COMPLETE` | 非零 `write_head()` 已返回 | 2 | before/before | postcommit |
| 30 | `HOME_INSTALL_COMPLETE` | `install_trans(0)` 已返回，header 尚非零 | 2 | after/after | postcommit |
| 40 | `HEADER_CLEAR_COMPLETE` | `lh.n=0` 的 `write_head()` 已返回 | 0 | after/after | postcommit |

当前 fixture 使用 blocks `1990/1991` 和同一 positive epoch 的完整 block pattern。runner 在开始前必须
从 bitmap 与 inode ownership 独立确认这两个 block 可用于 private image；固定 block number 本身不是
安全证明。

commit rule 只有一条：point 10 尚未 committed，因此恢复后只能是 before/before；point 20、30、40
已经 committed，因此恢复后只能是 after/after。任一 block 内 half-before/half-after，或两个 blocks
分别 before/after，都是 mixed state，不能被“至少一个新值可见”放过。

### Recovery、clear 与第二次启动

首进程第一次进入 `forkret()` 后调用 `fsinit()`。本分支的顺序是：

```text
forkret()
  -> fsinit(ROOTDEV)
     -> readsb()
     -> initlog()
        -> recover_from_log()
           -> read_head()
           -> install_trans(1)
           -> lh.n = 0
           -> write_head()
     -> ireclaim()
  -> kexec("/init", ...)
```

因此首启 oracle 不只检查 shell prompt。它要在 private image 上复核：

1. `recovery_seen_n` 与 crash point 一致；
2. nonzero header 恰好 replay 两个 targets，zero header 不 replay；
3. replay 后 on-disk header `n==0`；
4. 两个 home blocks 同为允许的 before 或 after；
5. QEMU orderly stop 后 offline consistency 通过。

随后对同一 image 第二次启动。第二次必须观察 `seen_n==0`、`installed==0`，home bytes 与首启后相同，
且 image SHA-256 不再因 recovery 改变。这是本项目的 idempotence oracle；重复进入 shell 不是替代品。

### Redo recovery 与 orphan reclaim 是两个修复阶段

`recover_from_log()` 只把 committed redo payload 复制到 header 指定的 home blocks。它不知道 crash 前
哪些 `struct inode.ref` 属于已经消失的 process。若 `sys_unlink()` 已 committed，把 directory entry
清除并把 on-disk `nlink` 变为 0，但一个 open reference 使 `iput()` 尚未执行最终 `itrunc()`，crash
会让 volatile reference 消失，却留下 `type!=0 && nlink==0` 的 allocated inode。

`ireclaim()` 必须在 log replay/clear 之后扫描 on-disk dinodes；它通过 `iget()` 建立临时 reference，
再在新的 `begin_op()/end_op()` transaction 中让 `iput()` 执行 `itrunc()`、`type=0` 和 bitmap cleanup。
所以 redo atomicity 不能推出 namespace reachability 或 orphan cleanup，offline checker 必须在完整启动
和 reclaim 后重新检查两者。

### 三类证据不能互相冒充

| profile | 操作与 observable | 能支持 | 不能支持 |
|---|---|---|---|
| QEMU logical order | hook 位于四个 synchronous return boundary；host kill exact QEMU PID | 当前 xv6/QEMU 路径在命名边界的 before/after 与 replay order | host 或真实介质 durability |
| host persistence | QEMU 已退出后由 host 重新打开 private image，解析 header/log/home 并比较 digest | 该 host-visible image 中可重读的 bytes | power-loss、cache flush 或 sector atomicity |
| synthetic tears | 只在 crash image copy 上执行声明式 mutation，再由相同 oracle 接受或拒绝 | oracle 能识别指定 half write、stale header 和非法 metadata profile | 故障概率、真实设备 write order 或未建模 tear |

六个 synthetic profiles 固定在 `scenarios.json`：`header-zero` 与 `payload-half` 必须被 postcommit
before/mixed oracle 拒绝；`home-half` 必须由仍非零的 committed header 修复；`header-restore` 必须允许
重复 redo 后仍为 after 且二启幂等；`invalid-count` 与 `invalid-target` 必须由 offline parser 拒绝，
不得把不可信 header 交给 kernel 启动。

### `fsck.py` 的边界

`resources/recovery/fsck.py` 是 publication acceptance oracle。它只读 xv6 image，校验 superblock/log
geometry，在内存中的 shadow copy 上 replay 合法 committed log，再复核 inode types/sizes/blocks、
bitmap ownership、directory `.`/`..`、reachability、link counts、duplicates 和 orphan。输入 SHA-256
必须保持不变。

它不是 learner authoritative answer、不是要求复制的实现，也不是 production fsck。learner candidate
是仓库外 crash-hook patch；candidate 不能修改 scenario、fixture、runner 或 `fsck.py` 来使自身通过。
正常首启后的 acceptance 还要求 `log_pending==false`、header count 为 0、recovered view clean；仅有
shadow replay 后 clean 不等于运行后的 image 已 clear/reclaim。

## 源码追踪计划

先画 normal commit，再画 boot recovery，最后连接 inode reclaim 与 offline layout：

```sh
rg -n '^write_log\(|^write_head\(|^install_trans\(|^commit\(' kernel/log.c
rg -n '^read_head\(|^recover_from_log\(|^initlog\(' kernel/log.c
rg -n '^fsinit\(|^ireclaim\(|^iput\(|^itrunc\(|^iupdate\(' kernel/fs.c
rg -n '^sys_unlink\(' kernel/sysfile.c
rg -n '^forkret\(' kernel/proc.c
rg -n 'struct logheader|struct superblock|struct dinode|IBLOCK|BBLOCK' kernel/log.c kernel/fs.h
rg -n '^virtio_disk_rw\(|^virtio_disk_intr\(' kernel/virtio_disk.c
rg -n 'RC_LOG_DATA_COMPLETE|RC_COMMIT_HEADER_COMPLETE|RC_HOME_INSTALL_COMPLETE|RC_HEADER_CLEAR_COMPLETE' \
  docs/xv6-tutorial/resources/recovery/recovery-audit.patch
```

按[恢复问题链](../questions/recovery.md)先完成 breadth-first flow，再深入四点 matrix、首启/二启、
三类 evidence profile、offline invariants 与 orphan reclaim。不要从 fixture 的 helper 名称反推 production
API；source truth 始终是 manifest pinned baseline 与仓库外 candidate 的实际 diff。

## 观察任务

先记录 release/candidate/resource identity，并验证 publication inputs：

```sh
rg -n '"baseline_commit"|"id": "project.crash-recovery"' docs/xv6-tutorial/curriculum.json
git rev-parse HEAD
python3 -m json.tool docs/xv6-tutorial/resources/recovery/scenarios.json >/dev/null
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/xv6-tutorial/resources/recovery/run-project.py --self-test
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/xv6-tutorial/resources/recovery/run-project.py --static-only
```

worksheet 至少追踪一次 point 20：从 after pattern 进入 `log_write()`，到两个 log data completion、
nonzero header completion、host exact-PID kill、crash image header/home/log bytes、首启 `seen_n=2`、
replay/clear、`ireclaim()`、首启后 offline result，再到二启 `seen_n=0`。每个时点记录 source edge、
observable、允许副作用和资源 owner。

## 有界修改任务

learner 在仓库外实现一个 candidate patch，把 ABI 1 的 stable points 接到四个 exact boundaries。published
`recovery-audit.patch` 只提供 private transaction driver、pattern、marker 和 audit adapter；它不是
完整 learner answer。candidate 的职责是：

- 使用 logical IDs `10/20/30/40`，且只在对应 synchronous operation 返回后触发一次；
- 保持正常运行未 arm 时的 commit/recovery 语义；
- marker 完整发布后冻结，等待 host 对 exact QEMU PID 执行 kill；
- 不把 hook、test syscall 或 fixture user program 放入 executable baseline；
- 不修改 `scenarios.json`、`run-project.py`、`fsck.py` 或 fixture 来放宽 oracle。

完整运行使用独立 baseline export 和每 case 一份 private image：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/xv6-tutorial/resources/recovery/run-project.py \
  --candidate /tmp/recovery-candidate.patch \
  --report /tmp/xv6-recovery-report.md
```

runner 应先以未 arm 的 candidate build 运行正常路径，再执行四个 crash points、首启/二启、六个
synthetic profiles、offline corruption self-test、focused filesystem/log regression、quick `CPUS=2`
和 full `CPUS=1`。candidate path/digest 进入报告，完整 patch 不复制进教程。

## Oracle、证据、失败路径和局限

| 维度 | trigger | 精确 oracle | 允许副作用与最终资源结果 |
|---|---|---|---|
| S | pinned source、candidate diff、scenario/ABI | 四个 hook 位于 exact return edges；10 precommit，20/30/40 postcommit；candidate scope 闭合 | 只改临时 export；reverse 后源码 snapshot 相同 |
| F | unarmed transaction、首启与二启正常完成 | marker schema、pattern/epoch、boot path、header/home/log parse 与 normal regressions 一致 | private image 可变；所有 QEMU/process/PID handles 回收 |
| B | 六个 synthetic tears 与 offline corruption mutations | 错误 postcommit/mixed/非法 log 被拒绝；repairable stale home/header 得到 after；checker 精确诊断 bitmap/duplicate/link/orphan | mutation 只作用于 disposable copies；危险 header 不启动 |
| C | N/A | project 固定 `CPUS=1` 以隔离 crash order；不做并发 claim | quick `CPUS=2` 仅是 regression |
| R | 四点 exact-PID crash、首启、二启、host reopen | 10 -> before；20/30/40 -> after；无 mixed；首启 header 0/clean；二启 seen/install 0 且 image stable | 每 case 独立 image；共享 `fs.img` 与 checkout digest 不变 |

任何以下结果都直接失败：marker 到达前后使用 timeout 猜 crash；kill make/driver process 而非已验证
QEMU PID；复用前一 case 的 image；postcommit 得到 before 或 mixed；首启后 header 非零；二启再次 replay；
offline checker 改写 input；把 recoverable shadow view 当作已运行 `ireclaim()`；把 host-visible bytes 写成
physical durability；或让 candidate/fixture 泄漏进 baseline。

## 退出产物与后续单元

按[报告模板](../resources/recovery/report-template.md)提交一份报告包，至少包含：

1. pinned baseline、tutorial commit、candidate/fixture/runner/scenario/fsck/report SHA-256；
2. 四个 stable point 的 crash/header/log/home 与首启结果 matrix；
3. 每点首启 replay/clear/offline 结果和同一 image 的二启 idempotence；
4. QEMU logical order、host persistence、synthetic tear 三张互不替代的证据表；
5. offline geometry/log/block/bitmap/directory/link/orphan 结果，以及 redo 与 `ireclaim()` 的责任边界；
6. focused/quick/full、QEMU/PID/private image/temp tree/reverse patch/shared-state cleanup；
7. S/F/B/C/R 结论、不能推出的 durability/tear coverage/formal proof，以及 non-author walkthrough 签名。

本单元只迁移旧题 `FS-09` 到 canonical `RECOVERY-09`；其他旧题不复制、不维护同步答案。完成独立
walkthrough、diff review、validator 与 generated-navigation check 后，后续 evidence 单元可以复用这份
报告中的 recovery contract，但不能把它扩大成全局正确性证明。
