# 用户程序如何成为可运行镜像

## 问题场景与本单元成果

`user/echo.c` 只是源码。QEMU 中能输入 `echo hi`，说明它已经被编译、链接成 RISC-V ELF，放入 `fs.img`，由 shell 请求内核装载，并从约定入口开始执行。

本单元的出口产物是一张 `echo.c -> user/_echo -> fs.img -> main -> write stub` 生成与执行图，以及一份对 ELF 入口和系统调用 stub 的实际检查记录。

## 前置单元与暂存黑盒

硬前置：[从 QEMU 启动到 shell 提示符](observe-system.md)。相关基础：[从 C 调用栈到基础 RISC-V](../foundation/machine-and-riscv.md)。

本单元解除 `shell-image-path`，但留下 `syscall-kernel-entry`：`ecall` 之后如何保存用户寄存器、切换页表并进入 C handler，下一单元才解释。

ELF program header 的完整校验、`exec` 两阶段提交、文件系统 inode 和磁盘块布局也暂存为黑盒。

## 最小模型和关键不变量

构建链：

```text
user/echo.c
  -> user/echo.o
  + user/ulib.o + user/usys.o + user/printf.o + user/umalloc.o
  + user/user.ld
  -> user/_echo (RISC-V ELF)
  -> mkfs/mkfs
  -> fs.img 中的 echo
```

`Makefile:UPROGS` 决定哪些用户 ELF 被送给 `mkfs`。`user/user.ld` 把用户映像从虚拟地址 0 开始布局。链接器没有显式 `-e` 或 `ENTRY(...)` 时，会选择名为 `start` 的符号；最终 ELF header 才是入口事实。`user/ulib.c:start` 调用 `main(argc, argv)`，若 `main` 返回则调用 `exit`。

系统调用 ABI 是另一条边：

```text
user/user.h 声明
  -> user/usys.pl 生成 stub
  -> a7 = SYS_<name>
  -> ecall
```

关键不变量：

- C 声明、生成器中的名称、系统调用编号和内核分派必须一致。
- ELF 入口、代码地址和 `exec` 建立的用户栈必须遵守同一 ABI。
- `user/usys.S` 是生成文件；修改 `user/usys.pl`，不要直接编辑生成结果。
- 构建成功只证明链接关系成立，不证明运行时参数和用户指针合法。

## 源码追踪计划

1. `Makefile:_ %` 模式规则与 `Makefile:UPROGS`。
2. `user/user.ld:SECTIONS`：用户地址布局。
3. `user/ulib.c:start`：ELF 入口包装。
4. `user/echo.c:main`：程序逻辑。
5. `user/user.h:write`：C 可见声明。
6. `user/usys.pl:entry`：stub 生成规则。
7. `Makefile:fs.img`：用户 ELF 进入镜像。

## 观察任务

构建并检查最终产物：

```sh
make user/_echo user/usys.S fs.img
readelf -h user/_echo
nm -n user/_echo | sed -n '1,20p'
rg -n '^write:|^getpid:|ecall|SYS_' user/usys.S
```

若宿主 `nm` 不支持 RISC-V ELF，使用工具链对应的 `riscv64-*-nm`。记录 ELF header 的入口地址，并在符号表中找出该地址对应的符号。再把 `echo.c` 的 `write` 调用与生成 stub 的 `write:` 对应起来。

最后运行 `make qemu`，执行 `echo abi-check`。说明这条输出至少依赖哪四类构建输入。

## 有界修改任务

在单独实验分支新增 `user/hello.c`，其 `main` 只打印一行固定文本并返回。把 `user/_hello` 加到 `UPROGS`，重新生成 `fs.img`，在 xv6 shell 中运行 `hello`。

边界严格限制为一个用户源码文件和 `UPROGS` 一项。不要修改内核。完成后提交 patch、构建命令、精确输出和删除 `_hello` 后镜像重新构建的清理记录。

## Oracle、证据、失败路径和局限

- `S`：生成图中的每条边都能指向 Makefile、链接脚本或生成器锚点。
- `F`：`readelf` 入口对应 `start`；`hello` 在 shell 中打印精确文本并正常退出。
- `B`：只创建 `hello.c` 而不加入 `UPROGS` 时，宿主文件存在但 xv6 shell 找不到程序；该失败定位到镜像输入，不是 C 编译。
- 这些证据没有验证恶意 ELF、参数栈溢出、用户指针或 `exec` 回滚。

## 退出产物与后续单元

提交生成与执行图、ELF/stub 检查记录、hello patch 和清理结果。你已经解除 `shell-image-path`，随后进入 [一次系统调用如何往返](syscall-roundtrip.md)，解除 `syscall-kernel-entry`。
