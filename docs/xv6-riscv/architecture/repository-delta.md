# 仓库差异基线与回归责任

本文固定一个可复查的源码基线，逐项说明当前分支相对该基线的变化。它回答的不是“当前 xv6 有哪些特性”，而是更窄也更可证的问题：从本地 tag `xv6-riscv-rev5` 到文档建设前的代码提交 `13a33b7`，哪些提交改变了行为、接口、构建或验证方法，这些变化应由什么测试守护。

相关的当前实现总览见 [架构总览](overview.md)，构建契约见 [构建、链接与文件系统镜像](../build/build-link-and-fs-image.md)，跨版本阅读时还应结合 [平台契约](platform-contracts.md)。

## 1. 固定比较对象

本次审计只使用本地 Git 对象，不通过远端分支猜测版本：

| 项目 | 固定值 | 本地证据 |
| --- | --- | --- |
| 基线 ref | `xv6-riscv-rev5` | `git cat-file -t` 显示它是直接指向 commit 的轻量 tag |
| 基线 commit | `7d7adbb1b0acbd67c9766a20d0f9900fef2789fa` | 提交主题为 `Add DOC: for reference in book` |
| 目标 commit | `13a33b76e81e9a5567c92153767f7b15a75551a5` | 文档建设前 `riscv`、`origin/riscv` 和 `origin/HEAD` 所指提交 |
| 祖先关系 | 基线是目标的祖先 | `git merge-base` 返回基线完整 OID |
| 提交数 | 35 | `git rev-list --count xv6-riscv-rev5..13a33b7` |

目标刻意固定为 `13a33b7`，而不是不断移动的 `HEAD`。因此，本文自身以及同批新增的其他文档不会被算作被审计差异。

本地配置有 `origin`、`mine` 和 `upstream` 三个 remote，但本地没有可用的 `upstream/*` 跟踪引用可以证明某个更新的远端状态。因此本文把 `7d7adbb` 称为“固定上游基线”，只表示它是仓库内明确标记、具有上游作者元数据的比较点；不声称它等同于某个未在本地证明的正式发行版，也不声称提交动机超出提交信息所写内容。

原始差异规模为 99 个文件、13,542 行增加和 2,090 行删除。这个数字主要由后续文档和格式化放大；使用 `--ignore-all-space` 后仍会计入新增文档，所以不能用行数直接衡量内核行为变化。

## 2. 证据与分类规则

每条结论按以下证据顺序建立：

1. 比较两个固定树的最终代码，确认变化确实保留在目标提交中。
2. 查看引入变化的提交及提交说明，确认变化由哪个提交带入。
3. 只把代码直接支持的结果写成“效果”；由提交主题或代码目的推知的内容明确写成“意图”或“推断”。
4. 空白、换行、注释、文档、编辑器配置和忽略规则不记作运行时行为，但仍在提交台账中列出。
5. 合并提交只作为内容载体登记；其已列出的父提交不重复算成另一项行为变化。

差异分四类：

- **运行时语义**：用户可观察结果、内核状态转移或错误处理发生变化。
- **内核/ABI 表面**：源码符号、可见性、入口约定或调试接口变化，正常功能可能不变。
- **构建与验证**：可接受工具链、编译语言、测试驱动或诊断行为变化。
- **非行为变化**：格式化、注释、文档、编辑器配置和生成物忽略规则。

## 3. 行为差异总表

