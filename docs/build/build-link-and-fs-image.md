# 构建、链接与文件系统镜像

本文说明当前仓库从源码生成 `kernel/kernel`、用户 ELF、`fs.img`，再交给 QEMU 或 GDB 的完整过程。主要依据是 `Makefile`、`kernel/kernel.ld`、`user/user.ld`、`mkfs/mkfs.c`、`kernel/fs.h` 和 `.gdbinit.tmpl-riscv`。这些文件共同定义的并不只是“怎么编译”，还包括内核入口地址、用户虚拟地址、磁盘 ABI、初始目录内容和调试连接协议。

相关文档：

- [启动与初始化](../architecture/boot-and-init.md)
- [用户 ABI 与运行库](../user/runtime-and-abi.md)
- [文件系统](../kernel/filesystem.md)
- [存储栈](../kernel/storage-stack.md)
- [VSCode 调试](vscode-debug.md)

## 1. 产物和依赖图

执行 `make qemu` 时，主要依赖关系是：

```text
Makefile:OBJS 对应的 kernel/*.{c,S} + kernel/kernel.ld
  -> kernel/*.o
  -> kernel/kernel
  -> kernel/kernel.asm + kernel/kernel.sym

user/usys.pl
  -> user/usys.S
  -> user/usys.o

user/<program>.c + ULIB + user/user.ld（普通用户程序）
  -> user/<program>.o
  -> user/_<program>
  -> user/<program>.asm + user/<program>.sym

mkfs/mkfs.c + kernel/fs.h + kernel/param.h
  -> mkfs/mkfs

README + UPROGS + mkfs/mkfs
  -> fs.img

kernel/kernel + fs.img
  -> 作为 qemu-system-riscv64 的启动输入
```

不带目标的普通 `make` 并不执行上面整条链。GNU make 把 Makefile 中出现的第一个普通目标 `kernel/kernel` 作为默认目标，因此它只构建内核对象、`kernel/kernel`，并由同一个链接 recipe 顺带生成 `kernel/kernel.asm` 和 `kernel/kernel.sym`；它不会构建用户程序、`mkfs/mkfs` 或 `fs.img`，也不会启动 QEMU。要得到可启动且带根文件系统的完整输入，必须明确使用 `make qemu`、`make qemu-gdb`，或分别请求 `kernel/kernel` 与 `fs.img`。

下划线只用于宿主构建产物，例如 `user/_cat`，避免与宿主的 `cat`、`rm` 等命令混淆。`mkfs` 写入镜像时会移除 `user/` 前缀和开头的 `_`，所以 xv6 中看到的是 `/cat`、`/sh`、`/init`。

## 2. 内核对象列表是链接边界

`Makefile` 的 `OBJS =` 明确列出进入内核的全部目标文件，从最早入口 `entry.o`、启动代码 `start.o`，到 VM、进程、trap、文件系统、设备驱动。新增内核实现文件若没有加入这个列表，即使能单独编译，也不会出现在最终内核中。

对象顺序大多不决定 C 符号解析，但这里有一个关键布局例外。`kernel/kernel.ld` 在 `.text` 开头写了 `kernel/entry.o(_entry)`；GNU ld 的这个语法中括号内匹配的是输入 section 名，不是符号名，而 `kernel/entry.o` 的入口实际位于 `.text`，所以该选择式没有命中。当前 `_entry` 位于链接基址，是因为 `Makefile:OBJS` 把 `entry.o` 列在第一项，随后 `*(.text .text.*)` 按输入顺序收集它。调整对象顺序或 section 名时必须重新核对最终 ELF，不能把布局只归因于该选择式。

汇编文件通过专门规则编译：

```make
$K/%.o: $K/%.S
	$(CC) -march=rv64gc -g -c -o $@ $<
```

C 文件使用 make 的隐式 `.c -> .o` 规则和全局 `CFLAGS`。`-MD` 令编译器同时生成 `.d` 依赖文件，末尾的 `-include kernel/*.d user/*.d` 让头文件变化触发相应重编译。上面的内核汇编规则只传入 `-march=rv64gc -g`，既不使用完整 `CFLAGS`，也不生成 `.d`。这已经是当前缺陷，不是未来假设：`kernel/trampoline.S` 现在就通过预处理器包含 `kernel/riscv.h` 和 `kernel/memlayout.h`，但这两个头文件的变化不会由 `.d` 文件触发 `kernel/trampoline.o` 重编译。修改这类汇编所含头文件后，需要显式重建相应对象，或修正规则使其生成依赖。生成的 `user/usys.S` 有自己的规则并使用完整 `CFLAGS` 编译，所以其 `#include "kernel/syscall.h"` 会进入 `user/usys.d`。

## 3. 工具链探测

若调用者没有设置 `TOOLPREFIX`，`Makefile` 依次探测多种 RISC-V 前缀：

```text
riscv64-unknown-elf-
riscv64-elf-
riscv64-none-elf-
riscv64-linux-gnu-
riscv64-unknown-linux-gnu-
```

