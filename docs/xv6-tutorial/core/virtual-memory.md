# 虚拟内存、lazy fault 与 NX

## 问题场景与本单元成果

`sbrklazy()` 返回一个用户地址时，进程已经扩大了逻辑地址空间，却可能尚未拥有
对应物理页。第一次 load、store、`copyin()` 或 `copyout()` 才建立 leaf；同一个
地址若用于取指则必须失败。若只把页表理解成“VA 到 PA 的表”，就无法解释谁
拥有 root/intermediate/data page、为什么 `exec()` 失败仍能回到旧映像、为什么
trampoline 能跨 `satp` 切换继续执行，以及 OOM 后何时才算真正回收。

本单元的唯一出口是一份可独立复核的“地址空间生命周期与 lazy-NX 证据报告
包”。报告包含 kernel/user root 与特殊映射图、四种首次物化、invalid/
permission/NX 分类、`PTE_X` mutation、data/L1/L0 三点模拟 OOM、late `exec`
rollback、页账本、回归和清理；这些分节共同构成一个出口产物，不是若干独立
日志。

## 前置单元与暂存黑盒

硬前置：[进程生命周期与回收](process-and-memory.md)。那里已建立 proc、fork、
exec、exit 和 wait 的身份/资源交接，本单元把“用户映像”展开为 root、页表节点、
leaf、物理页和特殊映射。相关边界来自[一次系统调用如何往返](syscall-roundtrip.md)
与[启动、陷阱、中断与汇编边界](boot-traps-and-interrupts.md)。

本单元解除 `first-user-address-space` 和 `full-trampoline-page-table-contract`，完整
拥有当前基线的地址翻译、映射权限、lazy fault 分类、地址空间 rollback 和
teardown。以下机制继续是显式边界：

- Copy-on-Write 引用计数、write fault 和多 hart TLB 协调由下一项目解释；当前
  `uvmcopy()` eager 复制已存在页并跳过 lazy hole。
- 文件内容、log 和磁盘恢复由后续持久化阶段解释；`exec` 只作为页表事务观察。
- 当前 xv6 没有同地址空间用户线程；一次 `CPUS=2` 回归不构成 remote TLB
  shootdown 或并发证明。

## 最小模型和关键不变量

### 三层翻译同时承载权限与所有权

Sv39 用 `satp` 中的 root PA 和 VA 的三级 `PX(level, va)` 索引逐级读取 PTE；
level-2/1 的 `V=1,R/W/X=0` 项指向下一张页表，leaf 的 `V/R/W/X/U` 决定用户访问。
`kernel/vm.c:walk()` 在 `alloc=1` 时取得缺失的中间页，`mappages()` 最后才发布
leaf。TLB 缓存的是翻译结果，不是所有权来源；修改可见 PTE 后需要适用的
`sfence.vma`。

必须把四类页分开记账：

| 对象 | 创建/发布 | 最终所有者与释放 |
| --- | --- | --- |
| kernel root/intermediate | `kvmmake()`/`kvmmap()` | 全部 hart 共享，本单元不销毁 |
| 每进程 user root/intermediate | `uvmcreate()`/`walk(alloc=1)` | proc；`freewalk()` 递归释放 |
| 普通 user data leaf/PA | `uvmalloc()`、`uvmcopy()` 或 `vmfault()` | proc；`uvmunmap(...,1)` 释放 |
| trampoline/trapframe leaf | `proc_pagetable()` | leaf 不拥有目标 PA；特殊映射先以 `do_free=0` 移除 |

kernel root 直接映射设备、kernel text `R|X`、kernel data/RAM `R|W`、每槽 kernel
stack 和 trampoline。每个进程有不同 user root；普通 user leaf 带 `PTE_U`。
只有 trampoline 保证在 kernel/user 两张表中使用同一 `TRAMPOLINE` VA、同一
trampoline PA、`R|X` 且无 `U`。user 表的 `TRAPFRAME` VA 映射当前
`p->trapframe` PA、`R|W` 且无 `U`；kernel 表同一数值 VA 是 guard hole，kernel
代码通过该 PA 的 direct-map 地址访问 trapframe。

### 地址空间从构造到替换再到回收