| 变化 | 引入提交 | 最终效果 | 主要风险 | 最小回归 |
| --- | --- | --- | --- | --- |
| 用户入口接收 `argc/argv`，`main()` 返回值成为退出状态 | `d366b51`、`996f6ee` | 用户启动包装器从 `main(); exit(0)` 变为 `exit(main(argc, argv))` | 不兼容的 `main` 声明、退出状态意外改变 | 参数程序 + 返回非零状态的专用程序 |
| 用户态 64 位整数格式化不再截为 32 位 | `c6a6eaa` | `%ld/%lld/%lu/%llu/%lx/%llx` 的高 32 位可输出 | 边界值、符号转换和缓冲区长度 | 0、`2^32`、最大值和负值 |
| 管道读在 `copyout` 成功后才消费字节 | `4689d66` | 首字节失败返回 `-1`；部分成功返回已复制字节数；失败字节留在管道 | 环形计数推进、EOF 与错误混淆、惰性补页/OOM | `copyout`、`lazy_copy`、错误后重读 |
| eager/lazy `sbrk` 均不能越过 `TRAPFRAME` | `c71a6c4` | 合法末端是 `TRAPFRAME`，再增长失败，不覆盖保留映射 | 边界 off-by-one、整数溢出、eager/lazy 分支不一致 | `usertests lazy_sbrk` |
| GCC 旧原子内建迁移到 `__atomic`，锁缩为 acquire/release | `c6fa4fc`、`b51eab7` | 发布/获取关系保留，锁路径去掉冗余全栅栏；其他共享点仍用顺序一致栅栏 | 弱内存序、编译器代码生成、DMA 可见性 | 多 hart 压力测试 + 反汇编核对 |
| 内核陷阱不再无用地保存 `tp` | `b87eccb` | 少一次栈写；返回路径仍保留当前 hart 的 `tp` | 若以后 C/汇编错误地把 `tp` 当普通临时寄存器会破坏 `cpuid()` | 定时抢占和多 hart 调度压力 |
| `timerinit` 去掉重复的 `mie.STIE` 写 | `b0bcf86` | 中断使能仍由既有 `sie.STIE` 路径完成，减少一次 CSR 写 | 平台/委托假设改变时可能漏使能时钟中断 | 启动后 ticks 前进和抢占测试 |
| 内核格式化输出符号改名为 `printk` | `241bdd0` | 输出内容不变；源码符号、对象名和调试断点名改变 | 外部补丁、脚本和断点仍引用 `printf` | 构建、启动 banner、panic/未知系统调用路径 |
| `dirent.name` 标记为 `nonstring` | `4f1bdde`、`c14b639` | 磁盘布局不变，编译器获知 14 字节名字可能无 NUL | 主机编译器属性兼容、误把字段当 C 字符串 | clean build + `usertests fourteen` |
| 工具链选择和目标 ISA 被固定 | `e90b257`、`5474d4b`、`3c85134`、`7f5dfd3` | 接受 `riscv64-none-elf-`，固定 `rv64gc` 和 GNU99，不再声明未直接使用的 `AS` | 非 GCC 工具链、ISA 漂移、汇编规则遗漏参数 | 两种已支持前缀的 clean build + ELF 属性检查 |
| 测试复位和 `exectest` 诊断更可靠 | `b39b788`、`214bf4c` | `fs.img` 不存在不再中止复位；重定向 stdout 后错误仍可见 | 测试假阳性；额外诊断 fd 在成功 `exec` 后也会由 `echo` 继承到退出 | `make clean` 后跑驱动；故意破坏 `echo` 路径 |

下面逐项给出前态、后态、风险和验证责任。

## 4. 用户入口和退出状态

基线 [user/ulib.c](../../../user/ulib.c) 的 `start()` 不接收参数，以无原型声明调用 `main()`，忽略其返回值并总是 `exit(0)`。目标代码的 `start(int argc, char **argv)` 将内核构造的启动参数原样传给 `main`，再把返回值交给 `exit`。

两个提交主题分别明确写了“传播 main 返回值”和“修复 start 参数”，因而这里无需猜测动机。可观察效果有两项：

- 一个只 `return 7` 的用户程序，其父进程现在通过 `wait()` 看到 7，而不是 0。
- `main(int, char **)` 不再依赖调用约定碰巧保留 `a0/a1`；入口函数显式接收并转交这两个参数。

这是一项用户运行时 ABI 变化，不是普通重构。风险在于历史用户程序可能用了不兼容的 `main` 类型；C ABI 常使 `main(void)` 继续工作，但这不是所有错误声明都合法的理由。当前树中 `user/logstress.c` 以 `return 0` 结束，可覆盖正常返回，但现有测试没有一个专门断言非零返回值通过 `wait()` 传播。

验收应增加一个最小程序：检查 `argc/argv` 后 `return 37`，父进程必须观察到状态 37；再用缺少参数和多个参数各跑一次。`exectest` 能覆盖 `exec` 的参数传递和子进程状态为零，但不能替代非零返回值断言。

## 5. 用户态 64 位格式化