探测方式是运行对应 `objdump -i` 并检查输出中是否有 `elf64-big`。找到后，`CC`、`LD`、`OBJCOPY`、`OBJDUMP` 都使用同一前缀。这里的失败分支虽然打印 `Couldn't find a riscv64 version of GCC/binutils` 并让探测 shell 返回非零，但 GNU make 的 `$(shell ...)` 本身不会据此立即中止；`TOOLPREFIX` 会成为空串，后续可能退回宿主 `gcc`/`ld`，再以架构或选项错误失败。调用者显式设置 `TOOLPREFIX` 时也会完全绕过探测，因此仍应核对实际工具路径和目标架构。

宿主工具与交叉工具必须区分：

| 任务 | 工具 |
|---|---|
| 编译/链接 RISC-V 内核和用户程序 | 交叉 GCC、LD、objdump |
| 编译在宿主运行的 `mkfs/mkfs` | 宿主 `gcc` |
| 生成系统调用汇编 | 宿主 `perl` |
| 构建文件系统镜像 | 宿主进程 `mkfs/mkfs` |
| 运行 xv6 | `qemu-system-riscv64` |

`mkfs` 包含与内核共享的磁盘结构头文件，但它自身不是 RISC-V 程序。

## 4. 编译选项表达的运行模型

主要 `CFLAGS` 包括：

```text
-march=rv64gc
-mcmodel=medany
-ffreestanding
-nostdlib
-fno-common
-fno-omit-frame-pointer
-ggdb -gdwarf-2
-O
-Wall -Werror
```

它们的含义是：

- `rv64gc` 选择 64 位 RISC-V 以及通用扩展和压缩指令；
- `medany` 允许代码在较宽的 PC 相对地址范围内链接，适合位于 `0x80000000` 的内核；
- freestanding/nostdlib 表示没有宿主 C 运行库，字符串、输出和分配都由 xv6 自己提供；
- `fno-common` 让重复的未初始化全局定义成为链接错误；
- 保留 frame pointer 和 DWARF 2 调试信息，便于 GDB 回溯；
- `-O` 开启优化，因此调试时源码行与指令不总是一一对应；
- 警告作为错误，防止明显接口不一致进入镜像。

大量 `-fno-builtin-*` 防止编译器把 xv6 的 `memmove`、`printf`、`malloc` 等调用替换为宿主库假设或其他内建序列。Makefile 还探测并禁用 stack protector 和 PIE；内核没有宿主提供的栈保护运行时，链接地址也必须由链接脚本确定。

