# 实验：可并行且保持唯一性的 buffer cache

当前 `bget()` 在单个 `bcache.lock` 下线性扫描 30 个 buffer。实验把查找改为 hash buckets，降低不相关块的元数据锁竞争，同时保留 `(dev,blockno)` 唯一对象、sleeplock、pin和日志协议。

## 1. 不变量优先

任何优化都必须维持：

```text
U1: 对每个 (dev,blockno)，cache中至多一个已命名 buffer
U2: bget返回时 refcnt>0；caller取得 b->lock 后才读写 data/valid/disk
U3: refcnt>0 或 pin引用存在的 buffer绝不被重新命名
U4: victim从旧 bucket移到新 bucket期间，对查找要么不可见，要么以新 key唯一可见
U5: 不持 bucket spinlock等待 b->lock、VirtIO、sleep或日志commit
U6: log_write pin的 home buffer直到install/commit释放，不会被回收
```

LRU精确顺序不是 correctness；若换成近似时间戳/clock，必须明确替代策略和饥饿边界。

## 2. 推荐结构

采用固定数量、最好为质数的 buckets，每个有 spinlock和链表。`hash(dev,blockno)` 唯一决定 key所属 bucket。另设 `evict_lock` 只串行化 miss后的 victim选择/迁移；这是较简单的教学设计，仍允许不同 bucket的 cache hit并行。

`bget` 轮廓：

1. 取目标 bucket lock并查 key；命中则 ref++、释放 bucket lock，再 acquiresleep；
2. miss后释放 bucket，取得 `evict_lock`；
3. 重新检查目标 bucket，处理另一 CPU已创建的 race；
4. 找 `refcnt==0` victim；取得 source/target bucket locks（按全局 bucket id顺序）；
5. 再验证 victim仍可用、目标 key仍不存在；原子式摘除、改 key/valid/ref、插入；
6. 释放 bucket/evict locks，再取得 victim sleeplock；
7. 无 victim时保留当前 panic语义，或另立有证明的等待协议；不要悄悄自旋。

若 source==target，只取一次锁。锁排序必须按 bucket id，不按“source先/target后”，否则两个反向迁移形成 ABBA。

## 3. `brelse`、pin 与时间

`brelse` 先释放 sleeplock，再在所属 bucket下 ref--；若变 0，更新 eviction metadata。`bpin/bunpin` 在正确 bucket锁下改 ref，并对 underflow断言。

时间戳若来自 `ticks` 会只随 hart 0 timer更新，多个 release可同值；它只能做近似顺序。全局原子 sequence更清晰但会成为新热点。报告比较这些取舍，不要求先验选定唯一答案。

## 4. 与日志和 VirtIO 的边界

- `log_write` 对同一 home block absorption，依赖所有 caller拿到同一 buffer；duplicate buffer会让两份 data分歧并破坏日志。
- commit会再次 `bread` home/log block；调用 `end_op` 前不能仍持相关 buffer sleeplock，否则自锁。
- buffer在 `virtio_disk_rw` 等待期间 ref>0且持 sleeplock，不可被 victim选择；bucket锁必须已释放。
- IRQ完成路径会 wakeup和更新 `b->disk`，不应需要 eviction locks。
- 所有 buffer被引用/pin时，当前语义是 `bget: no buffers` panic；如果改为睡眠，必须定义条件锁和防止“等待者仍持 buffer/log reservation”的死锁证明。

## 5. 分阶段任务

1. 为原实现加唯一性扫描断言和 lookup/hit/scan counters，建立功能基线。
2. 引入 buckets但保留全局 `evict_lock`；先让所有操作正确，再测并行。
3. 实现 double-check miss和有序跨 bucket迁移。
4. 接入 `brelse/bpin/bunpin`，给 ref underflow、duplicate key、victim-with-ref加 panic断言。
5. 运行文件系统事务、recovery和并发读写；最后才调整 eviction策略。

## 6. 验收条件

- 任意 checkpoint 扫描 cache，非空 key无重复；同 key并发 miss返回同一 `struct buf*`；
- `usertests`、大文件、并发创建/删除、crash recovery全部通过；
- pin期间强制大量冲突 miss，目标 buffer地址/key/data不变；
- 不在 spinlock持有期间 acquire sleeplock/sleep/做 VirtIO I/O；lock trace无排序环；
- 所有 `bget` 与 `brelse`/pin引用平衡，稳定 workload后 ref分布回基线；
- 多 hart不相关 bucket hit可重叠；用事件 trace证明，不要求特定性能倍数；
- `CPUS=1` 行为与原版一致，包括明确的耗尽结果。

## 7. 确定性故障与竞态注入

| checkpoint | 强制交错 | oracle |
|---|---|---|
| 两 CPU target bucket初查后 | 同 key双 miss | double-check后只命名一个 buffer |
| victim选中后、迁移前 | 另一 CPU尝试获取旧 key | 不得拿到已重命名数据或创建重复新 key |
| 持 source bucket等待 target前 | 反向迁移 | id排序避免死锁 |
| `log_write` pin后 | 用超过 NBUF的不同块施压 | pinned buffer不移动；按规定成功等待或明确 panic |
| VirtIO submit后 | 大量 cache lookup/evict | in-flight buffer保持身份至completion |
| ref--到0边界 | 同时 hit相同 key | 要么 hit并ref++，要么victim迁移后重新读取；无 UAF |

加入 mutation：跳过 double-check，测试必须产生 duplicate-key断言；交换一次锁序，lockdep/checkpoint必须稳定发现环。验收提交关闭 mutation。

## 8. 观测与报告

记录每 bucket长度、hit/miss、检查节点数、迁移数、evict_lock等待、pin high-water和VirtIO等待。报告应分别说明 correctness和contention；“更少全局锁”并不自动等于更快，也不能以性能为由删除唯一性断言。