[user/printf.c](../../../user/printf.c) 中 `printint()` 的内部无符号量由 32 位 `uint` 改为 `unsigned long long`。基线已经从可变参数读取 64 位的 `ld/lld/lu/llu/lx/llx`，但随即存进 32 位变量，因此高 32 位被截断。目标代码保留完整值再逐位取模。

这不改变 `%d/%u/%x` 的 32 位约定，也不改变 `%p` 的固定 16 个十六进制数字格式。回归至少应比较：

```text
%lu  4294967296          -> 4294967296
%lx  0x100000000         -> 100000000
%ld  -4294967297         -> -4294967297
%llu 18446744073709551615 -> 18446744073709551615
```

残余风险是 `printint()` 对最小有符号 64 位数执行 `x = -xx`：一元负号先在有符号 `long long` 中求值，`LLONG_MIN` 无可表示正值，属于 C 溢出未定义行为，之后再赋给无符号量并不能补救。`vprintf()` 的 `%ld/%lld` 分支还用 `va_arg(ap, uint64)` 取得调用者通常以有符号 `long/long long` 传入的值，严格 C 可变参数类型也不匹配。本次提交只解决内部无符号工作变量的 32 位截断，没有提供完整标准库式 `printf` 合规保证；测试应单列 `INT64_MIN`，并只针对当前支持的格式集合，而不是假定宽度、精度或浮点格式存在。

## 6. 管道读取与失败原子性

基线 `piperead()` 先执行 `pi->nread++`，随后调用 `copyout()`。如果用户目标地址无效，或者惰性页在 `copyout()` 中无法分配，字节已经从环形缓冲区逻辑删除；首字节失败还会返回 0，而 0 通常表示管道 EOF。

目标 [kernel/pipe.c](../../../kernel/pipe.c) 改为：

1. 用当前 `nread` 只读取候选字节。
2. `copyout()` 成功后才递增 `nread`。
3. 第一字节失败返回 `-1`。
4. 复制若干字节后失败，返回已经复制的字节数，尚未复制的首个字节仍留在管道。

这建立了逐字节提交点：

```text
copyout(addr + i) 成功 happens-before nread++
```

因此它同时修复数据丢失和“错误伪装成 EOF”。提交说明明确把惰性 `sbrk` 的 `copyout()` 补页失败列为原因。锁仍覆盖复制循环，所以这项变化没有引入另一个 reader 在候选字节和计数推进之间竞争。

现有 `usertests copyout` 与 `usertests lazy_copy` 覆盖坏地址和惰性内存，`pipe1` 覆盖普通环绕读写。仍建议增加一个定向用例：先写入唯一标记字节，让第一次 read 指向非法地址并断言 `-1`，再读到合法地址并断言相同字节仍在；另加跨页缓冲区，让前一页成功、后一页失败，断言返回部分长度且剩余数据顺序不变。

## 7. `sbrk` 顶部边界

目标在 eager 路径 [kernel/proc.c](../../../kernel/proc.c) 的 `growproc()` 和 lazy 路径 [kernel/sysproc.c](../../../kernel/sysproc.c) 的 `sys_sbrk()` 中都拒绝 `addr + n > TRAPFRAME`。这保护用户页表顶部的 `TRAPFRAME` 和 `TRAMPOLINE` 固定映射。

边界语义是：

- 新逻辑大小等于 `TRAPFRAME` 合法，因为新增区间是半开区间，最后映射页位于 `TRAPFRAME - PGSIZE`。
- 再增长一个字节必须返回 `-1`。
- lazy 路径先检查无符号加法回绕，再检查 `TRAPFRAME`；eager 路径依赖进程大小始终不超过该边界这一既有不变量。
- 失败不能修改 `p->sz`，也不能覆盖已经存在的 trapframe/trampoline PTE。

`8402fc9` 同时加入 `lazy_sbrk` 回归，覆盖大步逼近边界、最后一页零填充，以及 eager/lazy 各自越界一字节失败。该测试就是本变化的首要验收。还应在失败前后读取 `sbrk(0)`，显式证明逻辑大小未变，并在失败后执行一次系统调用，证明 trap 返回路径仍完整。

## 8. 原子操作和锁内存序

`c6fa4fc` 把旧的 `__sync_*` 内建替换为 `__atomic_*`：

