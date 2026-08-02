# 一次 lazy page fault：逻辑扩容到首次映射

本文追踪本分支特有的 lazy `sbrk`：用户先扩大 `p->sz` 而不分配物理页，之后由用户 load/store trap 或内核 `copyin()`/`copyout()` 在首次使用时补页。核心实现位于 `user/ulib.c`、`kernel/sysproc.c`、`kernel/trap.c`、`kernel/vm.c` 和 `kernel/vm.h`。

当前源码树没有 `kernel/vmfault.c`；`vmfault()` 与 `ismapped()` 均位于 `kernel/vm.c`。本文按这一实际编译单元描述，不采用其他分支可能存在的文件拆分。

相关生命周期还涉及 `kernel/proc.c` 中的 `growproc()`、`uvmcopy()` 调用和 `freeproc()`，物理页来自 `kernel/kalloc.c`，页大小/PTE/`MAXVA` 定义来自 `kernel/riscv.h`，行为回归由 `user/usertests.c` 覆盖。

完整 VM 结构见[虚拟内存](../kernel/memory.md)，trap 入口见[Trap 与中断](../kernel/traps-and-interrupts.md)，系统调用寄存器路径见[一次系统调用往返](syscall-round-trip.md)。

## 1. eager 与 lazy 用户 API

用户头文件公开：

```c
char *sbrk(int n);
char *sbrklazy(int n);
```

二者都调用生成的双参数 stub `sys_sbrk`：

```text
sbrk(n)     -> sys_sbrk(n, SBRK_EAGER)
sbrklazy(n) -> sys_sbrk(n, SBRK_LAZY)
```

成功都返回调用前的 break，失败返回 `(char *)-1`。对从低地址到旧 break 已连续物化的常规地址空间，差异在正增长：eager 为增长涉及的新页建立映射，lazy 只移动逻辑边界。

两种 API 可以混用，而 `uvmalloc()` 假定旧 break 所在的部分页已经映射，并从 `PGROUNDUP(oldsz)` 才开始循环。若先用 `sbrklazy()` 把非页对齐的 break 留在一个未映射页中，随后调用正增长的 `sbrk()`，eager 路径不会回填 break 所在页：当 `newsz <= PGROUNDUP(oldsz)` 时分配循环完全为空；即使增长越过该边界，分配也从下一页开始，原来的 hole 仍保留。因此这里的 eager 只描述本次 `uvmalloc()` 循环覆盖的页，不能理解为无条件物化 `[oldsz, newsz)` 的每个字节。

## 2. 正向 lazy 扩容

`sys_sbrk()` 先保存：

```c
addr = myproc()->sz;
```

若策略不是 eager 且 `n >= 0`，它验证：

```text
addr + n does not wrap below addr
addr + n <= TRAPFRAME
```

成功仅执行：

```c
myproc()->sz += n;
return addr;
```

页表、页表中间级和物理页都不变化。于是区间 `[oldsz, newsz)` 在“进程合法地址范围”内，却可能没有 PTE，这就是 lazy hole。

当前条件对未知正策略值也走 lazy 分支，而非拒绝；正规用户库只传两个定义值，因此这属于内部 ABI 未严格校验的边界。

## 3. 逻辑大小不等于已映射内存

扩容后需要同时看两个维度：

```text
VA < p->sz             address is logically owned by process
valid user PTE exists  address currently has physical backing
```

eager 区域通常两者都真，lazy 未触及页只有第一项真。`p->sz` 是 fault 合法性和释放范围上界；页表是实际 CPU 访问能力。

因为 oldsz 不一定页对齐，扩展起点所在页可能早已映射。对同一页内新增字节的访问不会 fault，也无需新物理页；只有进入首个未映射页才触发补页。

## 4. 用户 store 首次触页

假设：

```c
char *p = sbrklazy(PGSIZE);
p[0] = 1;
```

若 `p` 所在页未映射，CPU 页表遍历失败，产生 store page fault：

- `scause == 15`；
- `stval ==` 出错虚拟地址；
- `sepc ==` 原 store 指令地址。

硬件进入 trampoline uservec，保存用户现场并切内核页表，最终调用 `usertrap()`。

