# 用户程序如何成为可运行镜像

## 问题场景与本单元成果

`user/echo.c` 只是宿主文件系统中的 C 源码。QEMU 里的 shell 能执行
`echo abi-check`，至少还需要完成编译、链接、装入 `fs.img`、按 ELF 入口
建立进程映像和 shell 发起 `exec`。其中任意一条边断开，“宿主上有这个文件”
都不等于“xv6 能运行这个命令”。

本单元的出口是一份可独立复核的用户程序生成与执行报告包，包含两个分节：

1. 一张从 `user/echo.c` 到 shell 输出的生成与执行图，并附 Make 元数据、
   生成 stub 和 ELF 的检查记录。
2. 一份隔离的 `hello` 实验报告；报告必须包含未进入镜像时的失败、进入
   镜像后的精确输出、相关回归和逆向清理结果。

## 前置单元与暂存黑盒

硬前置：[从 QEMU 启动到 shell 提示符](observe-system.md)。相关基础：
[从 C 调用栈到基础 RISC-V](../foundation/machine-and-riscv.md)。

本单元解除 `shell-image-path`：说明用户 ELF 怎样进入 `fs.img`，当前分支
怎样装入 `/init`，以及 `init` 怎样启动 `sh`、`sh` 怎样请求执行普通命令。
这里仍把 `syscall-kernel-entry` 留作黑盒：能检查用户 stub 的 `ecall`，也能
定位 `kernel/syscall.c:syscall`，但不在本单元解释 trapframe、页表切换和
内核分派的往返细节。该黑盒由下一单元解除。

ELF program header 的全部拒绝条件、`kexec()` 的两阶段提交、inode 查找、
文件系统块布局，以及第一个进程怎样取得第一次调度机会也不在本单元展开。
这些边界分别由后续进程/内存与文件系统单元负责。

## 最小模型和关键不变量

### 生成图不是一条编译命令

普通用户程序遵循 `Makefile:_%` 模式规则：

```text
user/echo.c
  -> user/echo.o
  + ULIB = user/ulib.o + user/usys.o + user/printf.o + user/umalloc.o
  + user/user.ld
  -> user/_echo (RISC-V ELF)
  -> user/echo.asm + user/echo.sym

user/usys.pl
  -> user/usys.S（包含 #include "kernel/syscall.h"）

user/usys.S + kernel/syscall.h
  -> user/usys.o

Makefile:UPROGS + README + mkfs/mkfs
  -> fs.img 中名为 echo、init、sh 等文件
```

`user/_echo` 的前导下划线是宿主构建产物命名，不是 xv6 里的命令名；
`mkfs/mkfs` 把它以 `echo` 写入镜像。`UPROGS` 是镜像输入的唯一权威列表。
能单独生成 `user/_hello`，并不能证明 `hello` 已经进入镜像。

`user/usys.S` 是生成文件。`Makefile:$U/usys.S` 只调用
`user/usys.pl:entry`；生成文本写入 `#include "kernel/syscall.h"`，编译
`user/usys.o` 时才读取编号头文件，并在 `user/usys.d` 记录依赖。因此应修改
生成器或编号源，不应直接修改 `user/usys.S`。

ULIB 的另外两条边也属于用户运行时，而不是内核 ABI：`user/printf.c:vprintf()`
解析格式后经 `putc() -> write()` 写指定 fd；`user/umalloc.c:malloc()` 在进程内维护
free list，只有 `morecore()` 才用 `sbrk()` 扩大 heap。前者不是
`kernel/printk.c:printk()`，后者也不直接拥有物理页；它们最终仍经过本图中的 syscall stub。

当前分支有一个会改变观察结果的例外：`Makefile:user/_forktest` 使用更小的
库集合，并显式指定 `-e main -Ttext 0`；不要用 `_forktest` 推断普通用户
程序的入口规则。

### ELF 入口把链接结果交给运行时

`user/user.ld:SECTIONS` 从虚拟地址 `0` 布置段，但没有写
`ENTRY(start)`；普通链接命令也没有 `-e`。本工具链因此按默认入口选择规则
采用全局符号 `start`。这是一个需要检查的构建事实，不应只凭源码名字断言：

