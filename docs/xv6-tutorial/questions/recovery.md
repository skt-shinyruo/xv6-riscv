# 崩溃恢复与离线一致性问题

学习目标：先从 normal commit 与 boot recovery 建立全景，再沿四个 stable point 复核 before/after、
首启/二启和 offline ownership，最后区分 QEMU logical order、host persistence 与 synthetic tears。
硬前置是[Buffer cache、日志与事务](../core/persistence.md)。每题都要回到 pinned source、raw image
fields 和报告关系；shell prompt、timeout、guest `PASS` 或固定 digest 不能单独作答。

## 高层总览

### RECOVERY-00 一次 crash/restart 要跨过哪些状态边界？

从 `kernel/log.c:commit`、`kernel/log.c:recover_from_log`、`kernel/fs.c:fsinit` 和
`kernel/proc.c:forkret` 开始，画出 cache、on-disk log data/header、home blocks、QEMU process、host
image 与 rebooted kernel 的对象图。哪些状态会随 process 消失，哪些 bytes 会由下一次启动消费？

## 流程骨架

### RECOVERY-01 normal commit 与 boot replay 怎样汇合？

breadth-first 追踪 `write_log -> write_head(nonzero) -> install_trans(0) -> write_head(zero)`，再追踪
`read_head -> install_trans(1) -> write_head(zero)`。先列全部阶段、header `n` 和 data movement，不先
深入 hook；指出 normal path 与 recovery path 共享和不共享哪些动作。

### RECOVERY-02 四个 logical IDs 怎样锚定 crash matrix？

从 `scenarios.json` 的 `10/20/30/40` 和 candidate diff 定位 `LOG_DATA_COMPLETE`、
`COMMIT_HEADER_COMPLETE`、`HOME_INSTALL_COMPLETE`、`HEADER_CLEAR_COMPLETE`。逐项说明 hook 为什么
必须在 synchronous call return 之后，以及源码重排行号为何不能改变 logical ID。

## 局部深入

### RECOVERY-03 point 10 为什么只能恢复到 before-state？

给定 log payload 已是 after、header `n==0`、两个 home blocks 都是 before，沿 `read_head()` 与
`install_trans(1)` 预测首启 `seen_n/installed`、header 和 home。为什么 payload bytes 存在仍不表示
transaction committed？

### RECOVERY-04 points 20、30、40 为什么都只能得到 after-state？

分别从 header=2/home=before、header=2/home=after、header=0/home=after 出发追踪首启。解释 point 30
重复 install 为什么安全，point 40 为什么无需 replay，以及任何 before/mixed 结果违反了哪个 commit
invariant。

### RECOVERY-05 第二次启动怎样把 replay idempotence 变成可观察关系？

在同一 private image 上比较首启前、首启 orderly stop 后和二启 stop 后的 header、home pattern、
`seen_n/installed` 与 SHA-256。哪些字段必须为 0，哪些 digest 必须相等？为什么“第二次仍能启动”
比这些关系弱？

### RECOVERY-06 exact-PID kill 与 image isolation 解决了什么归因问题？

从 QEMU marker、process PID/pidfd、case image copy 和 host reopen 记录解释 trigger 与 observable 的
先后。若 kill 的是 `make`、复用前一 case image、marker 前超时或读取共享 `fs.img`，分别失去了哪条
因果关系或污染保护？

### RECOVERY-07 三类 persistence evidence 为什么不能合并？

比较 QEMU logical completion、QEMU 退出后 host 可重读 image 和 disposable copy 上的 synthetic
mutation。对每类填写它的输入、observable、oracle 和不能推出；特别解释为何 `bwrite()` return、
host SHA-256 和 `payload-half` 都不能单独证明 physical power-loss durability。

### RECOVERY-08 offline checker 如何从 raw image 构造可拒绝的 shadow view？

沿 `kernel/fs.h:struct superblock/struct dinode/struct dirent` 与 `resources/recovery/fsck.py` 的 geometry、
log parse、shadow replay、inode/block、bitmap、directory、reachability 和 nlink 检查建立流程。比较
`raw_clean`、`recovered_clean`、`log_pending`，说明为什么 post-recovery acceptance 还要求 header 0
与 input digest 不变；为什么 invalid count/target 必须 offline 拒绝而不能先 boot？

## 横切 ownership 与失败路径

### RECOVERY-09 日志恢复后为什么还要执行 `ireclaim()`？

这是旧题 `FS-09` 的唯一 canonical 迁移。构造一个 open file 已被 `sys_unlink()` committed、on-disk
`nlink==0`，但最后 `iput()` 尚未执行即 crash 的流程。沿 `sys_unlink -> iupdate -> commit` 和首启
`fsinit -> initlog/recover_from_log -> ireclaim -> iget -> iput -> itrunc/iupdate`，解释 redo 为什么只能
恢复 transaction bytes，不能从磁盘重建已消失的 volatile inode references；再列出 reclaim transaction
对 inode type、size/addrs 和 bitmap 的资源结果。

### RECOVERY-10 六个 synthetic profiles 分别检验哪条 oracle？

从 `header-zero`、`payload-half`、`home-half`、`header-restore`、`invalid-count`、`invalid-target` 的
source image 和 operation 出发，预测接受/拒绝、首启 before/after/mixed、offline diagnostic 与是否允许
boot。解释 why negative oracle self-test 不是 kernel runtime recovery evidence。

## 全流程串联

### RECOVERY-11 如何独立复核一份 recovery evidence report？

只复用 RECOVERY-00..10：选择 point 20，串起 candidate hook、after transaction、exact-PID kill、host
header/log/home parse、首启 replay/clear/`ireclaim()`、offline fsck、二启 idempotence，再对 point 10 与
三个 postcommit points 做 matrix 比较。最后把 S/F/B/C/R、candidate/resource digests、focused/quick/full、
private image 与共享状态 cleanup 放入同一报告，并明确 durability、tear coverage 和形式化证明仍开放。

## 提交边界

答案只进入一份 recovery report bundle；完整 learner candidate 保存在仓库外。`fsck.py` 是只读
publication acceptance oracle，不是 learner authoritative answer 或 production fsck。本页只 canonicalize
旧题 `FS-09`；不复制其他旧题，也不建立同步答案副本。