## 5. `usertrap()` 识别可修复 fault

在系统调用/设备中断之后，`usertrap()` 接受：

```c
(scause == 15 || scause == 13) &&
vmfault(pagetable, stval, scause == 13 ? 1 : 0) != 0
```

13 是 load page fault，15 是 store/AMO page fault。instruction page fault 不会 lazy 分配，因为 heap 页不应可执行。

成功时不修改 `trapframe->epc`。返回用户态后，同一条 load/store 指令重新执行，这次页表映射已存在。若误像 syscall 一样 `epc += 4`，导致 fault 的访存会被跳过，程序观察到未完成操作。

## 6. `vmfault()` 的判定顺序

`vmfault(pagetable, va, read)`：

```text
if va >= current process sz: fail
va = PGROUNDDOWN(va)
if any valid PTE already maps va: fail
mem = kalloc(); if none: fail
zero all 4096 bytes
map va -> mem with PTE_R | PTE_W | PTE_U
if mapping fails: kfree(mem); fail
return physical address
```

对硬件 fault，传入的 `va` 是原始 `stval`。检查 `va < p->sz` 发生在向下对齐前，因此刚越过精确 break、但仍落在同一页的未映射地址会失败；如果该物理页因旧有效字节已映射，硬件根本不会 fault，页级保护也无法阻止访问页尾超出逻辑 sz 的少量字节。

copy helper 的情况不同：`copyin()`/`copyout()` 传给 `vmfault()` 的是页首 `va0`，不是原始用户指针。只要 `va0 < p->sz`，略微越过 break 但仍位于同一页的 copy 请求可能物化该页并成功。这也是当前页级边界的一部分，不能把 `vmfault()` 的参数检查误写成所有入口都对原始用户地址做字节级验证。

当前实现没有 heap 起点或 lazy 区间表。`vmfault()` 只检查地址低于 `p->sz` 且 PTE 无效，所以 `[0, p->sz)` 内任何真正页洞都有资格变成 `R|W|U` 页，不只是在概念上属于 heap 的洞。已有 text/guard PTE 会阻止权限升级，但若未来加入稀疏 ELF、`mmap` 或不同权限匿名区间，就需要显式的区间与权限元数据。

## 7. 权限保护不会被 fault 升级

`ismapped()` 只检查 PTE 是否存在且 `PTE_V`。因此：

- 对只读 text 执行 store，硬件虽产生 page fault，但 `ismapped == true`，`vmfault` 拒绝，不会偷偷加写权限。
- guard page 有有效映射但无 `PTE_U`，用户访问 fault 后同样拒绝。
- trampoline/trapframe 或超出 `p->sz` 的地址拒绝。

`usertrap()` 随后打印 unexpected trap、标记 killed，并 `kexit(-1)`。lazy handler 只填真正没有映射且位于逻辑范围的页洞。

## 8. 分配和映射权限

新物理页必须清零，既满足新内存初值语义，也防止泄露前一使用者的数据。映射权限固定为：

```text
PTE_R | PTE_W | PTE_U
```

没有 `PTE_X`，所以 lazy heap 不能执行。当前 `read` 参数虽然由 usertrap 区分 load/store 传入，但 `vmfault()` 函数体没有使用它；load fault 和 store fault 都得到可写页。这应按当前源码记录，不能假设实现了按访问类型收窄权限。

这些新叶 PTE 也没有显式设置 Accessed（A）或 Dirty（D）位。当前路径能在返回后成功重试，依赖平台采用硬件更新方案：硬件可推测性地设置 A，而 D 必须由实际写访问触发。若平台启用 Svade、要求软件管理 A/D，并且仍把新 PTE 的这些位初始化为 0，重试会再次产生 page fault；此时 PTE 已有 `PTE_V`，`vmfault()` 会把它当作“已经映射”而拒绝，当前 `usertrap()` 随后杀死进程。适配时可以在发布叶 PTE 前预置适当的 A/D；若要保留按访问置位的语义，则必须增加独立的 A/D fault handler，识别有效叶 PTE、更新相应位并执行所需的 `sfence.vma`，而不能把它交给 lazy-hole 分配逻辑。

## 9. 分配失败

