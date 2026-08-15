# Foundation gate

## 问题场景与本单元成果

“我大概看懂了”不能作为进入核心路径的标准。本门槛要求你提交四类可复核产物，证明已经能操作 C、机器状态、构建工具和 GDB。

本单元不引入新知识。成果是一个完整 gate 包；只有全部检查通过，才能开始核心路径。

## 前置单元与暂存黑盒

必须完成：

- [从命令行构建并运行程序](command-line-build.md)
- [用 C 表达内存、指针和链式结构](c-memory.md)
- [从 C 调用栈到基础 RISC-V](machine-and-riscv.md)
- [用 GDB 观察寄存器、内存和栈](guided-debugging.md)

操作系统子系统仍是黑盒；门槛只检查进入核心路径所需的工具和推理能力。

## 最小模型和关键不变量

Gate 包包含：

1. `c-memory.md`：三节点程序 patch、内存图、位标志真值表、set/test/clear 观察和失败场景说明。
2. `machine.md`：`_entry` 状态表、hart 0/2 栈计算、寄存器角色表。
3. `debug-trace.md`：三个启动断点及 `pc/sp/ra`、栈内存和 backtrace。
4. `environment.md`：源码 commit、dirty 状态、编译器、GDB、QEMU、命令与退出状态。

关键不变量是可复现：评审者在相同 baseline 和声明环境中，应能运行命令并理解每项观察怎样支持结论。

## 源码追踪计划

门槛复核以下已学锚点，不增加新锚点：

- `docs/xv6-tutorial/resources/foundation/pointer-list.c:main`
- `docs/xv6-tutorial/resources/foundation/pointer-list.c:has_flag`
- `kernel/entry.S:_entry`
- `kernel/start.c:start`
- `kernel/main.c:main`
- `Makefile:qemu-gdb`

## 观察任务

从干净的 gate 工作目录重新执行宿主检查和一次 QEMU GDB 追踪。不要引用之前的“成功”文字代替本次输出。把命令、实际结果和退出状态写入环境记录。

## 有界修改任务

在临时副本中完成三节点链表改动，用掩码分别设置、测试和清除 `NODE_PINNED`，运行检查，再恢复为基线并重新运行。提交 patch、位标志真值表和各次结果，证明你能区分预期变化与清理后的基线。

## Oracle、证据、失败路径和局限

- `S`：内存图、位标志运算、调用栈和寄存器说明内部一致，并能指回源码锚点。
- `F`：宿主检查通过；QEMU/GDB 能连接并命中三个断点。
- `B`：至少记录一次预期失败，并说明失败位于路径、编译、连接还是运行阶段。
- 无法用 `|`、`&` 和 `~` 分别设置、测试和清除单个 bit，或任一必需工具缺失、追踪靠猜测补全、资源修改未恢复、baseline 未记录，门槛都不通过。
- 通过 gate 不代表已经理解 xv6；它只证明你具备开始核心路径的入口能力。

## 退出产物与后续单元

提交完整 gate 包并按 rubric 自评。当前教程发布仍为 `draft`，还需要非作者走查才能把本门槛标记为 `verified`。通过后进入 [从 QEMU 启动到 shell 提示符](../core/observe-system.md)。
