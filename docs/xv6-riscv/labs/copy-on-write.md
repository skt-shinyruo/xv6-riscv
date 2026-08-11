# 实验：Copy-on-write `fork`

将 `uvmcopy()` 从“为每个物化页立即复制物理页”改为“父子共享，只在写入时复制”。本实验的难点不是 PTE 位本身，而是物理页引用、父页表降权、kernel `copyout`、失败回滚和 TLB 可见性的闭环。

## 1. 目标与非目标

目标：fork 成本与物化 writable 页的复制解耦；父子读取共享 PA，任一方首次写入后获得私有副本；只读页保持共享只读。保留当前 lazy holes、ELF权限和高位特殊映射。

非目标：线程共享地址空间、swap、page dedup、huge page、用户可见 page-fault API。

## 2. 设计决策

### PTE 软件位

在 RISC-V software-reserved 位中定义 `PTE_COW`，并断言它不与硬件 flag 重叠。只有“原本带 `PTE_U|PTE_W`、现因 COW 清 W”的用户 leaf 才带 COW。原本只读 text 不带 COW，写它必须失败。exec guard 页也位于 `uvmcopy()` 扫描的 `[0,p->sz)` 内，而且硬件 flags 中有 W、没有 U；它必须继续保持用户不可访问，可选择共享只读或仍 eager copy，但不能误标成一个期待由用户写 fault resolver 处理的普通 COW 页。trampoline/trapframe 则由 `proc_pagetable()` 为 child 单独建立，不属于这轮低地址 leaf 复制。

### 物理页 refcount

为 `KERNBASE..PHYSTOP` 的每个 4 KiB页维护引用计数和锁：

- allocator 首次分配后 ref=1；
- 建立额外 COW映射前 increment；
- unmap/free/exec/exit 释放映射时 decrement；仅 1->0 才放回 freelist；
- freelist拥有的页 ref=0；任何映射到 ref=0 页是 bug；
- `freerange()` 的启动灌入是独立边界：若统一的释放 API 会先 decrement，就必须在首次放入 freelist 前把该页初始化为 ref=1，或提供只用于启动的入链操作；不能对初始 ref=0 直接执行普通 decref；
- PA 转换为 refcount 数组下标前先检查页对齐和 `KERNBASE..PHYSTOP` 范围，increment 还要拒绝计数溢出；
- trapframe、页表页和普通 allocator用户也必须遵循明确的 ref API，不能一半直调 `kfree`、一半 decref。

定义清楚计数是“allocator owner数量”还是“用户 leaf映射数”。推荐统一为所有活动 owner的引用，减少例外。

### fork 发布顺序

child 在 `RUNNABLE` 发布前不可见。对每个 parent leaf：带 `PTE_U` 的只读页直接共享并 incref；带 `PTE_U|PTE_W` 的页在父/子 PTE中清 W、置 COW，再 incref；不带 U 的 exec guard 按前述独立策略处理。修改 parent PTE 后，本 hart 在下一次用户访问前必须经过 `sfence.vma`，防止 stale writable TLB 绕过 fault。当前返回路径在 `trampoline.S:userret` 切换 `satp` 后执行全量 flush，已提供这条边界；只有在该边界之前复用同一用户页表翻译时才需要 helper 内立即 flush。

已有 COW页的嵌套 fork 只增加 ref并复制 COW flags，不能把它恢复 writable。

## 3. 写 fault 路径

只处理 `scause=15` 且 leaf满足 `V|U|COW`、不满足 W：

1. 先令 `pageva=PGROUNDDOWN(fault_va)`，校验 `pageva < p->sz` 且低于 `TRAPFRAME`，再查目标 leaf。不能用未取整的 `fault_va < p->sz` 作为条件：当前 xv6 的最后一个部分页在 byte-granular break 以上仍可能访问，COW 不应改变这一既有页粒度语义；
2. 取旧 PA/flags，在 refcount锁协议下稳定其生命周期；
3. 若 ref==1，可原地清 COW、置 W；
4. 若 ref>1，分配新页、复制 4096 字节，安装保留 R/X/U 等原 flags 的 private writable PTE；
5. 保证安装 PTE 后、下一次用户访问前存在本地 TLB flush；当前可由统一 `userret` 路径提供；
6. 新 PTE 可见后减少旧页引用，或用已证明不会产生 UAF 的相反顺序。

OOM 时旧 PTE/旧 ref保持可用，目标普通进程被 kill；不能先减旧 ref再尝试分配。

