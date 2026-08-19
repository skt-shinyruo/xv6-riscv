# 崩溃恢复与离线一致性答案与证据标准

本页回答[崩溃恢复与离线一致性问题](../recovery.md)。答案中的 point、image 和 digest 都必须来自
同一 pinned baseline 的原始记录；固定结论不能替代报告中的 raw evidence。

## RECOVERY-00

normal transaction 的 dirty buffer、in-memory `log.lh`、`outstanding/committing`、process、open file
和 `struct inode.ref` 都属于 QEMU process 内存，kill 后消失。on-disk log payload、header 和 home
blocks 位于该 case 的 private image；QEMU 退出后 host 可重新打开并解析这些 bytes。下一次启动先建
新 cache，首进程在 `forkret()` 中调用 `fsinit()`；`initlog()` 经 `recover_from_log()` 消费 header
和 payload，clear 后 `ireclaim()` 扫描 on-disk inodes，最后才 `kexec("/init", ...)`。reboot 不会消费
旧 cache 或旧 references，只能以 image 中可识别的 committed state 重建状态。

## RECOVERY-01

normal `commit()` 先由 `write_log()` 把 pinned home-buffer bytes 写到 `log.start+tail+1`；第一次
`write_head()` 把 nonzero `n` 和 targets 写到 `log.start`，其 `bwrite()` 返回后 transaction 才
committed。`install_trans(0)` 把 payload 复制到 home blocks 并 `bunpin()`，随后 `lh.n=0` 的第二次
`write_head()` clear header。

boot recovery 不再生成 payload：`read_head()` 从 disk 装入 `log.lh`，`install_trans(1)` 复用同一
copy-to-home loop，但新 cache 中没有 normal commit pins，所以不 `bunpin()`；最后同样令 `lh.n=0`
并 `write_head()`。两条路径共享 install 与 clear，只有 normal path 执行 `write_log()` 和 nonzero
header publish。

## RECOVERY-02

ABI 1 的语义锚点是：

- `10 LOG_DATA_COMPLETE`：`write_log()` 返回后；
- `20 COMMIT_HEADER_COMPLETE`：nonzero `write_head()` 返回后；
- `30 HOME_INSTALL_COMPLETE`：`install_trans(0)` 返回后；
- `40 HEADER_CLEAR_COMPLETE`：zero-header `write_head()` 返回后。

每个 return 表示对应同步 `bwrite()` 路径已经观察 completion；把 hook 放在 call 前只能证明 submit
或部分 loop，不能支持该 point 的 matrix。`scenarios.json` 用 `logical_id/point` 绑定语义而不是行号，
所以重排源码可以移动 hook，却不能重新解释 ID。candidate review 必须逐点检查实际 call edge。

## RECOVERY-03

point 10 的 crash image 是 header `n=0`、homes `before/before`，虽然两个 log payload 已是 after。
首启 `read_head()` 得到 0，`install_trans(1)` 循环零次，因此 `seen_n=0`、`installed=0`；clear 再写 0，
homes 仍为 before。log data block 只是无 header 引用的残留 bytes；redo protocol 以 nonzero header
作为 transaction publish record，不能从“payload 看起来完整”推断 commit。故 precommit oracle 只接受
before/before，且拒绝 after 或 mixed。

## RECOVERY-04

point 20 有 header `n=2`、after payload、homes `before/before`；首启看到 2，按 targets 安装两个
payload，得到 after/after，再 clear。point 30 仍有 header 2，但 homes 已 after/after；replay 重写
相同完整 block，因此结果仍为 after/after，再 clear。point 40 已 header 0、homes after/after，首启
不 replay，结果不变。

因此 first recovery 的 `seen_n` 依次为 `2/2/0`，三个 postcommit points 都只接受 after/after。
point 20 得到 before 表示已发布 transaction 丢失；point 30/40 得到 before 表示已安装 state 回退；
任一 byte-mixed 或 block-mixed 都违反一个 committed transaction 对所有 targets 的 before-or-after
原子可见性。

## RECOVERY-05

first boot 后每个 case 都必须 header `n=0`、homes 等于该 point 的允许 final state，offline result
clean。随后原样启动同一 image：second marker 必须 `seen_n=0`、`installed=0`，header 仍为 0，两个
完整 pattern 不变；first orderly-stop SHA-256 必须等于 second orderly-stop SHA-256。crash image 与
first-stop digest 不要求相等，因为 first boot 可能合法 replay/clear。第二次出现 shell 只说明系统
仍能运行，不能排除又 replay、重写错误 target 或产生相同用户可见文件但不同 metadata。

## RECOVERY-06

host 从自己启动的 QEMU `Popen` 保留 exact PID/pidfd，先从该 process stdout 解析完整 crash marker，
再 kill 并 wait，之后才 reopen image。这个顺序把“哪个 semantic point 已完成”与“哪一个 process
停止写 image”绑定起来。kill `make` 或 wrapper 可能让 QEMU 继续执行 install/clear；marker 前 timeout
只说明 watchdog 到期，不知道 crash edge。复用前一 case image 会把旧 header/home/epoch 带入下一
oracle；读取共享 `fs.img` 既不能归因到本 case，还会污染 executable checkout。每点必须从 pristine
image 的独立 copy 开始，最终比较共享 image/worktree/index digest。

## RECOVERY-07

