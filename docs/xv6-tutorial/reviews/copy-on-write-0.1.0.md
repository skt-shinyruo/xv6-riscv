# Copy-on-Write 0.1.0 非作者走查记录

- 教程版本：`0.1.0`；单元状态：`verified`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`5b4be45c0a6c582d2b7b96e44727db560a8f24b1`；R3 工作树由下列
  fixture/runner/report SHA-256 绑定，本记录与修订通过最终 amend 纳入晋级提交；
  amend 后的最终提交由 GitHub issue #12 closure comment 记录
- 走查单元或连续路径：`project.copy-on-write`、COW-00..COW-10、外部 candidate 与 audit fixture
- 匿名入口能力：`COW-C20-R4`；已通过进程、调度、虚拟内存前置单元，未参与正文、fixture 或 runner 编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、GNU Make 4.3、Python 3.12.3、`riscv64-linux-gnu-gcc` 13.3.0；focused/related `CPUS=2`，quick `CPUS=2`，full `CPUS=1`，guest RAM 128 MiB，临时 `fs.img`

## 观察到的卡点

首轮审查发现 fixture 依赖 prototype 的 `PTE_COW`/`kref_*` 名称，且 guest marker
含有无法由 host 复算的常量。修订把边界改为六个固定签名的只读
`cowproject_*` adapter；fixture 不引用 candidate 内部名，snapshot 会重复读取
adapter 结果并拒绝可观察的非幂等或 ledger 变化。R3 又加入三代实际写入、late
`exec_bad` 回滚、PID 定向 PAIR 清理和 generation 绑定；所有 marker 现在输出运行时
flags、PA、ref、bytes、status、failpoint、hart 和 ledger 字段。host validator
拒绝缺失、未知、重复字段及关系/值/PA/generation 篡改。candidate 的 `uvmcopy()`
也在引用递增失败时恢复当前 parent PTE。

## 入口、状态和所有权时间线（S）

| 时点 | owner / source edge | PTE 与 PA 观察 | 提交和清理责任 |
| --- | --- | --- | --- |
| fork 前 | `kernel/proc.c:kfork()` -> `kernel/vm.c:uvmcopy()` | 一个 writable user leaf，ref=1 | parent 持有 data PA |
| fork 发布后 | parent/child 两棵 root | 两个 leaf 同 PA，`V/R/U`、无 `W`、normalized COW=1，ref=2 | uvmcopy 逐 leaf 发布；失败逆向撤销 |
| 第一次 user store | `kernel/trap.c:usertrap()` -> candidate resolver | 写者新 PA、`W=1/COW=0/ref=1`；另一 leaf 保持旧 PA/ref | fault 成功后 local `sfence_vma`/`userret` flush |
| kernel copyout | `kernel/pipe.c:piperead()` -> `kernel/vm.c:copyout()` | 与 store 同一拆分关系；跨页失败允许已提交前缀 | caller 保留未消费字节；child wait |
| exit/exec | `kernel/proc.c:freeproc()`、`kernel/exec.c:kexec()` | unmap/commit/bad 不重复释放；pid/trapframe 边界保持 | owner 负责 uvmunmap/freewalk |
| wait/free | `kernel/proc.c:kwait()` -> `proc_freepagetable()` | 最后 owner ref 归零，CLEAN ledger 回基线 | parent wait 后关闭 pipe/gate/failpoint |

特殊映射（trampoline、trapframe、root/intermediate）不计入 data ref ledger；A/D
和实现临时位只在 host 比较时从 flags mask 排除。adapter ABI 的正式签名为：

    uint cowproject_ref(uint64 pa);
    uint cowproject_active(void);
    uint cowproject_total(void);
    uint cowproject_errors(void);
    int cowproject_free_pages(void);
    int cowproject_is_cow(pte_t pte);

## 验收产物

- audit fixture：`resources/copy-on-write/audit-fixture.patch`，SHA-256
  `ddf86e24c513ec324219915723b36ff6c985f959c38ce83fa8c86e11338b0b23`
- runner：`resources/copy-on-write/run-project.py`，SHA-256
  `f57a27091f4ab01a4c31530b2649b11ab502de1b68a0a7e302fc4946d08a7087`
- 主 candidate（仓库外）：`/tmp/ticket12-candidate-adapter.patch`，SHA-256
  `41e70606040ec27e625ef50c3e9ea3912e145101181b0025fbb5e7c992c94083`
- 改名 candidate：`/tmp/ticket12-candidate-renamed.patch`，SHA-256
  `b0c20ae01aae87f8ffd7201c3caa964cd0a78687e433f5af370d9dcf62b0435f`
- 主机器报告：`/tmp/ticket12-final-main-r4.md`，SHA-256
  `5f3965ec25b27e1ef086beb9e05548dbb28afc38f7b6a0dd8f5399e0cfd9168e`