`allocproc()` 先取得独立 trapframe PA，再由 `proc_pagetable()` 创建 user root
及两个 supervisor-only 特殊 leaf。`userinit()` 从空 user 区开始，第一次
`forkret()` 的 `kexec("/init")` 才装入 ELF。当前基线没有传统 `uvmfirst()`/
embedded initcode 路径。

`uvmalloc()` 为 ELF segment、exec stack 和 eager `sbrk()` 取得 zero-filled data
PA，并把 `kernel/exec.c:flags2perm()` 的 ELF `X/W` 转成 leaf 权限。guard page
由 `uvmclear()` 清除 `PTE_U`。`uvmcopy()` 复制已存在 leaf 及其 flags，遇到缺失
intermediate 或 invalid leaf 就跳过，因此 fork 保留 lazy holes，但不是 COW。

`kexec()` 先独占一个临时 user root，装入 ELF、stack 和 argv；只有全部成功后
才替换 `p->pagetable/p->sz` 与 trapframe 的 user `epc/sp`，再释放旧映像。
`bad` 路径释放临时表并保留旧映像。`sys_exec()` 在进入 `kexec()` 前从旧映像
导入 argv：指针数组的 `copyin()` 可能物化合法 lazy 页，而字符串的
`copyinstr()` 不会。这个旧映像页是允许副作用，不能错误承诺 exec failure
逐位不改旧页表。

`kexit()` 只留下 zombie；parent 的 `kwait()` 才经 `freeproc()` 调用
`proc_freepagetable()`。它先移除 trampoline/trapframe leaf 且不释放共享
trampoline 或单独所有的 trapframe PA，再以 `uvmfree()` 释放普通 user leaf、
中间页和 root；trapframe PA 由 `freeproc()` 自己释放。

### logical size、hole 和四种 fault 结果

`kernel/vm.h:SBRK_EAGER/SBRK_LAZY` 是用户运行时传给 `sys_sbrk()` 的模式 ABI；
它只选择 `growproc()` 立即映射或仅推进 logical size，不定义 PTE layout，也不表示已有物理页。
正向 lazy `sys_sbrk()` 只检查 unsigned wrap 与 `<=TRAPFRAME`，再增加 `p->sz`；
`p->sz` 是逻辑上界，不是 heap interval metadata。当前 `vmfault()` 的分类是：

```text
va >= p->sz                 -> invalid, reject
已有 valid PTE              -> permission/already-mapped, reject
未映射且 va < p->sz         -> kalloc zero page, map V|R|W|U, X=0
data 或 walk 分配失败        -> reject; user hardware fault 最终 kill -1
instruction page fault 12   -> never call vmfault, kill -1
```

`usertrap()` 只把 load cause 13 和 store/AMO cause 15 送入 lazy repair；成功时不
增加 `epc`，所以返回用户态重试原指令。cause 12 不得分配。`copyin()`/
`copyout()` 在 `walkaddr()` 失败时也能补页，但传入 `PGROUNDDOWN(pointer)`；
`copyinstr()` 直接失败。`copyout()` 还检查 `PTE_W`，不能借 lazy handler 升级
只读 ELF text。

因此硬件入口用原始 `stval` 判断 `va < p->sz`，helper 入口却用页首。break 位于
页中时，helper 首次访问略越 break、但页首仍低于 size，可能物化整页；页一旦
映射，当前硬件/helper 也没有按字节重新检查 `p->sz`。这是一条当前分支边界，
不能改写成通用内存安全模型。`vmfault(pagetable, ...)` 还用参数表检查，却固定
把 leaf 写入 `myproc()->pagetable`；它只适用于当前进程页表，不是离线 table
的通用接口。

OOM 的“立即 rollback”和“最终 cleanup”也不同。data 分配失败或较早 walk
失败时没有有效 leaf，data PA 被释放；若已创建一个空 intermediate 后再失败，
当前 `mappages()` 不立即拆空分支。它仍由该 proc 拥有，并在 `wait()` 后由
`freewalk()` 回收。所以 oracle 是“立即无 reachable leaf/data leak，最终总页
账本恢复”，不是“失败瞬间整棵 PTE tree 逐位不变”。

### trampoline 是跨页表执行协议