Makefile 没有显式设置 `-msmall-data-limit=0`，而内核 `_entry` 和用户 trap 入口都没有建立 kernel `gp`。本地 GCC 13.3.0 在当前相关目标选项下恰好报告 small-data limit 0，已有 ELF 的反汇编也没有 C 生成的 `gp` 相对访问；这是已观察产物事实，不是 `-mcmodel=medany` 单独提供的保证。换工具链或修改 flags 后必须重新查询 target options 并审计 `objdump`，详见[平台契约](../architecture/platform-contracts.md#5-psabi系统调用-abi-与汇编边界)。

`LDFLAGS = -z max-page-size=4096` 把 ELF 最大页对齐限制为 4 KiB，与 xv6 `PGSIZE` 和链接脚本的页边界假设一致。

## 5. 内核链接脚本 `kernel/kernel.ld`

`kernel/kernel.ld` 首先声明：

```ld
OUTPUT_ARCH("riscv")
ENTRY(_entry)
. = 0x80000000;
```

这里必须区分三个彼此独立、当前数值恰好都为 `0x80000000` 的地址：

- ELF `PT_LOAD` 给出的装载地址决定内核各字节被放到哪段 RAM；
- ELF header 的 `e_entry` 由 `ENTRY(_entry)` 设置，是 ELF 的入口元数据；
- 当前 QEMU `virt -bios none` 直接启动路径在 `0x1000` 生成 MROM reset stub，并把该 stub 的跳转目标设为 `virt` 的 DRAM/固件起始地址 `0x80000000`。

第三项不是从这个内核 ELF 的 `e_entry` 读取的。也就是说，只把 `ENTRY(...)` 改到另一个符号不会改变当前 MROM stub 的交接地址；stub 仍会跳到 `0x80000000`。`-bios none` 去掉的是 OpenSBI 等外部固件，不会去掉机器自身的 reset stub。若改用 OpenSBI 或另一种装载器，交接协议可能不同，不能把这里的直接启动约定外推到所有 `-kernel` 用法。

因此链接器首先必须让真正的第一条 xv6 指令 `_entry` 位于 MROM 的固定交接地址；同时应让 ELF `e_entry` 与 `_entry` 一致，供调试器、分析工具以及可能遵循 ELF entry 的其他装载器使用。`ENTRY(_entry)` 只完成后一个元数据设置，不会移动符号，也不会决定输入 section 的摆放。当前 `_entry == 0x80000000` 依赖位置计数器从该地址开始、`entry.o` 在链接对象列表中排第一，以及通配式随后首先收集它的 `.text`。可用 `readelf -h -l kernel/kernel`、`readelf -SW kernel/entry.o` 和 `nm -n kernel/kernel` 分别验证 ELF entry/装载段、实际输入 section 名和 `_entry` 地址；这些命令仍不能单独证明 MROM 跳转目标，后者应在 QEMU monitor/GDB 中检查 `0x1000` 的 reset stub 或实际跟踪第一次跳转。

### 5.1 trampoline 的整页约束

链接脚本在普通内核文本后对齐到 4096 字节，定义 `_trampoline`，再收集标记为 `trampsec` 的 `kernel/trampoline.S`：

```ld
. = ALIGN(0x1000);
_trampoline = .;
*(trampsec)
. = ALIGN(0x1000);
ASSERT(. - _trampoline == 0x1000,
       "error: trampoline larger than one page");
```

断言要求 trampoline 的内容对齐后恰好占一个链接页区间。内核页表把这段物理页映射到高虚拟地址 `TRAMPOLINE`，每个用户页表也映射同一页；若汇编增长超过一页，映射和相邻 `TRAPFRAME` 布局会失效，因此构建应立即失败。

### 5.2 其他 section 和 `end`

链接脚本依次显式放置 `.rodata`、`.data`、`.bss`，各内部按 16 字节对齐，最后 `PROVIDE(end = .)`。它没有显式列出 `.eh_frame`、`.got`、`.got.plt` 等 section；GNU ld 会按 orphan-section 规则把当前编译产生的这些内容插到相容位置。修改编译选项或工具链后，不能只读脚本文本就假设最终 section 顺序，仍要检查最终 ELF。物理页分配器从 `end` 向上按页对齐后释放直到 `PHYSTOP` 的内存，所以 `end` 必须覆盖所有可分配的内核静态 section，是“内核静态映像结束”和“可分配物理内存开始”的边界。

`.bss` 是 `SHT_NOBITS`，不会把与其内存大小相等的零字节存进 ELF 文件。当前内核的一个 `PT_LOAD` 同时覆盖有文件内容的 section 和 `.bss`，其 `p_memsz` 大于 `p_filesz`；ELF 装载契约要求装载器把这个差额对应的内存清零。xv6 在 `_entry` 前没有自己的清 BSS 循环，所以 `started == 0`、`cpus[]`/`proc[]` 等静态对象的零初值以及 `stack0` 所在内存的初始内容，都以 QEMU 的 ELF loader 正确落实该契约为前提。若以后改成 raw binary、自写 loader 或拆分 `PT_LOAD`，必须确保每个可装载段的 `[p_filesz, p_memsz)` 尾部仍在任何 hart 开始执行前清零；只复制文件字节是不够的。

`etext` 位于对齐后的 trampoline 页末尾。当前内核 ELF 由链接器形成一个带 `RWE` 的 `PT_LOAD`，但启动后的内核页表不照搬该 segment 权限：`[KERNBASE, etext)` 映射为只读/可执行，`[etext, PHYSTOP)` 映射为可读/可写。因此显式放在 `etext` 之后的 `.rodata` 在 xv6 页表中也属于可写区域；section 名本身不会自动建立硬件权限。

### 5.3 内核派生产物

链接后 Makefile 运行：

```text
objdump -S kernel/kernel -> kernel/kernel.asm
objdump -t kernel/kernel -> kernel/kernel.sym
```

`.asm` 混合反汇编和源码，适合核对编译器生成的调用/寄存器操作；`.sym` 是简化符号表。GDB 真正加载的是带调试信息的 `kernel/kernel`。

## 6. 用户程序链接脚本 `user/user.ld`

`user/user.ld` 把位置计数器设为虚拟地址 `0x0`：

```ld
. = 0x0;
```

随后依次收集 `.text`、`.rodata`、`.eh_frame`，再把 `.data` 对齐到下一 4 KiB 边界，放置 `.data` 与 `.bss`，最后提供 `end`。`kexec()` 读取 `LOAD` program header 中的虚拟地址、大小和 flags 并映射到新用户页表，因此链接脚本形成的 segment 布局直接成为运行时地址空间布局。它会检查 segment 对齐、大小关系与地址加法溢出，却不会验证 ELF `entry` 是否真的落在某个可执行 segment 内；构建验证必须单独核对 entry。

所有普通用户程序与 `ULIB` 一起链接：

```text
user/ulib.o
user/usys.o
user/printf.o
user/umalloc.o
```

这提供 `start()`、系统调用 stub、格式化输出和堆分配。没有动态链接器，也没有共享库。普通链接规则直接传入四个完整 object，并未使用 archive 按需抽取或 `--gc-sections`，所以每个 ELF 包含链接脚本接纳的全部相关 section，而不只是源码实际调用到的函数；`_forktest` 的专用精简规则是例外。

`user/user.ld` 没有写 `ENTRY(start)`。普通链接命令也没有 `-e`，所以这里依赖 GNU ld 的默认入口查找规则：存在全局符号 `start` 时，把它作为 ELF entry。`start()` 再调用 `main(argc, argv)`，并在 `main()` 返回时执行 `exit()`。`.text` 从地址 0 开始并不表示入口必然为 0；主程序对象排在 `ULIB` 前面，地址 0 通常反而是该程序的 `main` 或其他函数。重命名或隐藏 `start` 后，链接器可能退回 `.text` 起点，仍生成 ELF，却绕过正确的用户启动包装。

普通规则生成 `user/_name`，并附带 `user/name.asm` 与 `user/name.sym`。用户 ELF 从 0 链接是 xv6 的简化设计，不代表宿主能直接执行这些文件。

## 7. `user/usys.S` 是生成文件

`user/usys.pl` 是系统调用 stub 的单一来源，Makefile 规则是：

```make
$U/usys.S: $U/usys.pl
	perl $U/usys.pl > $U/usys.S
```

每个 `entry("name")` 生成一个全局汇编函数：把 `SYS_name` 放入 `a7`，执行 `ecall`，再 `ret`。生成的 `user/usys.S` 随后编译为 `user/usys.o` 并链接进所有用户程序。

修改系统调用时必须保持四处一致：用户声明、`user/usys.pl` entry、`kernel/syscall.h` 调用号、`kernel/syscall.c` 分派表。直接编辑 `user/usys.S` 会在重新生成或 `make clean` 后丢失。

## 8. `_forktest` 的特殊链接规则

`user/_forktest` 不使用完整 `ULIB` 和 `user/user.ld`，而是通过 `-N -e main -Ttext 0` 只链接：

```text
forktest.o + ulib.o + usys.o
```

目的是让程序足够小，以便 `forktest` 把进程表耗尽时不先被内存压力干扰。它不需要 `printf.o` 与 `umalloc.o`，但目标依赖仍写成 `$(ULIB)`，所以这两个未链接对象的时间戳变化也会触发一次不必要的重链接。

这个特例确实改变了运行布局：`-e main` 绕过 `ulib.c:start()`，因此 `forktest` 的 `main()` 必须自行调用 `exit()`；`-N` 不按普通脚本把数据放到下一页，并使当前唯一的 `LOAD` segment 带 `RWE` 权限；该规则只生成 `forktest.asm`，不生成 `.sym`。研究用户 ELF 时不能假设所有程序都经过 `user/user.ld` 或都从 `start` 进入。

## 9. `UPROGS` 决定初始用户空间

`Makefile` 的 `UPROGS=` 是写入 `fs.img` 的程序清单，包括常用工具、`init`、shell、`usertests`、压力与崩溃恢复程序。源码文件存在并不自动意味着能在 xv6 shell 中执行；它必须构建为用户 ELF并出现在 `UPROGS` 或以其他方式写入镜像。

`fs.img` 的规则还加入仓库根目录 `README`：

```make
fs.img: mkfs/mkfs README $(UPROGS)
	mkfs/mkfs fs.img README $(UPROGS)
```

因此初始根目录只包含 `.`、`..`、`README` 和列出的用户程序，没有子目录层次。

## 10. 磁盘 ABI 来自 `kernel/fs.h`

`kernel/fs.h` 同时被内核和宿主 `mkfs/mkfs.c` 使用，定义的结构必须具有一致布局：

```text
block 0: boot block（xv6 不使用其内容）
block 1: superblock
next:    on-disk redo log
next:    dinode blocks
next:    allocation bitmap
rest:    data blocks
```

关键常量和结构包括：

- `BSIZE = 1024`；`mkfs` 的 `wsect()`/`rsect()` 虽把参数命名为 sector，但每次按 `sec * BSIZE` 访问一个 1024 字节文件系统块；VirtIO block 设备的扇区是 512 字节，因此内核运行时一次 xv6 block I/O 实际跨 `BSIZE / 512 = 2` 个设备扇区；
- `ROOTINO = 1`；根 inode 编号固定；
- `struct superblock`；记录总块数、数据块数、inode 数和各区域起点；
- `struct dinode`；磁盘 inode，包含类型、设备号、链接数、大小和块地址；
- `NDIRECT = 12`、一个一级间接块、`MAXFILE = 268` 个数据块；
- `struct dirent`；2 字节 inode 编号加最多 14 字节、可不以 NUL 结尾的名字；
- `IBLOCK`、`BBLOCK`、`IPB`、`BPB`；完成 inode/bitmap 定位。

改变这些结构或常量后必须同时重建宿主 `mkfs` 和 `fs.img`。只重建内核而沿用旧镜像会让内核按新 ABI 解释旧字节。

## 11. `mkfs` 计算布局

`mkfs/mkfs.c` 使用 `NINODES = 200`，从 `kernel/param.h` 取得 `FSSIZE = 2000`、`LOGBLOCKS = 30`。计算为：

```text
nbitmap      = FSSIZE / BPB + 1
ninodeblocks = NINODES / IPB + 1
nlog         = LOGBLOCKS + 1       # header + logged data blocks
nmeta        = 2 + nlog + ninodeblocks + nbitmap
nblocks      = FSSIZE - nmeta
```

在当前结构大小下，`IPB = 16`、`BPB = 8192`，所以：

```text
nbitmap      = 1
ninodeblocks = 13
nlog         = 31
nmeta        = 47
nblocks      = 1953
first data allocation candidate = block 47
```

程序在当前小端宿主上形成这些值，并设置：log 从块 2 开始、inode 区紧随 log、bitmap 紧随 inode 区。除 `magic` 外的 superblock 数值会先经过 `xint()`；字节序限制见下一节。这里使用“整数除法再加一”，不是通常的向上取整表达式；当 `FSSIZE` 恰好整除 `BPB`，或 `NINODES` 恰好整除 `IPB` 时，会额外保留一个 bitmap/inode block。

## 12. 为什么 `mkfs` 显式转换字节序

`xshort()` 与 `xint()` 按字节写入低位到高位，将宿主整数编码为 RISC-V 镜像使用的小端表示；对读回的字段再次调用同一函数，也被当作解码使用。这样明确了大部分磁盘字段的字节格式，但当前 `mkfs` 并没有做到与宿主字节序无关：它把已经编码的 `sb.inodestart`、`sb.bmapstart` 等字段直接用于宿主侧地址计算，没有先解码；`sb.magic = FSMAGIC` 更是完全没有经过 `xint()`。常见的小端开发宿主不会暴露这些问题，在大端宿主上则会算错块号，并把 magic 写成 RISC-V 不能识别的字节序。

代码对 superblock 中除 `magic` 外的数值、所使用的 dinode 字段、目录项 inode 号和间接块地址执行转换。内核运行在小端 RISC-V 上，直接按自身结构读取这些字节。若要支持大端宿主，必须把“宿主计算值”和“写盘编码值”分开，并转换 `magic`，不能只保留现有的 `xint()` 调用。

结构布局还有编译器 ABI 前提。`mkfs` 只断言 `sizeof(int) == 4`、一个块能整除 `sizeof(struct dinode)` 和 `sizeof(struct dirent)`。这些检查不会证明宿主编译器与 RISC-V 编译器给出了完全相同的字段偏移；某个带额外 padding、但大小仍整除 1024 的布局也可能通过。修改共享磁盘结构时应另外核对 `sizeof`、字段偏移和实际镜像字节。

## 13. 镜像初始化顺序

`mkfs:main()` 的顺序是：

1. 以 `O_RDWR | O_CREAT | O_TRUNC` 打开输出镜像。
2. 计算 superblock 和各区域边界。
3. 向全部 `FSSIZE` 个块写零，确定文件大小并清除旧内容。
4. 把 superblock 写到块 1。
5. `ialloc(T_DIR)` 创建 inode 1；局部变量 `rootino` 保存返回值，并断言它就是 `ROOTINO`。
6. 向根目录追加 `.` 和 `..`，两者都指向 inode 1。
7. 为每个输入文件分配 `T_FILE` inode、添加根目录项并复制内容。
8. 把根目录大小向块边界扩展。
9. `balloc(freeblock)` 把已经使用的所有块在 bitmap 中标记为占用。

日志区域初始为全零，表示没有待恢复事务。boot block 也为零，因为 QEMU 使用 `-kernel` 直接加载内核，而不是从文件系统镜像启动。

`mkfs` 不是防御性镜像导入器。它没有在每次 inode 分配时检查 `NINODES`，也没有在 `freeblock++` 时检查 `FSSIZE`；过多输入文件可能把 inode 写进后续区域，总数据过大则可能直到 `rsect()` 访问镜像末尾之外才因短读失败。输入文件的 `read()` 若返回负值，复制循环也会像到达 EOF 一样结束，而不会报告该读取错误。正常 Makefile 清单规模避开了这些边界，但扩展 `UPROGS` 时必须检查 inode 数、单文件 `MAXFILE` 和总块数。

`argc`、整数大小和结构能否整除块大小的检查发生在打开输出之前；但程序会在逐个验证输入路径、名称和容量之前，以 `O_TRUNC` 打开输出。后续构建中断或普通错误退出可能留下部分写入、且时间戳最新的 `fs.img`。Makefile 没有声明 `.DELETE_ON_ERROR`；这种残留目标下一次可能被判断为已是最新。失败后应先删除该残留镜像，或用 `make -B fs.img` 强制完整重建，不能假设再次执行 `make fs.img` 一定会运行 `mkfs`。

## 14. 初始目录项和名称规则

处理输入路径时，`mkfs`：

```text
user/_cat -> strip "user/" -> _cat -> strip leading "_" -> cat
README    -> README
```

之后断言名字不含 `/` 且长度不超过 `DIRSIZ`。它不会递归创建目录，所有输入都作为根目录普通文件写入。重复名字没有主动检测，会产生多个同名目录项，路径查找将命中先出现者；正常 `UPROGS` 避免这种情况。

新 inode 的 `nlink` 直接初始化为 1。根目录的 `.`/`..` 并不会让 `mkfs` 调整目录链接计数到完整 Unix 语义；xv6 使用其简化规则。

根目录数据写完后，代码使用：

```text
off = ((size / BSIZE) + 1) * BSIZE
```

把目录大小推进到下一个整块边界，目的是把当前已分配尾块里的零目录项留给后续创建。即使原大小恰好对齐也会再推进一块，但代码只改 inode `size`，不会为额外范围分配数据块；这会在目录内部制造未映射空洞，而运行时 `readi()` 并没有只查询、不分配的 `bmap()` 路径。当前初始目录远小于一个块，不会触发该边界。正常尾块未使用部分因镜像预先清零，其目录项 inode 号为 0，会被内核忽略。

## 15. `iappend()` 的直接和间接块分配

`iappend(inum, data, n)` 从磁盘 inode 当前 `size` 继续写入，并循环处理跨块数据：

```text
fbn = off / BSIZE
if fbn < NDIRECT:
  allocate/use din.addrs[fbn]
else:
  allocate/use din.addrs[NDIRECT] as indirect block
  allocate/use indirect[fbn - NDIRECT]

copy min(remaining, end-of-current-block - off)
advance off and source pointer
```

新块由单调递增的 `freeblock++` 分配。镜像刚被清零，所以不需要扫描 bitmap，也不会产生碎片。若首次需要间接块，先为间接索引本身分配一块，再为数据分配一块。每次写完后更新 inode `size`。

`assert(fbn < MAXFILE)` 防止输入文件超过 12 个直接块加 256 个间接数据块。`mkfs` 没有双重间接块。

`iappend()` 对部分块采用 read-modify-write，是因为同一数据块可能先写入一段、随后继续追加。所有写入直接落镜像，没有运行时 redo log；构建期间崩溃只会留下一个无效构建产物，但必须删除它或强制重建，避免 make 因其新时间戳而跳过 recipe。

## 16. bitmap 的假设

`balloc(freeblock)` 把 `[0, freeblock)` 每个块对应的 bit 置 1。这包括 boot、super、log、inode、bitmap、目录数据、文件数据和间接块。

当前 `FSSIZE = 2000 < BPB = 8192`，所以只有一个 bitmap block。宿主 `balloc()` 只构造并写第一个 bitmap block，并断言初始连续已用区的末尾 `used < BPB`。这不等同于禁止 `FSSIZE > BPB`：后续 bitmap blocks 可以保持全零，供内核运行时分配高块；真正的现有限制是 metadata 加初始目录/文件占用不能达到或跨过第一个 bitmap 能表示的 8192 块，否则 mkfs 必须改为初始化多个 bitmap blocks。

`freeblock` 不单独写入 superblock。运行时 `balloc()` 扫描 bitmap 找 0 位，所以 bitmap 是已分配状态的权威来源。

## 17. `.PRECIOUS` 与增量镜像行为

Makefile 把 `%.o` 标为 `.PRECIOUS`，直接作用是避免 make 将规则链中的中间对象自动删除；Makefile 注释把它与保留首次构建后的镜像变化联系起来。决定是否真正重建 `fs.img` 的仍是目标及其直接依赖的时间戳：只要 `mkfs/mkfs`、`README` 和任一 `UPROGS` 都不比镜像新，make 就不会重新运行 `mkfs`。QEMU 以可写 raw drive 打开镜像，没有启用 snapshot 模式，所以 xv6 中已经落盘的变化会更新并保留在这个宿主文件中。

任一用户 ELF、`README` 或 `mkfs` 变新都会让下一次 `make qemu`/`make qemu-gdb` 重新执行 `mkfs/mkfs fs.img ...`；它以 `O_TRUNC` 打开镜像，因此此前运行时状态会被整体丢弃，而不是把新程序增量复制进去。删除 `fs.img`，或执行 `make clean` 后再构建，也会恢复由 `README + UPROGS` 定义的初始状态。`test-xv6.py` 的 reset 路径正是先删除镜像再 `make fs.img`。

make 比较的是已声明依赖的时间戳，不会因为 recipe 文本、变量值或依赖排列顺序改变而自动认为目标过期。这在当前 Makefile 有几个具体后果：

- 只在 Makefile 中重排 `OBJS` 不一定重链接已有 `kernel/kernel`，尽管 `_entry` 的当前位置恰好依赖这个顺序；改变 `CFLAGS`、`LDFLAGS` 或 `TOOLPREFIX` 也不一定重编译旧对象；
- 从 `UPROGS` 删除项目或只调整列表顺序，不一定重建已有 `fs.img`，因为 Makefile 本身不是镜像依赖；增加一个新构建的用户 ELF 通常会因新依赖时间戳触发重建；
- `mkfs/mkfs` 的显式依赖只有 `mkfs/mkfs.c`、`kernel/fs.h`、`kernel/param.h`，虽然源码还包含 `kernel/types.h` 和 `kernel/stat.h`；后两者改变时可能继续使用旧宿主工具；
- `kernel/kernel.asm`、`kernel/kernel.sym` 和普通用户程序的 `.asm`/`.sym` 是链接 recipe 的副产物，不是单独声明的目标；只删除副产物不会让 make 重新执行仍然最新的 ELF recipe。

因此涉及链接顺序、构建 flags、工具链、清单删除或漏列依赖的变更，应明确强制相应目标重建，并在强制 `fs.img` 前先决定是否需要保存其中的运行时数据。`make -B kernel/kernel` 会强制其依赖链，`make -B fs.img` 会截断并重建镜像；`make clean` 的范围更大，也会删除持久镜像。

## 18. QEMU 运行参数

`Makefile:QEMUOPTS` 的核心项是：

```text
-machine virt
-bios none
-kernel kernel/kernel
-m 128M
-smp $(CPUS)             # 默认 3
-nographic
-global virtio-mmio.force-legacy=false
-drive file=fs.img,if=none,format=raw,id=x0
-device virtio-blk-device,drive=x0,bus=virtio-mmio-bus.0
```

`-bios none` 与 `-kernel` 让 QEMU 不使用 OpenSBI，直接装载内核 ELF；每个 hart 仍先执行 `0x1000` 的 QEMU MROM reset stub，再跳到该直接启动路径的固定交接地址 `0x80000000`，而不是读取 ELF `e_entry` 后跳转。`virt` 机器的 RAM 从 `0x80000000` 开始。当前 `-m 128M` 与 `kernel/memlayout.h:PHYSTOP = KERNBASE + 128 MiB` 精确对应；减少 QEMU RAM 会让内核把不存在的地址当成可分配内存，增加 RAM 则不会自动让 xv6 使用额外部分。默认三 hart，可用 `make CPUS=1 qemu` 改变实际数量，但不能超过内核编译常量 `NCPU = 8`；`_entry` 在索引每 hart 栈前没有做越界检查。

磁盘作为 modern VirtIO MMIO block device 暴露，内核 `virtio_disk.c` 使用固定 MMIO 地址和中断号。`-nographic` 把串口连接到当前终端。

只有 `make qemu` 依赖 `check-qemu-version`；`make qemu-gdb` 没有这项依赖。该检查解析 `qemu-system-riscv64 --version`，借助宿主 `bc` 把截取出的 `major.minor` 当十进制数与 7.2 比较。它依赖标准首行格式和可用的 `bc`，而且把例如 `7.10` 当成十进制 `7.1`，可能误判两位数 minor 版本；这只是 Makefile 的前置检查，不是可靠的语义版本比较。

## 19. GDB 端口和模板

Makefile 根据用户 ID 计算：

```text
GDBPORT = uid % 5000 + 25000
```

这样共享机器上的不同用户较少碰撞。`.gdbinit` 规则读取 `.gdbinit.tmpl-riscv`，把模板中的 `:1234` 替换成实际端口。模板关键命令是：

```gdb
set architecture riscv:rv64
target remote 127.0.0.1:1234
symbol-file kernel/kernel
set disassemble-next-line auto
set riscv use-compressed-breakpoints yes
```

`make qemu-gdb` 给 QEMU 增加 `-S`，让 CPU 在 MROM reset PC `0x1000` 暂停，并按 QEMU 支持的命令行形式开放 GDB stub。随后在另一终端运行合适的 RISC-V GDB；符号文件必须是当前构建的 `kernel/kernel`。

生成规则只把模板列为 `.gdbinit` 的依赖，计算出的 `GDBPORT` 并不是文件依赖。若同一工作树换到不同 uid、或端口计算方式改变但模板时间戳没变，旧 `.gdbinit` 可能仍保留旧端口；此时用 `make -B .gdbinit` 重新生成，并用 `make print-gdbport` 核对。

`.vscode/launch.json` 当前固定连接 `127.0.0.1:26000`，而 Makefile 端口随 uid 计算。当前用户只有在计算结果恰为 26000 时二者天然一致；否则应让工作区调试配置与 `make print-gdbport` 输出一致。完整操作见 [VSCode 调试](vscode-debug.md)。

## 20. 清理与格式化目标

普通 `make` 的默认目标是 `kernel/kernel`，不是 `qemu` 或 `fs.img`。`make tags` 也有更窄的边界：它先确保 `OBJS` 中的内核对象最新，然后运行 `etags kernel/*.S kernel/*.c`，由 `etags` 在仓库根目录写 `TAGS`。该索引不包含内核头文件、用户程序、`mkfs`、Python 工具或文档。recipe 声明的目标名是小写 `tags`，实际产物却是大写 `TAGS`，且 `tags` 没有声明为 phony；通常因为小写文件不存在而每次都会重跑，但若根目录出现一个足够新的普通文件 `tags`，make 反而可能跳过 recipe，即使 `TAGS` 缺失或过期。

`make clean` 删除内核/用户对象、依赖、反汇编、符号、最终 ELF、`fs.img`、宿主 `mkfs`、生成的 `.gdbinit` 和 `user/usys.S`。这是会丢弃镜像运行时状态的操作。

`make fmt` 对 `kernel/*.[ch]`、`user/*.[ch]` 和 `mkfs/*.c` 运行 `clang-format -i`。它不格式化汇编、链接脚本、Perl、Python 或本文档。

Makefile 只把 `fmt` 声明为 `.PHONY`。`clean`、`qemu`、`qemu-gdb`、`print-gdbport` 和 `check-qemu-version` 等命令型目标都没有声明为 phony；若仓库根目录意外出现同名普通文件，并且时间戳满足 make 的“已是最新”判断，对应 recipe 可能被跳过。

## 21. 常见失败定位

| 现象 | 优先检查 |
|---|---|
| 找不到 RISC-V GCC/binutils | 设置正确 `TOOLPREFIX`，确认 objdump 支持 RISC-V ELF |
| `_entry` 地址不对或 QEMU 立即异常 | 分别核对 MROM 固定交接地址、`_entry`、ELF `e_entry` 和 `PT_LOAD`；再检查 `kernel.ld`、`entry.o` 的真实 `.text` section、`OBJS` 顺序和实际传给 QEMU 的 ELF |
| trampoline 断言失败 | `trampsec` 内容是否超过一个页、section 是否意外合并 |
| 用户程序在 shell 中不存在 | 是否列入 `UPROGS`，`fs.img` 是否实际重建 |
| 旧源码行为仍出现 | 查看 `.d` 依赖和时间戳；汇编没有自动头文件依赖；必要时明确重建相关产物 |
| 修改 `OBJS` 顺序、flags 或工具链却看不到变化 | recipe/变量变化不使目标自动过期；强制重建 ELF 及相关对象 |
| 内核无法识别镜像 | `kernel/fs.h` 与宿主 mkfs 是否同步，是否沿用旧 `fs.img` |
| `mkfs` 失败后再次 make 却未重建 | 删除残留的部分 `fs.img`，或执行 `make -B fs.img` |
| QEMU 报 `fs.img` 写锁 | 是否已有另一个 QEMU 实例占用同一镜像 |
| GDB 无符号 | 是否加载 `symbol-file kernel/kernel`，内核是否为当前构建 |
| GDB 无法连接 | `make print-gdbport`、QEMU `-S -gdb` 参数与客户端端口是否一致 |

## 22. 变更时必须维持的不变量

1. `_entry` 的链接地址必须等于 QEMU reset stub 的直接启动交接地址；ELF `e_entry` 应独立核对并保持指向 `_entry`，不能误以为它控制当前 stub。对象顺序不能悄悄改变 `_entry` 的布局。
2. trampoline 必须页对齐、能装入一个页，并被内核/用户页表映射到约定虚拟地址。
3. `end` 必须位于所有内核静态 section 之后，物理分配器不能释放内核映像页；装载器必须按每个 `PT_LOAD` 的 `p_memsz - p_filesz` 清零 BSS 尾部。
4. 用户 ELF entry 必须落在可执行映射内；普通程序应进入 `start`，`_forktest` 的显式 `main` 入口是例外。program header 的虚拟地址、权限和大小必须能被 `kexec()` 校验和映射。
5. `user/usys.pl`、系统调用号、分派表和用户声明必须同步。
6. `kernel/fs.h` 的磁盘结构必须在内核与宿主 mkfs 间逐字节兼容。
7. superblock 各区域不能重叠，bitmap 必须覆盖所有已使用块。
8. `UPROGS` 中每个 ELF 的最终根目录名必须唯一且不超过 14 字节。
9. QEMU 的内存、hart 数、设备类型和地址必须与内核常量一致。

## 23. 推荐验证

验证构建行为时，可依次检查：

```sh
make clean
make kernel/kernel
make fs.img
make print-gdbport
make qemu
```

`make clean` 只负责删除 `fs.img`；后续构建才会生成新的初始镜像。因此在镜像中有需要保留的数据时不要执行。可用交叉 `readelf -h -l kernel/kernel` 和 `readelf -h -l user/_init` 核对入口及 program headers，用 `readelf -SW kernel/entry.o` 确认 `_entry` 所在输入 section，并用 `nm`/`objdump` 核对 `_entry`、`_trampoline`、`end` 和用户 `start` 符号。

理解这条链后，源码变更带来的问题就能被定位到四个边界之一：编译对象是否进入 ELF、链接地址是否满足运行模型、用户文件是否进入镜像、QEMU/调试器是否使用了同一组产物。