- 改名机器报告：`/tmp/ticket12-final-renamed-r4.md`，SHA-256
  `1aa192f80a35cb3534467c375533a6aa678747873bdb00f3f2d9e5f241df4047`

主 candidate 的 focused raw marker 如下；改名 candidate 输出同一字段契约：

```text
COW SHARE pa=0x87F2B000 parent_new=0x87F37000 child_old=0x87F2B000 before_flags=0x1D3/0x1D3 before_refs=2/2 before_cows=1/1 after_flags=0xD7/0x1D3 after_refs=1/1 after_cows=0/1 fast_flags=0xD7 fast_ref=1 fast_cow=0 parent_alloc=1 child_alloc=0 values=34/51
COW MULTI root=4 child=6 grandchild=7 shared_pa=0x87F37000 before_flags=0x1D3/0x1D3/0x1D3 before_refs=3/3/3 before_cows=1/1/1 after_pa=0x87F37000/0x87F37000/0x87F15000 after_flags=0x1D3/0x1D3/0xD7 after_refs=2/2/1 after_cows=1/1/0 before_values=33/33/33 after_values=33/33/42
COW LAZY before=0/0 before_flags=0x0/0x0 parent_pa=0x87F32000 child_pa=0x87F2F000 after_flags=0xD7/0xD7 after_refs=1/1 after_cows=0/0 values=102/85
COW COPYOUT parent_pa=0x87F32000 child_pa=0x87F46000 flags=0x1D3/0xD7 refs=1/1 cows=1/0 parent=68 child=97/98/99/100
COW PARTIAL generation=5 eligible=2 first=16 retry=16 fail_at=2 fired=1 parent=0x87F32000/0x87F33000 child=0x87F39000/0x87F1A000 parent_flags=0x1D3/0x1D3 child_flags=0xD7/0xD7 parent_refs=1/1 child_refs=1/1 parent_cows=1/1 child_cows=0/0 parent_values=65/65 child_values=32/48
COW OOM generation=6 eligible=1 status=-1 fail_at=1 fired=1 old_pa=0x87F33000 old_flags=0x1D3 old_ref=1 old_cow=1 fast_alloc=0 value=51
COW ROLLBACK generation=7 fork=-1 fail_at=8 fired=1 eligible=8 preexisting_pa=0x87F33000 preexisting_flags=0x1D3 preexisting_ref=2 preexisting_cow=1 new_parent_flags=0xD7 new_parent_ref=1 new_parent_cow=0 fast_flags=0xD7 fast_ref=1 fast_cow=0 fast_alloc=0 setup_free=32474 after_fork_free=32474 values=65/82/67
COW PAIR generation=8 pids=14/15 harts=0/1 arrived=3 gate_open=1 eligible=2 old=0x87F33000 new=0x87F14000/0x87F13000 flags=0x1D3/0xD7/0xD7 refs=1/1/1 cows=1/0/0 values=115/114
COW BOUNDARY text_pa=0x87F24000 text_flags=0x5B text_ref=3 text_cow=0 guard_parent_pa=0x87F29000 guard_child_pa=0x87F2A000 guard_flags=0x7/0x7 guard_cows=0/0 guard_status=-1 text_status=-1 tail_status=0
COW TEARDOWN shrink_leaf=0 child_pa=0x0 parent_pa=0x87F33000 parent_flags=0x1D3 parent_ref=1 parent_cow=1 exec_bad=-1 bad_before_pa=0x87F33000 bad_after_pa=0x87F33000 bad_flags=0x1D3/0x1D3 bad_refs=2/2 bad_cows=1/1 bad_value=25 exec_status=0 errors=0
COW PASS cases=10
COW CLEAN base=32495 after=32495 errors=0 active_before=204 active_after=204 refs_before=204 refs_after=204
```

逐场景验收表：

