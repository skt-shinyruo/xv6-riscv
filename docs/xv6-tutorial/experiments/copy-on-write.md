# 证据项目：Copy-on-Write fork

## 问题场景与本单元成果

fork 若为每个用户页立即复制一份物理页，会把读共享和大量只读页变成不必要的
分配；若只把两个 PTE 指向同一个 PA，任一进程的写入又会破坏另一进程的观察
结果。本项目要求学习者在现有进程、地址空间和 lazy fault 模型上建立可复核的
共享所有权协议：共享页可读，写入者获得自己的可写页，最后一个 owner 退出时
PA 恰好回收。

出口不是一份“能通过测试的 patch”，而是一份合并报告包：源代码不变量图、逐
场景运行记录、资源账本、回归结果、清理证据和证据边界必须来自同一 pinned
baseline。公开资源只提供 audit-only fixture 与 partial oracle；完整 candidate
必须由调用者从仓库外通过 --candidate PATH 传入，不能把某个实现发布为
authoritative solution。

本单元的主要可观察成果是：读共享、一次写拆分、多代 fork、lazy hole、kernel
copyout、跨页部分失败、OOM/partial rollback、双 hart 同时拆页、guard/text
边界和 teardown 都能由 PTE/PA/ref/free-page 字段复算。出口提交物只有一份报告
包，不把每个 marker 当成独立结论。

## 前置单元与暂存黑盒

硬前置是[调度、同步与等待](../core/scheduling-and-synchronization.md)和
[虚拟内存、lazy fault 与 NX](../core/virtual-memory.md)。前者提供 p->lock、
跨 hart 运行和确定性 gate 的阅读基础；进程生命周期单元提供 fork()/wait()
的所有权背景；后者提供 Sv39 的
walk()/mappages()、lazy hole、copyout()、sfence_vma()、exec replacement
和最终 freewalk() 清理时点。

本项目解除“用户页如何共享 PA、何时把写保护页变成 private、引用归零由谁释放”
这组黑盒，并把它们连到现有入口：kernel/proc.c:kfork()、
kernel/vm.c:uvmcopy()、kernel/trap.c:usertrap() 与
kernel/vm.c:copyout()。下列问题仍是显式边界而非本项目的承诺：

- 当前 branch 没有同一地址空间的用户线程；双 hart fixture 只安排两个不同进程
  在同一 PA 上同时触发写 fault，不构成完整线程内存模型或 remote shootdown 证明。
- PTE_COW 的具体软件位、refcount 存储方式、锁粒度和回滚数据结构由学习者选择；
  rubric 只接受可观察契约，不要求复刻任一 candidate 的内部符号。为了让公开
  fixture 跨实现复核，candidate 另须提供一个很窄的 audit adapter ABI，签名固定为：

      uint cowproject_ref(uint64 pa);
      uint cowproject_active(void);
      uint cowproject_total(void);
      uint cowproject_errors(void);
      int cowproject_free_pages(void);
      int cowproject_is_cow(pte_t pte);

  它们只读地规范化 PA/ref、free-page ledger 与“该 leaf 是否可拆分”；这不是
  生产 API，也不规定内部表、锁或软件位。
- QEMU 的 A/D 位、缓存实现、物理地址布局和页分配顺序可能变化；报告必须比较
  flags mask、关系和状态，不得固定绝对 PA、pid 或串口时序。
- R（crash/recovery/persistence）不适用于本项目；磁盘、log 和 reboot 由后续阶段
  负责。

## 最小模型和关键不变量

### 一个用户 leaf 的状态与所有权

先把一个 va 的可见状态写成三元关系，而不是把“COW”当作一个布尔测试：

    (PTE flags, PA, ref(PA))

