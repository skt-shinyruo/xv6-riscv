# 用户 ABI、运行库与内存分配器

本文说明用户 ELF 如何进入 C `main()`、系统调用 stub 如何把参数交给内核、用户态辅助函数提供哪些最小语义，以及 `malloc/free` 怎样管理由 `sbrk()` 取得的堆。

## 1. 源码地图

| 源码 | 责任 |
|---|---|
| `user/user.ld` | 用户 ELF 的虚拟地址和 section 布局 |
| `user/user.h` | 用户可见系统调用、运行库和 allocator 声明 |
| `user/usys.pl` | 生成系统调用汇编 stub |
| `user/usys.S` | 构建时生成物；设置 `a7`、执行 `ecall`、返回 |
| `user/ulib.c` | ELF 入口 `start()`、字符串/内存函数和 `sbrk` 包装 |
| `user/printf.c` | 基于 `write()` 的用户格式化输出 |
| `user/umalloc.c` | K&R first-fit allocator、空闲块合并和扩堆 |
| `kernel/syscall.h` | 用户 stub 与内核分派共享的系统调用编号 |

构建规则见[构建、链接与镜像](../build/build-link-and-fs-image.md)，trap 和内核分派见[一次系统调用往返](../flows/syscall-round-trip.md)及[系统调用](../kernel/system-calls.md)。

## 2. 用户 ELF 布局

`user/user.ld` 从虚拟地址 0 开始放置 `.text`，随后放只读 section、`.eh_frame`，把 `.data` 向 4 KiB 页边界对齐，再放 `.bss`，最后提供 `end` 符号。

```text
0                    .text / .rodata / .eh_frame
page-aligned         .data / .bss
end                  静态映像结束
...                  exec 分配的 guard + user stack
```

链接器在没有显式 `ENTRY` 时会优先选择名为 `start` 的符号作为入口；因此对常规规则生成的用户 ELF，`user/ulib.c:start()` 是 ELF header 中的 entry，内核 `kexec()` 把 trapframe `epc` 设置到这个地址。`user/_forktest` 是唯一例外：Makefile 用 `-e main -Ttext 0` 单独链接它，因而直接从 `main()` 进入且不经过 `start()`；它的 `main()` 必须自己调用 `exit()`。

用户链接命令把程序自己的 object 和四个公共 `ULIB` object（`ulib.o`、`usys.o`、`printf.o`、`umalloc.o`）直接交给 linker。它们不是 archive member，链接参数也没有 `--gc-sections`，所以普通程序会包含这四个 object 中由链接脚本接纳的全部 section，而不是只保留实际调用到的函数。没有动态加载器、共享库或 libc；代价是即使程序从不调用 `malloc()`，普通规则生成的 ELF 仍带有 allocator 代码。`_forktest` 的专用规则只链接 `ulib.o` 和 `usys.o`，是唯一例外。

地址 0 不是 guard page：`.text` 就从 VA 0 开始，装载后至少可读、通常也可执行。这意味着 C 语言层面的 null pointer 仍是未定义行为，但一次空指针读取在当前映像中可能读到 text 字节，空函数指针调用也可能从 ELF 入口附近开始执行，而不是必然立即 page fault。只有未映射地址或 PTE 权限不允许的访问才保证触发 trap。

## 3. 从 `start()` 到 `main()`

`kexec()` 按 RISC-V 调用约定准备：

- `a0 = argc`：通过 `kexec()` 的返回值写入 trapframe；
- `a1 = argv`：指向用户栈上的 pointer array；
- `sp`：16-byte 对齐并位于一个固定大小用户栈页中；
- `epc = ELF entry`；常规程序为 `start()`，`_forktest` 特例为 `main()`。

对常规用户 ELF，`start(argc, argv)` 在 `user/ulib.c` 中按下面这个外部声明调用程序自己的入口：

```c
int main(int argc, char **argv);
```

若 `main()` 返回，`start()` 把返回值交给 `exit()`。因此用户程序既可以显式 `exit()`，也可以 `return`；不会从 ELF 入口返回到未知地址。

源码并没有让所有定义都严格匹配这个声明：`init.c`、`sh.c` 和 `zombie.c` 等定义 `int main(void)`。当前 RISC-V ABI 下调用者放入 `a0/a1`、无参数 callee 忽略它们，链接器也不做跨翻译单元类型检查，所以这些程序能够运行；从 C 类型兼容性看，这仍不是可移植的统一 `main(int, char **)` 契约。文档和新增代码都不能把“ABI 恰好兼容”写成“源码签名完全一致”。

