# Buffer cache、日志与事务答案与证据标准

## PERSIST-01

`filewrite()` 用 `begin_op()/end_op()` 包住每个 bounded chunk；`writei()` 取得并修改 home block
buffer，`log_write()` 只把 block number 加入 in-memory `log.lh` 并 pin 当前 cache identity。
最后一个 outstanding operation 触发 commit：cache bytes 先写 log data blocks，非零 header 再
发布 transaction，随后复制到 home blocks、解除 pins，最后把 header 写成 0。`virtio_disk_rw()`
是同步 request/completion 边界，但 completion 不等于物理介质断电持久性。

## PERSIST-02

报告使用 `CACHED_DIRTY -> INTENT_PINNED -> LOG_PAYLOAD_COMPLETE -> COMMITTED ->
HOME_INSTALLED -> CLEARED`。`log_write()` 之前只有 cache bytes；append 后有 intent/pin；
`write_log()` 完成后 payload 在 log data blocks；非零 header write 返回才 committed；每个 home
write 返回后 installed；零 header write 返回后 cleared。非最后一个 operation 可以在 group
commit 前返回，故 operation lifecycle 与 transaction lifecycle 不能合并。

## PERSIST-03

`bget()` 在 `bcache.lock` 下先查 identity；hit 先增加 ref，再释放全局锁并等 sleeplock。miss 在
同一锁下从 LRU tail 找 `refcnt==0` victim，写入新 dev/block、清 `valid`、令 ref=1 后才发布。
`bread()` 对 invalid buffer 发起设备 read 并置 valid；`brelse()` 先释放 sleeplock，再在 cache
lock 下减 ref，归零时移到 MRU head。设备 completion 前 `disk=1`；return 后为 0。

## PERSIST-04

唯一性来自 lookup 和 victim retag 都在同一 identity lock domain 中，而不是 sleeplock。若两个
miss 在没有共同 identity serialization 时各自 retag 一个 victim，同一 `(dev,block)` 会有两个
data/lock/ref state，后续 caller 和 log pin 无法确定哪个是权威副本。可信 same-block trace 要在
overlap 中看到同一 `slot/generation`、live identity count=1 和 ref peak=2；第二 actor 只能在第一
actor release 后取得 sleeplock。

## PERSIST-05

`holdingsleep()` 检查 caller 仍拥有 buffer；`releasesleep()` 后 caller 立即失去读写 `data` 的
权利。随后 cache lock 下 ref--；只有 ref 变 0 才成为 eviction candidate 并更新 recency。parallel
candidate 可以把一个 global LRU 改成 per-partition policy，但 victim 必须未引用、未 pin，retag
必须原子发布且增加 audit generation；旧 generation 的 caller 不能继续使用 slot。

## PERSIST-06

ordinary holder 与尚在等 sleeplock 的 caller 都已由 `bget()` 计入 ref；sleeplock 只标识当前
data user；首次 log entry 再通过 `bpin()` 加一个 ref；`b->disk` 单独表示 request in flight。
因此一个 holder、一个 waiter、一个 pin 可见 `refcnt=3`，但这个整数本身不说明三者构成。host
必须由 select/release 与 pin/unpin event 重算，最终 refs/pins/owners/waiters/disk_owned 全为 0；
`valid=1` 可保留。

## PERSIST-07

admission 检查 `lh.n + (outstanding + 1) * MAXOPBLOCKS > LOGBLOCKS` 时睡眠。当前 10/30 下，
`lh.n=0` 可 admitted 三个，第四个因 40>30 等待；`lh.n=1,outstanding=2` 的新 caller 因 31>30
等待。它睡在 `&log`，非最后一个 `end_op()` 减少 outstanding 后 wake，commit 完成也 wake；
恢复后必须在 while 中同时复查 capacity 与 `committing`。`begin_op()` 必须在获取 inode 等长期锁
之前：若 caller 持锁等待 log space，已有 reservation 的 operation 又需要该锁才能完成并调用
`end_op()`，capacity 就不会释放，形成资源等待环。

## PERSIST-08