若 `kalloc()` 返回 0，`vmfault()` 不改变页表。若 `mappages()` 失败，它释放刚分配页后返回 0。`usertrap()` 将其视为不可修复异常并 kill 当前进程，不向用户程序返回类似 `ENOMEM`。

`mappages()` 内部的 `walk(..., 1)` 可能在最终失败前已经安装空的中间页表页；`vmfault()` 只回收本次数据页，不立即拆除这些空分支。它们仍归当前页表所有，并在地址空间最终执行 `freewalk()` 时释放，因此不是永久泄漏，但也不是“失败后页表逐位完全不变”。

这与 eager `sbrk` 不同：eager 在系统调用时发现内存不足，回滚本次已分配的用户叶页并让 `sbrk()` 返回 -1；它同样可能保留本轮建立的空中间页表，留待整个页表销毁。lazy 把数据页失败推迟到实际访问，届时 faulting 进程被终止。

## 10. 内核 `copyin()` 也能首次补页

用户不一定先用 CPU 指令直接触页。例如：

```c
char *p = sbrklazy(PGSIZE);
write(fd, p, 10);
```

文件/pipe/device write 在内核读取用户 buffer 时调用 `copyin()`。它逐页 `walkaddr()`；找不到物理地址时调用：

```c
vmfault(pagetable, va0, 0)
```

合法 lazy 页会当场分配零页，随后复制出 10 个零字节。这个过程发生在系统调用 handler 内，不产生第二次用户 trap，也不改变用户 epc。

`walkaddr()` 只检查 `PTE_V|PTE_U`，`copyin()` 不另查 `PTE_R`。当前所有用户可访问页都由构造路径附带 `PTE_R`，所以实际映射满足读取要求；这不是 helper 自身独立实施的权限校验。

如果复制跨多个 lazy 页，每页独立分配。中间耗尽内存时 helper 返回 -1；此前已经补出的页仍属于进程，内核目标缓冲区也可能已有部分内容，均不回滚。上层系统调用可能返回 -1、0 或已完成的部分长度，取决于 file、pipe、console 等调用者怎样解释 helper 失败。

## 11. 内核 `copyout()` 也能首次补页

类似地：

```c
char *p = sbrklazy(PGSIZE);
read(fd, p, 10);
```

`copyout()` 找不到映射时调用 `vmfault()`，然后额外检查最终 PTE 含 `PTE_W`，防止向只读 text 写入。它还在每页开始显式拒绝 `va0 >= MAXVA`。成功后把内核数据复制到新零页的相应位置。

读文件、读 pipe、`wait(status)`、`fstat` 和 `pipe(fdarray)` 等任何通过 copyout 写用户内存的路径都可能成为 lazy 页的首次触发者。

## 12. `copyinstr()` 明确不补页

`copyinstr()` 遇到 `walkaddr() == 0` 立即返回 -1，不调用 `vmfault()`。因此：

```c
char *p = sbrklazy(PGSIZE);
open(p, O_RDONLY);
```

若 p 所在页未先触及，路径复制失败。用户先写 `p[0] = '\0'` 后，store fault 建页，后续 open 才能读取字符串。

与其他跨页 copy helper 一样，失败不会回滚已经写入内核目标缓冲区的前缀；`copyinstr()` 在找不到 NUL、超过 `max` 或后续页无效时也不保证目标以 NUL 结尾。调用者只能在返回 0 后把结果当作 C 字符串。

这是当前 `kernel/vm.c` 的明确实现契约，但 `user/usertests.c:lazy_copy` 只把未物化 lazy 字符串传给 `open()` 并忽略返回值，能够证明该路径不让内核 panic，不能直接观察 PTE 或证明“绝未补页”。是否调用 `vmfault()` 应通过源码、断点或页表观测验证，不能夸大这个测试的断言能力。实现本身确实使“数据 buffer 与字符串 pointer”对 lazy 内存的行为不一致。

## 13. fault 返回

成功补页后，`usertrap()`：

1. 再次检查 killed。
2. 不因普通 page fault yield。
3. `prepare_return()` 关闭中断，把 `stvec` 指向 `uservec`，填写 trapframe 的内核入口字段，清除 `sstatus.SPP`、设置 `sstatus.SPIE`，并把保存的用户 PC 写入 `sepc`。
4. trampoline `userret` 切换到用户页表，并恢复 trapframe 中保存的用户通用寄存器。
5. `sret` 回到原 load/store。