`exit` 被声明为 `noreturn`。用户代码没有构造/析构数组、TLS 初始化或标准 C 启动对象。特殊的 `_forktest` 直接进入 `main()`，但它的正常和错误终局都显式 `exit()`，所以不需要返回包装。

## 4. 系统调用 ABI

RISC-V 函数调用已经把前六个参数放在 `a0..a5`。生成的 stub 只需：

```asm
li a7, SYS_name
ecall
ret
```

约定如下：

| 寄存器 | 进入 stub | `ecall` 进入内核 | 返回用户态 |
|---|---|---|---|
| `a0..a5` | C 参数 | trapframe 中的参数 | `a0` 被返回值覆盖，其余恢复 |
| `a7` | 普通 caller-saved 值 | 系统调用号 | trapframe 恢复 `ecall` 时的系统调用号；调用者不应依赖 |
| `ra` | stub 返回地址 | 保存到 trapframe | `ret` 回 C 调用点 |

`kernel/syscall.h` 是编号的单一共享定义。新增系统调用必须同时更新编号、`user/usys.pl`、`user/user.h`、内核 handler 声明和分派表。

## 5. `usys.pl` 和生成物

Makefile 在需要 `user/usys.o` 时执行：

```text
perl user/usys.pl > user/usys.S
```

`user/usys.S` 带有“generated, do not edit”标记，不应手工修改。修改系统调用 stub 应编辑 `user/usys.pl` 并重新构建。

大多数 stub 名称与用户 API 相同。`sbrk` 是特殊情况：生成器输出全局符号 `sys_sbrk`，因为 `user/ulib.c` 还要提供两个策略包装：

```text
sbrk(n)     -> sys_sbrk(n, SBRK_EAGER)
sbrklazy(n) -> sys_sbrk(n, SBRK_LAZY)
```

若生成器直接输出名为 `sbrk` 的 stub，就会与 C 包装函数冲突，也无法注入第二个策略参数。

## 6. 用户 API 的错误约定

xv6 不提供 `errno`。大部分整数系统调用失败返回 `-1`，成功返回非负值或零。`sbrk` 返回旧 program break，失败用：

```c
#define SBRK_ERROR ((char *)-1)
```

调用者必须按具体 API 和请求判断返回值：正长度普通文件/pipe 读取的 0 通常表示 EOF，但零长度 `read(fd, buf, 0)` 也返回 0；console 路径若首个字符已经从输入缓冲取出、随后第一次 `copyout()` 失败，当前实现也返回 0 并消费该字符。`fork()` 的 0 表示子进程，`wait()` 返回 pid，`exec()` 成功永不返回。仅凭整数 0 脱离调用参数和 fd 类型解释结果是不准确的。

用户头文件只做类型声明，不提供权限、信号、线程或标准库抽象。

## 7. 基础字符串与内存函数

`user/ulib.c` 实现最小集合：

| 函数 | 当前语义 |
|---|---|
| `strcpy/strcmp/strlen/strchr` | NUL-terminated 字符串操作，无容量检查 |
| `memset/memcmp/memcpy` | 字节级内存操作；当前 `memcpy()` 直接调用 `memmove()`，所以实现上也支持重叠 |
| `memmove` | 根据相对地址选择前向/后向复制，支持重叠 |
| `gets` | 从 fd 0 逐字节读取，到换行、回车、EOF 或容量减一 |
| `atoi` | 只解析开头连续十进制数字，不处理符号和溢出 |
| `stat` | `open -> fstat -> close` 的便利包装 |

这些函数直接解引用用户地址，不会像内核 `copyin/copyout` 那样返回受控的坏地址错误。访问未映射或权限不允许的页会产生用户 trap，并使进程被杀死；但由于 VA 0 映射 text，不能把所有 C 层面的“非法指针”都等同于硬件不可访问地址。这些同名函数也不完全实现标准 libc 的所有边界：`strchr(s, '\0')` 返回 0 而不是指向结尾 NUL，`memcmp()` 以可能为 signed 的 `char` 做差，`gets()` 要求 `max > 0`，`memmove()` 要求长度非负。

`gets()` 总在容量允许时写结尾 NUL，但不提供行被截断的单独标志；它把 `read()` 错误和 EOF 都视为同一种终止条件，调用者无法从返回值区分二者。

## 8. Eager 与 lazy `sbrk`

