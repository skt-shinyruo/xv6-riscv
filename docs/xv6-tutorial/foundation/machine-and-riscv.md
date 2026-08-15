# 从 C 调用栈到基础 RISC-V

## 问题场景与本单元成果

C 源码写的是函数调用，但处理器执行的是指令、寄存器读写和内存访问。xv6 的启动、trap 和上下文切换都要求读者能在这两个视角间往返。

本单元不要求记住完整 RISC-V 指令集。出口产物是对 `_entry` 从入口到 `call start` 的全部有效指令所造成状态变化的逐步说明、一张普通函数调用的寄存器/栈角色表，以及一次三参数 `memcmp` 调用的 C 到 RISC-V 参数映射。不要预设指令条数；以当前基线反汇编为准。

## 前置单元与暂存黑盒

硬前置：[用 C 表达内存、指针和链式结构](c-memory.md)。

暂时把 CSR 的完整语义、machine/supervisor/user 特权规则、页表、timer 和多 hart 并发当作黑盒。本单元只引入后续追踪必须使用的寄存器：

- `pc`：下一条指令的位置。
- `sp`：当前栈顶。
- `ra`：函数返回位置。
- `a0` 至 `a7`：参数、返回值和系统调用约定使用的寄存器。
- `tp`：本分支内核用于保存 hart ID。

## 最小模型和关键不变量

函数调用至少包含三件事：把参数放到约定位置、把返回位置保存到 `ra`、把控制流改到被调用函数。函数若需要保存局部状态，会调整 `sp` 并使用栈内存。RISC-V 整数调用约定把前八个整数或指针参数依次放入 `a0` 至 `a7`，并用 `a0` 返回整数或指针结果；“指针参数”传的是地址，不是它指向的全部字节。

以当前源码 `kernel/string.c:memcmp(const void *v1, const void *v2, uint n)` 为具体例子。对下面的调用：

```c
unsigned char left[3] = {1, 2, 3};
unsigned char right[3] = {1, 2, 4};
int result = memcmp(left, right, 3);
```

调用边界的映射是 `a0 = &left[0]`、`a1 = &right[0]`、`a2 = 3`；`call`/`jal` 把返回位置写入 `ra`。`memcmp` 返回时，`a0` 不再承载 `left` 地址，而承载负数、0 或正数结果，本例应为负数。该映射描述 ABI 边界，不承诺编译器进入函数后继续把原值留在同一寄存器。

`kernel/entry.S:_entry` 在 QEMU 把每个 hart 带到 `0x80000000` 后执行。它根据 `mhartid` 为每个 hart 选择独立的 4096 字节栈，然后 `call start`：

```text
sp = stack0 + (hartid + 1) * 4096
```

`+1` 让 hart 0 使用第一块栈的顶端，而不是数组起点。各 hart 的栈区不能重叠，这是进入 C 代码前的关键不变量。

## 源码追踪计划

1. `kernel/kernel.ld:_entry`：链接脚本把入口放在 QEMU 跳转地址。
2. `kernel/entry.S:_entry`：建立每 hart 栈并调用 C。
3. `kernel/start.c:start`：设置最小 machine-mode 状态，最后通过 `mret` 到 `main`。
4. `kernel/main.c:main`：第一个 supervisor-mode C 入口。
5. `kernel/string.c:memcmp`：三个 C 参数、返回值和实际 RISC-V 参数寄存器。

使用 `rg` 找符号，用 `objdump` 或构建生成的 `kernel/kernel.asm` 对照实际指令。不要使用文档行号作为长期锚点。

## 观察任务

先不运行 QEMU，手工填写。表中的一行可以对应一条或一组只共同完成一个状态变化的指令：

| 时刻 | `a0` | `a1` | `sp` 表达式 | `ra` | `pc`/下一步 |
|---|---|---|---|---|---|
| 进入 `_entry` | 未约定 | 未约定 | 未建立 | 未约定 | `la sp, stack0` |
| `la`、`li` 后 | 4096 | 未约定 | `stack0` | 未约定 | 读取 `mhartid` |
| `csrr`、`addi` 后 | 4096 | `hartid + 1` | `stack0` | 未约定 | `mul` |
| `mul`、`add` 后 | `4096 * (hartid + 1)` | `hartid + 1` | 每 hart 栈顶 | 未约定 | `call start` |
| 进入 `start` | 保留偏移值 | 保留 `hartid + 1` | 每 hart 栈顶 | 返回到 `spin` | `pc = start` |

分别代入 hart 0 和 hart 2，写出相对 `stack0` 的 `sp` 偏移，并说明为何相差 8192 字节。

再运行：

```sh
make kernel/kernel
sed -n '/<memcmp>:/,+40p' kernel/kernel.asm
```

提交一张参数传递表，至少包含 `left`、`right`、`3`、返回位置和 `result` 五项，分别写出 C 含义、调用边界位置和返回后是否仍保持原含义。用反汇编中的 `a0`、`a1`、`a2` 读取和 `ret` 核对这张表；不要把数组三个字节误写成三个独立寄存器参数。

## 有界修改任务

`N/A`。本单元的目标是建立可核对的机器状态模型；修改入口栈大小会同时影响数组声明、立即数和 ABI 对齐，超出当前边界。后续实验必须先写出这些约束再允许改动。

## Oracle、证据、失败路径和局限

- `S`：表格中的每一步都能对应到 `kernel/entry.S:_entry` 的一条指令或 `kernel/start.c:start` 的一项状态设置。
- `S`：`memcmp(left, right, 3)` 的三个参数分别映射到 `a0/a1/a2`，`ra` 保存返回位置，返回结果覆盖 `a0` 原有参数含义。
- `F`：后续 GDB 观察到的 hart 0 `sp` 应落在第一块栈的顶端附近，并保持 16 字节对齐。
- 当前静态推导没有证明多个 hart 一定按某个打印顺序启动，也没有证明栈永远不会溢出。
- `mret`、CSR 和页表仍是显式黑盒，核心路径会重新访问。

## 退出产物与后续单元

提交 `_entry` 状态表、hart 0/2 的偏移计算、`pc/sp/ra/a0-a7/tp` 角色表和 `memcmp` 参数传递表。漏掉从源码中的 `la` 到 `call start` 的任一状态变化、栈公式与源码不一致、hart 0/2 偏移不相差 8192 字节、把 `ra` 当成当前 `pc`，或不能把 `left/right/3/result` 具体映射到 `a0/a1/a2/a0`，都表示本单元未通过。随后进入 [用 GDB 观察寄存器、内存和栈](guided-debugging.md)。
