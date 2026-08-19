# 故障注入与源码测试追踪

## 问题场景与本单元成果

测试名、源码符号和一句“这里会失败”不能形成可审查证据。维护者需要正向回答“一处源码由谁解释、
守护什么不变量、哪些测试观察它”，也要反向回答“这个符号变化后应重跑哪些 oracle、仍有什么缺口”。
确定性故障还必须说明 stable ID、作用域、第几次 eligible event、实际 fired 次数、可见结果、资源状态
和 cleanup；否则一次失败可能只是时间巧合。

本单元的唯一出口是一份**双向 source-test traceability 报告**。它把当前 baseline、已经 verified 的
tutorial fixture 与尚未实现的 learner proposal 分层记录，并为 proposed fault/scheduling injection
写出可验收合同。报告不是覆盖率声明，也不把拟议的 `FI_*`、`faultctl` 或通用 gate 当成当前能力。

## 前置单元与暂存黑盒

硬前置是[全局不变量与资源边界](global-invariants.md)。局部机制仍由 process、scheduling、filesystem、
persistence 和 recovery 单元拥有；本页只拥有 source-owner-invariant-test-gap 的双向映射，以及
deterministic injection 的合同词汇。

报告中的每一行必须使用以下状态之一：

- `baseline-current`：pinned baseline 自身已有的 source/test 行为；
- `verified-tutorial-fixture`：只存在于临时应用的教程 fixture，并由其 runner 验收；
- `proposed-learner-work`：尚未实现，只能写设计和验收条件。

baseline 没有通用 `faultctl`、通用 `FI_*` namespace 或可复用 scheduler gate。这些是显式 gap 与
non-goal，不是 manifest black box。下一单元只可在本报告基础上讨论 scalability 和 evidence synthesis。

## 最小模型和关键不变量

### 双向追踪记录

每个稳定 record 至少包含：

```text
(record_id, status, source_path:symbol, explanation_owner, invariant,
 evidence_dimensions, runner_or_test, raw_observable, remaining_gap)
```

正向表按 `source_path:symbol` 找 owner、invariant、证据与 gap；反向索引按 runner/test 找所有 record IDs、
观察字段和局限。ID 绑定语义，不绑定行号、地址、return PC 或某次编译布局。一个 test 可关联多条记录，
一条记录也可由静态与运行时证据共同支持；这仍不等于 path coverage 或形式化证明。

### 当前 seam 的精确边界

| status / seam | 当前确定性能力 | raw observable | 必须保留的 gap |
|---|---|---|---|
| `baseline-current` / `user/usertests.c`、`test-xv6.py` | `quicktests[]`、`slowtests[]` 与 `test_crash()` 是真实注册/driver 入口 | test name、exit 与 transcript | 普通 regression 不提供 stable fault site、nth occurrence 或资源账本 |
| `verified-tutorial-fixture` / filesystem | `fsaudit_balloc_fail()`、`fsaudit_dirlink_fail()` 按 fixture 内部 PID、mode 和 `fail_at` 计 `eligible`，仅一次 `fired` | `FS LINKFAIL/ALLOCFAIL` 的 fail_at、eligible、fired、result 与 ledger | raw marker 不发布 PID/site；scope 与 site 必须由 fixture source 复核，不是通用 ABI |
| `verified-tutorial-fixture` / scheduling | fixed/broken lost-wakeup 路径用 named events 和 scheduler gate 固定关系 | `SYNC EVENT/FINAL/CLEAN` 的 pid、hart、lock、`intr`、`noff`、gates | `record_event()` 的 32 槽满后静默丢弃；固定 15/18-event oracle 会间接拒绝当前 case 的溢出，但没有显式可复用 overflow signal/mutation |
| `verified-tutorial-fixture` / persistence | `scenarios.json` 声明 gate capability、actors、release 与 budget；bounded event array 满时设置 `error=1` 与 `gate_open=1` | `PERSIST META/EVENT/LEDGER`；host `ReplaySource` 拒绝非零 error、坏序列和非法 gate | overflow 分支没有唤醒已睡在 `&audit` 的 waiter，也没有 live overflow trigger；guest logical order 不是 host power-loss durability |
| `verified-tutorial-fixture` + 外部 candidate / recovery | external candidate 把 logical IDs 10/20/30/40 接到 log-data/header/home/clear 四个边界；published fixture 的默认 `recoveryaudit_hit()` 是 no-op | `RECOVERY CRASH/REPLAY`、image/fsck 与 first/second boot；报告必须绑定 candidate digest | 没有 nth occurrence 通用匹配；synthetic tear 不是物理介质 fault |
| `proposed-learner-work` | 统一 stable fault namespace、scope/nth rule 和 capability-checked gates | 由下述合同定义 | 未实现前不得写成 command、syscall 或当前 test capability |