QEMU logical-order evidence 来自 hook 位于 synchronous return 后以及 marker-before-kill；它支持当前
virtual device 路径中各阶段的顺序。host-persistence evidence 来自 QEMU 已退出后重新打开 private
image并读到 header/log/home bytes；它只支持该 host file 此时可重读。synthetic-tear evidence来自
disposable copy 上的精确 mutation 以及同一 host oracle 的接受或拒绝；它支持 oracle 能识别这六种
profile。

`bwrite()` return 不包含真实 controller/cache flush contract，host SHA-256 不模拟突然掉电，手工
`payload-half` 也不说明硬件发生概率或 write ordering。因此三类记录必须分栏，不能合并成 physical
power-loss durability、sector atomicity 或完整 tear-space 结论。

## RECOVERY-08

`fsck.py` 先按 `struct superblock` 检查 magic、image size、log/inode/bitmap geometry；再限制 header
count，拒绝重复或越界 target。合法 committed log 只 replay 到内存 shadow copy并把 shadow header
置 0。随后两种 view 都按 `struct dinode` 的 type/size/direct/indirect mapping 建 block-owner 表，
检查 holes/trailing blocks、region、duplicates、metadata/data bitmap，再解析 `struct dirent` 的 names、
`.`/`..`、parent/cycle、reachability 和 `nlink/orphan`。

`raw_clean` 描述未 replay image，`recovered_clean` 描述 shadow，`log_pending` 直接来自 raw header；
`clean` 采用 recovered view。运行后验收比“可恢复”更强：必须 `recovered_clean=true`、
`log_pending=false`、header count 0，且 checker 的 input SHA-256 前后相同。invalid count 返回
`E_LOG_COUNT`，log-region target 返回 `E_LOG_TARGET`；这类不可信 header 不交给会按 `lh.n/targets`
执行 kernel copy loop 的 boot path。

## RECOVERY-09

open file 的 file object 持有 inode reference。`sys_unlink()` 可以在同一 transaction 中清 directory
entry、令 on-disk `nlink` 变 0 并 commit；由于仍有 open reference，末尾 `iput()` 不满足
`ref==1 && valid && nlink==0`，所以不会 `itrunc()`。若此时 crash，volatile file/inode references
消失，但 image 留下 `type!=0 && nlink==0`、仍占用 addrs/bitmap 的 orphan。

`recover_from_log()` 只按 committed header 重放 bytes，不知道 crash 前有哪些 references，也不能从
`nlink==0` 判断 normal execution 是否还会 close。启动时 replay/clear 完成后，`ireclaim()` 扫描
dinodes；对 orphan 用 `iget()` 建立唯一临时 reference，在新的 `begin_op()/end_op()` 中
`ilock/iunlock/iput`。此时 `iput()` 触发 `itrunc()`：释放 direct/indirect blocks、清 size/addrs 并
`iupdate()`，随后写 `type=0`。最终 inode 和 bitmap allocation 回收。reclaim 必须在 replay 后，因为
redo 可能刚发布 `nlink==0` 或相关 metadata。

## RECOVERY-10

- `header-zero`：在 point 20 copy 上把 count 置 0；boot 留下 before。它是 postcommit case，必须拒绝。
- `payload-half`：在 point 20 copy 上把第一个 payload 的一半换成 before；header 2 导致 mixed home，
  必须拒绝。
- `home-half`：在 point 30 copy 上把第一个 home 的一半换成 before；保留的 header 2 与 after payload
  必须把它修复成 after/after。
- `header-restore`：在 point 40 copy 上恢复 committed header；重复 redo 仍得 after，clear 后 second
  boot `seen_n=0` 且 digest 相同。
- `invalid-count`：把 count 设为 `LOGBLOCKS` 之外；offline 精确拒绝 `E_LOG_COUNT`，不 boot。
- `invalid-target`：把第一个 target 指向 log region；offline 精确拒绝 `E_LOG_TARGET`，不 boot。

前四项检验 before/after、mixed rejection 和 redo idempotence，后两项检验 trust boundary。它们是
host parser/oracle 的 negative self-test；只有真实 QEMU point run 才构成当前项目的 runtime recovery
evidence。

## RECOVERY-11

独立 reviewer 先固定 baseline、tutorial、candidate、fixture、runner、scenario、fsck 和 report digest，
再确认 candidate 的 point 20 hook 位于 nonzero `write_head()` return 后。fixture 写入同 epoch 的 after
transaction；host 收到 marker后 kill exact QEMU PID，reopen 独立 image，必须看到 header 2、targets
`1990/1991`、after payload和 homes before。first boot 必须 `seen/installed=2/2`、homes after、header 0、
offline clean 且无 pending log；second boot 必须 `0/0`，first/second stop digest 相等。

同一方法扩展到完整 matrix：point 10 是 precommit -> before；points 20/30/40 是 postcommit -> after；
四者都禁止 mixed。报告再绑定六个 synthetic profile、offline corruption rejection、focused/quick/full
regressions，以及 exact PID、private images、temporary export、reverse patch 和共享状态 cleanup。
`S` 支持 source/ABI/scope，`F` 支持 unarmed 与正常 boot，`B` 支持命名 mutation rejection/repair，
`C=N/A`，`R` 支持这四个 point 的 replay/clear/idempotence。结论仍不包括 physical durability、所有
tears、并发 recovery 或形式化证明；完整 learner candidate 始终留在仓库外。