用户 trap 到达 `uservec` 时仍使用 user `satp`，但处于 S-mode。因为
TRAPFRAME 无 `PTE_U` 不妨碍 supervisor 访问，它先以 `sscratch` 保存 user a0，
把全部 user GPR 写入固定 `TRAPFRAME`，再读取 `kernel_sp/kernel_hartid/
kernel_trap/kernel_satp`。第一次 `sfence.vma` 让此前访问在旧表下完成，
`csrw satp` 切 kernel root，第二次 flush stale user translations，再 `jalr`
进入 `usertrap()`。

返回时 `prepare_return()` 关闭中断，设置 `stvec=uservec`，刷新四个 kernel 字段，
设置 user `sepc` 与 `sstatus`。`userret` 在同一 trampoline PA 上切回 user root，
再次用两次 `sfence.vma` 围住 `satp` 写入，随后从 TRAPFRAME 恢复 GPR 并 `sret`。
trampoline 链接段由 `kernel/kernel.ld` 页对齐并断言恰好一页；kernel-mode trap
走 `kernelvec`，不是这条 user page-table 协议。

## 源码追踪计划

用稳定 `path:symbol` 建立 translation、lifecycle、fault 和 trampoline 四张图：

```sh
rg -n '^#define SBRK_(EAGER|LAZY)' kernel/vm.h
rg -n '^#define (PTE_[VRWXU]|MAXVA)|^#define PX|MAKE_SATP|sfence_vma' kernel/riscv.h
rg -n '^#define (TRAMPOLINE|TRAPFRAME|KSTACK)' kernel/memlayout.h
rg -n '^kvmmake\(|^walk\(|^mappages\(|^uvmalloc\(|^uvmcopy\(' kernel/vm.c
rg -n '^uvmunmap\(|^freewalk\(|^uvmfree\(|^copyin\(|^copyout\(|^copyinstr\(' kernel/vm.c
rg -n '^vmfault\(|^ismapped\(' kernel/vm.c
rg -n '^allocproc\(|^freeproc\(|^proc_pagetable\(|^proc_freepagetable\(' kernel/proc.c
rg -n '^growproc\(|^kfork\(|^kwait\(' kernel/proc.c
rg -n '^flags2perm\(|^kexec\(|^loadseg\(|^bad:' kernel/exec.c
rg -n '^sys_sbrk\(' kernel/sysproc.c
rg -n '^usertrap\(|^prepare_return\(' kernel/trap.c
rg -n '^uservec:|^userret:' kernel/trampoline.S
rg -n '_trampoline|trampsec|ASSERT' kernel/kernel.ld
rg -n '^kalloc\(|^kfree\(' kernel/kalloc.c
rg -n '^sbrklazy\(' user/ulib.c
rg -n '^lazy_(alloc|unmap|copy|sbrk)\(|^sbrkfail\(|^execout\(' user/usertests.c
```

图中至少包含：

```text
VA + satp root -> walk L2/L1/L0 -> leaf flags -> PA
allocproc -> proc_pagetable -> exec temporary root -> commit -> old root free
sbrklazy -> p.sz only -> 13/15 or copy helper -> vmfault -> V/R/W/U leaf -> retry
uservec(user root) -> TRAPFRAME -> trampoline -> kernel root -> usertrap
prepare_return -> trampoline -> user root -> TRAPFRAME -> sret
```

## 观察任务

先区分 manifest 的 pinned baseline 与当前教程提交，再运行静态门：

```sh
rg -n '"baseline_commit":' docs/xv6-tutorial/curriculum.json
git rev-parse HEAD
python3 docs/xv6-tutorial/resources/virtual-memory/run-lab.py --static-only
```

静态门导出 pinned baseline，验证上述 ownership、权限、fault dispatch、exec
commit/bad、teardown 和完整 trampoline；再应用 tutorial patch、构建 kernel/
`vmtrace`/私有镜像，`make clean`、逆向 patch 并比较逐文件 snapshot。

完整证据报告命令是：

```sh
python3 docs/xv6-tutorial/resources/virtual-memory/run-lab.py \
  --report /tmp/virtual-memory-report.md
```

runner 在私有 `CPUS=1` QEMU 中运行 12 个结构化场景，再运行 `lazy_alloc`、
`lazy_unmap`、`lazy_copy`、`lazy_sbrk`、`sbrkfail`、`sbrkbugs`、`sbrklast` 和
`execout`。quick 使用 `CPUS=2`，完整 `usertests` 使用 `CPUS=1`；每个 QEMU/
driver 都是独立进程组。host 逐字段复算 cause、flags、status 和 ledger，不把
guest 的 `VM PASS` 当作唯一 oracle。

