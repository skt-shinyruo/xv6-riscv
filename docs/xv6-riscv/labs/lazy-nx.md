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

枚举所有调用 `mappages` 建用户 leaf 的位置，记录 permission来源：`uvmalloc`、`uvmfirst`、`exec` segment、trapframe/trampoline、`vmfault`、fork copy。确认没有 helper 在 fault 后无条件添加 `PTE_X`。

### B. 建立行为测试

child 调 `sbrklazy(PGSIZE)`，先读首/末字节并验证零，再写入 RISC-V `ret` 指令编码，在调用函数指针前执行 `fence.i`。预期 instruction page fault，child 以 -1 状态被回收。parent 必须存活。

测试需要分别覆盖：

- 先由用户 store 物化，再执行；
- 先由内核 `read`/`write` 的 `copyout/copyin` fallback 物化，再执行；
- 页边界首/末地址；
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
- bad address `>=p->sz`、`>=TRAPFRAME` 仍失败且不分配；
- 每个失败 child 被 wait 后，数据页和中间页表页计数回到基线。

## 5. 故障注入

对单次 fault 的最多三次页分配分别注入失败：

| 位置 | 预期 |
|---|---|
| 数据页 `kalloc` | `vmfault` 失败，进程被 kill，无 PTE/页泄漏 |
| L1 page-table page | 已分配数据页被释放；可能创建的更高层结构按当前契约最终可回收 |
| L0 page-table page | 同上，不得留下有效 leaf |

另做 mutation：临时给 lazy PTE 加 `PTE_X`，NX 测试必须真的执行成功或以不同原因失败，从而证明测试能区分权限，而非总因坏指令/坏函数地址失败。mutation 后必须恢复源码。

## 6. 多 hart 与指令同步边界

测试中的 `fence.i` 只保证执行 hart 对刚写指令的本地同步；NX 应在取指权限检查阶段先阻止执行。仓库没有通用远端 `fence.i`/IPI 协议，不能把本实验解释为验证跨 hart 动态代码发布。记录该边界，并在[信任与失败模型](../architecture/trust-and-failure-model.md)的假设下解释结果。

## 7. 清理

删除仅调试用 PTE syscall和 mutation，保留用户态 NX 回归测试、必要的命名 flags/断言及文档。用 `CPUS=1` 与多 hart各运行定向测试和完整测试集。
