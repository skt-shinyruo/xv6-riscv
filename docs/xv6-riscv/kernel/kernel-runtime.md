# 内核基础运行时

xv6 以内核 freestanding 环境运行，不链接宿主 libc。本篇说明基础类型、公共接口、全局容量、内存/字符串原语、格式化输出和不可恢复错误处理。

## 1. 源码地图

- `kernel/types.h`：固定宽度整数别名和页目录项类型；
- `kernel/defs.h`：跨内核编译单元的函数声明、前向结构声明和 `NELEM`；
- `kernel/param.h`：编译期容量和子系统间预算；
- `kernel/string.c`：`memset/memmove/memcpy/memcmp` 和小型字符串函数；
- `kernel/printk.c`：内核格式化输出、输出串行化和 `panic()`。

设备层的 `consputc()`/`uartputc_sync()` 见[设备与控制台](devices.md)，锁原语见[同步与锁](synchronization.md)。

## 2. Freestanding 编译环境

Makefile 使用 `-ffreestanding -nostdlib`，并用多项 `-fno-builtin-*` 阻止编译器把本地实现替换为假定存在的 libc 调用。其结果是：

- 内核必须提供编译器可能生成或源码直接调用的基础内存函数；
- 不存在 `errno`、locale、动态链接或标准 I/O；
- 内核与用户库分别实现自己的字符串/格式化函数，不能假设两边符号共享；
- API 的参数宽度和溢出行为就是当前 C 实现所体现的行为。

## 3. 基础类型

`kernel/types.h` 定义 `uint8/16/32/64` 及传统的 `uint/ushort/uchar`。在当前 RISC-V 64 位目标上，`unsigned long` 用作 `uint64`。

`pde_t` 是 `uint64`，与 `riscv.h` 中 `pte_t` 同宽。页表本质上是 `uint64 *`，代码直接使用物理地址可解引用这一事实，依赖内核 RAM 的恒等映射。

磁盘格式的结构使用 `uint`、`ushort` 等固定预期宽度。`mkfs` 在宿主机上显式把 16/32 位字段编码为 little-endian，不能随意把这些类型替换为宿主宽度可变的 `long`。

## 4. `defs.h` 的作用

`kernel/defs.h` 不是一个运行期注册表，而是内核内部的人工接口清单：

- 用前向声明避免所有模块包含彼此完整结构；
- 按实现文件分组声明对外函数；
- 暴露少量全局状态，如 `ticks/tickslock`；
- 提供 `NELEM(x)` 计算编译期数组元素数。

新增跨文件函数时需要同步声明；仅在单个 `.c` 使用的函数应保持 `static`，避免把内部实现变成隐含全局 API。

分组注释保留实现文件名，例如 `bio.c` 和 `vm.c`；它们把声明映射回定义位置，却不会由编译器验证该函数仍在对应文件中。移动实现时必须同时更新分组，否则链接仍可能成功，但接口地图已经失真。

`defs.h` 依赖包含者先引入必要类型，例如 `pagetable_t` 来自 `riscv.h`。当前包含顺序是代码契约的一部分。

## 5. 编译期参数

`kernel/param.h` 同时承担容量限制和正确性预算。特别需要区分：

- 单对象容量：`NOFILE`、`MAXARG`、`MAXPATH`；
- 全局槽位：`NPROC`、`NFILE`、`NINODE`、`NBUF`；
- 硬件/启动容量：`NCPU`；
- 文件系统格式和事务预算：`FSSIZE`、`MAXOPBLOCKS`、`LOGBLOCKS`；
- 地址空间策略：`USERSTACK`。

`LOGBLOCKS = MAXOPBLOCKS * 3` 和 `NBUF = MAXOPBLOCKS * 3` 是有意耦合。日志允许多个 outstanding 操作保留空间，且所有被记录但未提交的 buffer 会被 pin。只改其中一个可能造成永久等待、`bget: no buffers` 或 transaction panic。

## 6. 内存和字符串原语

### 6.1 `memset()`

按字节填充，不做宽字优化。物理页分配器用不同字节模式标记已释放和已分配页面，以便更快暴露悬空引用；清零用户页则显式传入零。

### 6.2 `memmove()` 与 `memcpy()`