### Proposed trigger rule

一个 proposed fault rule 必须记录 namespace/ABI、semantic ID、site、PID/hart/controller scope、`nth`、
`eligible`、`fired`、action/result、允许的 side effects、before/after resource ledger、retry/recovery 和 cleanup。
匹配顺序固定为：

```text
semantic ID -> site and scope match -> eligible++ -> eligible == nth -> fired++ -> action
```

未命中必须得到 `fired=0`，命中一次必须得到 `fired=1`；重复命中、缺字段、unknown ID、错误 scope 和
超预算都使 run 失败。规则不能用源码行号、地址或 return PC 作为 stable ID，也不能把“函数返回 -1”
当作 resource cleanup 的替代。

### Gate capability 与 overflow

gate 必须先声明 observation point 的 capability：`observe-only`、`sleep-ok`、
`sleep-ok-with-buffer-sleeplock` 或 `spin-2cpu-ok`。每个 gate 还要列 actors、held locks、interrupt state、
release actor/event、self-dependency、CPU 数和 iteration/time budget。

- `observe-only` 只能记录，不能阻塞；
- 可 sleep gate 必须有合法 sleep lock/predicate/producer，不能在 IRQ context 睡眠；
- 持 buffer sleeplock 的 gate 不能等待必须取得同一 lock 的 release actor；
- spin gate 只可在 `CPUS=2` 且有另一 hart producer 时使用，并有有限 iteration budget；
- event overflow 必须设置 error、唤醒并开放所有 gate waiter 让 guest 可退出，并由 host 拒绝整次 run。

persistence 当前只满足 error/gate flag 与 host rejection：本 ticket 在既有 `ReplaySource` self-test 中加入
`error=1` mutation，证明 reducer 会拒绝 overflow report；它没有 live overflow trigger，也没有在该分支
`wakeup(&audit)`。scheduling 的固定 event 数会间接拒绝当前 case 的溢出，但同样缺少显式 overflow ABI。
两项缺口都必须保留，不能跨 seam 推论；完整 fail-closed 行为仍是 proposed learner contract。

## 源码追踪计划

先找 baseline 机制与 test registration，再进入教程 fixture；不要从 proposed API 名称反推实现：

```sh
rg -n '^kalloc\(' kernel/kalloc.c
rg -n '^scheduler\(|^sleep\(|^wakeup\(' kernel/proc.c
rg -n '^balloc\(|^dirlink\(' kernel/fs.c
rg -n '^bget\(' kernel/bio.c
rg -n '^begin_op\(|^commit\(|^recover_from_log\(' kernel/log.c
rg -n '^virtio_disk_intr\(' kernel/virtio_disk.c
rg -n 'quicktests\[\]|slowtests\[\]' user/usertests.c
rg -n '^def test_crash\(' test-xv6.py
rg -n 'fsaudit_(balloc|dirlink)_fail|fail_at|eligible|fired' \
  docs/xv6-tutorial/resources/filesystem/filesystem-audit.patch
rg -n 'record_event|SYNC_MAX_EVENTS|scheduler_skip' \
  docs/xv6-tutorial/resources/scheduling-and-synchronization/syncproject.patch
rg -n 'point_capabilities|iteration_budget' \
  docs/xv6-tutorial/resources/persistence/scenarios.json
rg -n 'PA_MAX_RECORDS|audit.error = 1' \
  docs/xv6-tutorial/resources/persistence/persistence-audit.patch
rg -n 'logical_id|point' docs/xv6-tutorial/resources/recovery/scenarios.json
```

对每个 record 先 breadth-first 画 `source -> owner -> invariant -> runner/test -> raw observable -> gap`，
再进入 fault counter、gate 或 crash hook 的局部实现。所有 manifest owner 与 source anchor 以
`curriculum.json` 为准。

## 观察任务

