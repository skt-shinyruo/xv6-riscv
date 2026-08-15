# 从 QEMU 启动到 shell 提示符

## 问题场景与本单元成果

第一次运行 xv6 时，终端会从 QEMU 启动走到 shell 提示符。这个结果横跨机器入口、内核初始化、调度、首进程、文件系统和用户程序；若一开始就展开所有机制，主线会被细节淹没。

本单元只建立一条可复查的因果骨架。出口产物是一份从 QEMU 启动到 `$ ` 的源码与运行时间线：每一步记录触发器、`path:symbol`、实际观察和当前黑盒，并附单/多 hart 配置实验、边界失败和清理证据。

## 前置单元与暂存黑盒

硬前置：[Foundation gate](../foundation/gate.md)。先复用其中的命令行、RISC-V 栈和 GDB 能力，不在这里重复教学。

本轮明确保留五个黑盒：

- `first-user-process`：`userinit()` 怎样分配空进程、发布 `RUNNABLE` 状态并取得第一次运行机会。
- `scheduler-selection`：`scheduler()` 怎样选择 `RUNNABLE` 进程、跨 `swtch()` 传递锁责任，以及多 hart 怎样协调。
- `first-user-address-space`：`kexec()` 怎样建立用户映射，以及 `prepare_return()`、trampoline 和 `sret` 怎样完成地址空间与特权级切换。
- `shell-image-path`：`/init`、`sh` 和其他用户程序怎样进入 `fs.img`，又怎样由 `kexec()` 查找并装载。
- `console-device-path`：`printk()`、`printf()` 和 `write()` 怎样经过 console、UART、PLIC 与中断产生当前输出。

这些黑盒分别覆盖尚未解释的进程与内存、调度、文件系统和设备机制。本单元可以观察它们的边界，不能据此声称已经解释或验证其内部正确性。

## 最小模型和关键不变量

先使用下面的因果骨架；箭头表示当前源码中存在的控制或发布关系，不表示每个内部步骤都已展开：

```text
QEMU -kernel kernel/kernel
  -> kernel/entry.S:_entry
  -> kernel/start.c:start
  -> kernel/main.c:main
  -> kernel/proc.c:userinit
  -> kernel/proc.c:scheduler
  -> kernel/proc.c:forkret
  -> kernel/exec.c:kexec("/init")
  -> kernel/trap.c:prepare_return
  -> kernel/trampoline.S:userret -> sret
  -> user/ulib.c:start
  -> user/init.c:main
  -> user/sh.c:main
  -> user/sh.c:getcmd -> "$ "
```

| 可观察边界 | 当前可说明的最小变化 | 暂不展开 |
|---|---|---|
| QEMU -> `_entry` | `-kernel` 指定的 ELF 被装入，hart 从内核入口开始执行 | QEMU 设备模型与 reset 细节 |
| `_entry` -> `start` | 每个 hart 按 `mhartid` 取得独立栈，然后调用 C | 完整物理内存布局 |
| `start` -> `main` | 设置 supervisor 入口与基本 CSR，`mret` 后执行 `main()` | 中断委托、页表和保护的完整契约 |
| hart 0 `main` -> `userinit` | 共享子系统按源码顺序初始化，首进程被发布为可运行 | 各子系统内部状态 |
| `userinit` -> `scheduler` -> `forkret` | scheduler 最终把第一次运行机会交给首进程 | 选择策略、锁和 `swtch()` |
| `forkret` -> `/init` | 第一次 `forkret()` 初始化文件系统并请求装载 `/init` | 路径查找、ELF 和地址空间替换 |
| `prepare_return` -> `userret` -> `sret` | 准备用户 PC 和状态，经 trampoline 切换页表并离开 supervisor mode | trapframe、页表映射和 CSR 完整契约 |
| `user/ulib.c:start` -> `user/init.c:main` | 用户 runtime 调用 `/init` 的 `main()` | ELF 装载与用户栈构造 |
| `init` -> `sh:main` -> `sh:getcmd` -> `$ ` | `init` 打印启动行并启动 shell；`getcmd()` 写出提示符 | fd、console 和设备中断路径 |

### Branch delta

本分支的 `userinit()` 只分配一个空首进程并把它设为 `RUNNABLE`。第一次 `forkret()` 在普通进程上下文中运行 `fsinit()`，随后直接调用 `kexec("/init", ...)`。不要套用“内核把一段 initcode 复制到首进程”的常见 xv6 路径。

本单元使用以下不变量约束时间线：

