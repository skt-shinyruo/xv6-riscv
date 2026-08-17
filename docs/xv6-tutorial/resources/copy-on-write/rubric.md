# Copy-on-Write 证据项目 rubric

本 rubric 只定义可复核的证据合同。audit-fixture.patch、run-project.py 和
cowtrace 是 publication/non-author walkthrough 的 audit-only seam；它们不是
生产修复、学习者答案或可提交的完整实现。完整 candidate 必须来自仓库外，并在
报告中只记录路径和 digest。

fixture 不约束 candidate 的内部 flag、refcount 表或锁，但完整运行要求 candidate
实现以下只读 audit adapter ABI：

    uint cowproject_ref(uint64 pa);
    uint cowproject_active(void);
    uint cowproject_total(void);
    uint cowproject_errors(void);
    int  cowproject_free_pages(void);
    int  cowproject_is_cow(pte_t pte);

前五项把 candidate 的 owner ledger 规范化为计数，最后一项只把 candidate 自选的
软件位归约为 0/1。adapter 不得改变 PTE、ref 或 allocator 状态；runner 会检查
这些符号由外部 candidate 提供，而 fixture 本身不得引用 PTE_COW 或 kref_* 等
任一原型的内部名字。

## 通过前提

| 项目 | 通过标准 |
| --- | --- |
| baseline | 记录 manifest pinned baseline、教程 commit、host/toolchain、QEMU/CPUS、candidate/fixture/runner/report SHA-256；candidate 不在仓库内 |
| model | 以 riscv.h、kalloc.c、vm.c、proc.c、trap.c 的实际符号解释 PTE、PA、ref、owner 和 teardown；adapter 只规范化观察，不以完整 patch 代替模型 |
| normal F | SHARE、MULTI（含一代实际写入）、LAZY、COPYOUT、BOUNDARY、TEARDOWN 的 raw marker 与 PTE mask、PA/ref、bytes/status、wait 后 ledger 全部满足 |
| failure B | PARTIAL 精确报告第一段/第二段 count、四个互异 PA 与 generation/fail_at=2；OOM 报 status=-1、非零 old PA/value/ref、generation/fired=1、fast path 无分配；ROLLBACK 报 generation=7、旧映射/bytes/ref 不变与 setup ledger |
| ownership | writable user page 的共享、拆分、最后 owner 释放和 pre-existing read-only text/guard 可区分；页表页、trampoline、trapframe 不混入 data ref 结论 |
| copyout | kernel copyout() 跨页和 pipe/file 调用者的提交语义被单独说明；不能把 helper 的 -1 泛化成所有 syscall 的统一返回值 |
| multihart C | PAIR 的 barrier 到达数、开放状态、两个 hart 与三组 PA/ref 都由 host 重算；CPUS=2 只作为受控并发证据 |
| TLB | 明确 local sfence_vma()/userret 前提和没有 remote shootdown；不能把 CPUS=2 marker 写成线程或形式化证明 |
| regressions | focused、related、quick、full 均有命令、退出状态、精确 marker/transcript digest；无 fixture marker 泄漏到基线回归 |
| cleanup | 每个 child wait() 后资源账本可解释；pipe、barrier、failpoint、临时 patch/build/tree、私有 fs.img 和进程组清理；共享工作树与共享镜像 digest 不变 |
| limits | S/F/B/C 只写实际证据；R 明确 N/A；说明 A/D mask、模拟 OOM、有限 VA/ELF、固定 page size、guest marker 与未覆盖交错的限制 |

## 原始 marker 约束

runner 应逐行解析以下标记，不接受只截取 COW PASS：

    COW SHARE ...
    COW MULTI ...
    COW LAZY ...
    COW COPYOUT ...
    COW PARTIAL ...
    COW OOM ...
    COW ROLLBACK ...
    COW PAIR ...
    COW BOUNDARY ...
    COW TEARDOWN ...
    COW PASS cases=10
    COW CLEAN base=<n> after=<n> errors=0

具体 PA、pid 和 hart id 每次可变。marker 必须同时给出 raw flags、normalized cow、
ref 和适用的 bytes/status；host 必须复算关系，而不是比较固定数字或信任 guest
预先归约的布尔值：