每个 `end_op()` 先 outstanding--。若仍非零，它只 wake capacity waiters 后返回；其修改可能与
其他 operation 一起尚未 commit。若变为零，该 caller 在 log lock 下置 `committing=1`，释放锁后
执行 commit，完成 clear 后再置 0、wakeup 并返回。commit 不持 `log.lock`，因为 bread/bwrite
路径可能 sleep。可信 trace 必须按 epoch 关联 group members，不能把每个 syscall 当独立 disk
transaction。

## PERSIST-09

`log_write()` 扫描现有 `lh.block[]`。首次出现写入 block number、`bpin()` 并令 `lh.n++`；重复
出现停在已有 index，不再 pin，也不增加 n。commit 较晚从 cache 复制，因此同一 transaction
最后一次修改成为 log payload。重复 entry 浪费有限 log capacity；过早 unpin 允许 slot 在
`write_log()` 前被 eviction/retag。install 每个 distinct home block 后恰好一次 `bunpin()`。

## PERSIST-10

payload write complete 只说明 redo bytes 已写 log 区；header submit 还未观察 request 完成。
`write_head()` 对 `header_n>0` 的 `bwrite()` 返回后，源码才把它作为 true commit point；之后 home
可以逐块变化，因为完整 redo transaction 已可识别。第二次 `write_head()` 写 `n=0` 完成才是
clear。QEMU completion 是逻辑 device contract，仍不能证明 host cache flush 或掉电介质语义。

## PERSIST-11

30 个 log entries 不是 30 个独立 buffer 的静态映射。每个 entry pin 一个 home buffer；
`write_log()` 同时取得 log destination 与 home buffer，install 又取得 log/home pair，其他 caller
和 waiter 也可能保留 refs。cache 只有在找到 `refcnt==0` victim 时前进，否则精确 panic。因此
必须按 owner lifecycle 记账，不能从两个宏相等推出可用性。

## PERSIST-12

same-block case 在两 actor 进入前确认 block 未缓存，入口 rendezvous 后让 A 持 buffer，直到 B
已经选择同一 identity；oracle 是同 slot/generation、B acquire 晚于 A release。collision 由
topology discovery 选两个不同 block/同 partition，要求两个 identity 和正确内容。parallel 选
不同 partition，在 `CPUS=2` 下让两 hart 于各自 lock-held point 到达有限 spin gate，才支持一次
真实重叠。随机 stress 只能补充，timeout 只终止失控运行。

## PERSIST-13

scenario 描述 trigger/gate，不携带期望真值；guest 只记录 versioned semantic event。`QemuSource`
收集 live trace，`ReplaySource` 用 mutation 验证同一个 host reducer；后者不构成 kernel evidence。
host 必须拒绝 schema/extra field、seq gap/duplicate、非法 point capability、overflow、顺序交换、
duplicate identity、capacity violation 和 ledger imbalance。hook 会改变时序，candidate adapter 按
教学的非对抗模型接受，但仍需静态 review，故证据不是形式化证明。

## PERSIST-14

本单元的 R 只接受：无 crash transaction 按逻辑顺序完成，QEMU orderly stop 后 private image 的
目标 bytes 正确且 on-disk header n=0。这证明指定环境中正常完成结果可由 host file 再读，不证明
power-loss durability。#17 才在 log data/header/home/clear 设置 stable crash IDs，检查
before-or-after、non-mixed home、replay、第二次启动幂等、synthetic tears、orphan 与 offline fsck。

## PERSIST-15

合格报告先固定 baseline/scenario/fixture/runner/candidate hashes，再以一个 epoch 串起 dirty、唯一
append/pin、absorption、payload completions、nonzero header completion、home installs/unpins 和
zero header completion。capacity 两例必须包含原生 wait 与 event-driven release；cache 三例必须
分别证明 same identity、collision correctness 和 different-partition overlap；full case 在独立
只读运行精确 panic。每个正常 case 最终 ordinary refs/pins/owners/waiters/disk/gates 为 0，IO
submit=complete，`n/outstanding/committing=0`；随后 focused、quick/full、reverse patch、private
image 和 process-group cleanup 共同闭合，仍明确保留 #17 黑盒。
