# 实验：证明并加固 lazy heap 的 NX

当前 `vmfault()` 给 lazy 数据页设置 `PTE_R|PTE_W|PTE_U`，没有 `PTE_X`；`usertrap()` 只把 load/store page fault（13/15）送入 lazy 修复，instruction page fault（12）应杀死普通进程。本实验把这条源码事实变成可回归的安全不变量。

## 1. 目标与非目标

目标：所有由 `sbrklazy()` 首次物化的 heap 页可读写、初始为零且不可执行；内核 `copyin/copyout` 补出的同类页权限一致。非目标：实现完整 W^X loader、用户 `mprotect`、JIT 或内核 text/rodata加固。

## 2. 不变量

```text
lazy data leaf: V=1, U=1, R=1, W=1, X=0
logical hole:   p->sz covers VA, but no valid leaf until data access/helper
instruction fault: never allocates a lazy page
failed allocation: no reachable partial leaf and no leaked data page
```

已由 ELF loader 建立的 text 权限不应被改变。不要用“VA 位于 heap”作为唯一权限来源；未来 stack/COW 等映射需要显式的 permission policy。

## 3. 分阶段任务

### A. 静态审计

枚举当前所有建立用户地址空间 leaf 的路径并记录 permission 来源：`uvmalloc()`（ELF segment、eager heap 和 exec stack）、`proc_pagetable()`（不带 `PTE_U` 的 trampoline/trapframe）、`uvmcopy()`（fork 复制已有 flags）以及 `vmfault()`（lazy data）。当前仓库没有传统 xv6 的 `uvmfirst()/initcode` 路径；首进程在 `forkret()` 中直接 `kexec("/init", ...)`。确认没有 helper 在 fault 后无条件添加 `PTE_X`。

### B. 建立行为测试

每个子例都在独立 child 中取得一整页新的 lazy 虚拟区间：先检查当前 break 是否页对齐；若不对齐，用检查过返回值的增长推进到下一页边界，再调用并检查 `sbrklazy(PGSIZE)` 的返回值恰为该对齐 break。选择页内 4 字节对齐且不跨页的执行地址，按该子例规定的首次访问物化页面，再按小端放入 RISC-V `ret` 编码 `0x00008067`；在调用函数指针前执行 `fence.i`。预期 instruction page fault，child 以 -1 状态被回收，parent 必须存活。不能先用通用的零值/边界预读准备所有子例，因为这会抢先物化本应验证 `copyin` 或 `copyout` fallback 的页面。

测试需要分别覆盖：

- load-first 子例先读首/末字节验证零，再做页内读写边界和执行检查；
- store-first 子例先由用户 store 物化，再执行；
- copyout-first 子例在 `read` 前不得由用户触碰该页；由内核 `read` 的 `copyout` fallback 首次物化并写入一条有效 `ret` 指令，再执行；
- 先由 `write` 的 `copyin` fallback 物化零页，立即观察 `X=0`，随后再由用户 store 写入有效指令并执行；`copyin` 本身不产生指令字节，不能直接执行零页并把 illegal-instruction 误当成 NX 证据；
- 页内边界子例中的执行指令仍必须完整、4 字节对齐且不跨页，不能把“最后一个字节”直接当作可执行指令地址；
- 普通 ELF text 仍可执行，heap 普通 load/store仍成功。

### C. 收紧接口

若重构 `vmfault`，用枚举/命名 flags 表达 fault access，禁止当前含义模糊的整数参数扩展成权限捷径。instruction cause 必须在调用前被拒绝；`copyin` 的“内核读取用户内存”与用户 load cause 不能混成 PTE_R 授权。

### D. 可观察性

加入测试专用 PTE dump helper或 GDB script，在 fault 前后打印 VA、三级 PTE、PA和 flags。不要提交可让用户读取任意页表/物理地址的生产 syscall。

## 4. 验收条件

- load-first 首次访问返回 0，store/read-back 正常；
- 执行 lazy heap 在 `scause=12` 失败，不调用 `vmfault`、不新增物理页，普通 child 退出状态 -1；
- 合法 ELF text、fork/exec、eager `sbrk` 和 lazy tests 无回归；
- `walk` 观察到 lazy leaf 至少包含 `R|W|U|V` 且不含 X；访问后允许硬件设置 A/D 位。若要比较初始 flags，必须在首次 load/store 前停住；
- lazy 边界按入口分别验收：硬件 load/store fault 传原始 `stval`，所以未映射的 `addr >= p->sz` 必须失败；`copyin/copyout` 传页首 `PGROUNDDOWN(addr)`，因此 break 落在页中间时，越过 break 但仍处于最后一页的 copy 可以首次物化该页，只有页首也 `>=p->sz` 时才失败；页已映射后，用户访问和 copy helper 都不会再做字节级 `p->sz` 检查。`TRAPFRAME` 及以上仍不得分配。测试必须分别覆盖“硬件首次触碰越 break”“copy 首次触碰同一页内越 break”“已物化最后部分页的页尾”和“下一整页”，不能把它们合并成一个边界；
- 每个失败 child 被 wait 后，数据页和中间页表页计数回到基线。

## 5. 故障注入

对单次 fault 的最多三次页分配分别注入失败：

| 位置 | 预期 |
|---|---|
| 数据页 `kalloc` | `vmfault` 失败，进程被 kill，无 PTE/页泄漏 |
| L1 page-table page | 已分配数据页被释放；可能创建的更高层结构按当前契约最终可回收 |
| L0 page-table page | 同上，不得留下有效 leaf |

另做 mutation：临时给 lazy PTE 加 `PTE_X`。每条执行路径都必须先确认目标地址含有效 `ret` 编码；mutation 后函数调用应正常返回，而不能只要求“以不同原因失败”。这样才能证明测试区分的是取指权限，而非坏指令、错误函数地址或数据准备失败。mutation 后必须恢复源码。

## 6. 多 hart 与指令同步边界

测试中的 `fence.i` 只保证执行 hart 对刚写指令的本地同步；NX 应在取指权限检查阶段先阻止执行。仓库没有通用远端 `fence.i`/IPI 协议，不能把本实验解释为验证跨 hart 动态代码发布。记录该边界，并在[信任与失败模型](../architecture/trust-and-failure-model.md)的假设下解释结果。

## 7. 清理

删除仅调试用 PTE syscall和 mutation，保留用户态 NX 回归测试、必要的命名 flags/断言及文档。用 `CPUS=1` 与多 hart各运行定向测试和完整测试集。