```text
ELF header.entry == symbol(start)
```

`kernel/elf.h:struct elfhdr` 定义 `entry/phoff/phnum`，`struct proghdr` 定义每段的
`off/vaddr/filesz/memsz/flags`；`kexec()` 只接受 `ELF_MAGIC` 并逐个装入
`ELF_PROG_LOAD`。linker 产物与 loader 因而通过同一份磁盘格式相接，而不是共享 C 调用约定。

不同程序的 `main` 大小不同，`start` 的数值地址也可能不同，不能把某次
`0x7a` 或 `/init` 的 `0xbc` 当成全局常量。`kernel/exec.c:kexec` 读取 ELF，
建立参数栈，令 `trapframe->epc = elf.entry`，并把 `argc`、`argv` 放在 RISC-V
约定的 `a0`、`a1` 位置。进入 `user/ulib.c:start` 后才调用
`main(argc, argv)`；`main` 返回时，`start` 调用 `exit`。

### 同一次 `write` 跨过三份契约

把三层接口分开记录：

| 层 | 可见契约 | `write(1, buf, n)` 的证据 |
|---|---|---|
| C 接口 | 类型、参数个数、返回类型 | `user/user.h:write` |
| RISC-V C 调用约定 | `a0`-`a2` 传前三个参数，`a0` 返回结果，`ra` 保存返回点 | `user/_echo` 反汇编中的寄存器准备与 `jal write` |
| xv6 系统调用 ABI | `a7 = SYS_write`，`a0`-`a2` 保留参数，`ecall`，随后 `ret` | `user/usys.pl:entry`、`kernel/syscall.h:SYS_write` 和生成的 `write:` stub |

`ecall` 不是普通 C 调用；而 `write:` stub 也不是内核 handler。下一单元才会
从这个 `ecall` 继续追踪。

当前分支还有一个局部生成差异：生成器把 `sbrk` stub 命名为 `sys_sbrk`，
`user/ulib.c:sbrk` 与 `sbrklazy` 再传入不同分配模式。观察 stub 时看到
`sys_sbrk` 不是缺失 `sbrk`，也不能把这个特例推广到 `write`。

### 镜像到 shell 的执行链

当前分支没有先运行一段嵌入内核的 `initcode`。`userinit()` 只创建空的第一
个进程；该进程首次到达 `kernel/proc.c:forkret` 时初始化文件系统并直接
调用 `kexec("/init", ...)`。这是会改变首进程模型的分支差异。

```text
Makefile:UPROGS
  -> fs.img:/init, /sh, /echo
  -> kernel/proc.c:forkret -> kexec("/init")
  -> user/ulib.c:start -> user/init.c:main
  -> fork -> exec("sh")
  -> user/sh.c:main -> runcmd -> exec("echo", argv)
  -> kernel/sysfile.c:sys_exec -> kernel/exec.c:kexec
  -> user/ulib.c:start -> user/echo.c:main
  -> write stub -> ecall -> [syscall-kernel-entry]
```

这里用户 ABI 仍叫 `exec`，内核实现函数在当前分支叫 `kexec`。名称不同没有
增加一层 ABI；它只是避免内核 C 符号与用户接口混淆。

关键不变量如下：

- C 声明、生成器名称、系统调用编号和内核分派槽必须一致。
- ELF 入口必须等于 `start`；`kexec()` 必须把该值写入 `epc`，而不是跳到
  `main`。
- `UPROGS` 中的宿主 ELF、`fs.img` 中的文件名和 shell 传给 `exec` 的路径
  必须能互相对应。
- 构建成功只证明生成关系成立，不证明镜像包含该程序，也不证明系统调用的
  用户指针、失败回滚或并发行为正确。

## 源码追踪计划

从构建边开始，再追运行边；不要从输出倒推缺失的中间步骤。

1. `Makefile:_%`、`Makefile:ULIB`：普通用户 ELF 的输入与链接顺序。
2. `Makefile:$U/usys.S`、`user/usys.pl:entry`：生成 stub 的权威来源。
3. `user/user.ld:SECTIONS`、`kernel/elf.h:struct elfhdr`、`user/ulib.c:start`：
   磁盘格式、地址布局、入口包装和返回。
