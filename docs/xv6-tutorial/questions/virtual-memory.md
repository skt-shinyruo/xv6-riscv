# 虚拟内存问题

本页由 `core.virtual-memory` 拥有。先完成[虚拟内存、lazy fault 与 NX](../core/virtual-memory.md)
及同一份地址空间生命周期报告，再查看[答案与证据标准](answers/virtual-memory.md)。

稳定入口包括 `kernel/vm.c:walk()`、`kernel/vm.c:mappages()`、
`kernel/vm.c:vmfault()`、`kernel/proc.c:proc_pagetable()`、
`kernel/proc.c:proc_freepagetable()`、`kernel/exec.c:kexec()`、
`kernel/trap.c:usertrap()`、`kernel/trap.c:prepare_return()`、
`kernel/trampoline.S:uservec` 和 `kernel/trampoline.S:userret`。每题都应沿
path:symbol 与报告字段复算，不能用行号或一次运行的绝对 PA 代替关系。

## 问题

### VM-00

从首进程空 user root 到 `/init` 映像，再到 lazy growth、fork、exec replacement
和 parent wait 回收，一个用户地址空间依次由哪些对象和 owner 构成？kernel root
与 per-process user root 在这条全景中怎样协作？

证据要求：先画 `allocproc -> proc_pagetable -> kexec -> usertrap/return ->
fork/exec -> exit/wait/freewalk` 的 breadth-first 骨架，再分别标出 root、
intermediate、leaf、data PA、trapframe PA 和 trampoline PA；暂不深入 COW。

### VM-01（原 VM-01）

`sbrklazy()` 只增加 `p->sz` 后，哪些地址“逻辑有效但无 leaf”？fork、负向 shrink、
exit/wait 和 exec commit 分别怎样处理这些 holes？

证据要求：比较 `sys_sbrk()`、`uvmcopy()` 的两个 `continue`、`uvmdealloc()`、
`uvmfree()` 和 `kexec()`；说明稀疏空间节省物理页，却仍可能按 `sz/PGSIZE`
扫描，不能把 `p->sz` 当成映射数量。

### VM-02（原 VM-02）

同一未物化 lazy 地址分别传给直接 user load/store、`copyin()`、`copyout()` 和
`copyinstr()` 时，哪些路径会补页，收到的是原始地址还是页首，会产生哪些
用户可见差异？

证据要求：连接 `usertrap -> vmfault` 与三个 helper；用报告的 load/store/
copyin/copyout access 字段证明来源，并说明 `copyinstr()` 为什么没有同类 marker。

### VM-03（原 VM-03）

`copyout()` 跨两页时，如果第一页成功、第二页 lazy 分配或权限检查失败，第一页
写入是否回滚？上层系统调用能否统一承诺 `-1`、0 或 partial count？

证据要求：按 `copyout()` 循环逐页列出 `len/src/dstva` 的提交点，再追踪至少一个
file/pipe 调用者怎样解释失败。不得虚构当前源码不存在的跨页事务。

### VM-04（原 VM-04）

`kexec()` 如何在临时 user root 中建立 ELF、guard、stack 和 argv，并把 commit
与 `bad` rollback 分开？为什么不存在文件或坏 ELF 不能验收“后期临时映像
清理”？

证据要求：指出 `proc_pagetable -> loadseg -> uvmalloc -> copyout -> commit/bad`
的顺序；报告必须用有效 `echo` 加两个合法长参数得到 status=42，而不是早期失败。

### VM-05（原 VM-05）

exec commit 后为什么可以立即释放旧 user root/leaf，却不会释放当前 kernel stack、
trapframe PA、fd/cwd 或 pid？`sys_exec()` 在 commit 前导入 argv 又允许旧映像产生
哪一种 lazy side effect？

证据要求：区分 proc identity、kernel direct-map 对象和 user image；连接
`oldpagetable`、`proc_freepagetable()` 与 `sys_exec():fetchaddr/fetchstr`。

### VM-06（原 VM-06）

trampoline 和 trapframe 为什么存在于每个 user page table，却不能带 `PTE_U`？
从 `uservec` 到 `usertrap` 再到 `userret/sret`，两次 `satp` 切换、四次
`sfence.vma`、固定 VA 与 PA ownership 怎样组成完整协议？

证据要求：用 `VM SPECIAL` 证明只有 trampoline 在 kernel/user roots 中同 VA/PA；
说明 user TRAPFRAME 指向当前 `p->trapframe`，kernel 同数值 VA 未映射，并逐项
追踪 `sscratch`、GPR save/restore 和 `prepare_return()` 的四个 kernel 字段。

### VM-07（原 VM-07）

当前 fork/exec/lazy fault 为什么没有实现跨 hart TLB shootdown？在哪些具体前提
下仍能工作，哪些未来变化会打破这些前提？

证据要求：区分“一个进程一次只在一个 hart 运行”“切 `satp` 时本地 flush”与
“同地址空间并发运行”。`CPUS=2` quick 只能作为回归，不得写成 C evidence。

### VM-08（原 VM-08）

当前 `uvmcopy()` 如何分别处理普通有效页、lazy hole 和 guard page？若下一项目
改成 COW，哪些 leaf flags、PA refs、write fault、copyout 和 teardown 路径都必须
一起改变？

证据要求：先按当前源码说明 eager copy/skip/preserve flags，再只列 COW 必须解除
的黑盒；不能把计划中的 refcount 或 TLB 协议描述成当前能力。

### VM-09（原 VM-09）

为什么 `vmfault(pagetable, va, access)` 还不能安全地用于任意离线 page table？
参数 table、`myproc()->pagetable`、`p->sz` 和 copy helper 的调用前置怎样形成隐含
契约？

证据要求：比较 `ismapped(pagetable, va)` 与 `mappages(p->pagetable, ...)`，再说明
exec 临时 table 的 `copyout()` 为何通常不会触发这条缺页路径。

### VM-10

valid lazy allocation、invalid address、mapped permission failure 和 NX instruction
fault 的最小可区分 oracle 分别是什么？为什么执行零页或坏 opcode 不能证明 NX？

证据要求：用 cause 13/15/12、before/after flags、allocations、stval/epc、child
status 和同一 `ret 0x00008067` 的 `PTE_X` mutation 建表；mutation 必须同时包含
`fence.i` 与 `sfence.vma`。

### VM-11

一次 high-VA lazy fault 最多需要 data、L1、L0 三次分配；分别在第 1/2/3 次模拟
OOM 时，什么必须立即恢复，什么可以暂存到 process teardown，怎样用两个时点的
page ledger 避免错误结论？

证据要求：逐项推导 `allocations=fail_at`、无 valid leaf、data PA 已释放；解释
第 3 点为何 `free_after=free_before-1` 仍可接受，以及 parent wait 后为何必须
`base==after`。

### VM-12

给定整份报告，怎样从 `VM SPECIAL`、四条 normal、invalid/permission/NX、
mutation、OOM 1/2/3 和 exec rollback 重建一个完整地址空间生命周期？哪些结论
分别属于 S、F、B，哪些明确是 C/R N/A？

证据要求：只复用前面已经建立的对象与关系；逐阶段连接 trigger、source edge、
observable、allowed side effect、resource result 和 cleanup，最后说明 A/D、
simulated OOM、单地址空间线程模型与有限场景的证据边界。

## 提交边界

所有问题引用同一份地址空间生命周期与 lazy-NX 报告包，不另建同步副本。旧位置
的 `VM-01..09` 在本单元 verified 后只保留兼容入口；下一 `project.copy-on-write`
单元才拥有 COW 修改与并发引用所有权。