## 有界修改任务

资源 [`lazy-nx.patch`](../resources/virtual-memory/lazy-nx.patch)、
[`run-lab.py`](../resources/virtual-memory/run-lab.py) 和
[`rubric.md`](../resources/virtual-memory/rubric.md) 构成 tutorial-only audit
seam。patch 只应用到临时导出的 pinned baseline，添加一个命令复用的
`vmprobe` syscall、固定单记录、按 pid/generation/fault scope 的 failpoint、
free-page 计数和 `vmtrace` guest；它不进入 executable baseline。

学习者的 bounded change 是把含义模糊的 `vmfault(..., int read)` 收紧成命名
access kind，建立 NX 回归、三点 OOM 和 late exec rollback 证据。fixture 的完整
实现只用于 publication/non-author walkthrough，不是可复制的 authoritative
answer patch；提交应按 rubric 独立解释权限来源、fault 分类和所有权账本。

mutation 在写入有效 little-endian `ret` `0x00008067` 后执行 `fence.i`，再临时为
同一 leaf 加 `PTE_X` 并 `sfence.vma`。它只证明 oracle 区分 NX 与坏指令，不是
`mprotect`、JIT 或 W^X API。OOM 在 VA `1<<30` 的新 root 分支依次命中 data、
L1、L0 分配尝试；hook 只在 `CPUS=1`、active pid 和 vmfault/walk 窗口内生效。

允许副作用仅为临时源码/build、私有 `fs.img` 和短寿命进程组。每个 child 都由
parent `wait()` 后比较 free pages；最后必须 `make clean`、逆向 patch、比较
snapshot，并确认共享工作树/索引/内容和共享 `fs.img` digest 不变。

## Oracle、证据、失败路径和局限

| 维度 | 触发器 | 必须观察到 | 资源结果与不能推出 |
| --- | --- | --- | --- |
| S | pinned baseline + 12-path patch | translation/ownership、特殊 leaf、full trampoline、fault policy、exec commit/bad、teardown 与 hook scope 成立 | 只写临时导出；静态关系不证明 runtime cause |
| F | load/store/copyout/copyin 首次访问 | absent -> `V/R/W/U`、`X=0`；user cause 13/15 不前进 epc，helper cause=0；pipe 非零/零 payload 保真且 child status=0 | wait 后 free pages 回基线；ELF/lazy focused 通过 |
| B | invalid、text store、NX、mutation、OOM 1/2/3、late exec | invalid/permission 不分配；NX cause=12/status=-1；同一 ret 加 X 后返回；OOM 无 leaf，exec 返回旧映像状态 42 | L0 failure 可暂存一个 intermediate，wait 后必须恢复；mutation/failpoint 必须清除 |
| C | N/A | `CPUS=2` quick 仅检查未 arm seam 无回归 | 不证明 TLB shootdown、并发或所有交错 |
| R | N/A | 无 crash/reboot/persistence claim | 私有镜像只隔离写入 |

hardware 可设置 A/D 位，所以 flags oracle 只比较 `V/R/W/X/U` mask。free-page
freelist 记录所有 whole-page allocator 对象，能证明总账回收，不能仅凭 delta 给
每个未观察对象命名。模拟 OOM 不是随机真实压力；有限 fault/VA/ELF 场景不能
穷尽映射状态，也不是形式化隔离证明。

## 退出产物与后续单元

提交一份报告包，至少包含：

1. kernel/user root、普通 leaf、trampoline/trapframe 的 VA/PA/flags/owner 图；
2. 地址空间从 `allocproc -> exec commit/bad -> fork holes -> exit/wait teardown`
   的所有权时间线；
3. 四种 normal 与 invalid/permission/NX/mutation 的逐字段表；
4. OOM data/L1/L0 的 immediate/final ledger，以及 late exec rollback；
5. focused/related/quick/full、patch reverse、进程组和共享状态清理；
6. S/F/B/C/R 边界与不能推出的结论。

配套[虚拟内存问题](../questions/virtual-memory.md)用于从全景到实现细节复核同一
报告。下一步计划单元 `project.copy-on-write` 将在这些 leaf/owner/fault 不变量
上增加共享 PA 引用、write fault 和 TLB 协调；当前单元不先
给出那一项目的答案。