`prepare_return()` 是为返回用户态重新配置 CSR，而不是恢复 trap 前的整个 `sstatus`。TLB 刷新随 `satp` 切换两侧的 `sfence.vma` 完成，因此新 PTE 对重试可见；在硬件更新 A/D 的平台前提下，保存的用户通用寄存器恢复，`sepc` 仍指向 faulting 指令，程序从该指令重新执行。

## 14. fork 如何处理 lazy hole

`kfork()` 的 `uvmcopy()` 遍历 `[0, p->sz)`，但对不存在页表级或无 `PTE_V` 的页直接 continue。结果：

- 逻辑 `sz` 完整复制；
- 父已触及页被分配新物理页并复制内容；
- 父未触及 hole 在子页表中仍是 hole；
- 父子以后分别首次访问、分别分配，不共享物理页。

这避免 fork 一个 1 GiB 稀疏 lazy 区时突然分配 1 GiB。它仍不是 COW，已实际存在的页全部 eager copy。

不过 `uvmcopy()` 仍以 `PGSIZE` 为步长扫描整个 `[0, sz)`；稀疏地址空间节省的是物理页复制，不是扫描时间。接近 `TRAPFRAME` 的巨大 lazy size 会让 fork 具有 `O(sz / PGSIZE)` 的高成本。

复制已物化页失败时，`uvmcopy()` 解除此前复制的子叶页；可能已建立的空中间页表由随后 `kfork()` 的 `freeproc()`/`freewalk()` 回收。父页表和父物理页保持不变。

## 15. 缩小区域

`sys_sbrk()` 对任何 `n < 0` 都调用 `growproc(n)`，无论策略参数 eager/lazy。对没有发生无符号下溢的正常缩容，`uvmdealloc()` 只对跨过的完整页范围调用 `uvmunmap(..., do_free=1)`，并返回精确新 size。

`growproc()` 没有单独拒绝负数 `n` 造成的 `sz + n` 无符号下溢；若结果绕成不小于旧 size，`uvmdealloc()` 会保留旧 size 并让调用返回成功。`lazy_copy` 的大负参数片段覆盖的是这种“不损坏旧 break”的回归边界，不应把当前行为描述成严格的负参数范围校验。

本分支 `uvmunmap()` 对缺失中间页表或无效 PTE 执行 continue，因此可以安全跨过 lazy holes；有效页则释放物理页并清 PTE。若新旧 break 仍在同一物理页，不释放该页，但逻辑 `p->sz` 缩小。这里的 continue 不会跳过整个缺失子树：外层循环仍以 `PGSIZE` 走遍缩减范围，所以大范围 `uvmdealloc()` 是 `O((oldsz-newsz)/PGSIZE)`，而不是按实际物化页数计费。

缩小后再访问已超新 size 的未映射页，`vmfault` 因 `va >= p->sz` 拒绝。若地址仍落在未释放的边界物理页，硬件可能不 fault，这是页粒度与字节粒度的同一限制。

## 16. exit 与 exec 释放带洞地址空间

`uvmfree(pagetable, sz)` 对整个 page-rounded 区间调用能容忍空洞的 `uvmunmap`，再递归释放页表页。容忍空洞不等于按稀疏映射优化：第一阶段仍扫描 `sz/PGSIZE` 个逻辑页，之后 `freewalk()` 才按实际存在的页表节点递归。`kexit()` 本身只把进程留为 ZOMBIE；父进程之后在 `kwait()` 中调用 `freeproc()` 才真正销毁其用户页表。这个最终回收路径和 exec 的旧映像替换都能处理 lazy holes，不会因“预期每页存在”panic，但即使只物化少数页也可能付出 `O(sz/PGSIZE)` 的同步清理时间。

exec 构建的新 ELF/stack 使用 eager `uvmalloc()`；成功提交后旧 lazy 地址空间整体释放。新程序后续调用 sbrklazy 才产生自己的 holes。