`sbrk(n)` 使用 eager 策略：正增长时 `uvmalloc()` 从 `PGROUNDUP(oldsz)` 起立刻分配、清零并映射所跨入的新页；`sbrklazy(n)` 只增长 `p->sz`，首次 load/store 或某些内核用户拷贝才分配页。两种接口混用时有一个边界例外：若旧 break 已由 lazy 增长推进到某个尚未映射页的中间，随后 eager 正增长仍未跨过下一页边界，`uvmalloc()` 的循环为空，这一页不会仅因本次 `sbrk()` 被物化，首次访问仍要走 fault。即使增长跨页，旧 break 所在的那段 lazy hole 也不在从 `PGROUNDUP(oldsz)` 开始的分配范围内。

两者都返回变化前的 break；负增长都由内核立即解除现有映射，lazy 策略不会延迟释放。用户库不在本地缓存 break，每次都进入内核。

lazy 空间的语义限制包括：

- `copyin/copyout` 可补页，`copyinstr` 不补页；
- instruction page fault 不补页；
- 未触碰的洞在 `fork` 中保持未映射；
- 逻辑范围仍受 `TRAPFRAME` 下界限制。

完整路径见[Lazy page fault](../flows/lazy-page-fault.md)。

## 9. 用户格式化输出

`user/printf.c` 的底层 `putc(fd, c)` 对每个字符调用一次 `write()`。`vprintf()` 支持：

- signed `%d/%ld/%lld`；
- unsigned `%u/%lu/%llu`；
- hexadecimal `%x/%lx/%llx`；
- `%p/%c/%s/%%`。

不支持宽度、精度、浮点和缓冲输出。十六进制数字使用大写 `A..F`；`%p` 固定输出 `0x` 加 16 个 hex digits。

状态机还有几个容易误判的边界：null `%s` 输出 `(null)`；未知格式输出原始 `%` 和当前字符；格式串末尾孤立的 `%` 只把状态切到等待格式字符，循环随即结束，因此该 `%` 被静默丢弃。`%p` 从变参中按 `uint64` 取值，而不是按 `void *` 取值；这依赖当前 RV64 表示。`fprintf()` 和 `printf()` 调用 `va_start` 后没有调用 `va_end`。`user/user.h` 上的 GCC `format(printf, ...)` attribute 按宿主编译器理解的完整 printf 语法检查调用，它不能证明 xv6 的精简状态机支持该格式。

`fprintf(fd, ...)` 选择任意 fd，`printf(...)` 固定 fd 1。`vprintf` 没有返回写入字符数，也不检查单字符 `write` 的错误。大量输出会产生大量系统调用，这是教学实现的明确取舍。带 `l/ll` 的有符号格式在源码中仍以 `uint64` 从 `va_list` 取值，并依赖当前 RISC-V/GCC ABI 的位模式表现；对最小 `long long` 求负还会遇到 C 有符号溢出边界。它不是可移植的完整 `printf` 实现。

## 10. Allocator 的 block 格式

`user/umalloc.c` 使用 K&R allocator。每个 block 前有一个按 `long` 对齐的 `Header`：

```text
+----------------------+------------------------+
| Header(ptr, size)    | payload returned to C  |
+----------------------+------------------------+
```

`size` 以 `Header` 单位计数，并包含 header 自身。请求 `nbytes` 转换为：

```text
nunits = ceil(nbytes / sizeof(Header)) + 1
```

union 中的 `Align x` 强制 payload 具有至少 `long` 对齐。

## 11. 循环空闲链表

`freep` 最终指向一个按地址排序的循环链表，静态 `base` 是 size 0 的哨兵。但两者的 BSS 初值都是零；哨兵环只在第一次调用 `malloc()`、看到 `freep == 0` 时才建立：

```text
base -> free block -> free block -> ... -> base
```

所以“空堆已有哨兵环”只描述首次 `malloc()` 初始化完成后的状态。此前直接调用 `free()` 会从空 `freep` 开始遍历并解引用，`free(NULL)` 也会先计算一个伪 header；它们都不是标准 libc 的安全 no-op。当前程序只释放由成功 `malloc()` 返回的非空地址，并由 `malloc()` 在第一次 `morecore()` 调用 `free()` 前先建立环，因而正常调用链满足该前置条件。

地址排序在最高地址回到最低地址处有一个 wrap point。`free()` 搜索插入位置时的条件：

- 通常寻找 `p < bp < p->ptr`；
- 若 `p >= p->ptr`，当前边跨越地址空间末尾，只要 `bp > p` 或 `bp < p->ptr` 就属于该间隙。

这个判断是循环有序链表能同时接收高地址和低地址 block 的核心。

## 12. `malloc()` 的 first-fit 与切分

`malloc()` 从 `freep->ptr` 开始循环 first-fit：

