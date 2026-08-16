# Scheduling-and-synchronization 0.1.0 非作者走查记录

- 教程版本：`0.1.0 draft`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`53e92993888b9ededd5e99fd2aac38c89b8c6d27` 加 #10 未提交候选 diff；最终提交保留本记录、晋级和迁题
- 走查单元或连续路径：`core.scheduling-and-synchronization`、迁移题 `SCHED-00..10` 和隔离 `syncproject` lost-wakeup 实验
- 匿名入口能力：`SCHED-C20-R4`；已通过 Foundation、系统观察、用户 ABI、系统调用、boot/trap 与进程生命周期单元，未参与候选正文、patch 或 runner 编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、GNU Make 4.3、Python 3.12.3；lost-wakeup 与 quick 使用 `CPUS=2`，full 使用 `CPUS=1`，128 MiB、私有 `fs.img`

## 观察到的卡点

早期候选把 fixed 的 release 后采样误当成 waiter 仍持条件锁，也没有建立
producer 已开始竞争、origin scheduler 已跳过 target 与 other-hart selection
之间的完整先后关系。第一次稳定性压力运行还暴露一个真实死锁：waiter 在非
origin hart 持 `p->lock` 等待 release gate，而 origin hart 的 timer handler
同时等待该锁。GDB 复现后，runner 将 origin skip 与 resume gate 分开，并让
broken window 在配对的嵌套 `push_off/pop_off` 中固定 hart。

文档复查又修正了 caller-saved 值不必都 spill 到栈、timer interrupt 在存在当前
进程时可以从 `kerneltrap()` 调度，以及 sleeplock 不能用于中断上下文的真正调用
前提。项目 rubric 最终明确区分 publication/non-author 的完整 instrumentation
fixture 与学习者独立提交的模型和证据报告，避免把 audit patch 当成答案补丁。

晋级候选的独立内容审查随后发现首版问题集省略了 process-to-scheduler 的第一次
`swtch`、kernel-mode `kernelvec` migration、无 runnable 时的 `wfi` 分支和跨 hart
`noff` 交接；`SYNC-03` 兼容入口还丢失了 `bread()`/`begin_op()` 约束。答案又把
“以 RUNNING 进入 sched”的后果错写成同时执行。这些均作为验收失败处理，而非
编辑建议。

## 验收产物

- S：pinned baseline 上复核 `scheduler/sched/yield/swtch`、spinlock memory
  order、`noff/intena`、sleep/wakeup、pipe/sleeplock condition loop、timer
  preemption 与 11-path targeted hook；默认未 arm 的生产路径不受影响。
- F：fixed 两轮均建立 `lk + p->lock -> release(lk) -> SLEEPING ->
  WAKE_MATCH -> RUNNABLE -> scheduler ownership -> recheck`，`miss=0`、
  `rescue=0`、`match=1`。
- B：broken 两轮均在 target 仍 `RUNNING/chan absent` 时记录 `WAKE_MISS`，随后
  主动读到 `ready=1/SLEEPING/expected chan/miss=1/killed=0`；只有
  `BAD_ORACLE` 后才执行一次 rescue，timeout 与 kill 都不作成功 oracle。
- C：四个连续 generation 都记录 origin hart skip，随后由另一 hart select 与
  resume；每轮 child 数、条件锁、channel、32 槽 trace 和全部 gate 都恢复基线。
- regressions：`usertests preempt/pipe1/killstatus`、quick `usertests`（2 hart）
  与完整 `usertests`（1 hart）均精确得到一次 `ALL TESTS PASSED`，且无失败或
  `SYNC` marker。
- 非作者最终报告：`/tmp/ticket10-nonauthor-walkthrough.md`，SHA-256
  `b9d9044ddac314b4602572dac421f54389dd11b721dd5322146b52fe5f4665dd`；patch
  SHA-256 为 `322158e3cb302ed56575e79b070c1ae1269fdeb93c847a57028d7de1865aec21`，
  runner SHA-256 为 `0d08bb94427824f50b8b648c8674f2b6b1d7650a1921dbe142f09cc4086c76f6`。

## 修正与复查

最终 runner 对每个命名事件逐字段检查 `seq/hart/state/ready/chan/cond_owner/
proc_owner/noff`，执行 fixed/broken/fixed/broken 四轮、三组定向回归、quick 和
full，并把所有命令放进独立进程组。它最后 `make clean`、逆向 patch、比较完整
源码快照并删除临时目录；共享 `fs.img` 前后都不存在，工作树状态不变，也没有
遗留 QEMU/driver 进程。报告只记录上述可复核命令和 transcript，不把临时调试
脚本或未保存的压力运行当作发布证据。

独立走查还通过普通/development validator、generated navigation、5 个 validator
单测和 `git diff --check`。走查完成后只修正一处 `intena` 病句，按 ADR 0017
原子加入本记录、晋级 `verified`，并把旧 `PROC-01/02/09/10`、`BOOT-10`、
`SYNC-01/03/04/09` 替换为兼容入口；这些发布元数据改动不改变已走查 runner、
patch 或动态 oracle。

内容审查失败后，问题与答案补齐双向 `swtch`、`kernelvec` 不恢复 `tp`、
`intr_on -> intr_off -> scan -> wfi`、origin/destination CPU 各自的 `noff` 变化、
额外 `push_off()` 失败路径，以及 `bread/acquiresleep/begin_op` 的完整中断上下文
契约；manifest 同步增加稳定 anchors 与 secondary ownership。修正后的最终完整
报告为 `/tmp/scheduling-and-synchronization-final-r2.md`，SHA-256
`ec8ff226c369bc1d13dc191943537348ae0e97523af7d3f06af61ed454db64d9`，再次通过
static、F/B/C、focused、related、quick、full 和 cleanup。复测代理在回报阶段
遭遇服务端 429；这不替代前述非作者完整运行，也不被记作一次成功复测。

## 结果

| 单元 | 结果 | 晋级依据 |
| --- | --- | --- |
| `core.scheduling-and-synchronization` | 通过 | scheduler/continuation 所有权、锁与中断嵌套、确定性 fixed/broken lost-wakeup、跨 hart 恢复和精确资源清理共同闭合 `S/F/B/C` oracle |

本记录不证明公平性、全部交错、设备 DMA memory order、page-table/TLB 合同或
持久化恢复；这些边界保留给 #11 及后续单元。