但 `sys_exec()` 在调用 `kexec()` 构建新页表前，先从当前旧地址空间导入参数。路径和各参数字符串经 `fetchstr()->copyinstr()` 读取而不补页；`argv[]` 指针元素则由 `fetchaddr()->copyin()` 逐槽读取，所以只有指针向量所在的合法 lazy 页可能在参数导入阶段被物化。该副作用发生在旧 `p->pagetable` 上：参数导入失败时会释放已经分配的内核参数页，后续 `kexec()` 失败时还会清理已经创建的临时新映像，但两者都不会撤销旧地址空间中已经补出的 lazy 页。这些页继续归旧地址空间所有，之后可由缩容、进程回收或下一次成功 exec 释放。

## 17. 与 guard/text/高地址的边界

| 场景 | `vmfault()`/helper 的判断 | 直接用户硬件访问 | `copyin()`/`copyout()` |
|---|---|---|---|
| 低于 size 的未映射页洞（正常来自 lazy heap） | 分配 R/W/U 零页 | 返回并重试原指令；还依赖硬件更新 A/D | helper 在新页上继续复制 |
| 已触及的普通 lazy page | 已有有效、权限匹配的 PTE | 正常访问，本来不应 fault | 正常复制 |
| Svade 下有效叶 PTE 仅缺 A/D | `vmfault()` 因 `PTE_V` 拒绝；需预置位或独立 A/D handler | 当前零位实现最终 kill | helper 的软件 walk 不检查用户 PTE 的 A/D |
| 只读 text 的 store/copyout | store fault 时 `vmfault()` 见 PTE 有效；`copyout()` 另查 `PTE_W` | store fault 不可修复，最终 kill | `copyout()` 返回 -1；`copyin()` 仍可读取可读 text |
| stack guard | `walkaddr()` 因无 U 失败，`vmfault()` 又因 PTE 有效而拒绝 | fault 不可修复，最终 kill | helper 返回 -1，本身不 kill 进程 |
| 传给 handler 的 `va >= p->sz` | 拒绝；硬件传原地址，copy helper 传页首 | fault 不可修复，最终 kill | 当传入的页首也越界时返回 -1，本身不 kill |
| instruction fault | cause 12，`usertrap()` 不调用 `vmfault()` | 最终 kill | 不适用 |
| 为页洞分配时物理内存耗尽 | `vmfault()` 返回 0 | fault 不可修复，最终 kill | helper 返回 -1，本身不 kill |

表中的 helper 返回值还要由上层系统调用解释；它不保证用户最终一定看到 `-1`，但 helper 自身不会像直接用户 fault 路径那样调用 `kexit(-1)`。

Svade 行描述的是单独考察用户 PTE 时的结果，并假定内核直映、内核 text、trampoline 和其他页表已经完成平台适配。当前内核为这些映射新建 PTE 时通常也不传 A/D；未经全局适配的 Svade 环境可能在启动、trap 过渡或 helper 通过内核直映访问物理页时更早失败，不能把问题局限为 lazy 用户页。

## 18. 并发、锁和原子性

xv6 没有同一地址空间内的多用户线程，正常情况下不会有两个 hart 同时为同一进程同一 VA fault。进程调度可能迁移 hart，但同一时刻只在一个 CPU 上 RUNNING。

`vmfault()` 自身没有页表锁；这一单线程进程模型是“检查未映射 -> 分配 -> map”无需处理重复映射竞态的前提。若未来增加共享地址空间线程，必须为该序列增加同步和重复 fault 回收逻辑。

它用参数 `pagetable` 执行 `ismapped()`，却固定向 `myproc()->pagetable` 执行 `mappages()`。当前所有会真正缺页的入口都传当前进程页表；`exec` 对未提交新页表使用 `copyout()` 时，stack 已 eager 映射，不会进入 `vmfault()`。因此“参数页表就是当前进程页表”是隐含前置条件，函数不能安全地作为任意离线页表的通用 fault handler。

