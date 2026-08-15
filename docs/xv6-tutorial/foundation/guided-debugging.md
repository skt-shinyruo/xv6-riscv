# 用 GDB 观察寄存器、内存和栈

## 问题场景与本单元成果

静态阅读告诉你代码可能怎样执行；调试器让你检查这一次执行实际停在哪里、寄存器和内存是什么。两者必须互相校验，不能用一次观察代替通用结论。

出口产物是一份 `_entry -> start -> main` 的短追踪记录，至少包含三个断点、每个断点的 `pc/sp/ra` 和一段栈内存。

## 前置单元与暂存黑盒

硬前置：[从 C 调用栈到基础 RISC-V](machine-and-riscv.md)。

暂时不解释 QEMU gdb stub 的协议、所有 CSR、调试器如何插入断点以及多 hart 停止策略。VSCode 是可选界面，不是本单元前置。

## 最小模型和关键不变量

调试需要两个进程：

1. `make qemu-gdb` 构建 xv6，启动 QEMU，开放 gdb stub，并在第一条指令前暂停。
2. RISC-V GDB 或 `gdb-multiarch` 读取 `kernel/kernel` 符号，通过 `.gdbinit` 连接 QEMU。

普通 `make qemu` 和 `make qemu-gdb` 都会使用 `fs.img`。不要同时启动两个可写实例。退出旧 QEMU 后再改变模式。

本仓库从 `.gdbinit.tmpl-riscv` 生成 `.gdbinit`，并根据用户 ID 选择端口。不要假定模板中的 `1234` 就是实际端口；运行 `make print-gdbport` 查看。

## 源码追踪计划

1. `Makefile:qemu-gdb`：QEMU 调试入口。
2. `Makefile:print-gdbport`：实际端口。
3. `.gdbinit.tmpl-riscv:target remote`：连接、架构和符号设置。
4. `kernel/entry.S:_entry`、`kernel/start.c:start`、`kernel/main.c:main`：三个断点。

## 观察任务

终端 A：

```sh
make qemu-gdb
```

终端 B 使用可用的 RISC-V GDB，例如：

```sh
gdb-multiarch kernel/kernel
```

若本地 GDB 默认不读取仓库 `.gdbinit`，显式输入：

```text
set architecture riscv:rv64
target remote 127.0.0.1:<make print-gdbport 的输出>
symbol-file kernel/kernel
```

然后按 `resources/foundation/gdb-commands.txt` 的意图执行，但不要盲目粘贴尚未理解的命令。依次在 `_entry`、`start`、`main` 断下，记录：

```text
info registers pc sp ra a0 a1
x/8gx $sp
backtrace
```

如果工具链或 QEMU 缺失，把精确命令、退出状态和错误信息记录为环境失败；这时本单元不能标记完成。

## 有界修改任务

复制 GDB 命令资源到临时文件，增加 `break usertrap`。解释为什么系统启动阶段不会立即命中它，以及必须发生什么用户态事件才可能命中。不要修改仓库中的共享资源文件。

## Oracle、证据、失败路径和局限

- `F`：三个启动断点按 `_entry -> start -> main` 的因果顺序命中；记录实际 hart 和寄存器值。
- `B`：错误端口必须导致连接失败；缺少符号文件时，地址可能可见但源码符号不可用。
- `S`：观察到的 `sp` 与上一单元的每 hart 栈公式一致。
- 一次断点顺序不能证明所有 hart 的全局顺序，也不能证明没有断点扰动。

## 退出产物与后续单元

提交填好的[源码追踪工作表](../templates/trace-worksheet.md)和环境失败记录（如有）。满足前四个基础单元后，进入 [Foundation gate](gate.md)。