正常共享状态必须能同时解释以下事实：两个有效 user leaf 指向同一个 PA；该 leaf
不允许 user 写入；软件保留位（如果实现选择使用）能区分“共享只读”与原本就只读
的 text/guard。拆分后，触发写入的 leaf 指向新 PA、带 W 且不再被标成 COW，另一
leaf 仍指向旧 PA；旧 PA 的 ref 恰好减一。只有最后一个映射被 teardown 时，旧 PA
才可返回 allocator。

这不是实现步骤。报告必须以当前 candidate 的实际字段证明四个不变量：

1. 映射不变量：有效 user leaf 的 VA、PA 和 V/R/W/X/U mask 相互一致；PTE_FLAGS
   中的 A/D 或 fixture 临时位不应污染比较。
2. 所有权不变量：每个被共享的 PA 的 ref 等于可见 owner 数，不把页表页、
   trampoline PA、proc-owned trapframe PA 混进用户 data ledger。
3. 写者不变量：一个 owner 写入后只改变自己的 bytes 和 leaf；同一 PA 上的其他
   owner 仍可读到原值。
4. 销毁不变量：uvmunmap()、proc_freepagetable()、exec commit/bad、fork rollback
   和最后一个 wait() 的释放责任不重复也不遗漏。

### 从入口到回收的流程骨架

breadth-first 先画这条骨架，再进入局部实现：

    kfork
      -> uvmcopy(old, new, sz)
      -> parent/child PTE + PA/ref visible
      -> user store fault 或 kernel copyout
      -> resolver / retry / private bytes
      -> exit -> wait -> unmap/freewalk -> allocator ledger

exec、lazy hole、guard page、text page 和 partial failure 是这条骨架上的分支，
不是另一个“简化版 COW”。任何分支都要标明：谁先发布 PTE，谁拥有 ref，失败
发生在提交前还是提交后，以及 parent 在等待 child 前后能观察什么。

### 进程级资源账本

fixture 的 cowaudit_snapshot 记录 pa、flags、ref、free_pages、active_pages、
ref_total 和 ref_errors。这些字段只能支持总账与被观察 leaf 的关系；它们不能凭
空证明所有 PA 都已分类。因此报告还要记录每次分配/释放的触发器、child wait()
时点、pipe/gate 的关闭和临时构建树的逆向操作。

## 源码追踪计划

使用稳定 path:symbol，不要把行号或 candidate 的内部函数名当作教程契约。唯一
约定的 candidate-side seam 是上面的 audit adapter ABI：

    rg -n '^#define PTE_|^PTE_FLAGS|^sfence_vma' kernel/riscv.h
    rg -n '^kalloc\(|^kfree\(|^freerange\(' kernel/kalloc.c
    rg -n '^walk\(|^mappages\(|^uvmunmap\(|^uvmfree\(|^uvmcopy\(|^copyout\(' kernel/vm.c
    rg -n '^kfork\(|^freeproc\(|^proc_freepagetable\(' kernel/proc.c
    rg -n '^usertrap\(|^prepare_return\(' kernel/trap.c
    rg -n '^piperead\(|^pipewrite\(' kernel/pipe.c
    rg -n '^kexec\(' kernel/exec.c
    rg -n '^copyout\(|^forktest\(|^lazy_copy\(|^sbrkfail\(' user/usertests.c

按下面顺序读，而不是先读某个“标准 COW 实现”：

1. riscv.h 的 PTE flag/PA 转换和 sfence_vma()，再对照当前基线中没有 COW 的
   uvmcopy()、uvmunmap()、uvmfree()。
2. kalloc()/kfree() 的 whole-page allocator 与 proc_freepagetable() 的页表页/
   数据页释放边界；记录原有 owner，再看 candidate 如何扩展账本。
3. kfork() 到 parent/child 两棵 user root 的可见 PTE；明确 lazy hole、guard 和
   text 不应被同一个写权限规则吞掉。
4. usertrap() 的 store fault 分支与 copyout() 的逐页循环；把硬件 fault 的原始
   stval 和 helper 的页首参数分开。
