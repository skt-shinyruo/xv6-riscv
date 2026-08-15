# 用 GDB 观察寄存器、内存和栈

## 问题场景与本单元成果

静态阅读告诉你代码可能怎样执行；调试器让你检查这一次执行实际停在哪里、寄存器和内存是什么。两者必须互相校验，不能用一次观察代替通用结论。

出口产物是一份 `_entry -> start -> main` 的短追踪记录，至少包含三个断点、每个断点的 `pc/sp/ra` 和一段栈内存。

## 前置单元与暂存黑盒

硬前置：[从 C 调用栈到基础 RISC-V](machine-and-riscv.md)。

暂时不解释 QEMU gdb stub 的协议、所有 CSR、调试器如何插入断点以及多 hart 停止策略。VSCode 是可选界面，不是本单元前置。

## 最小模型和关键不变量

调试需要两个进程：

1. `make CPUS=1 qemu-gdb` 构建 xv6，启动单 hart QEMU，开放 gdb stub，并在第一条指令前暂停。
2. `gdb-multiarch` 读取 `kernel/kernel` 符号并连接 QEMU。

普通 `make qemu` 和 `make qemu-gdb` 都会使用 `fs.img`。不要同时启动两个可写实例。退出旧 QEMU 后再改变模式。

本仓库从 `.gdbinit.tmpl-riscv` 生成 `.gdbinit`，并根据用户 ID 选择端口。不要假定模板中的 `1234` 就是实际端口；运行 `make print-gdbport` 查看。

## 源码追踪计划

1. `Makefile:qemu-gdb`：QEMU 调试入口。
2. `Makefile:print-gdbport`：实际端口。
3. `.gdbinit.tmpl-riscv:target remote`：连接、架构和符号设置。
4. `kernel/entry.S:_entry`、`kernel/start.c:start`、`kernel/main.c:main`：三个断点。

## 观察任务

第一次学习启动链时固定 `CPUS=1`，避免另一个 hart 先命中同一断点。多 hart 不是错误，但必须额外记录 GDB thread 和 hart；核心路径再引入这项观察。

终端 A：

```sh
make CPUS=1 qemu-gdb
```

终端 B 使用预检通过的 `gdb-multiarch`：

```sh
gdb-multiarch -q -nx kernel/kernel
```

`-nx` 明确不读取任何隐式初始化文件，因此本次练习总是显式连接实际端口。在 GDB 中输入：

```text
set architecture riscv:rv64
target remote 127.0.0.1:<make print-gdbport 的输出>
symbol-file kernel/kernel
source docs/xv6-tutorial/resources/foundation/gdb-commands.txt
```

先读懂命令资源再执行。它依次在 `_entry`、`start`、`main` 断下，并记录：

```text
info threads
info registers pc sp ra a0 a1 tp
x/i $pc
x/8gx $sp
backtrace
```

进入 `_entry` 时 `sp` 尚未建立，所以该断点只记录寄存器和 backtrace；在 `start` 和 `main` 才读取 `$sp` 指向的内存。若在 `_entry` 执行 `x/8gx $sp` 得到 `Cannot access memory at address 0x0`，这是模型预测的边界结果，不是可以忽略后继续猜测的栈证据。

如果工具链或 QEMU 缺失，把精确命令、退出状态和错误信息记录为环境失败；这时本单元不能标记完成。

## 有界修改任务

复制 GDB 命令资源到临时文件，在文件末尾追加 `break usertrap` 和 `continue`。必须追加在末尾，因为资源在早期阶段会用 `delete breakpoints` 清理启动断点；若插在这些命令之前，新断点也会被删除。解释为什么前三个启动断点不会命中 `usertrap`，以及随后必须发生什么用户态 trap 才会命中。不要修改仓库中的共享资源文件。

## Oracle、证据、失败路径和局限

- `F`：三个启动断点按 `_entry -> start -> main` 的因果顺序命中；记录实际 hart 和寄存器值。
- `B`：错误端口必须导致连接失败；缺少符号文件时，地址可能可见但源码符号不可用。
- `S`：观察到的 `sp` 与上一单元的每 hart 栈公式一致。
- 一次断点顺序不能证明所有 hart 的全局顺序，也不能证明没有断点扰动。

## 退出产物与后续单元

提交填好的[源码追踪工作表](../templates/trace-worksheet.md)和环境失败记录（如有），并写明 `CPUS`、GDB thread 与 hart。三个断点缺少任一项、顺序不符、`start` 的 `sp` 不满足上一单元公式、没有实际栈内存/backtrace，或任一必需工具缺失，都表示本单元未通过。满足前四个基础单元后，进入 [Foundation gate](gate.md)。
