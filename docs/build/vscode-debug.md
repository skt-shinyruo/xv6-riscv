# 在 WSL 中使用 VSCode/GDB 调试 xv6

本文记录在 Windows + WSL Ubuntu 中使用 VSCode 调试 xv6-riscv 的配置和流程。

## 环境要求

- 使用 VSCode 的 Remote - WSL 打开本仓库，左下角应显示 `WSL: Ubuntu`。
- 在 WSL 中安装并使用 `gdb-multiarch`：

```bash
which gdb-multiarch
```

当前 `.vscode/launch.json` 把路径固定为 `/usr/bin/gdb-multiarch`；若命令输出不同，应同步修改 `gdbpath`，不能只保证命令位于 `PATH`。

- 在 WSL 侧安装 VSCode 扩展：
  - `webfreak.debug`，用于直接驱动 GDB，支持 RISC-V remote debug。
  - `ms-vscode.cpptools`，用于 C 语言基础功能。

可以在 VSCode 扩展页中确认扩展安装位置是 `WSL: Ubuntu`，也可以运行：

```bash
code --list-extensions | grep -E 'webfreak.debug|ms-vscode.cpptools'
```

## VSCode 配置

仓库中的 [`.vscode/extensions.json`](../../.vscode/extensions.json) 声明了工作区推荐扩展，其中 `webfreak.debug` 提供下面使用的 Native Debug 配置；`ms-vscode.cpptools` 提供 C 语言编辑能力。推荐项只负责提示安装，真正的调试参数位于 [`.vscode/launch.json`](../../.vscode/launch.json)：

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "xv6: attach to QEMU gdb",
      "type": "gdb",
      "request": "attach",
      "target": "127.0.0.1:26000",
      "remote": true,
      "executable": "${workspaceFolder}/kernel/kernel",
      "cwd": "${workspaceFolder}",
      "gdbpath": "/usr/bin/gdb-multiarch",
      "debugger_args": [
        "--nx"
      ],
      "valuesFormatting": "disabled",
      "stopAtConnect": true,
      "autorun": [
        "set architecture riscv:rv64",
        "set confirm off",
        "set disassemble-next-line auto",
        "set riscv use-compressed-breakpoints yes"
      ]
    }
  ]
}
```

这里使用的是 Native Debug 扩展的 `"type": "gdb"`，不是 C/C++ 扩展的 `"type": "cppdbg"`。`cppdbg` 在当前环境中会因为 RISC-V 架构名 `riscv:rv64` 报 `Parameter 'arch'`。

仓库还提供 [`.gdbinit.tmpl-riscv`](../../.gdbinit.tmpl-riscv)，供 `make qemu-gdb` 生成本地 `.gdbinit`。模板中的 `symbol-file kernel/kernel` 加载内核符号，`target remote 127.0.0.1:1234` 使用 `1234` 作为占位端口；Makefile 生成 `.gdbinit` 时会把它替换为：

```text
GDBPORT = uid % 5000 + 25000
```

uid 对 5000 取模等于 1000 时，结果才是当前 `launch.json` 固定的 `26000`；常见的 uid 1000 满足这个条件。先运行 `make print-gdbport` 核对；若结果不同，应同步修改 `launch.json` 的 `target` 及下文命令中的端口。`.gdbinit` 的依赖只有模板，uid 或计算结果变化不会自动触发重建，可用 `make -B .gdbinit` 刷新。

VSCode 配置使用 `executable` 和 `target` 表达模板中的符号文件和远程目标。`debugger_args` 里的 `--nx` 会禁止 GDB 读取任何 init 文件，包括刚生成的项目 `.gdbinit`；这正是 `autorun` 再次列出架构、反汇编和压缩断点设置的原因。两条调试路径彼此独立，但端口、符号文件和架构必须一致。

## 启动调试

先在 VSCode 的 WSL 终端中启动 QEMU GDB stub：

```bash
make qemu-gdb
```

正常情况下终端会停在类似输出：

```text
*** Now run 'gdb' in another window.
qemu-system-riscv64 ... -S -gdb tcp::26000
```

较旧 QEMU 可能由 Makefile 选择 `-s -p 26000` 这一兼容形式。另一个容易忽略的差异是：`qemu-gdb` 不依赖 Makefile 的 `check-qemu-version`，所以它不会像 `make qemu` 那样先执行名义上的 7.2 下限检查。该检查把 `major.minor` 当十进制数交给 `bc`，会把 `7.10` 误作 `7.1`，本身也不是可靠的语义版本验证。

不要关闭这个终端，也不要按 `Ctrl+C`。此时 QEMU 已暂停，等待 GDB 连接。

该目标会先按时间戳构建 `kernel/kernel`、`.gdbinit` 和 `fs.img`。QEMU 随后把 `fs.img` 作为可写 raw drive 使用，不是临时快照；调试中的文件系统写入会保留。若 `README`、任一 `UPROGS` 或 `mkfs/mkfs` 比镜像新，启动前会重新运行 `mkfs` 并截断旧镜像，原有运行时数据随之丢失。

然后在 VSCode 中按 `F5`，选择：

```text
xv6: attach to QEMU gdb
```

连接成功后，VSCode 左侧 Call Stack 会显示多个 CPU/hart，Debug Console 中会看到类似：

```text
0x0000000000001000 in ?? ()
The target architecture is set to "riscv:rv64".
```

这是正常状态。RISC-V `virt` 机器的 hart 复位 PC 是 QEMU 在 `0x1000` 提供的 MROM reset stub；`-bios none` 只取消 OpenSBI 等外部固件，并不移除这段 reset stub。当前 `virt -bios none` 直接启动路径把 stub 中的跳转目标设为 DRAM/固件起始地址 `0x80000000`，并不是从内核 ELF header 的 `e_entry` 取值。当前 `ENTRY(_entry)` 也把 ELF entry 设为 `0x80000000`，而 `_entry` 实际落在该地址还依赖 `entry.o` 排在 `OBJS` 第一项并由 `.text` 通配式首先收集；三者数值相同，但职责不同，`ENTRY` 本身既不会移动符号，也不会改变 MROM 交接地址。

## 进入断点

1. 在内核源码中打断点，例如 `kernel/main.c` 的 `main()` 内。
2. 启动 `make qemu-gdb`。
3. VSCode 按 `F5` attach。
4. attach 后不要从 `0x1000` 一步步 Step，直接点击顶部调试工具栏的 Continue，或再按一次 `F5`。也可以同时在 `*0x80000000` 和 `_entry` 下断点：前者核对 MROM 的实际交接位置，后者核对符号位置；若二者不相等，只下 `_entry` 断点可能掩盖错误，因为 stub 根本不会按 `e_entry` 去寻找它。
5. QEMU 会继续运行，进入 xv6 kernel 后命中你设置的断点。

如果想确认断点是否设置成功，可以在 Debug Console 中输入：

```gdb
info breakpoints
```

## 常见问题

### `Failed to get "write" lock`

错误示例：

```text
qemu-system-riscv64: Failed to get "write" lock
Is another process using the image [fs.img]?
```

原因通常是已有 QEMU 进程正在以可写方式使用同一个 `fs.img`。先列出候选进程：

```bash
pgrep -af qemu-system-riscv64
```

确认工作目录确实是当前工作树后，只结束对应 PID，再重新运行：

```bash
readlink -f /proc/PID/cwd
kill PID
make qemu-gdb
```

Makefile 给 QEMU 的镜像路径是相对路径 `fs.img`，所以仅看进程命令行未必能区分两个 xv6 工作树；必须用 `/proc/PID/cwd` 核对工作目录。不要直接按进程名结束全部 QEMU；同一用户可能还在其他工作树运行不同虚拟机。也不要用删除 `fs.img` 解决锁冲突：已打开文件仍由旧进程持有，而且删除镜像会丢失其持久状态。

### `QEMU: Terminated via GDBstub`

这个信息表示有 GDB 客户端连接过 QEMU，并通过 GDB stub 结束了 QEMU。常见于 VSCode 调试启动失败后的清理阶段。

先确认是否还残留当前工作树的 QEMU：

```bash
pgrep -af qemu-system-riscv64
```

若存在，用 `/proc/PID/cwd` 核对工作目录后，对具体进程执行 `kill PID`（把 `PID` 换成实际数字）。随后重新运行 `make qemu-gdb`，再在 VSCode 中按 `F5` attach。

### `could not connect: Connection timed out`

通常说明 VSCode 连接时 `127.0.0.1:26000` 没有 QEMU 在监听，或者 Makefile 实际选择了别的 `GDBPORT`。

检查顺序：

1. 运行 `make print-gdbport`，确认输出与 `launch.json` 的 `target` 相同。
2. 再运行 `make qemu-gdb`，确认终端停在 QEMU 命令处，没有退出。
3. 确认没有另一个客户端已经占用或关闭 stub，再按 `F5` attach。

端口为 26000 时可检查：

```bash
ss -ltnp 'sport = :26000'
```

### `Parameter 'arch'`

错误示例：

```text
Unable to start debugging. Specified argument was out of the range of valid values. (Parameter 'arch')
```

这是 `ms-vscode.cpptools` 的 `cppdbg` 对 RISC-V GDB 架构名支持不好导致的。使用当前文档中的 Native Debug 配置，即 `.vscode/launch.json` 中：

```json
"type": "gdb"
```

不要改回：

```json
"type": "cppdbg"
```

### `type gdb is not recognized`

说明 VSCode 没有识别 Native Debug 扩展。处理方式：

1. 确认 `webfreak.debug` 安装在 WSL 侧。
2. 在 VSCode 命令面板运行：

```text
Developer: Reload Window
```

3. 重新按 `F5`。

## 手动验证 GDB 连接

如果 VSCode 仍有问题，可以先用命令行验证 QEMU/GDB 本身是否正常。

一个终端运行：

```bash
make qemu-gdb
```

另一个 WSL 终端运行：

```bash
gdb-multiarch -q --nx \
  -ex "set architecture riscv:rv64" \
  -ex "file kernel/kernel" \
  -ex "target remote 127.0.0.1:26000" \
  -ex "info registers pc"
```

命令中的 `26000` 必须替换为 `make print-gdbport` 的实际输出。如果能看到类似：

```text
pc             0x1000  0x1000
```

说明 QEMU stub、命令行 GDB、端口和内核符号文件这条连接链正常；若 VSCode 仍失败，再集中检查扩展是否安装在 WSL 侧、`gdbpath`、`launch.json` 和 Debug Console 报错。