- 启动 hart 发布、`forkret` 的一次性初始化以及 VirtIO ring 交接仍使用 `__ATOMIC_SEQ_CST` 栅栏。
- 自旋锁使用 `__atomic_exchange_n(..., __ATOMIC_ACQUIRE)` 获取，用 `__atomic_store_n(..., __ATOMIC_RELEASE)` 释放。

随后 `b51eab7` 删除锁获取后的额外顺序一致栅栏和释放前的额外顺序一致栅栏，因为 acquire exchange 与 release store 已经表达锁所需的单向排序。这不承诺整个机器上的所有原子操作形成单一全序；它承诺前一持有者临界区中的写，通过 release/acquire 同步后对后一持有者可见。

这次 API 替换也没有把 `started` 或 `forkret.first` 变成原子对象：两处仍是普通对象配合 `__atomic_thread_fence()`，其中 `started` 的跨 hart 普通读写在 ISO C 抽象机中仍构成 data race。相对基线，目标保持了当前 GCC/RISC-V 下原有 fence 协议，不能把提交名解读成已经获得可移植的 C11 发布/获取；若要建立该保证，应把发布 store 和观察 load 本身改为 release/acquire 原子访问。

提交说明称在当时的 GCC 15.2.0 上生成机器码不变，这是提交者记录的观察，不应提升为所有工具链上的保证。主要回归责任是：

- `make` 后检查 `kernel/kernel.asm`，获取路径应有 acquire 语义，释放路径应在清零前提供 release 语义。
- 多 hart 反复运行 `usertests -q`、`grind`、`stressfs` 和并发管道测试。
- 对 `started` 验证从 hart 只能在启动 hart 完成全局初始化后继续。
- 对 VirtIO 验证 descriptor/ring 内容在通知设备前可见，used ring 内容在驱动消费前可见。

最后两项涉及设备和 MMIO，不应只用单核 QEMU 成功启动作为充分证明。

## 9. 陷阱寄存器与时钟初始化清理

### 9.1 `kernelvec` 不保存 `tp`

基线保存 `tp`，但返回路径本来就不恢复它；目标 [kernel/kernelvec.S](../../../kernel/kernelvec.S) 把这次保存也注释掉。当前约定用 `tp` 保存当前 hart id，而 `kerneltrap()` 可能在 timer 中断中 `yield()`，进程恢复时可能位于不同 hart。恢复旧 `tp` 反而会让 `cpuid()` 错认 hart，因此返回路径保留恢复时的当前值是必要约定。

本提交实际删除的是死存储，正常语义意图不变。风险来自未来维护：任何被 `kernelvec` 调用的代码都不得把 `tp` 当作普通 caller-saved 临时寄存器。定向验证应在多个 hart 上制造高频 timer yield，并在锁、`mycpu()` 与 `myproc()` 路径中断言 hart id 合法。

### 9.2 `timerinit` 不重复写 `mie.STIE`

目标 [kernel/start.c](../../../kernel/start.c) 已在委托后通过 `w_sie(... | SIE_STIE)` 使能 supervisor timer interrupt，故 `timerinit()` 中再次写 `mie` 的代码被删除。提交主题只声称“无需设置两次”；本文据此把它归为预期无行为变化的低层清理。

回归必须观察 ticks 持续增长，并证明 CPU 密集用户进程会被抢占。若以后改变 `mideleg`、Sstc 使用方式或启动特权级，这一结论必须重新审计，不能机械沿用。

## 10. 源码和调试接口变化

### 10.1 `printf` 改名为 `printk`

`241bdd0` 把 [kernel/printk.c](../../../kernel/printk.c)、对象文件、声明、初始化函数和所有内核调用点从 `printf` 系列改为 `printk` 系列。用户态 `printf` 不变，内核输出格式和设备路径也没有实质改写。

这项变化的价值可从最终命名直接看出：内核输出与用户库同名 API 不再混淆。但本地提交没有记录更具体的动机，故不作进一步归因。风险主要在树外代码和工具：补丁中的 `printf()`、GDB 的 `break printf`、符号抓取脚本以及只替换部分调用点都会失败。回归除启动和 panic 输出外，还应运行：

```bash
rg -n '\bprintf(init)?\b' kernel Makefile
rg -n '\bprintk(init)?\b' kernel Makefile
```