4. `user/echo.c:main`、`user/printf.c:vprintf`、`user/umalloc.c:malloc`、
   `user/user.h:write`：程序、用户运行时与 C 声明边界。
5. `kernel/syscall.h:SYS_write`：`a7` 中编号的来源。
6. `Makefile:UPROGS`、`Makefile:fs.img`、`mkfs/mkfs.c:main` 中的
   `shortname` 分支及 `mkfs/mkfs.c:iappend`：宿主 ELF 的镜像名称、输入边
   和写入动作。
7. `kernel/proc.c:forkret`、`kernel/exec.c:kexec`：当前分支的 `/init`
   装入路径和 ELF entry 提交点。
8. `user/init.c:main`、`user/sh.c:main`、`user/sh.c:runcmd`：`init -> sh ->
   echo` 的用户态控制流。
9. `kernel/sysfile.c:sys_exec`：普通 shell 命令最终再次到达 `kexec()` 的
   位置；把此前的 trap 往返明确留给下一单元。

## 观察任务

先把权威源码基线与当前教程提交分栏记录，再构建权威产物：

```sh
rg -n '"baseline_commit":' docs/xv6-tutorial/curriculum.json
git rev-parse HEAD
make user/_echo user/usys.S fs.img
make -nB user/_echo
make -nB fs.img | rg 'usys\.pl|user/_echo|mkfs/mkfs fs\.img'
rg -n 'shortname =|shortname\[0\].*_' mkfs/mkfs.c
```

第一条输出是 manifest 固定的可执行源码 baseline，第二条才是走查时教程
提交；两者用途不同，即使教学源码在其间没有变化也不能互换。`make -nB` 只
展示如果强制重建会执行什么，不把其输出当成已执行证据。记录普通链接命令
中的 `echo.o`、四个 `ULIB` 对象和 `user.ld`；再记录 `fs.img` 配方确实接收
`user/_echo`、`user/_init` 与 `user/_sh`，并由 `shortname` 分支去掉前导
下划线。

独立重放生成器并与构建产物逐字比较：

```sh
tmp_usys=$(mktemp)
perl user/usys.pl > "$tmp_usys"
cmp "$tmp_usys" user/usys.S
rm -f "$tmp_usys"
rg -n '^write:|^sys_sbrk:|li a7, SYS_write|ecall|ret' user/usys.S
rg -n '^#define SYS_write ' kernel/syscall.h
```

`cmp` 退出 `0` 才支持“生成结果有可追溯来源”。临时文件必须删除。

检查最终 ELF，而不是从链接脚本猜入口：

```sh
readelf -h -l user/_echo
rg ' (main|start|write)$' user/echo.sym
sed -n '/<main>:/,/<start>:/p; /<write>:/,+5p' user/echo.asm
```

`echo.sym` 与 `echo.asm` 是 Makefile 用已经解析出的 `OBJDUMP` 生成的，因而
不要求学习者猜工具链前缀。记录 ELF `Machine`、所有 `LOAD` 段、entry、
`main/start/write` 地址，并验证：

- entry 与 `start` 地址相等，而不要求等于 `main`；
- `main` 调用 `write` 前能看到参数寄存器准备和 `jal`；
- `write` stub 装入 `SYS_write` 后执行 `ecall`，但本单元不解释其后内核路径。

最后使用[隔离 QEMU runner](../resources/observe-system/run-qemu.sh)或实验资源，
在私有镜像中执行 `echo abi-check`。精确记录：

```text
$ echo abi-check
abi-check
```

这条输出至少依赖 `echo.c`、`ULIB/usys`、链接脚本、`UPROGS/mkfs`、
`forkret -> /init -> sh` 和 `sh -> exec("echo")` 六类输入；它本身不能区分
哪一条边出错。

## 有界修改任务

资源目录给出唯一实验源码、`UPROGS` patch 和隔离验收器：