- SHARE 在 fork 后同 PA/ref=2；parent 写后新旧 PA 分离，parent ref=1、child
  仍为旧共享页；child fast path 的 eligible=0，写后必须 W=1、cow=0、ref=1。
- MULTI 先证明 root/child/grandchild 三 owner 同 PA/ref=3，再由 grandchild 单独写入；after 必须是 root/child 仍共享旧 PA/ref=2、writer 为新 PA/ref=1，bytes=33/33/42。
- LAZY fork 前后目标 VA 都无 PA；child、parent 首次写各自获得 private PA，
  另一个进程的初始值不变。
- COPYOUT 的 child 读回四字节 payload 与 host 写入完全相同，parent 原值不变，
  两个 PA/ref=1。
- PARTIAL 的跨页写在 generation=5、fail_at=2 后返回第一段 16、重试第二段 16；parent 两页
  原值保持，child 两页各自 private 且四个 PA 互异，eligible=2、fired=1。
- OOM 的 generation=6 child status=-1、非零旧 PA/value/ref 保持，第一次 failpoint fired，
  关闭 failpoint 后同一写可成功且 fast_alloc=0；该字段只描述 fixture 的 arm 窗口。
- ROLLBACK 的 generation=7 fork=-1、fail_at=8，已有共享页 PA/ref/bytes 不变；ref=1 fast path
  写后必须 W=1、cow=0 且 PA 不变；setup ledger
  相等即可表示 fork rollback，当 high lazy 分支的 empty intermediate 允许留到
  teardown；外层 CLEAN 才证明最终总账。
- PAIR 必须有 generation=8、两个正 PID 的 child 到达位图 arrived=3（1|2）且 marker 显式
  gate_open=1，两个 child 的 hart 不同，旧 PA 与两个新 PA 两两不同、ref=1；每个 child
  使用独立 finish pipe 按 PID wait；这是 barrier 受控交错，不是所有并发证明。
- BOUNDARY 中 text 仍同 PA、W=0、COW=0，guard 仍 U=0、COW=0；两个独立 child
  的 text/guard 写入都返回 status=-1；tail shrink 后 child 写成功并正常退出。
- TEARDOWN 中 child shrink 后无 leaf；合法 ELF 的 late `exec_bad=-1` 前后旧 PA、flags、
  ref、COW 和 value 保持，另一个合法 ELF commit child status=0，parent ref=1、
  ref_errors=0，场景 ledger 恢复。

## 直接不通过

- 发布或要求复刻完整 COW candidate；把 fixture 的 instrumentation 当作生产代码。
- 用 timeout、shell prompt、guest PASS、单个 usertests 或固定绝对 PA/pid 当
  correctness oracle。
- 把 text/guard 的原始只读语义误判为 COW，或允许共享 leaf 同时带 W。
- 只检查 child 被 kill，不检查 OOM/partial failure 的 old bytes、PTE、ref、
  failpoint 记录和 wait 后 ledger。
- 要求 high-VA L0 fail 后瞬间整棵页表逐位恢复，忽略 empty intermediate 的
  teardown 契约；或反过来不要求外层 COW CLEAN base==after。
- 把 copyout() 的 partial commit、exec/unmap/exit 的 owner 交接和 sfence_vma
  省略为“写 fault 会复制页”。
- 将 CPUS=2 PAIR 写成 remote TLB shootdown、共享地址空间线程、全交错或形式化
  并发证明；将 R 写成已验证的 crash/recovery。
- 让 test-only syscall、failpoint、PTE_COW mutation 或 fixture user program
  进入 executable baseline。

## 独立复核

reviewer 应在独立临时树运行 run-project.py --report <path>，重新解析全部 raw
marker，计算 baseline/fixture/runner/report digest，并核对 focused、related、
quick、full 与 cleanup。reviewer 至少重算一轮 SHARE、PARTIAL、ROLLBACK 和 PAIR；
若报告只能借助 rubric 之外的同步副本才能成立，则出口不通过。
