# 调度与 lost-wakeup 证据项目 rubric

## 适用边界

`syncproject.patch` 是 publication 与 non-author walkthrough 使用的可执行
instrumentation fixture，不是生产修复、学习者答案或可提交的 reference patch。
它只在 pinned baseline 的临时导出中制造一个已知正确交接和一个已知错误窗口，
以便验证教程声明的事件关系。学习者的出口是独立报告，不复制 fixture 实现，也
不能把 runner 的 `PASS` 当作论证。

学习者可以使用 `trace-worksheet.md` 和 `bounded-change-report.md` 组织自己的报告；
runner 给出可复算的原始字段，下面的 rubric 只规定证据类别，不规定答案措辞、图
的布局或 learner branch 的实现结构。

## 评分维度

| 维度 | 通过要求 | 不通过示例 |
| --- | --- | --- |
| scheduler 模型 | 画出两个方向的 `p->lock` continuation 交接，并区分 process context 与 hart state | 把 `swtch()` 写成用户/内核或页表切换 |
| 锁与中断 | 分开解释 acquire/release memory order、`noff/intena` 和 can-sleep 约束 | 用“关中断”解释跨 hart 可见性 |
| 正常等待 F | 从实际 fixed 字段证明 check、发布、match、RUNNABLE、跨 hart resume 和 recheck | 只引用最终进程退出或 `PASS` |
| 失败边界 B | 在 rescue/timeout/kill 之前证明 ready=1、SLEEPING、channel match、wake miss、killed=0 | 用超时或 kill 后退出当 lost-wakeup oracle |
| 并发 C | 给出命名事件间的 happens-before，并区分受控 gate 与生产协议 | 把串口行到达时间当实时顺序 |
| 资源与清理 | 核对 child、condition lock、channel、32 槽 trace、gate、进程组、临时树和镜像 | 只写“make clean 成功” |
| 证据局限 | 明确不推出公平性、全交错、DMA memory order、形式化正确性或 R | 从四轮运行推广为所有执行都正确 |

## 独立复核

reviewer 必须从报告中的 `seq/hart/state/ready/chan/cond_owner/proc_owner/noff`
字段复算至少一轮 fixed 与一轮 broken，并核对原始 `FINAL/CLEAN`、回归 transcript
digest、pinned baseline、fixture digest 和共享状态指纹。若报告只能借助本 rubric
之外的同步副本才能成立，则出口产物不通过。
