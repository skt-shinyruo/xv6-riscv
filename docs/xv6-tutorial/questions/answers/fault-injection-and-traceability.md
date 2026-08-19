# 故障注入与源码测试追踪答案与证据标准

本页给出检查标准，不提供通用 fault framework 的实现。每条结论必须能回到 manifest、pinned source、
fixture/runner 或保存的 raw report。

## TRACE-00

合格图至少包含 source symbol、唯一 explanation owner、invariant、S/F/B/C/R、runner/test、raw observable
和 remaining gap；反向索引必须从 runner/test 回到同一 record ID。只有函数名和 test 名的列表看不出
owner、oracle strength、cleanup，也无法判断一次 source change 影响哪些有界证据。

## TRACE-01

`quicktests[]`/`slowtests[]` 是 baseline registration；`fsaudit_dirlink_fail()` 只在临时教程 patch 中；
通用 `FI_*`/`faultctl` 尚未实现。三者必须分别标为 `baseline-current`、
`verified-tutorial-fixture`、`proposed-learner-work`。把后两者写成 baseline API 会制造不存在的 source
anchor 与运行命令。

## TRACE-02

fixture 只对 armed PID 生效，`fsaudit_balloc_fail()` 每次匹配增加 `eligible`，第二次才令 `fired=1`
并返回 fail。raw `ALLOCFAIL` 必须同时给出 `fail_at=2/eligible=2/fired=1/write=-1`、size 不变、允许的
empty indirect block，以及 block/inode ledger；close/unlink 后 AFTER 回基线。site/PID 没有出现在 raw
marker，所以它们只能由 fixture source 与隔离执行支持。

## TRACE-03

fixed 路径在持 `p->lock` 的 sleep handoff、condition-lock release、producer acquire/wakeup、scheduler
skip/release/select 与 waiter resume 间建立 named order；broken 路径把 release 与 sleep publication 分开，
观察 miss 后再 rescue。事件中的 lock owner、`intr`、`noff`、pid/hart 与 CLEAN gate ledger 都是 oracle，
串口到达时间不是。双 hart spin 不能缩成 `CPUS=1`。

## TRACE-04

`record_locked()` 达到 `PA_MAX_RECORDS` 后设置 `audit.error=1` 与 `gate_open=1`；`_validate_common()` 又要求
META 与 LEDGER error 一致且为零。self-test 把 good trace 的两处 `error=0` 同时改为 1，证明 reducer
拒绝 overflow report。当前 overflow 分支没有 `wakeup(&audit)`，也没有 live overflow trigger，所以不能
声称已睡 waiter 必然退出；完整 proposed contract 必须同时 error、wake/open all waiters、host reject。

## TRACE-05

10 是 log data complete/precommit，20 是 nonzero commit header complete，30 是 home install complete，
40 是 zero header clear complete。published fixture 的默认 `recoveryaudit_hit()` 是 no-op；verified 外部
candidate 才把这些 ID 接到 transaction semantic boundary，报告必须绑定 candidate digest。guest marker 给
logical order，SIGKILL 后 reopen image 给 host-visible bytes，synthetic mutation 枚举特定 tear；只有结合
first/second boot 与 offline checker 才支持已声明的 recovery 关系，仍不支持物理掉电模型。

## TRACE-06

完整规则字段见正文合同。匹配必须先验证 semantic ID/site/scope，再增加 `eligible`，只有
`eligible==nth` 才增加 `fired` 并执行 action。预期命中却 `fired=0`、一次规则 `fired>1`、unknown ID、
scope 漂移、结果或 ledger 不符都应 fail。地址、行号和 return PC 会随构建漂移，不是 stable ID。

## TRACE-07

`observe-only` 不得等待；`sleep-ok` 需要合法 predicate/lock/producer；持 buffer sleeplock 时 release
actor 不得依赖同一 lock；`spin-2cpu-ok` 需要另一 hart、显式 release 和有限 budget。IRQ context、关中断
单 hart spin、自依赖或无界等待都不合法。capability 是 point 合同，不可由某次成功运行倒推。

## TRACE-08

scheduling `record_event()` 在 slot 超过 32 时直接返回，没有 error 字段；当前 fixed/broken reducer 要求
精确 15/18 events，因此饱和 trace 会因关系不符被间接拒绝，但没有专门 mutation 或 reusable overflow
signal。persistence 的 ReplaySource error rejection 是另一 fixture 的能力；两套 ABI 都不足以证明完整
live wake-all，追踪表必须分别保留 gap。

## TRACE-09

`commit()` 变化至少影响 persistence transaction order/ledger 与 recovery 的 10/20/30/40 before-after；
filesystem 和 ordinary usertests 提供相关 regression，但不替代 crash/image oracle。反向索引应逐项列
runner、raw fields 与 strength，按被改变的 invariant 选择必跑项；未枚举 tear、hardware durability 和
formal ordering gap 不会因回归全绿而消失。

## TRACE-10

registration 只证明 driver 会发现并调用 test。evidence oracle 还要固定 trigger/configuration、解析 raw
schema、验证关系与负向 mutation、记录资源/cleanup，并限制结论。`test_crash()` 的定时 kill 是 baseline
stress regression，不是 recovery fixture 的 exact stable crash point。

## TRACE-11

合格提交把 proposed rule 和当前证据分栏：当前只可引用真实 source/fixture/runner；实现后验收栏必须给
stable ID、scope/nth、result/ledger、gate capability、overflow mutation 与 cleanup；限制栏保留未覆盖
site/interleaving/fault model。任何当前时态的未实现 command/syscall/API、用 PASS 替代 raw relation，或
把一次 injection 称为 correctness proof 的答案都不通过。