第一条只应命中明确讨论用户 API 的注释或有意保留内容，不应出现未迁移的内核调用或 `$K/printf.o`。

### 10.2 `uartgetc` 收窄为文件内符号

`c358af1` 从 [kernel/defs.h](../../../kernel/defs.h) 删除 `uartgetc`，并在 `uart.c` 中声明为 `static`。运行行为不变，但树外调用者不能再链接该符号。该提交主题写的是 `drop uartputc from defs.h`，与实际 diff 的 `uartgetc` 不一致；归因应以 diff 和最终符号为准，不能据标题把 `uartputc_sync` 误记成被移除。测试是 clean build 和 `nm kernel/kernel` 的符号可见性检查；未来若其他驱动需要直接轮询 UART，必须先重新定义所有权接口，而不是私自复制声明。

### 10.3 仅注释中的命名同步

`fa60353` 的题目是 `usertrapret() -> prepare_return()`，但在本比较区间的实际树差异只修改 `proc.h` 和 `trampoline.S` 注释，运行符号在基线中已经是 `prepare_return`。因此它不能在本基线下再记成一次函数改名。类似地，`9dbd536`、`ffdff7c`、`df440e2` 和 `f8973b5` 只校正入口、内存增长、UART/console 和字符串长度注释。

## 11. 文件系统结构的编译器契约

`4f1bdde` 给 [kernel/fs.h](../../../kernel/fs.h) 的 `dirent.name[DIRSIZ]` 添加 `__attribute__((nonstring))`，并注明 14 字节名称可能没有结尾 NUL。结构体字段大小、对齐和磁盘格式没有改变；变化是阻止编译器把它误当普通 C 字符串并产生错误诊断或优化假设。

为兼容不认识该属性的编译器，内核 CFLAGS 加入 `-Wno-unknown-attributes`；`c14b639` 把同一选项补到宿主机编译的 `mkfs`。这形成一个双工具链契约：交叉编译器和宿主编译器都包含同一头文件，但可能对属性支持不同。

必须验证：

- `sizeof(struct dirent)` 在 kernel 和 mkfs 侧仍为 16。
- 恰好 14 字节的名称可以创建、查找和删除，且代码不越界寻找 NUL；`usertests fourteen` 已提供主体覆盖。
- 不支持属性的工具链只忽略语义提示，不能因 `-Werror` 阻断构建。
- 磁盘镜像不因换编译器发生 dirent 布局漂移。

## 12. 构建可重复性变化

以下变化不改 xv6 的设计语义，但会改变“什么环境能构建出什么指令”的边界：

| 提交 | 变化 | 影响 |
| --- | --- | --- |
| `e90b257` | C 与独立 `.S` 规则都显式传 `-march=rv64gc` | 避免新版 GCC 默认 ISA 漂移到 xv6/QEMU 契约之外 |
| `5474d4b` | 自动探测增加 `riscv64-none-elf-` | 扩大可直接使用的裸机工具链前缀 |
| `3c85134` | 删除未直接使用的 `AS` 变量 | 明确汇编由 `CC` 驱动，避免误以为可单独切换 assembler |
| `7f5dfd3` | 显式 `-std=gnu99` | 固定 GNU C99 语言和扩展集合，降低编译器默认标准变化风险 |
| `2c2766c`、`74f8418` | 新增 `.clang-format` 和 `make fmt`，后者覆盖 `kernel/*.[ch]`、`user/*.[ch]`、`mkfs/*.c` | 建立机械格式化入口；不应改变对象语义 |

审计者应分别检查 C 和 `.S` 编译命令，因为独立汇编规则没有复用完整 `CFLAGS`。推荐保存 `make V=1` 或普通 make 输出、`readelf -A kernel/kernel`、链接 map/符号表和 `kernel/kernel.asm`，在升级工具链时比较，而不是只看源码未变。

格式化提交 `2c2766c` 与 `74f8418` 修改了大量文件，但逐提交差异显示其目的是格式和格式化范围。审计行为时应先看未忽略空白的提交保证没有 token 级意外，再用 `git diff --ignore-all-space` 降噪；不能仅凭提交标题跳过审查。

## 13. 测试工具和诊断变化