补页中的唯一 VM 内部锁是 `kalloc()` 短暂获取的 `kmem.lock`；页表检查和 PTE 写入没有锁。copy helper 可能在调用者持锁时补页，例如 pipe 路径持 `pi->lock`，文件路径可能持 inode/buffer sleeplock，`kwait()` 持 `wait_lock` 和子进程锁。当前路径不会睡眠，锁顺序为“业务锁 -> `kmem.lock`”，而分配器持 `kmem.lock` 时不获取业务锁。若以后加入换页、文件回填或内存回收，必须重新审计这些持锁调用点。

## 19. 失败与部分效果

| 场景 | 结果 |
|---|---|
| lazy 正增长越过上界或无符号加法回绕 | 系统调用返回 -1，sz 不变 |
| 负缩容使 `sz + n` 无符号下溢 | 当前实现可能返回成功但保持旧 sz；不是严格的范围拒绝 |
| 第一次直接访问分配失败 | 进程 exit(-1) |
| 跨页 copyin/out 在后页失败 | helper 返回 -1；调用者可能返回错误或部分长度；已复制数据和前面补出的页不回滚 |
| copyinstr 遇 lazy hole | 系统调用返回 -1，不补页、不 kill |
| store 到 text/guard | fault 不可修复，进程 exit(-1) |
| fork 复制已触及页失败 | fork 回滚 child 并返回 -1 |
| 缩容穿过未映射 hole | 正常跳过，不 panic |

## 20. 测试映射

`user/usertests.c` 中：

- `lazy_alloc`：逻辑扩 1 GiB，每 64 页触一页并回读。
- `lazy_unmap`：稀疏触页，子进程缩回整区后再访问必须被 kill。
- `lazy_copy`：让 copyinstr 接触 lazy hole 而不应 panic，覆盖异常大负 shrink，并验证 read/write 对坏高地址失败；它不直接断言 PTE 未被补出。
- `lazy_sbrk`：推进到 `TRAPFRAME` 下界、检查 eager 页清零和再增长失败。
- `sbrkfail`/`sbrkbugs`/`sbrklast`：覆盖耗尽、缩到边界和部分页保留。

可运行：

```sh
./test-xv6.py lazy_alloc
./test-xv6.py lazy_unmap
./test-xv6.py lazy_copy
./test-xv6.py lazy_sbrk
```

这些命令会重建并改写 `fs.img`。

## 21. GDB 观察点

```gdb
b sys_sbrk
b usertrap
b vmfault
b copyin
b copyout
b copyinstr
```

一次选在新页洞内的直接 fault 应看到：调用后 `p->sz` 已增长、目标 `walk()` 尚无有效 leaf；`scause` 为 13/15，`stval` 在范围内；`kalloc` 返回页被清零；mappages 后 leaf 含 V/R/W/U；`epc` 前后不变。若扩容只增加同一已映射页内的字节，本来就不会出现 fault。

一次系统调用内补页不会第二次进入 usertrap，应从 copy helper 直接到 vmfault。字符串失败则应确认 copyinstr 没有进入 vmfault。

## 22. 核心不变量

1. 只有传给 `vmfault()` 的地址严格小于当前 `p->sz` 才可 lazy 分配；硬件入口传原始 `stval`，copyin/out 入口传页首 `va0`。
2. 已存在有效 PTE 的权限 fault 不能通过 lazy handler 升级权限。
3. 新页在映射给用户前必须完整清零。
4. lazy heap 页必须用户可读写但不可执行。
5. 修复成功后重试原指令，不能推进 epc。
6. fork/缩容/释放必须容忍 `[0, sz)` 内没有 PTE 的页洞。
7. copyin/copyout 可补页，copyinstr 的当前实现不补页；`lazy_copy` 只覆盖安全失败，不能单独证明未分配。
8. 当前零位 PTE 的成功重试依赖硬件更新 A/D；Svade 平台必须预置适当位，或提供独立的软件 A/D fault handler。

## 23. 核心结论

lazy allocation 延迟的是“物理页和 PTE 的建立”，不是地址所有权：`p->sz` 先提交逻辑范围，首次使用再提交单页映射。当前实现把硬件 fault handler 与 `copyin()`/`copyout()` 都作为补页入口，因此理解某个地址是否会成功，必须同时问它是否在 `p->sz` 内、当前是否已有有效 PTE、handler 收到的是原始地址还是页首，以及访问通过 CPU、copyin/out 还是 copyinstr 发起。