- `_entry` 调用 C 前，每个 hart 已有独立且 16 字节对齐的栈。
- hart 0 完成共享初始化并发布 `started=1` 前，其他 hart 不能进入其每 hart 初始化。
- `userinit()` 返回前，首进程已变为 `RUNNABLE`；`scheduler()` 只把 CPU 交给 `RUNNABLE` 进程。
- `fsinit()` 在首进程第一次 `forkret()` 中只执行一次，随后才请求 `kexec("/init")`。
- `$ ` 说明本次运行已到达用户态，并经过足以显示提示符的文件系统、进程和 console 路径；它不证明这些机制在失败、并发或崩溃下正确。

## 源码追踪计划

从仓库根目录依次定位，不用行号作为提交物：

```sh
rg -n '^qemu:|^qemu-gdb:|QEMUOPTS|CPUS' Makefile
rg -n '^_entry:|call start' kernel/entry.S
rg -n '^start\(|mret|^main\(|userinit\(|scheduler\(' kernel/start.c kernel/main.c
rg -n '^userinit\(|^scheduler\(|^forkret\(|fsinit\(|kexec\(|prepare_return\(|trampoline_userret' kernel/proc.c
rg -n '^kexec\(' kernel/exec.c
rg -n '^prepare_return\(' kernel/trap.c
rg -n '^userret:|sret' kernel/trampoline.S
rg -n 'TRAMPOLINE' kernel/memlayout.h
rg -n 'MAXVA' kernel/riscv.h
printf '0x%x\n' "$(( (1 << (9 + 9 + 9 + 12 - 1)) - 4096 ))"
rg -n '^start\(' user/ulib.c
rg -n 'init: starting sh|exec\("sh"' user/init.c
rg -n '^main\(|^getcmd\(|write\(2, "\$ "' user/sh.c
```

在追踪工作表中为每个锚点写一行：进入条件、所在 hart/进程、关键状态变化、下一个可观察边界、以及是否跨入上述黑盒。长源码清单不是出口产物。

## 观察任务

先记录可复现环境，并构建当前基线：

```sh
git rev-parse HEAD
git status --short
qemu-system-riscv64 --version
gdb-multiarch --version
make -B kernel/kernel fs.img
readelf -h user/_init | rg 'Entry point address'
rg ' start$' user/init.sym
```

把 `user/_init` 的 ELF entry 与 `user/init.sym` 中 `start` 的地址并列记录；当前基线两者都必须是 `0xbc`。这项检查只建立“`sret` 将去哪里”的可观察连接，不在本单元解释 ELF 生成和装载过程。

所有运行都使用临时镜像，避免两个 QEMU 写同一个 `fs.img`：

```sh
OBS_DIR=$(mktemp -d /tmp/xv6-observe.XXXXXX)
cp fs.img "$OBS_DIR/cpus-3.img"
cp fs.img "$OBS_DIR/cpus-1.img"
cp fs.img "$OBS_DIR/gdb.img"
cp fs.img "$OBS_DIR/invalid.img"
sha256sum fs.img
```

先执行默认的三 hart 观察：

```sh
docs/xv6-tutorial/resources/observe-system/run-qemu.sh 3 "$OBS_DIR/cpus-3.img"
```

看到 `$ ` 后输入：

```text
echo tutorial-observation
```

确认输出中单独出现 `tutorial-observation`，再用 QEMU 的 `Ctrl-a x` 退出。记录实际出现的 `xv6 kernel is booting`、`hart N starting`、`init: starting sh`、`$ `、输入回显和命令输出；不要把一次 `hart 1/2` 打印顺序写成并发保证。

然后在终端 A 启动单 hart 调试实例：

```sh
docs/xv6-tutorial/resources/observe-system/run-qemu.sh 1 "$OBS_DIR/gdb.img" 26000
```

在终端 B 启动 GDB：

```sh
gdb-multiarch -nx kernel/kernel
```

逐行输入：

```gdb
set pagination off
target remote :26000
break _entry
break start
break main
break userinit
break scheduler
break forkret
break prepare_return
set $trampoline_userret = 0x3ffffff000 + (userret - trampoline)
break *$trampoline_userret
continue
```

每次命中后记录 `info threads`、`p/x $pc`、`p/x $sp`、`p/x $tp` 和 `x/i $pc`，再 `continue` 到下一个断点。连接时 PC 可能仍在 QEMU ROM；`_entry` 才是第一条仓库代码。`_entry` 入口处 `sp` 尚未由当前指令序列建立，到了 `start` 才能检查独立栈。

