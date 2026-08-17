# Copy-on-Write 问题

本页由 project.copy-on-write 拥有。先完成[证据项目：Copy-on-Write fork](../experiments/copy-on-write.md)，
再查看[答案与证据标准](answers/copy-on-write.md)。问题从当前 branch 的
kfork()/页表/allocator/陷阱路径出发；不要把私有 candidate 或 fixture 的函数
名当作公开 API，也不要迁移旧的 VM 问题。

稳定入口包括 kernel/riscv.h:PTE_FLAGS、kernel/riscv.h:sfence_vma()、
kernel/kalloc.c:kalloc()、kernel/kalloc.c:kfree()、kernel/vm.c:walk()、
kernel/vm.c:uvmcopy()、kernel/vm.c:uvmunmap()、kernel/vm.c:uvmfree()、
kernel/vm.c:copyout()、kernel/proc.c:kfork()、kernel/proc.c:freeproc()、
kernel/proc.c:proc_freepagetable()、kernel/trap.c:usertrap()、
kernel/trap.c:prepare_return()、kernel/trampoline.S:userret、
kernel/pipe.c:piperead() 和 kernel/exec.c:kexec()。每题都要回到 source
anchor 与报告字段，不能用固定 PA、pid 或单个 PASS 代替关系。

## 问题

### COW-00：全景与边界

从一次 kfork() 开始，系统要解决的“共享、写入、回收”问题是什么？主要组件如何
分工，输入/输出和边界在哪里？请先画 kfork -> uvmcopy -> PTE/PA/ref ->
store/copyout -> wait/freewalk 的 breadth-first 骨架，并标出哪些事实来自前置
VM 单元，哪些是本项目新观察；不要先选择某一种 refcount 结构。

### COW-01：PTE 状态与页所有权

给定一个 parent/child 同时可见的 user leaf，怎样从 PTE_FLAGS、PA 和 ref 字段
判断它是共享可写保护、原本只读 text，还是 supervisor-only guard？哪些字段必须
从 mask 排除，为什么 PTE_W 与软件 COW 标志不能同时代表“当前 owner 可写”？从
kernel/riscv.h、vm.c:walk() 和 fixture snapshot 复算一组 SHARE 与 BOUNDARY
记录，并说明 cowproject_is_cow() 只规范化观察、为何不等于规定内部软件位。

### COW-02：allocator ref 生命周期

一个 data PA 从 kalloc() 到 fork 增加 owner、写拆分、unmap/exit 和最后释放，
每一阶段谁创建、谁修改、谁消费 ref？怎样证明页表中间页、trampoline PA 和
trapframe PA 没被错误计入同一 ledger？沿 kalloc.c:kalloc/kfree、
proc.c:proc_freepagetable 和 vm.c:uvmunmap/uvmfree 写出成功与重复释放的可观察
差异。

### COW-03：fork 发布与 partial rollback

kfork() 在 parent/child 两棵 root 中发布共享映射时，哪些状态必须一起变得可见？
若中途分配或映射失败，哪些 parent PTE/ref/bytes 必须恢复，哪些 empty intermediate
可以延迟到 freewalk()？用 ROLLBACK 的 fail_at、setup ledger 和 wait 后 COW CLEAN
复算“即时回滚”和“最终清理”两个时点；不要假设一次失败会逐位还原整棵页表。

### COW-04：user store fault 的拆分

用户 store fault 从 trap.c:usertrap() 到 candidate 的 resolver 再回到原指令，
控制流和数据流经过哪些边界？在 refs=1 与 refs>1 时，哪些 PTE/PA/ref/bytes
关系必须分别成立，OOM 时 child status、旧值和 ledger 又应如何变化？用 SHARE、
OOM marker 说明如何区分快速升级与实际复制，不要求给出实现函数顺序。

### COW-05：kernel copyout 与跨页部分提交

vm.c:copyout() 和 pipe.c:piperead() 如何把 kernel 产生的 bytes 写入 COW user
leaf？它与硬件 store fault 的 VA/cause 参数有何不同？跨页时若第二页在 fail_at=2
失败，第一段是否回滚、调用者应报告什么 count？沿 COPYOUT/PARTIAL 的 raw 字段
和现有 pipe 调用者写出提交点，不能把 helper 错误泛化成所有 syscall 返回语义。

### COW-06：lazy、text、guard 与边界

fork 遇到 lazy hole、ELF text、stack guard 和尾页 partial shrink 时，哪些映射
应保持 absent、只读或 supervisor-only？为什么它们不能被“所有非 W leaf 都是
COW”这一单一规则覆盖？用 LAZY、BOUNDARY 和 exec.c:kexec() 的 flags/commit
路径，分别追踪首次访问、失败 status 和允许的 teardown side effect。

### COW-07：多代 fork、exec 与 teardown

root -> child -> grandchild 后，一个 PA 的 owner 集合如何变化？任一代先写、先
exec、先 exit 或 parent wait 时，哪些 ref/PTE/bytes 必须仍可观察？把 MULTI、
TEARDOWN 与 proc.c:freeproc/proc_freepagetable、exec.c:kexec 串起来，说明“最后
owner”与 proc identity/fd/cwd/trapframe 的边界。

### COW-08：OOM 与资源账本

如何为 user store、copyout、fork 三类失败分别设置确定性触发器，并把
eligible/fail_at/fired 映射到实际分配尝试？哪些资源必须立即释放，哪些可由
child wait/freewalk 最终释放？比较 OOM、PARTIAL、ROLLBACK 的两个 ledger 时点，
并解释为什么随机压力、timeout 或 child 被 kill 不能替代 oracle。

### COW-09：双 hart、锁与 TLB

PAIR fixture 如何安排两个不同 pid 在 barrier 后同时拆分同一 PA？arrived、
gate_open、hart1/2 和三组 PA/ref 能证明什么，不能证明什么？结合
prepare_return()、trampoline.S:userret 和 local sfence_vma()，说明当前单进程
单 hart 前提为何不等于 remote shootdown；指出若未来允许同一 root 在多个 hart
同时运行，还缺少哪类协议，而不直接设计它。

### COW-10：端到端证据综合

给定完整报告，如何从 COW-00..09 已建立的关系重建一次“fork 读共享 -> parent
写 -> child copyout -> partial/OOM rollback -> multi-hart split -> wait/teardown”
流程？逐阶段列出 trigger、source edge、observable、allowed side effect、resource
result 和 S/F/B/C/R 分类；最后说明 A/D mask、模拟 OOM、有限 VA/ELF、CPUS=1 full、
无 crash/recovery 和无 remote shootdown 使哪些结论仍然开放。

## 提交边界

问题答案只服务一份 COW 报告包。旧的 VM 问题页保持原位置和原题，不创建同步副本；
本页随 project.copy-on-write 已完成非作者走查、独立 review、runner 和
generated-navigation 验证并标记为 verified；后续修改必须重新绑定报告包。