`b39b788` 把 [test-xv6.py](../../../test-xv6.py) 的 `rm fs.img` 改为 `rm -f fs.img`。因此 `make clean` 后第一次由测试驱动重建文件系统不会因镜像本来就不存在而提前报错。验收序列是：

```bash
make clean
./test-xv6.py -q usertests
```

该脚本捕获重建异常后只打印错误而没有立即重新抛出，这是既有行为；`rm -f` 只修复“文件不存在”这一已知前置状态，不证明所有重建失败都能使测试快速失败。

`214bf4c` 改进 `exectest`：子进程在关闭 fd 1 前 `dup(1)` 保存诊断 fd，失败消息写到保存的 fd；父进程还把非零状态打印出来并统一以测试失败退出。成功路径的预期输出仍不变，但 fd 环境并非完全不变：xv6 没有 close-on-exec，故保存的 `errfd` 也会被成功执行的 `echo` 继承，直到该短命进程退出。验证诊断路径不能只运行成功测试；可临时在隔离工作树中让 `echo` 不可打开或让期望 fd 断言失败，确认错误出现在控制台。README 同提交的另一个变化只是贡献者名单更新。

`8402fc9` 添加的 `lazy_sbrk` 属于测试覆盖增量，已在第 7 节说明。它本身不改变内核运行时，但与 `c71a6c4` 共同定义了可执行的边界规格。

## 14. 基线中已经存在、不可重复归因的特性

[文档索引](../README.md) 提醒读者本仓库与一些外部 xv6 资料存在六项明显差异。逐项读取 `7d7adbb` 的树可以证明，它们在本次固定基线中已经存在：

| 当前特性 | 基线证据 | 本比较区间结论 |
| --- | --- | --- |
| 内核使用 `kfork/kexec/kexit/kwait/kkill` | 基线 `kernel/defs.h` 已声明这些符号 | 非 `7d7adbb..13a33b7` 新增 |
| `userinit` 建空进程，首次 `forkret` 执行 `/init` | 基线 `kernel/proc.c` 已有该流程 | 非本区间新增 |
| `sbrk` 有 eager/lazy 第二参数 | 基线 `sys_sbrk` 和 `user.h` 已存在 | 本区间只新增顶部边界检查 |
| `copyin/copyout` 可经 `vmfault` 补页，`copyinstr` 不补 | 基线 `kernel/vm.c` 已是该实现 | 本区间只修复管道消费次序 |
| 日志恢复后 `ireclaim` 扫描 orphan inode | 基线 `kernel/fs.c` 已调用并实现 | 非本区间新增 |
| UART 用单字节 `tx_busy` 与 THRE 中断推进；只有下一字节遇 busy 才睡眠 | 基线 `kernel/uart.c` 已有 `tx_busy/sleep/wakeup` 协议 | 本区间只有注释、符号可见性和 `printk` 名称变化 |

这张表很重要：它阻止后来者把“当前仓库与另一份教程不同”误写成“当前分支相对 rev5 新增”。如果未来要追溯这六项的真正来源，必须把比较基线继续向 `7d7adbb` 之前移动，并重新做历史审计。

## 15. 文档、编辑器配置和其他非行为差异

这些变化与代码差异分开维护：

- `421b2b5` 添加 `.vscode/extensions.json`、`.vscode/launch.json` 和最初的 VSCode/GDB 说明。
- `f790dec` 添加从 QEMU 启动到内核的说明。
- `b6308a0` 添加从启动到 shell 的说明和学习路线。
- `13a33b7` 用当前分层文档集替换早期单篇说明，新增架构、构建、流程、内核和用户态文档。它没有修改内核或用户程序源码。
- `a8620f0` 收窄 `.gitignore` 中生成物路径；这改变 Git 工作树可见性，不改变生成物内容或运行时。
- `1eec3aa` 为 clang-format 增加局部保护并补充注释，`2c2766c` 和 `74f8418` 执行格式化。
- `README` 在 `214bf4c` 中只调整贡献者名单。

`.vscode` 配置会影响个人调试工作流，文档会影响理解和导航，但它们不应被列入内核功能回归。反过来，调试配置里硬编码的端口、GDB 路径或扩展仍可能过时；这属于工具文档维护问题，不是 xv6 运行语义。