- block 大小正好：从链表摘除；
- block 更大：从空闲 block 的尾部切出 `nunits`，剩余低地址部分继续留在链表；
- 绕回 `freep` 仍无结果：调用 `morecore(nunits)`。

从尾部切分无需改变前驱指针，只有剩余 block 的 size 改变。返回值是 `p + 1`，跳过 header。

成功后 `freep` 更新为命中 block 的前驱，使下一次搜索从附近继续，减少总从链头开始的偏差。

`malloc(0)` 没有特殊返回分支；换算公式仍得到一个 Header 单位，因此可能返回一个可释放但 payload 长度为零的非空指针。它与 `free(NULL)` 的非标准行为都应按本实现说明，而不能套用完整 libc 约定。

## 13. `morecore()` 扩堆

`morecore(nu)` 至少向 eager `sbrk()` 申请 4096 个 `Header` 单位，而不是 4096 字节。当前 64 位结构中一个 Header 通常为 16 字节，因此最小批次通常为 64 KiB。

成功后：

1. 把新区域开头解释为 Header；
2. 设置其 size；
3. 调用普通 `free(hp + 1)` 把整块插入并与相邻空闲区合并；
4. 返回新的 `freep`，`malloc` 继续搜索。

失败返回零，最终 `malloc()` 返回零。allocator 不主动把空闲 block 还给内核；进程退出只先进入 `ZOMBIE`，父进程随后通过 `wait()` 进入内核 `kwait()/freeproc()` 时才统一释放整个地址空间。

## 14. `free()` 的相邻合并

插入位置找到后，`free()` 最多做两次合并：

1. 若释放 block 的末尾等于后继 block 起点，与后继合并；
2. 若前驱 block 的末尾等于释放 block 起点，与前驱合并；
3. 否则分别连接指针。

合并后循环链表仍按地址排序，能够降低长期分配后的外部碎片。

allocator 不保存 allocated/free 标志、不验证 payload 是否来自 `malloc`，也不检测 double free、越界写或 header 损坏。`malloc()` 还不检查 `nbytes` 向 Header 单位换算时的无符号溢出，`morecore()` 也假定单位数乘 `sizeof(Header)` 可表示。这些行为会导致过小分配或破坏链表，属于调用者违反当前教学 allocator 的前提。

## 15. 并发和 `fork` 语义

用户 allocator 没有锁；xv6 没有用户线程，同一进程只有一条用户执行流，因此当前模型足够。若以后增加多线程，`freep`、链表修改和 `sbrk` 都必须同步。

`fork()` 完整复制父进程已映射用户页，因此父子各自得到 allocator 元数据和 heap 内容的副本；之后的分配互不影响。未映射 lazy 洞保持洞，但 allocator 默认通过 eager `sbrk` 扩堆。

`exec()` 替换整个地址空间，旧 allocator 状态随旧页表释放，新程序的静态 `base/freep` 从 BSS 零值开始。

## 16. 不变量和限制

- 用户栈由内核 `exec` 创建，不由 `user.ld` 或 allocator 创建。
- 系统调用参数/返回遵循寄存器 ABI，stub 不在用户栈重新打包参数。
- `user/usys.S` 是生成物，不直接编辑。
- 常规用户 ELF 的 `start()` 必须保持为链接入口，并保证 `main` 返回时调用 `exit`；`_forktest` 的 `-e main` 特例必须单独维持。
- allocator 空闲链表必须循环、有序且 block 不重叠。
- `freep == 0` 是“从未进入 `malloc()`”的独立初始化状态；任何 `free()` 调用都要求哨兵环已建立且参数来自本 allocator。
- `Header.size` 以 Header 为单位；把它误解为字节会造成严重越界。
- 运行库没有边界检查、线程安全、locale、buffered stdio 或标准 C 完整兼容性。

## 17. 验证

- `make user/_echo` 后用 `riscv64-*-objdump -f user/_echo` 查看 start address，再在 `start` 和 `main` 设断点。
- 检查生成的 `user/usys.S`，确认每个 stub 的 `SYS_*`、`ecall` 和特殊 `sys_sbrk` 名称。
- `usertests exectest` 验证 `exec -> start -> main` 和 fd 继承；`bigargtest` 验证栈参数上限。
- `usertests mem` 压力分配器和物理内存回收；完整 `usertests` 前后还会用 `countfree()` 检查丢页。
- `usertests lazy_alloc/lazy_unmap/lazy_copy/lazy_sbrk` 验证两种 `sbrk` 策略的边界。
- 在 `malloc/free/morecore` 断点打印 `freep` 环，逐项检查地址顺序、size 和首尾闭环。
