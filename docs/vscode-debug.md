# VSCode Debug xv6 in WSL

本文记录在 Windows + WSL Ubuntu 中使用 VSCode 调试 xv6-riscv 的配置和流程。

## 环境要求

- 使用 VSCode 的 Remote - WSL 打开本仓库，左下角应显示 `WSL: Ubuntu`。
- 在 WSL 中安装并使用 `gdb-multiarch`：

```bash
which gdb-multiarch
```

本机当前路径为 `/usr/bin/gdb-multiarch`。

- 在 WSL 侧安装 VSCode 扩展：
  - `webfreak.debug`，用于直接驱动 GDB，支持 RISC-V remote debug。
  - `ms-vscode.cpptools`，用于 C 语言基础功能。

可以在 VSCode 扩展页中确认扩展安装位置是 `WSL: Ubuntu`，也可以运行：

```bash
code --list-extensions | grep -E 'webfreak.debug|ms-vscode.cpptools'
```

## VSCode 配置

仓库中已经提供 [.vscode/launch.json](../.vscode/launch.json)：

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

不要关闭这个终端，也不要按 `Ctrl+C`。此时 QEMU 已暂停，等待 GDB 连接。

然后在 VSCode 中按 `F5`，选择：

```text
xv6: attach to QEMU gdb
```

连接成功后，VSCode 左侧 Call Stack 会显示多个 CPU/hart，Debug Console 中会看到类似：

```text
0x0000000000001000 in ?? ()
The target architecture is set to "riscv:rv64".
```

这是正常状态。`0x1000` 是 QEMU 启动后的早期位置，还没有进入 xv6 的 `main()`。

## 进入断点

1. 在内核源码中打断点，例如 `kernel/main.c` 的 `main()` 内。
2. 启动 `make qemu-gdb`。
3. VSCode 按 `F5` attach。
4. attach 后不要从 `0x1000` 一步步 Step，直接点击顶部调试工具栏的 Continue，或再按一次 `F5`。
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

原因是已有 QEMU 进程正在使用 `fs.img`。先结束旧进程：

```bash
pkill -f qemu-system-riscv64
```

然后重新运行：

```bash
make qemu-gdb
```

### `QEMU: Terminated via GDBstub`

这个信息表示有 GDB 客户端连接过 QEMU，并通过 GDB stub 结束了 QEMU。常见于 VSCode 调试启动失败后的清理阶段。

处理方式：

```bash
pkill -f qemu-system-riscv64
make qemu-gdb
```

然后重新在 VSCode 中按 `F5` attach。

### `could not connect: Connection timed out`

通常说明 VSCode 连接时 `127.0.0.1:26000` 没有 QEMU 在监听。

检查顺序：

1. 先运行 `make qemu-gdb`。
2. 确认终端停在 QEMU 命令处，没有退出。
3. 再按 `F5` attach。

可以检查端口：

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

如果能看到类似：

```text
pc             0x1000  0x1000
```

说明 QEMU、GDB、端口和符号文件都正常，问题只在 VSCode 调试扩展或 `launch.json`。