## 16. 35 个提交的完整台账

以下台账确保范围内没有因“看起来像格式化”而被静默遗漏：

| 提交 | 分类 | 审计结论 |
| --- | --- | --- |
| `d366b51` | 运行时 ABI | `main` 返回值传给 `exit` |
| `4f1bdde` | 编译器/磁盘结构契约 | `dirent.name` 标记 `nonstring`，布局不变 |
| `c6a6eaa` | 用户可见行为 | 修复 64 位整数输出截断 |
| `996f6ee` | 运行时 ABI | `start` 显式转交 `argc/argv` |
| `9dbd536` | 注释 | ELF 入口注释改指向 `ulib.c:start` |
| `c14b639` | 构建 | mkfs 同步未知属性告警选项 |
| `ffdff7c` | 注释 | `growproc` 注释恢复为可增可减 |
| `4689d66` | 内核运行时 | 管道读 copyout 失败语义 |
| `8402fc9` | 测试 | 新增 `lazy_sbrk` 边界回归 |
| `c71a6c4` | 内核运行时 | eager/lazy 增长限制到 `TRAPFRAME` |
| `df440e2` | 注释 | console/UART 注释澄清 |
| `e90b257` | 构建 | 固定 `rv64gc` |
| `fa60353` | 注释 | 把残留 `usertrapret` 文本改为 `prepare_return` |
| `b3f9fa6` | 合并 | 合入 `e90b257`，无额外未列行为 |
| `5474d4b` | 构建 | 探测 `riscv64-none-elf-` |
| `3c85134` | 构建 | 删除未使用 `AS` 定义 |
| `ca6a468` | 合并 | 合入 `5474d4b`，无额外未列行为 |
| `7f5dfd3` | 构建 | 固定 GNU99 |
| `c6fa4fc` | 并发/设备契约 | 迁移到 `__atomic` 内建 |
| `c358af1` | 内部 API | `uartgetc` 变为文件内静态符号 |
| `f8973b5` | 注释 | 修正 `argstr` 返回长度说明 |
| `a8620f0` | 仓库元数据 | 更新生成物忽略范围 |
| `b39b788` | 测试驱动 | 缺失 `fs.img` 时复位可继续 |
| `b51eab7` | 并发契约 | 锁使用直接 acquire/release 语义 |
| `b0bcf86` | 启动清理 | 删除重复 STIE 设置 |
| `b87eccb` | 汇编清理 | 删除 `tp` 死存储 |
| `214bf4c` | 测试诊断 | `exectest` 保留错误输出通道并报告状态 |
| `1eec3aa` | 格式化准备 | clang-format 局部保护和注释 |
| `2c2766c` | 格式化/工具 | 加入格式配置与 `make fmt`，格式化 C 文件 |
| `74f8418` | 格式化/工具 | 将头文件纳入格式化 |
| `241bdd0` | 内部 API | 内核 `printf` 系列改名 `printk` |
| `421b2b5` | 编辑器/文档 | 添加 VSCode 调试配置和说明 |
| `f790dec` | 文档 | 添加 QEMU 启动说明 |
| `b6308a0` | 文档 | 添加启动到 shell 和学习路线 |
| `13a33b7` | 文档重组 | 建立当前分层文档集，移除被替代的旧篇章 |

## 17. 回归矩阵与尚未覆盖项