`0x3ffffff000` 必须由当前 `kernel/riscv.h:MAXVA` 和 `kernel/memlayout.h:TRAMPOLINE` 的表达式复算；不能只抄教程中的结果。GDB 表达式把 ELF 中 `userret - trampoline` 的偏移换算到运行时高地址。命中高地址 `userret` 时额外记录 `p/x $a0`、`p/x $sepc`、`p/x $sstatus` 和 `p/x $satp`：此时 `a0` 是将安装的用户 `satp`，当前 `satp` 仍是内核页表，而 `sepc` 是随后 `sret` 使用的用户 PC。把实际 `sepc` 与前面独立取得的 `/init` ELF entry 和 `start` 地址比较；三者必须相等，才能把 `userret -> sret` 与 `user/ulib.c:start` 连成一条证据链。本单元只记录这个边界，不展开页表与 trapframe 契约。记录后执行 `detach`、`quit`，等待终端 A 到达 `$ `，再用 `Ctrl-a x` 退出。

GDB 的源码行表示下一条将执行的源码位置，而不是已完成的效果。单 hart 追踪给出一条可复查骨架；三 hart 输出只补充实际并发观察，两者都不能证明跨 hart 的唯一全序。

## 有界修改任务

本实验只改变 QEMU 的 CPU 数配置，不改 xv6 源码或仓库镜像。先运行单 hart：

```sh
docs/xv6-tutorial/resources/observe-system/run-qemu.sh 1 "$OBS_DIR/cpus-1.img"
```

执行相同的 `echo tutorial-observation` 并退出。与三 hart 记录比较：两者都必须依次出现内核启动行、`init: starting sh`、shell 提示符和精确 echo 结果；单 hart 记录不得出现 `hart 1 starting` 或 `hart 2 starting`。

再触发一个不会进入 guest 的配置边界：

```sh
docs/xv6-tutorial/resources/observe-system/run-qemu.sh invalid "$OBS_DIR/invalid.img"
printf 'exit=%s\n' "$?"
```

Oracle 要求 QEMU 报告 `smp.cpus` 需要整数、退出状态非零，并且输出中没有 `xv6 kernel is booting`。错误文本可能随 QEMU 小版本变化；判定依据是参数类别、非零退出和 guest 未启动，而不是逐字匹配整行。

最后证明共享输入未被运行实例使用并清理：

```sh
sha256sum fs.img
ps -ef | rg '[q]emu-system-riscv64|[g]db-multiarch'
rm -r -- "$OBS_DIR"
git status --short
```

两次 `fs.img` 哈希必须一致；进程检查不得留下本实验实例；`OBS_DIR` 必须删除。允许的副作用只有构建产物和临时镜像，不能留下 tracked 源码修改。

## Oracle、证据、失败路径和局限

- `S`：时间线每条边都落到本单元 manifest 登记的源码锚点；本分支首进程路径明确为 `userinit -> scheduler -> forkret -> kexec("/init") -> prepare_return -> userret -> sret`；`userret` 的 `sepc`、`user/_init` ELF entry 和 `user/ulib.c:start` 符号地址三者相等；五个黑盒没有被偷换成解释结论。
- `F`：`CPUS=3` 与 `CPUS=1` 都到达 `$ `，并精确输出 `tutorial-observation`；GDB 在同一 hart 上按 `_entry -> start -> main -> userinit -> scheduler -> forkret -> prepare_return -> userret` 取得八个状态记录，`sepc` 指向已独立检查的 `/init:start`，随后终端出现 `init: starting sh` 和 `$ `。
- `B`：`invalid` CPU 配置在 guest 启动前以非零状态失败；共享 `fs.img` 哈希不变；所有 QEMU/GDB 和临时镜像均清理。timeout 只能充当 watchdog，不能替代任一结果判定。
- 资源结果：实验不改变 tracked 源码，不让并行 QEMU 共享可写镜像；临时镜像允许在 guest 运行期间改变，清理后不再存在。
- `C` 不适用：这里只记录一次多 hart 输出，不控制事件交错，也不建立 happens-before 结论。
- `R` 不适用：没有 crash point、持久性或恢复主张；临时镜像隔离也不是恢复证据。

若工具链、QEMU、GDB、端口或构建失败，记录最早失败命令、退出状态和环境；缺少任一必需观察时本单元不能通过。shell 提示符、一次 echo 和一次成功启动都不是系统正确性的通用证明。

## 退出产物与后续单元

提交填好的[源码追踪工作表](../templates/trace-worksheet.md)，其中包含：环境与基线、从 QEMU 到 `getcmd()` 的完整源码/运行时间线、`/init` ELF entry 与 `start` 地址、八个 GDB 断点状态、`sepc` 等值连接、单/三 hart 对比、`invalid` 边界失败、共享镜像哈希、进程与临时目录清理，以及五个仍未解除的黑盒。任一缺项都不能把本单元标为 `verified`。

随后进入[用户程序如何成为可运行镜像](user-program-and-abi.md)，解除 `shell-image-path`。其余黑盒由后续进程与内存、调度、通信和设备单元负责。
