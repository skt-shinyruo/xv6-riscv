# User-program-and-ABI 0.1.0 非作者走查记录

- 教程版本：`0.1.0 draft`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`25f5e7cdb181db7abe99accd30d687346475da09` 加 #5 未提交候选 diff；最终提交保留本记录与修正后的完整 diff
- 走查单元或连续路径：`core.user-program-and-abi`
- 匿名入口能力：`ABI-C19-R1`；已通过 Foundation gate 与 `core.observe-system`，未参与候选稿编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、RISC-V GCC 13.3.0、Python 3.12.3；`CPUS=1`、128 MiB；全部构建和 QEMU 写入位于临时源码树

## 观察到的卡点

首轮走查发现自动验收器只搜索 `hello` 与 `echo` 的目标子串。候选程序若先
打印额外一行再打印 `hello-from-xv6`，原检查仍会通过，不能支持正文声明的
精确输出。验收器随后改为比较去除回车后的完整命令 transcript：命令回显、
唯一结果行和下一提示符必须逐字相等。走查者对修订版重新执行了完整实验。

独立候选审计还修正了三处源码证据归属：`kernel/syscall.h` 在编译
`user/usys.o` 时而不是生成 `user/usys.S` 时成为依赖；manifest 固定的源码
baseline 与走查时教程提交必须分栏；`_echo -> echo` 名称变换必须锚到
`mkfs/mkfs.c:main` 的 `shortname` 分支，不能只引用通用 `iappend()`。

## 验收产物

- 生成证据：`user/usys.pl` 独立重放后与 `user/usys.S` 的 `cmp` 退出 `0`；
  `user/usys.d` 记录 `user/usys.o: user/usys.S kernel/syscall.h`。Make dry-run
  显示 `_echo` 链接输入含 `echo.o`、四个 `ULIB` 对象和 `user.ld`，镜像配方
  接收 `_echo`、`_init` 与 `_sh`。
- ELF/ABI 证据：`user/_echo` 是 RISC-V ELF，entry 与 `start` 均为 `0x7a`，
  `main=0x0`、`write=0x32e`；`write` stub 实际装入 `a7=16` 后执行 `ecall`。
  `/init` 与 `sh` 的 entry 分别等于各自 `start`，而不是复用固定地址。
- 运行证据：私有镜像中的 `echo abi-check` 只输出 `abi-check`。源码时间线
  与当前分支一致：`UPROGS -> fs.img -> forkret -> kexec("/init") -> init ->
  sh -> exec("echo") -> kexec -> start -> main -> write stub`。
- 首个边界：实验树已生成 `user/_hello`，但未加入 `UPROGS`；QEMU 中完整
  transcript 为 `hello`、`exec hello failed`、下一 `$ `，没有成功输出。
- 功能路径：应用只增加一项的 `UPROGS` patch 并重建镜像后，完整 transcript
  为 `hello`、`hello-from-xv6`、下一 `$ `。随后 `echo abi-regression` 精确
  输出 `abi-regression`。
- 回归：同一隔离树执行完整 `usertests`，最终得到 `ALL TESTS PASSED`。
- 清理边界：逆向 patch，删除 `hello.c/.o/.d/_hello/.asm/.sym` 并重建后，
  `hello` 再次精确得到 `exec hello failed`，`echo` 相关回归仍通过。
- 隔离：实验前后原仓库 `git status --short` 相同；共享 `fs.img` 的 SHA-256
  前后均为 `c470503efebc0f8b33d67aed363d65a03a0bd6ca1f1389ccbd276b71d2b60f1a`。
  临时树、QEMU、Python cache 均无残留。

## 修正与复查

修订后的 `check-qemu.py` 对 `hello` 的 missing/present transcript 与 `echo`
回归做整段精确比较，完整 `usertests` 仍使用其权威终止 marker。走查者重新
运行 `run-lab.sh`，B/F/B、相关回归、完整回归和清理全部通过；脚本最终报告
`hello lab passed: boundary, success, regression, cleanup`。

技术复查同时通过普通与 development validator、generated navigation check、
5 个 validator 单测、shell 语法、Python 编译、patch apply check 和
`git diff --check`。现有 `docs/questions/` 问题跨越后续 trap、进程、内存或
文件系统 owner，本单元未迁移问题，也没有创建同步副本。

## 结果

| 单元 | 结果 | 晋级依据 |
|---|---|---|
| `core.user-program-and-abi` | 通过 | 构建元数据、生成器、ELF/ABI、当前分支 init/shell 路径及隔离 B/F/B 实验均有独立 `S/F/B` oracle，完整回归与清理通过 |

本记录只支持 `core.user-program-and-abi` 晋级为 `verified` 并解除
`shell-image-path`。它不解除 `syscall-kernel-entry` 或完整 trampoline/页表
契约，也不提供恶意 ELF、崩溃恢复、多 hart 并发的 `C/R` 证据。
