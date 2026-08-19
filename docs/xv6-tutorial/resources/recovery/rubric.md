# 崩溃恢复与离线一致性验收 rubric

## 交付边界

学习者提交一份 recovery evidence report bundle。完整 learner candidate 是仓库外 patch；报告只记录
absolute path 和 SHA-256，不把实现复制进公开资源。`recovery-audit.patch`、`run-project.py`、
`scenarios.json` 与 `fsck.py` 是 publication/non-author walkthrough 的 audit seam：fixture 提供 transaction
driver、pattern 与 marker，runner 建立 isolated QEMU/images，`fsck.py` 只读复核 image。它们都不是
production code 或 authoritative learner answer。

candidate 不能修改 scenario、fixture、runner、`fsck.py` 或 baseline image。host 必须从 raw marker 和
image bytes 独立重算 header/log/home/replay 关系；不接受 guest 自报 `PASS`、shell prompt、timeout 或
固定 image digest 作为 correctness oracle。

## Stable point ABI 与 crash matrix

ABI 1 只有四个 crash points：

| ID | point | phase | crash `header.n` | crash home | first recovery `seen_n` | recovered home |
|---:|---|---|---:|---|---:|---|
| 10 | `LOG_DATA_COMPLETE` | precommit | 0 | before/before | 0 | before/before |
| 20 | `COMMIT_HEADER_COMPLETE` | postcommit | 2 | before/before | 2 | after/after |
| 30 | `HOME_INSTALL_COMPLETE` | postcommit | 2 | after/after | 2 | after/after |
| 40 | `HEADER_CLEAR_COMPLETE` | postcommit | 0 | after/after | 0 | after/after |

hook 必须位于对应 synchronous function return 后；同一 armed epoch 只触发一次。marker 必须至少让
host 验证 ABI、point、hart、epoch、count、两个 home block 与 fired state。marker 完整发布后才能
冻结；host 通过已验证 PID/pidfd kill exact QEMU process 并 wait，不能 kill `make`、shell wrapper 或
不明 child。

每个 case 从同一 pristine private image 的独立 copy 开始。runner 必须先证明 blocks `1990/1991`
在该 image 的 bitmap 中 free 且没有 inode owner，再允许 fixture 使用。case 间不得继承 header、home、
QEMU process 或 boot transcript。

## First boot、second boot 与 offline oracle

每个 crash case 的 first boot 必须复核：

- recovery marker 的 `seen_n/installed/cleared` 与上表一致；
- on-disk header 变为 `n==0`；
- 两个 home blocks 都是允许的 complete pattern，不得 block-mixed 或 byte-mixed；
- point 10 只有 before，points 20/30/40 只有 after；
- orderly stop 后 `fsck.py` 返回 `clean=true`、`recovered_clean=true`、`log_pending=false`，且 checker
  input SHA-256 前后相同；
- `fsinit()` 中 log replay/clear 先于 `ireclaim()`，offline result 取完整启动后的 image。

随后原样重启同一 image。second boot 必须 `seen_n=0`、`installed=0`，header/home/offline result 不变，
且 first-stop 与 second-stop image SHA-256 相等。只比较文件内容、只启动一次或第二次只有 prompt 都退回。

## Synthetic tears 与 corruption rejection

`scenarios.json` 的六个 profile 是闭集：

| id | source/operation | 通过标准 |
|---|---|---|
| `header-zero` | point 20 image 的 header count 置零 | 启动后得到 before，postcommit oracle 必须拒绝 |
| `payload-half` | point 20 image 的第一个 log payload 做 half tear | replay 后出现 byte-mixed，oracle 必须拒绝 |
| `home-half` | point 30 image 的第一个 home block 做 half tear，committed header 保留 | first boot 重放后两个 homes 都为 after，offline clean |
| `header-restore` | point 40 image 恢复 point 30 的 committed header | 重复 redo 后仍为 after；clear 且 second boot 幂等 |
| `invalid-count` | header count 改为超出 `LOGBLOCKS` | `fsck.py` 精确拒绝 `E_LOG_COUNT`，不得 boot |
| `invalid-target` | 第一个 target 指向 log region | `fsck.py` 精确拒绝 `E_LOG_TARGET`，不得 boot |

oracle self-test 还必须让 checker 在 disposable copies 上拒绝 geometry、duplicate ownership、bitmap
missing/leak、directory `.`/`..`、reachability/nlink 与 orphan corruption。negative mutation 证明 parser/
oracle 会拒绝坏证据，不是 QEMU runtime 或真实介质故障证据。

## Evidence dimensions

- `S`：pinned source anchors、candidate path scope、point ABI、commit/recovery matrix、filesystem geometry
  与 redo/`ireclaim()` ownership contract。
- `F`：unarmed transaction、四次 first boot 和四次 second boot 均正常结束；marker/image fields 由 host
  parse，focused filesystem/log regressions 通过。
- `B`：六个 synthetic profiles 与 offline corruption mutations 精确接受/拒绝；危险 header 不 boot。
- `C`：N/A。crash cases 固定 `CPUS=1`；quick `CPUS=2` 只提供 regression，不支持并发 recovery claim。
- `R`：四个 exact-PID crash point 的 before/after、non-mixed home、header clear、offline clean 与
  idempotent restart；三种 evidence profile 分栏报告。

## Redo、orphan 与 resource ledger

报告必须解释：`recover_from_log()` 按 committed header redo blocks；它不持有 crash 前的
`struct inode.ref`。`ireclaim()` 扫描 `type!=0 && nlink==0` dinodes，并在新 transaction 中通过
`iput()/itrunc()/iupdate()` 回收 inode 和 blocks。仅有 header clear 不能推出 orphan-free，只有
shadow replay clean 也不能替代完整启动后的 offline check。

每个 case 结束必须满足：

```text
qemu_processes=0 pid_handles=0 armed_hooks=0
header_n=0 mixed_blocks=0 log_pending=0
private_images_removed=1 temporary_export_removed=1
candidate_reversed=1 fixture_reversed=1 source_snapshot_equal=1
shared_worktree_equal=1 shared_index_equal=1 shared_fs_image_equal=1
```

valid bytes 可留在本 case 的 image，直到该 disposable image 被清理；它们不能泄漏到共享 `fs.img`。

## Commands、regressions 与评定

```sh
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/xv6-tutorial/resources/recovery/run-project.py --self-test
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/xv6-tutorial/resources/recovery/run-project.py --static-only
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/xv6-tutorial/resources/recovery/run-project.py \
  --candidate /tmp/recovery-candidate.patch \
  --report /tmp/xv6-recovery-report.md
```

完整运行必须包含 candidate/fixture build、unarmed focused case、四点 crash/restart、六个 synthetic
profiles、offline oracle self-test、focused filesystem/log tests、quick `CPUS=2` 与 full `CPUS=1`
regression。每项记录命令、exit status、raw transcript/image/report digest；timeout 只用于 watchdog。

- **通过**：上述 matrix、first/second boot、offline/synthetic、S/F/B/R、回归与 cleanup 全部成立，
  单一报告包附机器证据 digest，并有 non-author walkthrough 与 independent review。
- **退回**：precommit after、postcommit before、任意 mixed、二启 replay、危险 image boot、candidate
  越界、checker 修改 input、复用 image、共享状态变化，或把 QEMU/host/synthetic 任一层写成 physical
  durability/formal proof。

reviewer 至少独立重算 points 10/20、`payload-half`、`header-restore`、一个 bitmap/orphan rejection 和
second-boot digest；若必须借助 rubric 之外的同步答案副本才能通过，交付无效。