5. pipe.c/exec.c 的调用者，确认 kernel 写入用户内存和 exec 临时映像是否走同一
   ownership contract。
6. prepare_return()、trampoline.S:userret 与 sfence_vma()；说明 local userret
   flush 的作用和未实现的 remote shootdown。

最后把读到的关系映射到 fixture 的 raw marker：COW SHARE、MULTI、LAZY、COPYOUT、
PARTIAL、OOM、ROLLBACK、PAIR、BOUNDARY、TEARDOWN，以及外层 COW PASS/CLEAN。
marker 是待复算的输入，不是答案。

## 观察任务

观察任务不应改 executable baseline。先在干净临时树记录：

    python3 docs/xv6-tutorial/resources/copy-on-write/run-project.py --static-only

完整实验由调用者在仓库外准备 candidate 后运行：

    python3 docs/xv6-tutorial/resources/copy-on-write/run-project.py \
      --candidate /absolute/path/outside/repository/candidate.patch \
      --report /tmp/cow-report.md

runner 应导出 manifest 的 pinned baseline，在私有树按顺序应用 candidate 和
audit-fixture.patch，并拒绝 candidate 位于当前仓库内。fixture 的 syscall、
cowaudit_snapshot 和 user cowtrace 只是 audit seam；它们不应被复制进提交的
executable baseline。

学习者先完成一张 source/runtime 图，再读 raw marker：

| 观察组 | 要回答的关系 | 最小字段 |
| --- | --- | --- |
| SHARE/MULTI | 读共享与多代 owner 是否同 PA、ref 是否等于 owner 数；一代写是否只拆自己的 leaf | pid、PA、raw flags、normalized cow、ref、before/after values |
| LAZY | fork 时 hole 是否仍无 leaf，两个进程首次写是否各自物化 | before/after pa、flags、values |
| COPYOUT/PARTIAL | kernel 写入是否触发同一拆分；跨页第二段失败时第一段是否提交 | child bytes、page pa、fail_at、eligible/fired |
| OOM/ROLLBACK | 分配失败、fork partial failure 后旧 bytes/PTE/ref 是否恢复；generation 与窗口是否匹配 | status、record、PA、setup/free ledger |
| PAIR | 两个不同 pid 在 barrier 后是否都完成独立拆页 | arrived、gate_open、hart1/2、三组 pa/ref |
| BOUNDARY/TEARDOWN | text/guard/tail 与 shrink、合法 ELF commit/late bad、等待的边界是否保持 | raw flags、normalized cow、ref、bad exec 前后 PA/value、guard/text status、ref_errors |

不得把“进 shell”、COW PASS 或 timeout 当作 oracle。每个 marker 都要在报告中
写出 trigger、source edge、observable、允许副作用、资源结果和失败时点。

## 有界修改任务

学习者的修改范围是一个私有实验分支，不是把 fixture 或 candidate 发布到教程：

- 选择并记录 COW flag 表示、PA ref owner 记录和锁/中断约束；在报告中解释为何
  它们与现有 kalloc/kfree、页表 teardown 和 scheduler 约束相容。
- 对 uvmcopy、用户 store fault、copyout、unmap/exec/rollback 的修改分别写出
  提交点与失败清理责任。不要仅贴实现，也不要假设 eager/lazy/text/guard 共用
  一条路径。
- 用 fixture 的 deterministic failpoint 与 pair barrier 复核 partial rollback、
  OOM 和两个 hart 的同时拆页；不能以随机 sleep 或压力运行代替。
- 运行 focused、related、quick、full；保存 raw marker 和 host 解析结果。C 只表示
  受控双 hart 事件，R 必须明确 N/A。

fixture 补丁是 audit-fixture.patch，其明确列出的 adapter ABI 和 raw marker
可供独立复核，但不构成
“如何写 COW”的顺序化答案。报告中若出现完整 candidate diff，应只作为仓库外
输入的 digest/路径记录，不能把 patch 复制进公开资源。