- [`hello.c`](../resources/user-program-and-abi/hello.c)
- [`add-hello-to-uprogs.patch`](../resources/user-program-and-abi/add-hello-to-uprogs.patch)
- [`run-lab.sh`](../resources/user-program-and-abi/run-lab.sh)

自动路径使用 `git archive HEAD` 建立临时源码树；用户现有修改、宿主构建产物
和共享 `fs.img` 都不作为实验输入。运行：

```sh
docs/xv6-tutorial/resources/user-program-and-abi/run-lab.sh
```

脚本按以下顺序执行，手工走查也必须保持同样边界：

1. 只复制 `hello.c`，显式构建 `user/_hello`，但不改 `UPROGS`；重建镜像后
   shell 必须报告 `exec hello failed`。这证明“ELF 已生成”不推出“镜像已
   包含命令”。
2. 应用唯一一行 `UPROGS` patch，强制重建 `fs.img`；shell 执行 `hello`
   必须只新增精确输出 `hello-from-xv6`，随后 `echo abi-regression` 必须仍
   正常工作。
3. 在同一隔离树运行完整 `usertests`，以 `ALL TESTS PASSED` 作为完整回归
   oracle。
4. 逆向应用 patch，删除 `hello` 的源码和宿主构建产物，重建镜像；再次
   执行 `hello` 必须恢复为 `exec hello failed`，`echo` 仍通过。
5. 删除整个临时树，并确认原仓库 `git status --short` 与共享 `fs.img` 哈希
   与实验前相同。

变化范围只能是实验树中的 `user/hello.c` 和 `UPROGS` 一项；不得修改内核、
生成的 `user/usys.S` 或现有用户程序。

## Oracle、证据、失败路径和局限

| 维度 | 输入或触发器 | 必须观察到的结果 | 不支持的结论 |
|---|---|---|---|
| `S` | Make dry-run、生成器重放、ELF header/symbol/disassembly | 每条生成边有独立锚点；`cmp=0`；entry 等于 `start`；`write` stub 含编号与 `ecall` | 内核已正确处理 trap |
| `F` | patch 后在私有镜像执行 `hello`、`echo`、`usertests` | `hello-from-xv6`、`abi-regression`、`ALL TESTS PASSED` | 所有参数、ELF 和并发情况正确 |
| `B` | 有 `_hello` 但不在 `UPROGS`；逆向清理后重试 | 两次都得到 `exec hello failed`；没有 `hello-from-xv6` | `kexec()` 的每个 ELF 拒绝分支都正确 |

失败定位规则：

- `make user/_hello` 失败，先查 C 接口、对象和链接输入。
- `_hello` 存在但 shell 报 `exec hello failed`，先查 `UPROGS` 和镜像重建，
  不要先改 `kexec()`。
- entry 不等于 `start`，查实际链接命令和 ELF，不要硬改运行时跳转地址。
- `hello` 成功但回归失败，不能把该实验标为通过；保存隔离树日志后再缩小
  到镜像输入或共享库变化。

本单元没有提供 `C` 或 `R` 证据，也没有覆盖恶意 ELF、超长参数栈、用户
指针、系统调用 trap 往返、文件系统崩溃恢复或多 hart 并发。完整 `usertests`
降低明显回归风险，但不是这些性质的证明。

## 退出产物与后续单元

提交以下内容后才算完成：

- 带源码锚点的生成/执行图；
- Make dry-run、生成器 `cmp`、ELF entry/symbol/disassembly 记录；
- `hello` 的 B/F/B 三阶段输出、完整 `usertests` 结果和原仓库未改变的证据；
- 按[有界修改实验报告](../templates/bounded-change-report.md)整理的范围、oracle、
  清理和证据局限。

本轮没有迁移 `docs/questions/`：现有启动、trap、进程、虚拟内存和文件系统
问题都跨越后续 owner，尚不属于已验证的 `user-program-build-and-abi` 单元。

完成后，`shell-image-path` 已解除。下一单元是
[一次系统调用如何往返](syscall-roundtrip.md)，它从 `write` stub 的 `ecall`
继续并解除 `syscall-kernel-entry`。
