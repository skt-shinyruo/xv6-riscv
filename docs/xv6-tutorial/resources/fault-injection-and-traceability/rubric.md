# 故障注入与源码测试追踪 rubric

本单元复用 filesystem、scheduling、persistence 和 recovery 的既有 publication seams。不得为填表新增
通用 fault dispatcher、syscall、第二份 manifest 或同步答案副本。

| 项目 | 通过要求 | 失败示例 |
|---|---|---|
| provenance | 绑定 pinned baseline、教程 candidate、manifest、所有引用 fixture/runner/scenario 与机器报告 digest | 只写 HEAD 或 PASS |
| forward trace | 每条 record 有 stable ID、status、`path:symbol`、manifest owner、invariant、S/F/B/C/R、runner/test、raw observable、gap | 只列函数和测试名 |
| reverse trace | 每个引用的 runner/test 回到 record IDs、观察字段、strength 与 limits；source change 可据此选复核 | 把 related test 都称为必然覆盖 |
| current/proposed boundary | 只用 `baseline-current`、`verified-tutorial-fixture`、`proposed-learner-work`；不存在的 `FI_*`/`faultctl` 始终 proposed | 把临时 fixture 或设计写成 baseline API |
| deterministic fault | rule 包含 namespace/ABI、semantic ID、site、scope、nth、eligible、fired、action/result、side effect、ledger、retry/recovery、cleanup | 用行号/地址 ID，或只观察返回值 |
| gate safety | 每个 point 有 capability、actors、held locks/interrupts、release、自依赖、CPUS、budget；非法 block/spin 被拒绝 | IRQ sleep、同锁自依赖、单 hart spin 或无界 gate |
| overflow | persistence good trace 的 `error=1` mutation 被 reducer 拒绝；合同要求 error、wake/open all waiters、host reject；两套 fixture 的 live/explicit gap 分别列出 | 把间接 count rejection、未挂死或 32 槽未满写成完整 overflow proof |
| bounded evidence | fresh filesystem nth-failure 与 scheduling named-event reports；persistence 的 pre-#19 dynamic/current self-static、recovery 的 external candidate/report 分开绑定 | 把 self/static 写成 fresh QEMU run，或漏掉 recovery candidate |
| cleanup/limits | resource/process/image/temp cleanup；S/F/B/C/R 分栏；无 coverage/formal/fairness/physical-durability 越界主张 | 只写 QEMU exited |

非作者至少独立复算一条 filesystem `fail_at/eligible/fired` 关系、一条 lost-wakeup gate 顺序、persistence
overflow host rejection 和一个绑定 external candidate 的 recovery logical ID，并从反向索引完成一次
source-change 影响查询，才可签署。
