# 故障注入与源码测试追踪问题

本问题链由 `core.fault-injection-and-traceability` 拥有。答案必须引用 pinned source、manifest owner、
runner/test 与 raw observable；proposed contract 只能标为 `proposed-learner-work`。

## 高层总览

### TRACE-00 双向追踪图有哪些节点和边？

从 `kernel/fs.c:dirlink`、filesystem `LINKFAIL`、对应 runner 与 raw marker 画
`source -> owner -> invariant -> evidence -> test -> gap`。再从 runner 反向找回这条 source record。
为什么单向“函数 -> test name”不能支持修改影响分析？

### TRACE-01 baseline、tutorial fixture 与 learner proposal 为什么必须分层？

比较 `user/usertests.c:quicktests[]/slowtests[]`、filesystem fixture 的 `fsaudit_dirlink_fail()` 和尚未
实现的通用 `FI_*`/`faultctl`。每层能作什么陈述，哪种混写会把临时教程 hook 误报为 baseline API？

## 控制流与状态转换

### TRACE-02 `ALLOCFAIL` 的第二次 eligible allocation 怎样变成可审查失败？

沿 `writei -> bmap -> balloc -> fsaudit_balloc_fail` 追踪 mode/PID scope、`fail_at=2`、`eligible=2`、
`fired=1`、write result、允许保留的 empty indirect block、最终 unlink cleanup 与 host reducer。

### TRACE-03 fixed/broken lost-wakeup 怎样固定事件顺序？

从 `sleep()`、`wakeup()`、`syncproject_scheduler_skip()` 和 `record_event()` 建立 wait/producer/
scheduler/rescue 的 happens-before。每个 gate 当时持什么锁、interrupt/noff 是什么，另一 hart 如何推进？

### TRACE-04 persistence event overflow 为什么必须 fail closed？

从 `PA_MAX_RECORDS` 到 `audit.error=1`、`gate_open=1`、`PERSIST META/LEDGER` 和 host
`_validate_common()` 追踪 overflow。当前 seam 验证了什么？为什么完整合同还要求唤醒已睡 waiter，并让
host 拒绝 run？

### TRACE-05 crash IDs 10/20/30/40 分别绑定什么语义边界？

从 recovery `scenarios.json` 回到 `write_log()`、`write_head()`、`install_trans()` 与
`recover_from_log()`。说明 stable logical ID、guest logical order、host image 和 synthetic tear 是四种
不同关系，不能互相代替。

## 局部设计边界

### TRACE-06 一条 proposed fault rule 最少需要哪些字段？

写出 namespace/ABI、semantic ID、site、PID/hart/controller scope、nth、eligible、fired、action/result、
side effects、resource ledger、retry/recovery 与 cleanup。按固定匹配顺序解释未命中和重复命中怎样失败。

### TRACE-07 一个 observation point 何时可以 block？

对 `observe-only`、`sleep-ok`、`sleep-ok-with-buffer-sleeplock` 和 `spin-2cpu-ok` 分别给出合法与非法例子。
检查 held locks、IRQ/can-sleep、release actor、自依赖、CPUS 与 budget，而不是只看 point 名称。

### TRACE-08 为什么 scheduling 的 32 槽 trace 不能证明 overflow safety？

阅读 `record_event()` 的 `slot >= SYNC_MAX_EVENTS` 分支，与 persistence 的 error/open/reject evidence
比较。固定 event-count 为什么会间接拒绝当前 case，却仍不是显式 reusable overflow oracle？这项缺口应
怎样写入 forward record 和 reverse test index？

## 跨切面与综合

### TRACE-09 修改 `kernel/log.c:commit()` 后如何从反向索引选择复核？

列出 persistence transaction、recovery crash IDs、filesystem regression 和普通 usertests 中相关但强度
不同的 evidence。说明哪些必须重跑、哪些只是 related，以及 remaining gap 为什么仍保留。

### TRACE-10 test registration 与 evidence oracle 有什么差别？

从 `quicktests[]`、`slowtests[]` 和 `test-xv6.py:test_crash()` 找到真实入口，再与 tutorial runner 的
schema/reducer 比较。一个测试“被注册且通过”还缺哪些 trigger、raw fields、resource 和 cleanup 条件？

### TRACE-11 如何审查一份 proposed injection 设计而不假装它已实现？

任选 filesystem、scheduler、cache/log 或 recovery site，填写完整 rule、gate capability、expected raw
observable、mutation、cleanup 和 S/F/B/C/R 边界。最后分别写“当前已证实”“实现后才可验收”“仍不可
推出”的最强结论。
