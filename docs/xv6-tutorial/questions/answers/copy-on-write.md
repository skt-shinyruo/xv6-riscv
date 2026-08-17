# Copy-on-Write 问题：答案与证据标准

本页不是 COW 实现答案。它给 reviewer 一组可检查的关系、失败边界和证据最低
标准；具体 flag 位、refcount 表和 resolver 结构必须从学习者 candidate 的 source
anchors 与 raw marker 重新复算。固定 pid、绝对 PA、串口延迟或单个 COW PASS
不能替代这些关系。

## COW-00

合格全景从 kfork() 进入 uvmcopy()，在 parent/child user roots 中建立可读共享
leaf 和 PA owner 关系；之后用户 store 或 kernel copyout() 触发写路径，最后由
exit/wait 的 unmap/freewalk 收束 ref。图必须区分页表节点、普通 data PA、
trampoline/trapframe 和 proc identity。COW 只加入 processes-and-memory 的
ownership/fault 解释，不声称 persistence 或同一 root 的并发线程。

## COW-01

共享 user leaf 的 flags mask 必须同时显示 V|R|U、禁止当前写入，并由实现选择的
标志表示“可在写 fault 中拆分”；PA 相同且 ref 等于可见 owner 数。text 可以是
V|R|X|U 且 W=0，guard 必须 U=0；两者不能因为“只读”就获得 COW 语义。A/D 和
fixture 临时位从比较 mask 排除。SHARE/BOUNDARY snapshot 应能让 reviewer 复算
这些关系，而不是看符号名称。公开 seam 只要求 cowproject_is_cow(pte_t) 把实现
自选的软件位归约为 0/1，并由 cowproject_ref/active/total/errors/free_pages
规范化 owner ledger；它们是只读 adapter，不是生产接口或内部结构答案。

## COW-02

allocator 的成功路径是 whole-page owner 增加/减少与 kfree 的最终入 freelist；
unmap、exec replacement、exit/freeproc 和 fork rollback 都只能提交各自拥有的
一次减少。页表中间页由 freewalk 管理，trampoline 映射不拥有其目标 PA，trapframe
是 proc 单独拥有；它们不能混入普通 user data ref。ref_errors 或重复释放计数
必须为零，不能只凭 free-page 总数猜测分类正确。

## COW-03

fork 的可见提交点是 parent/child 叶、PA ref 和写权限保护彼此一致；失败时已经
发布的 parent leaf/bytes/ref 必须回到调用前语义。高 VA 分支若留下空 intermediate，
立即 ledger 可与 setup 相等或按当前契约延迟一个节点，但 child/parent wait 后外层
COW CLEAN base==after 必须成立。ROLLBACK 的 fail_at=8、旧 PA/ref/value 和 setup
ledger 是最小可复算证据。

## COW-04

usertrap() 只把适用 store fault 交给 COW resolver；成功后返回用户态重试原指令，
不能把 epc 当作已执行两次。refs=1 可在保持 PA 的前提下恢复当前 owner 的可写
语义；refs>1 必须给触发者新 PA、复制 bytes、减少旧 ref，并使另一 owner 继续读
旧值。分配失败时 child 应以预期失败状态退出，旧值/旧 leaf/ref 保持，failpoint
关闭后同一写的快速/成功路径可单独验证。SHARE/OOM marker 必须同时满足这些字段。

## COW-05

copyout() 是 kernel continuation，不产生 user hardware cause；它按页处理目的
地址，必要时先取得 COW 可写语义，再提交 bytes。跨页没有隐含事务：PARTIAL 的
第一段 16 已提交，第二段在 fail_at=2 失败，重试后第二段 16 成功。调用者（如
pipe）可返回 partial count、-1 或其他既有语义，报告必须引用实际 caller，不能由
helper 的错误值推出统一 syscall contract。COPYOUT 必须证明 child payload 和
parent isolation。

## COW-06

lazy hole 在 fork 时保持 absent，双方首次访问各自物化；text 的 W=0 取指/写入
拒绝而不触发 COW；guard 的 U=0 不能变成用户可访问 leaf；尾页 shrink 后仍在
逻辑范围的合法写可按 candidate contract 成功。kexec() 临时 root 的 flags、
commit/bad 与这些类别分开，不能用不存在文件的早期失败冒充 teardown。LAZY 和
BOUNDARY marker 需展示 before/after PA、mask、独立 guard/text status 和 parent 初值。

## COW-07

多代 fork 的同一 PA/ref=3 必须在 root、child、grandchild snapshot 中成立；一代
写只拆自己的 leaf。exec commit 替换该 proc 的 user image 并释放旧 owner，bad
路径保留旧 image；exit 后 parent wait 才最终收集 child 的页。proc pid、kernel
stack、fd/cwd、trapframe 和 trampoline 不属于被 exec 替换的普通 user data image。
MULTI/TEARDOWN 以及 freeproc/proc_freepagetable/kexec 锚点共同支撑结论。

## COW-08

确定性 failpoint 的 generation/eligible/fail_at/fired 必须对应实际 allocator 窗口；
OOM、PARTIAL、ROLLBACK 分别观察“调用返回时”“child wait 后”两个账本时点。
即时要求是无 reachable valid leaf、旧 bytes/ref 不被破坏、已取得但未提交的资源有
明确 owner；最终要求是 free/ref 总账恢复、failpoint 清零、errors=0。随机压力、
timeout、shell prompt 或 child 被 kill 只提供 watchdog/cleanup 信号，不是失败 oracle。

## COW-09

PAIR 的 arrived=3 是两个 child 的到达位图 1|2，不包含 root；gate_open=1、两个 child hart 不同、旧
PA 与两个新 PA 两两分离且 ref=1，证明的是 fixture 安排的双 hart 到达和 ref 更新。
它不证明所有交错、线程共享 root、远端 TLB shootdown、调度公平性或形式化并发安全。
修改当前 hart leaf 后的 local sfence_vma() 与 userret flush 是本 branch 可声明的
前提；没有 remote invalidation 协议必须明确写成限制。

## COW-10

综合报告应把正常、边界、失败和并发按同一 owner 时间线串起：先由 SHARE/MULTI
建立 PA/ref，再由 LAZY/COPYOUT 展示入口差异，PARTIAL/OOM/ROLLBACK 展示提交与
清理边界，PAIR 展示受控并发，BOUNDARY/TEARDOWN 收束权限和最终释放。S 支持源
码关系，F 支持正常 bytes/flags/status，B 支持确定性 fault/rollback，C 只支持
barrier 交错，R 明确 N/A。A/D mask、模拟 OOM、有限 VA/ELF、固定 page size、
CPUS=1 full、无 crash/recovery 和无 remote shootdown 都必须列为不能推出的结论。

## 证据边界

完整报告必须附 raw marker、host 解析、focused/related/quick/full transcript、
wait 后资源 ledger、patch reverse 和共享状态 digest。guest 自报、单一场景、固定
地址或 fixture 代码都不能替代独立 source-grounded 复核；任何实现细节若未在
candidate diff 和运行字段中出现，应标记为未知而不是补写成事实。