`memmove()` 检测源区间在目标起点前方且发生重叠时，从尾部向前复制；否则从头部向后复制。`n == 0` 时直接返回，避免对空区间做指针边界运算。

`memcpy()` 仅调用 `memmove()`，主要用于满足编译器可能产生的符号引用。它因此也支持重叠，虽然标准 `memcpy` 不要求这一点。

### 6.3 `memcmp()`

`memcmp(v1, v2, n)` 把两段内存解释为 `const uchar *`，从低地址开始寻找第一对不同字节，并返回 `s1[i] - s2[i]`；全部 `n` 字节相同则返回 0。因而差值按无符号 8 位字节计算，符号只保证小于、等于或大于零，调用者不能假定结果固定为 `-1/0/1`。`n == 0` 时不会解引用两个指针。

### 6.4 比较和复制字符串

- `strncmp()` 最多比较 `n` 字节，使用 `uchar` 计算差值；
- `strncpy()` 可能在源长度达到 `n` 时不写 NUL，并会填充剩余空间；
- `safestrcpy()` 在 `n > 0` 时保证最后有 NUL，适合进程名等固定缓冲区；
- `strlen()` 返回 `int`，调用者必须已经保证指针有效且字符串终止。

这些函数都信任内核指针，不能用于直接读取用户地址。用户输入必须先由 `copyin/copyinstr` 搬到内核缓冲区。

## 7. `printk()` 输出路径

调用链为：

```text
printk
  -> printint / printptr / string loop
  -> consputc
  -> uartputc_sync
  -> UART THR MMIO
```

`printk()` 支持 `%d/%ld/%lld`、无符号对应格式、十六进制、`%p/%c/%s/%%`。未知格式原样打印 `%` 和字符以暴露错误；它不实现宽度、精度或浮点。与用户 `printf` 相同，带 `l/ll` 的有符号分支仍用 `uint64` 从 `va_list` 取值，并在打印负数时对 `long long` 求负；这依赖当前 ABI 的位模式表现，最小有符号值还会遇到 C 溢出边界。

正常情况下 `pr.lock` 保证一整次 `printk` 不与其他 CPU 的 `printk` 交错。`consputc()` 使用同步 UART 轮询，所以可在中断上下文调用，但可能忙等。

## 8. Panic 协议

`panic(s)` 的顺序是：

1. 设置 `panicking = 1`，使当前 CPU 后续 `printk()` 不再获取 `pr.lock`；
2. 输出 `panic: s`；
3. 设置 `panicked = 1`；
4. 当前 CPU 永久循环。

其他 CPU 进入 `uartputc_sync()` 看到 `panicked` 后也永久循环，避免继续写 UART。跳过 `pr.lock` 是为了避免 panic 发生时锁已被当前 CPU 或失效上下文持有而二次死锁。

该协议不提供 CPU 停机确认、转储或恢复，也不严格保证“第一条 panic 消息完整保留”。`panicking` 在输出前就被全局置位，使 panic 路径绕过 `pr.lock`；若另一 CPU 正在普通 `printk()`，或多个 CPU 几乎同时 panic，字符可能交错。只有当某个 CPU 完成输出并设置 `panicked` 后，之后进入 `uartputc_sync()` 的其他 CPU 才会永久自旋。`panicking/panicked` 是 volatile 标志而不是完整的多核 panic 状态机。

## 9. 不变量与边界

- `printkinit()` 必须在多个 CPU 并发打印前完成；启动最初几行由 CPU 0 串行执行。
- `printk()` 的格式字符串和 `%s` 指针必须来自可信内核内存。
- `panic()` 不返回；标注 `noreturn` 让编译器和静态检查理解控制流。
- 字符串函数没有长度发现或分配能力，所有目标缓冲区大小由调用者保证。
- 这些函数不获取可能睡眠的锁；`printk` 只使用 spinlock 和同步 UART。

## 10. 验证

- `make` 会在 `-Werror` 和 builtin 禁用设置下验证所有必需符号可解析。
- 在多个 CPU 同时打印短行，检查单次 `printk` 是否保持连续；不要用该实验推断多次 `printk` 组成的逻辑消息也原子。
- 通过一个受控 `panic` 检查首条消息保留；该实验需要重启 QEMU。
- `usertests` 大量依赖 `memmove`、字符串边界和格式化输出，是间接回归覆盖。
