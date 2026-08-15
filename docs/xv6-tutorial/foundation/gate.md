# Foundation gate

## 问题场景与本单元成果

“我大概看懂了”不能作为进入核心路径的标准。本门槛要求你提交四类可复核产物，证明已经能操作 C、机器状态、构建工具和 GDB。

本单元不引入新知识。成果是一个完整 gate 包；只有全部检查通过，才能开始核心路径。

## 前置单元与暂存黑盒

Entry learner 必须完成：

- [从命令行构建并运行程序](command-line-build.md)
- [用 C 表达内存、指针和链式结构](c-memory.md)
- [从 C 调用栈到基础 RISC-V](machine-and-riscv.md)
- [用 GDB 观察寄存器、内存和栈](guided-debugging.md)

已经具备这些能力的 Core learner 可以不重复前四个教学过程，但不能跳过 gate：必须提交同样的产物、运行同样的 oracle，并接受同一份 rubric。

操作系统子系统仍是黑盒；门槛只检查进入核心路径所需的工具和推理能力。

## 最小模型和关键不变量

Gate 包包含：

1. `c-memory.md`：三节点程序 patch、内存图、位标志真值表、set/test/clear 观察和失败场景说明。
2. `machine.md`：`_entry` 状态表、hart 0/2 栈计算、寄存器角色表和 `memcmp` 参数传递表。
3. `debug-trace.md`：三个启动断点及 `pc/sp/ra`、栈内存和 backtrace。
4. `environment.md`：源码 commit、dirty 状态、编译器、GDB、QEMU、命令与退出状态。

从 [`templates/foundation-gate.md`](../templates/foundation-gate.md) 复制清单，逐项链接这四份文件。rubric 只有“满足”或“不满足”，不使用总分抵消失败项：

| 能力 | 必须出现的可审阅证据 | 明确失败条件 |
|---|---|---|
| 命令行与构建 | 四类构建/运行对象、宿主检查、内核强制重建、退出状态和一次分类后的预期失败 | 不能区分源码/目标文件/可执行文件/进程，任一必需命令失败，或失败阶段靠猜测 |
| C 内存推理 | 三节点 patch、链表/数组内存图、位标志真值表、set/test/clear 观察、三个非法访问位置 | 生命周期/边界不明、单 bit 操作改变其他 bit、精确输出不符，或资源未恢复 |
| C/RISC-V 调用栈 | `_entry` 全部状态变化、hart 0/2 栈计算、寄存器角色表和 `memcmp(left, right, 3)` 的参数/返回值映射 | 状态无法指回源码、栈公式错误、混淆 `pc` 与 `ra`，或不能把具体 C 参数映射到 `a0/a1/a2` 和返回 `a0` |
| 引导式调试 | `_entry -> start -> main` 三断点的 `pc/sp/ra`、thread/hart、栈内存和 backtrace | 断点缺失/乱序、在未建立的栈上伪造内存、工具缺失，或观察值靠猜测 |
| 可复现与清理 | manifest 源码基线、走查时教程提交、dirty 状态、工具版本、`CPUS=1`、端口、命令结果和临时目录/QEMU 清理 | baseline 或环境未记录、同时运行可写 QEMU、临时资源未清理，或把 timeout 当 oracle |

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

从干净的 gate 工作目录重新执行宿主检查、内核强制重建和一次 QEMU GDB 追踪。不要引用之前的“成功”文字代替本次输出。把以下命令、实际结果和退出状态写入环境记录：

```sh
git rev-parse HEAD
git status --short
cc --version
qemu-system-riscv64 --version
gdb-multiarch --version
make -B kernel/kernel
make print-gdbport
sh docs/xv6-tutorial/resources/foundation/check-pointer-list.sh
make CPUS=1 qemu-gdb
```

`make -B kernel/kernel` 的实际编译命令证明 Makefile 找到了某个受支持的 RISC-V 交叉工具链；记录其前缀，不要预设一定是 `riscv64-unknown-elf-`。GDB 退出后在 QEMU 终端输入 `Ctrl-a x`，确认 QEMU 终止，再复核没有共享资源修改。

## 有界修改任务

在临时副本中完成三节点链表改动，用掩码分别设置、测试和清除 `NODE_PINNED`，运行检查，再恢复为基线并重新运行。提交 patch、位标志真值表和各次结果，证明你能区分预期变化与清理后的基线。

## Oracle、证据、失败路径和局限

- `S`：内存图、位标志运算、调用栈、具体 C/RISC-V 参数传递和寄存器说明内部一致，并能指回源码锚点。
- `F`：宿主检查通过；QEMU/GDB 能连接并命中三个断点。
- `B`：至少记录一次预期失败，并说明失败位于路径、编译、连接还是运行阶段。
- 无法用 `|`、`&` 和 `~` 分别设置、测试和清除单个 bit，或任一必需工具缺失、追踪靠猜测补全、资源修改未恢复、baseline 未记录，门槛都不通过。
- 通过 gate 不代表已经理解 xv6；它只证明你具备开始核心路径的入口能力。

## 退出产物与后续单元

提交完整 gate 包并按 rubric 自评；任一行“不满足”都不能进入核心路径。通过后进入 [从 QEMU 启动到 shell 提示符](../core/observe-system.md)。教程整体发布仍会在后续核心单元建设期间保持 `draft`，这不改变已验证 Foundation gate 的效力。