## 4. `copyout` 必须参与 COW

内核通过物理直映写用户页不会触发用户 store fault。`copyout()` 遇到 COW leaf 时必须调用共同的 `cow_resolve(pagetable,va)`，再重新 walk；不能仅因没有 `PTE_W` 返回 -1，也不能直接写共享 PA。`copyin/copyinstr` 只读，不需复制。

共同 helper 要区分当前进程页表与 `exec` 的临时页表；不要沿用 `vmfault` 中隐含 current-process 的接口错误。

## 5. 回滚设计

`uvmcopy` 可能在 child中间页表分配时失败。失败路径必须：

- 解除已建立的 child leaf并对应 decref；
- 释放 child page-table pages；
- 对本次新转换为 COW 的 parent PTE，只有在确认没有其他共享者且转换确由本次造成时才恢复 W；
- 保留调用前已经是 COW 的 parent页；
- 保证恢复后的 parent PTE 在下一次用户访问前经过 TLB flush；
- 返回失败，让 `kfork` 回收未发布 child。

先写一个“转换日志”（VA、旧 flags、是否本次改父、是否已 incref/映射）会让逆序回滚可证明。只靠重新扫 child很容易误改嵌套 fork状态。

## 6. 多 hart 边界

当前每进程单线程，同一页表不会同时在两个 hart 执行；fork时 parent正在 syscall所在 hart，child尚未发布，因此 parent local `sfence.vma` 足以覆盖当前使用者。父子发布后可在不同 hart运行，但使用不同页表，各自 fault/flush本地 TLB。

若未来引入线程共享页表，这个前提失效；修改 PTE需要 shootdown IPI和远端确认。实验报告必须写出该限制，不能把现方案称为通用 SMP COW。

## 7. 分阶段实现

1. 先实现 refcount API和 allocator断言，不改 fork；运行泄漏/双重释放测试。
2. 共享只读页，验证 exec text与退出回收。
3. 转换 writable页为 COW，暂让写 fault明确失败，观察 PTE/ref。
4. 实现用户 store fault resolve，并验证从 PTE 更新到 `sret` 的路径必经 TLB flush；若 helper 后会提前复用翻译，再增加局部 flush。
5. 接入 `copyout`。
6. 实现嵌套 fork和完整失败回滚。
7. 移除 debug syscall，保留 counters/assertions和回归测试。

## 8. 验收条件

- fork后、写前，父子对应物化页 PA相同；writable页双方 `COW=1,W=0`；
- 父写、子写、双方交替写均互不影响，内容和 refcount精确；
- read-only text写入被 kill，不被升级为 writable；
- exec guard 在父子中始终没有 `PTE_U`，用户访问和 kernel `copyout` 均失败，且不会被 COW resolver 升级；
- lazy hole fork后仍是 hole，父子首次触碰各自得到独立零页；
- break 位于页中间时，fork 后最后一个已映射页的页尾访问仍按原实现可用；COW fault 不因原始地址高于 byte-granular `p->sz` 而误杀；
- kernel `copyout` 到 COW页只改变目标进程副本；
- 多代 fork/exit/exec 后 free-page计数回基线，无 ref underflow/overflow；
- fork大内存时，在写前新增物理数据页显著少于 eager实现；只要求计数关系，不硬编码时间；
- `CPUS=1` 与多 hart完整 tests通过。

## 9. 故障注入

| 注入点 | 期望 |
|---|---|
| child L1/L0页表第 N 次分配失败 | fork -1，parent内容/权限可用，所有增量 ref和页回滚 |
| COW data page分配失败 | faulting child退出，其他 sharer内容不变，旧 ref少一个仅在 child回收时发生 |
| `copyout` 跨两页，第二页 COW OOM | 返回 -1/当前 helper规定的部分副作用，第一页只在目标中私有化 |
| ref==1 fast path checkpoint | 不分配新页，PTE变 W且 COW清；下一次用户写之前已观察到 flush boundary |
| 两个 sibling同时写同一共享 PA | 各得私有副本，无 UAF/重复 free |
| 测试构建故意绕过 PTE 更新到 `sret` 之间的全部 flush boundary | `pte_epoch <= tlb_epoch` 的返回前 trace/assert 必须失败；功能测试不能依赖 QEMU 一定缓存 stale TLB |

每个失败例在 parent wait后核对：所有活映射 PA的 ref>0，freelist页 ref=0，同一私有 writable页不被不同地址空间共享。
