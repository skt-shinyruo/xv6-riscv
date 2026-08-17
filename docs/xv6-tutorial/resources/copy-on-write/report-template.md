# Copy-on-Write 报告模板

这是一份单一出口报告包的骨架。把每一项填入同一 pinned baseline 的原始记录，
不要只贴摘要或 COW PASS。完整 candidate 保存在仓库外；本文件不接收完整实现
patch。run-project.py --report 生成的是机器原始记录附录；它必须原样附入本模板，
但不能代替下面的 owner 时间线、逐场景解释、局限和签名。

## 1. 可复现性记录

    unit: project.copy-on-write
    pinned baseline commit:
    tutorial checkout commit:
    host / kernel toolchain:
    QEMU version:
    CPUS focused / quick / full:
    runner command:
    run timestamp / timezone:
    candidate absolute path (outside repository):
    candidate SHA-256:
    audit-fixture.patch SHA-256:
    run-project.py SHA-256:
    report SHA-256 (after finalization):

    audit adapter ABI:
      cowproject_ref / active / total / errors / free_pages / is_cow

记录临时导出树、私有 fs.img 和独立 QEMU 进程组的路径；它们完成后必须清理。
共享工作树、索引和共享 fs.img 的 before/after digest 另列在清理段，不能用“没有
看到改动”代替。

## 2. 入口、状态和所有权图（S）

给出 kfork -> uvmcopy -> parent/child PTE -> user store/copyout -> teardown 的
breadth-first 图。每个关键节点至少写：

| 时点 | pid/owner | VA | PTE V/R/W/X/U（及实现的 COW 标志） | PA | ref | 提交/清理责任 |
| --- | --- | --- | --- | --- | --- | --- |
| fork 前 | | | | | | |
| fork 后共享 | | | | | | |
| 第一次写后 | | | | | | |
| child exit/wait | | | | | | |

另外标出 root/intermediate、普通 data PA、trampoline PA 和 proc-owned trapframe PA。
说明 A/D 位和 fixture 临时位如何从比较 mask 中排除。不要把 candidate 的函数名
当作规范；source anchor 必须是 path:symbol。

## 3. 正常路径 raw marker 与 oracle（F）

逐行附上 COW SHARE、MULTI、LAZY、COPYOUT、BOUNDARY、TEARDOWN 原始输出，再用
marker 中的 raw flags、normalized cow、ref 和 bytes 填关系核对；不能只抄 guest
归约结论：

| marker | trigger | 观察字段 | 精确期望 | 允许副作用与清理 |
| --- | --- | --- | --- | --- |
| SHARE | 读共享后 parent/child 写 | pa、raw flags、cow、ref、values、eligible | 同 PA/ref=2；写者新 PA/ref=1；另一 owner 保持旧值；fast path 无分配且 W=1,cow=0 | child wait，pipe close，场景 free ledger 回基线 |
| MULTI | root -> child -> grandchild；grandchild 单独写 | before 三 pid、PA/ref=3；after PA/flags/ref/cow/values | root/child 仍旧 PA/ref=2，writer 新 PA/ref=1，values=33/33/42 | 两层 child wait，释放临时页 |
| LAZY | fork hole 后两侧首次写 | before/after pa、flags、values | fork 时无 leaf；两侧各自 private，parent 初值不受 child 影响 | lazy growth shrink，child wait |
| COPYOUT | child 经 pipe 读入四字节 | parent/child pa、payload | payload 精确匹配；parent 原值；PA 分离 | pipe/gate/child 清理 |
| BOUNDARY | text/guard/tail | mask、guard/text status | text W=0,COW=0 且写失败；guard U=0,COW=0 且写失败；tail shrink 后成功 | child wait，尾页释放 |
| TEARDOWN | child shrink，valid ELF commit 与 late bad | shrink leaf、bad 前后 PA/flags/ref/cow/value、status、ref/errors | child leaf 消失；bad exec=-1 保留旧 image；commit status=0；parent ref=1/errors=0 | wait 三个 child、临时 image 逆向释放、ledger |

固定绝对 PA、pid、hart 或运行时长不是 oracle；host 必须复算关系。

## 4. 边界、失败和 rollback（B）

附上 COW PARTIAL、OOM、ROLLBACK 的 raw 输出和 runner 的解析记录：