| marker | trigger / source edge | host 复算的 observable | side effects / resource result |
| --- | --- | --- | --- |
| SHARE | fork 后 parent/child 读共享、分别写 | 同 PA/ref=2；写者新 PA；values=34/51；fast `W=1,COW=0` | child wait、pipe close、场景 free 回基线 |
| MULTI | root -> child -> grandchild；grandchild 单独写 | before 三 pid 同 PA/ref=3；after refs=2/2/1、PA 分离、values=33/33/42 | 两层 wait，data PA 回收 |
| LAZY | hole fork 后两侧首次写 | before 无 leaf；PA 分离；values=102/85 | growth shrink、child wait |
| COPYOUT | `piperead` 向 child 写四字节 | parent=68，child=97/98/99/100，PA 分离 | pipe/gate/child 清理 |
| PARTIAL | 跨页 copyout，generation=5 的第二 eligible allocation fail_at=2 | first/retry=16，四个 PA 两两不同，parent=65/65，child=32/48，fired=1 | 第一段提交；未消费字节留在 pipe；wait 后回基线 |
| OOM | store fault generation=6 的首分配失败 | status=-1、old PA/value/ref=非零/51/1、fired=1；解除后 fast allocation=0 | failpoint 清零，child wait |
| ROLLBACK | fork generation=7，fail_at=8 | fork=-1、eligible=8、旧 PA/ref/values 不变；setup/free 相等 | empty intermediate 可延迟到 teardown |
| PAIR | generation=8 的两 child 在 kalloc gate 同时拆页 | arrived=3、gate_open=1、hart distinct、PID 为正、三 PA distinct、values={114,115} | 每个 child 独立 finish pipe；按 PID 依次 wait，barrier 清零 |
| BOUNDARY | text/guard/tail shrink | text W=0/COW=0；guard U=0/COW=0；status=-1/-1/0 | 尾页释放，场景 ledger 回基线 |
| TEARDOWN | child shrink、合法 ELF exec commit、合法 ELF late bad | child leaf=0；bad `exec=-1` 前后 PA/flags/ref/cow/value 保持；exec_status=0、parent ref=1/errors=0 | wait 三个 child，临时 bad image 逆向释放，最后 unmap/free |

## 回归、失败窗口与并发限制

| 层级 | 命令 / 配置 | 结果 |
| --- | --- | --- |
| static | `python3 .../run-project.py --static-only` | `static, build, cleanup` |
| focused + related | runner 内 `cowtrace`、forktest、copyout、lazy_copy、sbrkfail，CPUS=2 | 全部精确 marker；每项一个 `ALL TESTS PASSED`，无 COW 泄漏 |
| quick | `test-xv6.py -q usertests`, CPUS=2 | 一个 `ALL TESTS PASSED` |
| full | `test-xv6.py usertests`, CPUS=1 | 一个 `ALL TESTS PASSED` |
| compatibility | 改名 candidate 重跑上述全部阶段 | `static, F/B/C, focused, related, quick, full, cleanup` |

主 focused/quick/full transcript SHA-256 分别为
`cdfe724e6613fef7ce6074a3c740f66f40ef25acb7293db4beb0b129b434b5ba`、
`912be1f87c2477bbdde063676dfe3f220d999b00719e29f04ba284265a57746f`、
`773f4b435e8f824b8681e61246b2af2571b11ef58d787d8f6bb252d807c1989f`。
改名报告对应 focused/quick/full 为
`cdfe724e6613fef7ce6074a3c740f66f40ef25acb7293db4beb0b129b434b5ba`、
`92ef7acdfaaae1fb0bcd9f4351c989f372f823b46f67466e0dc66eb575da1e3d`、
`7ca95da527bb62304cd417408ec79468d9d181a6885c8764b0ab77eafe9e02dd`。

C 只证明这次双进程受控到达和 ref 更新；不证明公平性、所有交错、同一地址空间
线程或 remote TLB shootdown。local `sfence_vma()`/`userret` flush、A/D mask、
有限 VA/ELF 场景和模拟 OOM 都已写入实验限制。R 为 N/A，不声称 crash/recovery
或 persistence。重复 adapter 读取只能拒绝可观察的非幂等结果，不能形式化证明
实现没有其他隐藏副作用；源码 review 仍须检查其锁与只读边界。

## 资源与清理证明

- 主报告 outer ledger：`base=32495, after=32495, errors=0, active 204->204,
  refs 204->204`；改名报告同样恢复。
- 每个 scenario 的 child 都在 parent `wait()` 后采样；pipe、gate、failpoint 和
  pair state 已关闭/清零；临时导出按 audit -> candidate 逆序撤销并 `make clean`。
- QEMU/driver 进程组均消失；私有 `fs.img` 已删除。
- 共享工作树/索引/内容指纹：`d2d1ec54a73493f974034acabe2e9e1b08adcf015cdb4f4535512a303d25e7fe`
  （before/after 相同）；共享 `fs.img`：`missing`（before/after 相同）。

## 修正与复查

正常/development publication validator、generated navigation `--check`、validator
单测 5/5、Python 编译和 `git diff --check` 全部通过。host 对未知/重复字段、
MULTI ref/value、PA/generation、PASS 计数和 CLEAN active ledger 的篡改均拒绝；
这些是独立负向 oracle。该记录是唯一合并
出口报告，`--report` 文件是其附带的机器原始记录，不是另一份出口。

## 结果

| 单元 | 结果 | 晋级依据 |
| --- | --- | --- |
| `project.copy-on-write` | verified | S/F/B/C raw marker、adapter 跨实现复核、确定性 rollback/OOM、双 hart 证据、回归、清理和限制均已由非作者独立复查 |

学习者签名：`COW-C20-R4`（确认 candidate 在仓库外，未将 fixture 当 solution）。

非作者 reviewer 签名：`COW-NONAUTHOR-R3`（确认上述命令、raw marker、负向 oracle 与清理证据）。
