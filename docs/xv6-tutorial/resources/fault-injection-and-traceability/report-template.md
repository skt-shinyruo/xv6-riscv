# 故障注入与源码测试追踪报告模板

## Provenance

- 教程提交 / candidate diff：
- pinned baseline / manifest SHA-256：
- filesystem fixture / runner / report SHA-256：
- scheduling fixture / runner / report SHA-256：
- persistence fixture / scenario / pre-#19 runner + report / current runner SHA-256：
- recovery fixture / scenario / runner / external candidate / verified report SHA-256：
- 环境、QEMU/toolchain、CPUS：
- non-author reviewer / walkthrough：

## 状态词汇

只使用 `baseline-current`、`verified-tutorial-fixture`、`proposed-learner-work`。列出所有 proposed 名称，
明确它们当前没有 command/syscall/API。

## Forward source trace

| record ID | status | `path:symbol` | manifest owner | invariant | S/F/B/C/R | current runner/test | raw observable | remaining gap |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |

## Reverse runner/test index

| runner/test | record IDs | observed fields/relations | evidence strength | limits / source changes requiring rerun |
|---|---|---|---|---|
| filesystem `LINKFAIL/ALLOCFAIL` | | | | |
| scheduling fixed/broken | | | | |
| persistence ReplaySource/scenarios | | | | |
| recovery crash points | | | | |
| `quicktests[]/slowtests[]/test_crash()` | | | | |

## Deterministic fault rules

| namespace/ABI + semantic ID | status/site/scope | nth / eligible / fired | action/result + allowed side effects | before/after ledger | retry/recovery/cleanup |
|---|---|---|---|---|---|
| filesystem `ALLOCFAIL` current fixture | | | | | |
| one proposed learner rule | | | | | |

粘贴 fresh filesystem raw `LINKFAIL` 或 `ALLOCFAIL` marker 与 AFTER，并独立复算
`fail_at == eligible`、`fired == 1`、result、允许副作用和 cleanup。

## Scheduling gates

| seam/point | capability | actors + held locks/interrupts | release/self-dependency | CPUS/budget | raw order/result | overflow behavior |
|---|---|---|---|---|---|---|
| scheduling fixed/broken | | | | | | exact-count indirect rejection；无 explicit signal/mutation |
| persistence selected gate | | | | | | ReplaySource rejects error；无 live overflow/wake-all proof |
| proposed learner gate | | | | | | |

粘贴一条 fresh scheduling named-event order。明确 scheduling 的 32 槽静默 drop 是 gap，不是通过项。

## Overflow and stable crash IDs

- persistence `error=1` mutation、期望 rejection 与实际 self-test：
- current guest error/gate flag、缺失 live wake-all 与 host reject 的边界：
- recovery 10/20/30/40 的 source semantic boundary：
- 任选一个 logical ID 的 marker/image/replay 关系与限制：

## Regression 与 cleanup

记录四个 runner 的 self/static、fresh filesystem/scheduling bounded runs、复用的 verified
report identity、pre-#19/current persistence runner 边界、recovery external candidate、
validator/navigation、patch reverse、shared worktree/image fingerprint、
QEMU process group 与临时目录结果。不能把历史动态报告写成 fresh run。

## Evidence boundary

| S/F/B/C/R | 本报告建立的结论 | remaining gap / 禁止的 proof claim |
|---|---|---|
| S | | |
| F | | |
| B | | |
| C | | |
| R | | |

最后执行一次反向查询：选择一个 source symbol 变化，列出必跑、related-only、不相关 evidence 和仍未覆盖
gap。结论不得声称 path coverage、formal proof、fairness、all interleavings 或 physical durability。