| 场景 | 触发/窗口 | 必须成立 | 立即与最终资源结果 |
| --- | --- | --- | --- |
| PARTIAL | generation=5，跨两页写，第二个 eligible allocation fail_at=2 | 第一段 16、重试第二段 16；parent 两页原值；child 两页 private 且四个 PA 互异；eligible=2、fired=1 | 第一段已提交不回滚；child wait 后 free/ref ledger 恢复 |
| OOM | generation=6 的 COW store 第一分配失败 | child status=-1；非零 old PA/value/ref 保持；fired=1；解除窗口后同一写成功且 fast_alloc=0 | failpoint 只在窗口生效，退出后清零；wait 后 ledger |
| ROLLBACK | generation=7 partial fork，fail_at=8 | fork=-1；旧共享 PA/ref/bytes/PTE 不变；ref=1 fast path 保持 PA、恢复 W 且 cow=0；setup free 相等 | empty intermediate 可延迟到 teardown；外层 CLEAN 必须 base==after |

说明失败发生在发布前还是发布后、哪个 owner 负责回滚，以及为什么“child 被杀死”
不足以证明 rollback。总账只能证明计数关系，不能凭空给每个未观察页命名。

## 5. 受控并发与 TLB 限制（C）

附上 COW PAIR raw marker，逐字段填写：

    arrived two-child bitmask（期望 1|2 == 3）:
    gate_open（marker 应为 1）:
    hart1 / hart2:
    root old PA / child1 new PA / child2 new PA:
    refs after split:

解释 barrier 如何先让两个 child 到达，再允许同时触发写拆分；指出两个 hart
不同只证明这次受控到达。明确当前协议可依赖修改当前 hart leaf 后的 local
sfence_vma() 与返回用户态路径 userret flush，但没有 remote TLB shootdown，也没有
同一地址空间的并发用户线程。PAIR 是 scoped C evidence，不能升格为完整线程、
shootdown 或全交错证明。

## 6. 回归记录

| 层级 | 命令/配置 | 退出状态 | 精确成功条件 | transcript/raw digest |
| --- | --- | --- | --- | --- |
| static | run-project.py --static-only | | manifest/source/resource/independence checks | |
| focused | COW fixture + targeted user cases | | 十个 case 与 COW PASS/CLEAN | |
| related | forktest、copyout、lazy_copy、sbrkfail 等当前适用测试 | | 逐项 marker/exit status，无 fixture 泄漏 | |
| quick | CPUS=2 | | focused + related，独立 QEMU | |
| full | CPUS=1 完整 usertests | | 完整回归成功，未改 baseline | |

列出失败重跑、随机性控制和任何未执行项；不要把 timeout 当测试通过。

## 7. 资源与清理证明

    scenario free_before / free_after:
    outer CLEAN base / after / errors:
    active_pages / ref_total before/after:
    child pids waited:
    pipes/condition gates closed:
    failpoint/pair state disarmed:
    temporary patch/build/tree removed or reversed:
    private fs.img removed:
    shared worktree/index/fs.img before digest:
    shared worktree/index/fs.img after digest:

对每个场景写清 parent 在哪个 wait() 后采样；ROLLBACK 允许 setup ledger 与最终
ledger 分两个时点，但最终 COW CLEAN base==after && errors==0 不可省略。记录页表
intermediate 的延迟清理是当前契约，不要假装它在每个失败点立即消失。

## 8. 证据分类与局限

| 维度 | 本报告真正支持的结论 | 明确不支持 |
| --- | --- | --- |
| S | source anchors、PTE/PA/ref/owner 关系与 candidate 控制流 | 静态图不证明所有运行时交错 |
| F | 六个正常 marker 的值、权限隔离、wait 后总账 | 未覆盖 VA/ELF/syscall 的普遍性 |
| B | partial、OOM、fork rollback 和 boundary 的确定性结果 | 模拟 OOM 不等于真实压力；总账不命名未观察页 |
| C | PAIR barrier 下两个 hart 的到达与 ref 更新 | remote shootdown、线程共享 root、全交错、公平性、形式化证明 |
| R | N/A：未运行 crash/reboot/disk recovery | 任何 persistence/idempotence 结论 |

补充固定 page size、有限 VA/ELF、A/D mask、CPUS=1 full、guest marker 由 host 解析
等限制。报告末尾由学习者和 reviewer 各签名一次，确认没有把 fixture 或 candidate
当 authoritative solution。
