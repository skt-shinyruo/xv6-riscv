# Foundation 0.1.0 非作者走查记录

- 教程版本：`0.1.0`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`3a6d442d00ab4b08e9f547ca1c50bedbda00c3d4` 加 #3 未提交候选 diff；走查记录了 dirty 状态，最终提交保留本记录和完整 diff
- 走查单元或连续路径：`foundation.command-line-build -> foundation.c-memory -> foundation.machine-and-riscv -> foundation.guided-debugging -> foundation.gate`
- 匿名入口能力：`FND-E17-R2`；能逐字执行终端指令，不预设 xv6、RISC-V 或 GDB 知识；未参与教程编写，也未读取实现参考文档
- 使用环境：WSL2 Linux 6.6.87.2 x86-64；GCC 13.3.0、Make 4.3、`riscv64-linux-gnu-*` 13.3.0/binutils 2.42、QEMU 8.2.2、GDB 15.1、`bc` 1.07.1

## 观察到的卡点

首轮走查不能通过。`gdb-commands.txt` 在 `_entry` 的 `sp=0` 时读取栈并中断，且没有 `start`、`main` 断点；默认 `CPUS=3` 使三个断点可能来自不同 hart。机器单元要求不存在的固定 13 条 `_entry` 指令，gate 引用的 rubric 未提供，compile/link 未分开演示，修改任务直接作用共享资源，数组越界和 C 局部变量初始化后的观察位置也不明确。

首轮还暴露了两个容易形成错误模型的位置：`head.next = NULL` 只是让节点不可达，不等于空指针解引用；函数入口断点处的当前源码行尚未执行，不能把未初始化局部变量当成对象状态。

## 验收产物

- 命令行记录：工具预检通过；分离的 `cc -c` 和链接均退出 `0`；基线输出为 `count=2 sum=12 active=2 pinned=1`。错误相对路径退出 `2`；临时 8/13 副本先以 expected 12/actual 21 退出 `1`，更新 oracle 后退出 `0`，临时目录已删除。
- C 内存记录：链为 `5 -> 11 -> 7 -> NULL`；set 阶段输出 `count=3 sum=23 active=3 pinned=2`，clear 阶段输出 `count=3 sum=23 active=3 pinned=1`，两次退出 `0`。提交了链表/数组图、bit 真值表，以及空指针、数组越界、悬空指针的首个非法访问位置；仓库原始脚本复查退出 `0`。
- 机器记录：`make -B kernel/kernel` 两次退出 `0`，Makefile 选择 `riscv64-linux-gnu-*`；当前 `_entry` 到 `jal start` 为 8 条实际指令。hart 0 栈偏移 `+4096`，hart 2 为 `+12288`，相差 `8192` 且满足 16 字节对齐。对 `memcmp(left, right, 3)`，调用边界为 `a0=&left[0]`、`a1=&right[0]`、`a2=3`、`ra=返回位置`；当前反汇编分别通过 `a0/a1` 读两个数组、用 `a2` 控制长度，并用返回 `a0` 覆盖原 `left` 参数含义，本例结果为负数。
- 调试记录：`CPUS=1` 时同一 `Thread 1.1`/hart 0 依次命中 `_entry -> start -> main`。`start` 处 `pc=0x80000054`、`sp=stack0+4096`、`ra=spin`；`main` 处 `sp=stack0+4064`、`tp=0`，栈内存和 backtrace 可读。错误端口 `26001` 连接失败并退出 `1`；临时追加的 `usertrap` 断点在用户态 `ecall` 后以 `scause=8` 命中。
- Gate 记录：在新的临时目录、强制内核重建和新的 QEMU/GDB 会话中重新取得上述证据；QEMU 到 shell `$` 后用 `Ctrl-a x` 终止。共享资源与临时 patch 比对恢复，没有遗留 QEMU/GDB 进程，`git diff --check` 通过。

## 修正与复查

教程改为显式列出 Linux/WSL 工具预检、分离 compile/link、自建自清理临时副本、链表和数组边界图，以及 set/clear 的精确输出。机器任务改为按当前反汇编覆盖 `_entry` 到 `call start`，不再预设条数。调试路径固定 `CPUS=1`，资源在栈建立后才读内存，并完整记录 `_entry`、`start`、`main`、thread/hart、寄存器、下一条指令、栈和 backtrace。Gate 新增二值 rubric 和提交模板，并明确 Entry/Core learner 使用同一个 performance gate。

非作者随后从头复查，五个单元和 gate 全部通过。复查提出的非阻断可用性问题也已收紧：Make 预检不再用可能吞掉退出状态的管道；宿主调试统一使用 `gdb-multiarch -nx`；共享 GDB 资源直接输出 thread 和 `tp`；`usertrap` 断点明确追加在启动断点清理命令之后。

独立 Standards 审查随后发现初版 gate 只要求通用寄存器角色，没有要求已接受 ADR 中的具体 C/RISC-V 参数传递。受影响的 machine、guided-debugging 和 gate 先降回 `draft`；教程、rubric 和 gate 模板补入上述真实 `memcmp` 三参数映射，并以当前源码和 `kernel.asm` 复核后，才允许重新按依赖顺序晋级。

## 结果

| 单元 | 结果 | 晋级依据 |
|---|---|---|
| `foundation.command-line-build` | 通过 | `F/B` 命令、四类对象、预期失败和清理证据完整 |
| `foundation.c-memory` | 通过 | `S/F/B` 内存、数组、bit、三节点修改和非法访问证据完整 |
| `foundation.machine-and-riscv` | 通过 | `S/F` 源码/反汇编状态表、具体参数传递与单 hart 运行观察一致 |
| `foundation.guided-debugging` | 通过 | `F/B` 三断点、栈、backtrace、错误端口和工具证据完整 |
| `foundation.gate` | 通过 | 四类 gate 包产物、五行 rubric、恢复与清理全部满足 |

状态晋级必须按上表和 manifest 的 `requires` 顺序进行。Foundation 没有待迁移的 owning question group，本次未移动 `docs/questions/` 内容，也没有创建同步副本。教程整体仍为 `draft`，本记录只支持 Foundation 五个单元晋级为 `verified`。