1. 从 filesystem 的 `LINKFAIL` 或 `ALLOCFAIL` 选择一条记录，正向填写 source、owner、trigger、raw
   result、resource ledger 与 cleanup，再从 runner 反向找回同一 record ID。
2. 从 scheduling fixed/broken 报告重建 named-event happens-before，核对每个 event 的 lock、interrupt、
   `noff` 和 gate state；明确 32 槽 overflow 为什么没有被当前 seam 验收。
3. 从 persistence scenario 判断每个 gate 是否可 block，并运行 overflow negative mutation；区分已验证的
   host rejection 与尚未实现的 live wake-all 行为。
4. 从 recovery IDs 10/20/30/40 解释 semantic ID 为什么比行号稳定，以及它们为什么仍不是通用 nth fault。
5. 从 `quicktests[]`、`slowtests[]` 与 `test_crash()` 建反向索引，区分真实 test registration、driver 与
   tutorial fixture oracle。

## 有界修改任务

通用 fault-injection framework 为 `N/A`：本单元不新增 syscall、kernel dispatcher、rule parser 或第二份
traceability manifest。最小 publication 改动只扩展 persistence runner 的既有 `ReplaySource` self-test，
把同一份 good trace 的 `META/LEDGER error=0` 改成 `error=1`，要求 reducer 拒绝。运行：

```sh
python3 docs/xv6-tutorial/resources/filesystem/run-lab.py --self-test
python3 docs/xv6-tutorial/resources/filesystem/run-lab.py --static-only
python3 docs/xv6-tutorial/resources/scheduling-and-synchronization/run-lab.py --static-only
python3 docs/xv6-tutorial/resources/persistence/run-lab.py --self-test
python3 docs/xv6-tutorial/resources/persistence/run-lab.py --static-only
python3 docs/xv6-tutorial/resources/recovery/run-project.py --self-test
python3 docs/xv6-tutorial/resources/recovery/run-project.py --static-only
```

filesystem 与 scheduling 的 fresh bounded reports 分别保存到 `/tmp/xv6-fi-filesystem.md` 和
`/tmp/xv6-fi-scheduling.md`。persistence 可引用绑定 pre-#19 runner 的 verified 历史动态报告，但要说明当前
runner 只新增 self-test mutation，并另存 fresh self/static；recovery 历史报告还必须绑定外部 candidate。
不能把仅重跑 self/static 写成 fresh QEMU evidence。

## Oracle、证据、失败路径和局限

| 维度 | 本单元可接受证据 | 不能推出 |
|---|---|---|
| S | pinned symbol、manifest owner/anchor、fixture trigger/runner reducer | runtime path coverage 或通用 framework 已存在 |
| F | registered tests 与 fresh filesystem/scheduling normal regressions | 未注册路径、所有 workload 或跨 seam 等价性 |
| B | nth filesystem failure、persistence `error=1` rejection、stable crash boundaries | 任意 fault site、概率、硬件 fault model |
| C | named scheduling events与 capability-checked persistence gates | fairness、所有 interleavings；显式 scheduling overflow signal 或 persistence live wake-all |
| R | verified recovery IDs、image bytes、offline/second-boot evidence | sector atomicity、FLUSH/FUA 或真实掉电 durability |

unknown/duplicate record ID、owner 与 manifest 不符、test 反向索引缺行、`eligible/fired` 关系错误、非法
blocking point、自依赖 gate、单 hart spin、无 budget、overflow 被接受、资源或 QEMU/temp/image 残留，均使
报告无效。报告必须把 source claim、raw observation、fixture-internal assertion 与 proposed design 分开。

## 退出产物与后续单元

按[报告模板](../resources/fault-injection-and-traceability/report-template.md)提交一份报告，并用
[rubric](../resources/fault-injection-and-traceability/rubric.md)复核。产物至少包含：

- 正向 source-owner-invariant-evidence-test-gap 表与反向 runner/test 索引；
- filesystem nth failure、scheduling named gate、persistence overflow rejection、recovery stable IDs；
- 一条完整 proposed rule、gate capability/lock/can-sleep 表和 overflow fail-closed 合同；
- artifact/runtime provenance、resource/process/image cleanup 与 S/F/B/C/R 限制。

下一单元把这张映射用于“可扩展性与证据综合”，分析容量、锁、per-hart state、
queue、cache 和 storage serialization。它复用本页记录，不重新发明 fault ABI。
