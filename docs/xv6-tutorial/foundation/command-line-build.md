# 从命令行构建并运行程序

## 问题场景与本单元成果

你拿到一份 C 源码后，终端里究竟发生了什么，才会出现一行输出？本单元把“输入命令”拆成编辑文件、编译、链接、启动进程和检查退出状态五个可观察步骤。

完成后，你应能区分源码、目标文件、可执行文件和运行中的进程，独立运行教程附带的检查脚本，并提交一份失败定位记录：如果命令失败，你能指出失败发生在路径、编译、链接还是运行阶段。

## 前置单元与暂存黑盒

没有知识性前置。你只需要能打开 Linux 或 WSL 终端并进入仓库根目录。整条 Foundation 路径需要 POSIX shell 常用工具、`git`、`rg`、宿主 C 编译器、Make、`bc`、Makefile 能识别的 RISC-V GCC/binutils、`qemu-system-riscv64` 和 `gdb-multiarch`。先逐项预检：

```sh
command -v sh git rg cc make bc qemu-system-riscv64 gdb-multiarch
make -Bn kernel/entry.o
```

最后一条只预览一个内核目标的构建，不写生成物；输出中的交叉工具链前缀可能是 `riscv64-linux-gnu-`、`riscv64-unknown-elf-` 或 Makefile 支持的其他前缀。这里不接管道，因此 shell 保留 `make` 自己的退出状态。任一命令非零退出都属于环境阻塞：记录缺失命令和退出状态，通过所用 Linux/WSL 发行版的包管理器补齐后再继续，不能用相似名字猜测后续输出。

本单元暂时把编译器内部、ELF 格式、操作系统如何创建宿主进程，以及 xv6 的 Makefile 规则当作黑盒。后续单元会逐步解除这些黑盒。

## 最小模型和关键不变量

命令行由“程序名”和“参数”组成。shell 根据当前目录和 `PATH` 找到程序，程序用退出状态告诉 shell 成功或失败：通常 `0` 表示成功，非零表示失败。

先确认自己位于仓库根目录：

```sh
pwd
test -f Makefile
```

教程资源的构建链是：

```text
pointer-list.c --编译--> pointer-list.o --链接--> pointer-list --运行--> 进程和一行标准输出
```

`pointer-list.c` 是人维护的源码，`.o` 目标文件包含尚未形成完整程序的机器码，`pointer-list` 是可由宿主启动的可执行文件。进程不是另一个磁盘文件，而是 shell 启动可执行文件后形成的一次运行实例。`cc source.c -o program` 会在一次命令中连续完成编译和链接，所以看不到独立 `.o` 文件并不表示没有这两个阶段。

检查脚本使用临时目录保存生成物，并在退出时删除它。源码是输入，生成物不是需要手工维护的事实。

关键不变量：

- 命令从仓库根目录执行，或使用脚本自己的绝对路径。
- 编译打开警告并把警告视为错误。
- 检查同时约束退出状态和精确输出。
- 临时生成物不会写进 xv6 的内核、用户程序或磁盘镜像。

## 源码追踪计划

先运行宿主资源，再只定位 xv6 构建入口，不尝试理解全部 Make 语法：

1. `docs/xv6-tutorial/resources/foundation/check-pointer-list.sh`：入口脚本。
2. `docs/xv6-tutorial/resources/foundation/pointer-list.c:main`：宿主程序入口。
3. `Makefile:qemu`：普通运行入口。
4. `Makefile:qemu-gdb`：受控调试入口。

使用下面的命令定位锚点：

```sh
rg -n '^qemu:|^qemu-gdb:' Makefile
rg -n '^main\(void\)' docs/xv6-tutorial/resources/foundation/pointer-list.c
```

## 观察任务

运行：

```sh
sh docs/xv6-tutorial/resources/foundation/check-pointer-list.sh
echo $?

foundation_tmp=$(mktemp -d)
cp docs/xv6-tutorial/resources/foundation/pointer-list.c \
  docs/xv6-tutorial/resources/foundation/check-pointer-list.sh \
  "$foundation_tmp/"
cc -std=c99 -Wall -Wextra -Werror -c \
  docs/xv6-tutorial/resources/foundation/pointer-list.c \
  -o "$foundation_tmp/pointer-list.o"
cc "$foundation_tmp/pointer-list.o" -o "$foundation_tmp/pointer-list"
test -f "$foundation_tmp/pointer-list.o"
test -x "$foundation_tmp/pointer-list"
"$foundation_tmp/pointer-list"
```

应看到：

```text
pointer-list check passed
0
count=2 sum=12 active=2 pinned=1
```

然后故意从一个不包含仓库的目录运行相对路径 `sh docs/...`，记录 shell 的错误。回到仓库根目录再运行，说明为什么这次能找到文件。不要用 `sudo` 修复路径错误。记录 `foundation_tmp` 的实际值；练习结束后先用 `test -n "$foundation_tmp"` 和 `test -d "$foundation_tmp"` 限定目标，再用 `rm -r -- "$foundation_tmp"` 只删除这个由 `mktemp -d` 返回的目录。

## 有界修改任务

只在已经复制到 `foundation_tmp` 的副本中把 `pointer-list.c` 的两个节点值改为 `8` 和 `13`。先预测 `sh "$foundation_tmp/check-pointer-list.sh"` 为何失败，再把临时检查脚本中的预期值改为 `count=2 sum=21 active=2 pinned=1`，确认检查恢复通过。最后重新运行仓库中的原始检查脚本，证明共享资源仍在基线。

修改范围只允许这两个临时副本。不要运行 `make clean`，也不要删除仓库其他生成物。

## Oracle、证据、失败路径和局限

- `F`：脚本退出 `0`，并打印精确的通过消息。
- `B`：错误的相对路径必须失败；程序输出与预期不一致也必须失败。
- 退出 `0` 只能说明这一个宿主程序满足当前检查，不能说明 xv6 工具链或 QEMU 已安装。
- timeout 在这里没有正确性含义；程序若卡住，timeout 只能帮助结束它。

## 退出产物与后续单元

提交一段不超过十五行的记录，包含执行目录、源码/目标文件/可执行文件/进程的区别、命令、退出状态、一次故意失败及其原因，以及临时目录的清理结果。脚本非零退出、不能区分四种对象，或不能把故意失败归入路径、编译、链接、运行之一，都表示本单元未通过。随后进入 [用 C 表达内存、指针和链式结构](c-memory.md)。