## Oracle、证据、失败路径和局限

| 维度 | 触发器与精确 oracle | 允许副作用/清理 | 不能推出 |
| --- | --- | --- | --- |
| S | pinned baseline + 11-path audit patch；translation/ownership、特殊 leaf、full trampoline、fault policy、exec commit/bad、teardown 与 candidate 实际控制流一致 | 私有导出树、临时 patch/build、fixture syscall 状态；结束时逆向 patch | 静态关系不证明所有 runtime 交错 |
| F | SHARE、MULTI（含一代写入）、LAZY、COPYOUT、BOUNDARY、TEARDOWN 的 values/status/pa/ref 满足报告字段；同一 owner 的 bytes 不互相污染；late exec bad 保留旧 image | child 均由 parent wait()；pipe/gate 关闭；free ledger 回到场景基线 | 不证明未观察 VA、所有 ELF 或所有 syscall |
| B | PARTIAL 的第一段/第二段 count、四个互异 PA 与 fail_at=2 一致；OOM 的 status=-1、非零 old PA/value/ref、fired=1；ROLLBACK 的 generation=7、fork=-1、旧 PTE/bytes/ref 和 setup ledger 保持 | failpoint 只在指定 generation/进程窗口 arm，随后必须清零；empty intermediate 可延迟到 teardown，但最终必须回收 | 模拟 OOM 不等于真实内存压力；总账不能命名每个未观察 PA |
| C | PAIR 的两位 child 到达位图 arrived=3（1\|2）、公开 marker 的 gate_open=1、两个 child 位于不同 hart，且旧 PA 与两个新 PA 两两不同、refs=1/1/1 | 双 hart QEMU 使用独立进程组；barrier、pipe 和 child 均清理 | 只证明受控双进程写拆分；不证明 remote TLB shootdown、共享 root 线程、全交错或形式化正确性 |
| R | N/A | 无 crash/reboot/persistence claim | 私有镜像只隔离写入 |

TLB 结论必须单独写出：修改当前 hart 正在使用的 leaf 后，candidate 若调用
sfence_vma()/userret local flush，可支持本实验的再次访问；当前 xv6 没有
同一地址空间并发在多个 hart 上运行的协议，因此 fixture 的 PAIR 提供的是受控
双进程 C 证据，不是 remote shootdown 证据。报告还应注明 A/D mask、
固定页大小、有限 VA/ELF 场景和 guest marker 解析的局限。

## 退出产物与后续单元

唯一出口是按[报告模板](../resources/copy-on-write/report-template.md)完成的一份
合并报告包。runner 的 --report 是其中不可变的机器原始记录附录，不是代替
学习者解释的完整报告；合并包至少包括：

1. pinned baseline、教程 HEAD、candidate/fixture/runner/report digest 和环境；
2. 一张从 kfork 到 wait/freewalk 的 owner/PTE/PA/ref 时间线；
3. SHARE、MULTI、LAZY、COPYOUT、PARTIAL、OOM、ROLLBACK、PAIR、BOUNDARY、
   TEARDOWN 的 raw marker 与逐字段 oracle；
4. focused/related/quick/full 回归、失败窗口、双 hart 限制和 local sfence/
   无 remote shootdown 结论；
5. child、pipe、gate、failpoint、临时树、私有镜像和 free/ref ledger 的清理证明；
6. S/F/B/C/R 边界与不能推出的结论。

先用[问题链](../questions/copy-on-write.md)完成 COW-00..COW-10，再用[答案与证据
标准](../questions/answers/copy-on-write.md)自查。问题页不会迁移旧的 VM 题目，
也不会和旧路径维护同步副本；本单元已完成非作者走查、独立 review、runner 和
generated-navigation 验证并标记为 verified。后续修改必须重新绑定一份报告包。
