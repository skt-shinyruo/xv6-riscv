# 全局不变量与资源边界 rubric

本单元复用 `resources/communication-and-io/communication.patch` 与 `run-lab.py`。复用是验收
边界的一部分：不得修改 fixture 来制造预期 marker，也不要求新增 syscall 或另一套 runner。

| 项目 | 通过要求 | 失败示例 |
|---|---|---|
| unified model | 每类资源都记录 identity、capacity、owner、state、acquire、transfer、rollback、release、exhaustion、post-failure | 只列容量或函数名 |
| global coverage | process、page、file、inode、buffer、log、device、interrupt、persistence 九类都有稳定 source anchor 与 owner | 用一个“kernel resource”节点合并不同 identity |
| capacity | 精确区分 return、sleep、kill、panic、partial result；说明常量联动 | 把提高数组长度当成完整扩容 |
| lock/publication | 标出 identity/count lock、对象发布点、可睡眠限制与 IRQ handoff | 把 wakeup 或 IRQ completion 当作最终 owner release |
| bounded oracle | `NOFILE=16`、BASE fd=3、filled=13、首个 `fdalloc` 失败、`pipe=-1`；fixture 内部检查饱和 failure 前后账本，host 复算公开 marker 的最终 AFTER=BASE | 把隐藏 fixture assertion 写成 host raw 字段，声称动态覆盖已安装 fd0 的分支，或只检查 `pipe==-1`/guest `PASS` |
| cleanup | duplicates/child/endpoints 回收；两次 fixture、focused/quick/full、patch reverse、`make clean`、worktree/image/process cleanup | 只说 QEMU 退出 |
| evidence limits | S/F/B/C/R 分开；明确未动态耗尽其他池，不声称 formal proof、fairness、all interleavings 或 physical durability | 把静态表或一次 run 称为正确性证明 |

报告必须绑定 pinned baseline、教程提交、复用 fixture/runner 和机器报告 SHA-256。非作者从 raw
marker 独立复算 `NOFILE-3==13` 与 AFTER=BASE 后，才可签署 walkthrough。