| 责任 | 现有自动覆盖 | 推荐补充 | 通过条件 |
| --- | --- | --- | --- |
| `sbrk` 顶部边界 | `usertests lazy_sbrk` | 失败前后 `sbrk(0)` 不变 | eager/lazy 到边界成功，越界失败，内核不 panic |
| 管道 copyout 提交点 | `copyout`、`lazy_copy`、`pipe1` | 错误后重读同一标记字节；跨页部分成功 | 不丢未复制字节，不把错误报成 EOF |
| 用户入口 ABI | `exectest` 间接覆盖参数和零状态 | 专用 `return 37` 程序 | `argc/argv` 正确，父进程得到 37 |
| 64 位打印 | 没有明确的边界断言 | 格式化输出 golden test，包含 `INT64_MIN` | 高 32 位和符号正确；不得依赖有符号溢出 |
| spinlock 内存序 | 多数并发测试间接覆盖 | 多 hart 长时 `grind/stressfs` + 汇编检查 | 无死锁、竞态症状；指令有预期 aq/release 排序 |
| VirtIO 栅栏 | 文件系统测试间接覆盖 | 并发 I/O、不同优化级别/编译器 | 无状态 panic、丢完成或数据错乱 |
| `tp`/timer 清理 | 普通多核启动间接覆盖 | 高频抢占、迁移时核对 hart id | ticks 前进，跨 hart 恢复后 `cpuid()` 正确 |
| `printk` 改名 | clean build 和启动输出 | 符号扫描、panic 路径 | 无残留内核调用；输出未丢失 |
| 14 字节目录名 | `usertests fourteen` | kernel/mkfs 双侧 `sizeof` 断言 | 构建无告警，镜像兼容，名字操作成功 |
| 测试驱动复位 | 可人工复现 | 回归测试从无 `fs.img` 的干净工作树启动 | 驱动完成重建并进入测试 |

不能把 `./test-xv6.py -q usertests` 一次成功当作全部差异的证明。尤其是 64 位格式化、非零 `main` 返回值、失败后管道字节保留、弱内存序和错误诊断路径，目前需要定向测试或静态检查。

## 18. 可重复审计过程

以下命令只依赖本地对象。建议在干净工作树或临时 worktree 中执行，避免把正在撰写的文档算入目标：

```bash
BASE_REF=xv6-riscv-rev5
BASE_OID=7d7adbb1b0acbd67c9766a20d0f9900fef2789fa
TARGET_OID=13a33b76e81e9a5567c92153767f7b15a75551a5

test "$(git rev-parse "$BASE_REF")" = "$BASE_OID"
test "$(git rev-parse "$TARGET_OID")" = "$TARGET_OID"
git merge-base --is-ancestor "$BASE_OID" "$TARGET_OID"
test "$(git rev-list --count "$BASE_OID..$TARGET_OID")" = 35

git log --reverse --format='%h%x09%ad%x09%s' \
  --date=short "$BASE_OID..$TARGET_OID"
git diff --name-status "$BASE_OID..$TARGET_OID"
git diff --stat "$BASE_OID..$TARGET_OID"
git diff --ignore-all-space "$BASE_OID..$TARGET_OID" -- \
  Makefile kernel user mkfs test-xv6.py README
```

逐提交复查不可省略，特别是 merge 和格式化提交：

```bash
git rev-list --reverse --topo-order "$BASE_OID..$TARGET_OID" |
while read -r commit; do
  git show --stat --oneline "$commit"
done

git show --ignore-all-space b3f9fa6
git show --ignore-all-space ca6a468
git show --ignore-all-space 2c2766c
git show --ignore-all-space 74f8418
```

行为验证建议在固定目标的临时工作树中运行：

```bash
AUDIT_DIR=/tmp/xv6-rev5-delta-audit
git worktree add --detach "$AUDIT_DIR" "$TARGET_OID"
cd "$AUDIT_DIR"
make clean
make
./test-xv6.py -q usertests
./test-xv6.py crash
```

不要在存在未保存 `fs.img` 实验状态的主工作树中直接执行 `make clean`。临时工作树完成后应由操作者确认路径和结果，再按项目的工作树管理方式移除。

## 19. 更新规则

以后目标提交前进时，维护者必须同时完成以下动作：

1. 把新的目标完整 OID 写入本文，保留旧 OID 以便审计跨度可解释。
2. 重新计算祖先关系和提交数；若不是线性后继，明确 merge-base 和两侧独有提交。
3. 把每个新提交加入完整台账，不能只更新摘要。
4. 对运行时、ABI、锁/内存序、磁盘格式、设备和构建变化补充“效果、风险、回归”。
5. 更新第 17 节，将推荐测试落实为自动测试后再改成“现有覆盖”。
6. 重新核对第 14 节；只有找到更早的引入提交，才能改变那六项特性的归属。
7. 保存 `git diff --check`、clean build 和相关测试结果；测试未运行必须写成未验证，不能写成通过。

这套规则把“当前分支与外部资料不同”拆成可追溯的版本事实：固定比较对象、逐提交责任、明确风险和可执行回归。只有这四部分同时更新，repository delta 才仍然可信。
