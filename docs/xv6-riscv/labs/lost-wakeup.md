# 实验：确定性制造并修复丢失唤醒

本实验不以“随机压力偶尔卡住”为目标，而是用受控 checkpoint 强制产生经典交错，再用同一 schedule 验证 xv6 的 `sleep(chan,lk)` 交接为什么不会丢失唤醒。

## 1. 条件等待协议

对谓词 `ready` 和条件锁 `lk`，正确协议是：

```c
// consumer
acquire(lk);
while (!ready)
  sleep(chan, lk);
consume();
release(lk);

// producer
acquire(lk);
ready = 1;
wakeup(chan);
release(lk);
```

`sleep` 内部先取得 `p->lock`，再释放 `lk`，设置 `chan/state` 并 `sched`。producer 必须持同一 `lk` 改谓词并 wakeup。于是任一时刻，producer若已能取得 `lk`，consumer要么还会看见新谓词，要么已由 `p->lock` 保护其睡眠发布。

## 2. 要制造的错误

只在 `#ifdef LAB_LOST_WAKEUP` 测试构建中，为一个独立测试条件实现错误版本：consumer 在取得 `p->lock` 前先释放 `lk`，并在二者之间停在 gate：

```text
C: holds lk, observes ready=0
C: releases lk                     // broken window opens
C: checkpoint WAIT_RELEASED
P: acquires lk, ready=1, wakeup     // sees no SLEEPING waiter
P: releases lk, checkpoint WAKE_DONE
C: acquires p.lock, publishes SLEEPING, sched
                                       // no future wakeup: stuck
```

不要直接破坏生产 `sleep()` 供整个内核使用；那会让 console、log、disk等无关路径一起挂死，难以归因。使用专用测试对象/包装器，或由精确 hook只命中目标 pid和第 N 次等待。

## 3. 确定性 gate

gate 至少包含：唯一 id、generation、armed/arrived/released状态和 trace sequence。控制器必须能：

1. 等 consumer 到 `WAIT_RELEASED`；
2. 只释放 producer；
3. 等 producer完成 `WAKE_DONE`；
4. 再释放 consumer发布睡眠；
5. 观察目标稳定处于 `SLEEPING,chan=expected`，同时 `ready==1`。

gate实现本身不能复用被测试的同一条件协议，也不能在持 `p->lock` 时睡眠。可用测试专用 spin polling加宿主/第二进程协调，但要有有界 watchdog和内存序明确的原子状态。

## 4. 分阶段任务

### A. 先证明正确版本

在原 `sleep` 的关键点插入只读 trace：持条件锁、取得 p lock、释放条件锁、发布 SLEEPING、从 sched返回。producer trace谓词写与 wakeup扫描命中。强制尽可能接近的交错，确认结果是“producer被条件锁挡住”或“wakeup看见 sleeper”。

### B. 启用单点 mutation

切换到错误包装器，按上述 gate精确排列。watchdog不是简单宣布失败；它读取并打印目标 `state/chan/killed`、谓词、gate generation和最近事件，证明这是丢失唤醒而非死锁在其他锁。

### C. 恢复正确顺序

关闭 mutation，保持完全相同的控制步骤。因为 producer无法穿过正确交接窗口，控制器应观察到不同但合法的阻塞点，最终 consumer消费条件并退出。

### D. 生产者 mutation

作为第二个负例，让 producer不持 `lk` 改 `ready`/wakeup。即使 consumer的 `sleep` 正确，也无法建立谓词与睡眠检查的全序；用另一组 gate制造反例。

## 5. 验收条件

- 错误版本在 100% 运行中到达可解释的 `ready=1 && target SLEEPING` 状态，不依赖延时或 CPU 数；
- 正确版本在相同 gate sequence 下每次完成，无 missed event；
- trace证明 consumer持 `p->lock` 到 scheduler接管，producer的 `wakeup` 也通过同一 `p->lock` 观察状态；
- `while` 循环能容忍无条件/spurious wakeup，不能改成单次 `if`；
- 测试结束通过受控最终 wake/kill回收目标，所有 proc槽和锁回到基线；
- 默认构建不包含可触发的 broken path，完整 tests在 `CPUS=1` 和多 hart通过。

## 6. 负例与故障注入

| mutation | 期望检测 |
|---|---|
| consumer先 release `lk`、后 acquire `p->lock` | wake发生在发布前，目标永久睡眠 |
| producer不持 `lk` 更新谓词 | consumer可在检查后错过事件 |
| consumer用 `if` 不用 `while` | spurious/kill wake后在谓词假时继续 |
| `wakeup` 比较错误 chan | state/chan trace显示扫描但不命中 |
| sleep返回后不重取 `lk` | race detector式断言或共享状态破坏 |

kill 本身会把 SLEEPING 改 RUNNABLE，会掩盖丢失唤醒。因此仅在 oracle已记录稳定坏状态后用于清理，不能作为等待期间的 watchdog动作。

## 7. 报告要求

提交一条短事件序列，逐事件列出 `lk owner`、`p->lock owner`、`ready`、`p.state`、`p.chan`。解释正确版本中哪条 happens-before 阻止该序列，而不是只附终端超时截图。
