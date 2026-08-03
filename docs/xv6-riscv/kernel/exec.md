# `exec`：ELF 装载与进程映像替换

本文说明当前仓库中 `exec` 的真实实现：用户参数如何进入内核，ELF 文件如何变成一套新的用户页表，参数如何按 RISC-V ABI 放到新栈，以及内核怎样在失败时保留旧进程映像。这里的 `exec` **不创建进程**；它在当前进程中替换用户地址空间，并保留进程身份与内核持有的资源。

相关背景可先阅读[虚拟内存](memory.md)、[进程与调度](processes-and-scheduling.md)、[系统调用](system-calls.md)和[文件系统](filesystem.md)。`fork`、`exec`、`wait` 如何组合成 shell 的程序启动路径，见[端到端 `fork -> exec -> wait`](../flows/fork-exec-wait.md)。

## 1. 职责与边界

一次成功的 `exec(path, argv)` 完成以下工作：

1. 从当前用户地址空间复制 `path`、`argv[]` 和所有参数字符串；
2. 按当前工作目录解析 `path`，锁定并读取对应 inode；
3. 检查 loader 实际支持的 ELF 条件；
4. 创建临时用户页表，装入所有 `PT_LOAD` 段；
5. 建立固定大小的用户栈和一个栈保护页；
6. 把参数字符串与 `argv[]` 指针向量复制到新栈；
7. 一次性把当前进程切换到新页表、入口 PC 和栈指针；
8. 释放旧用户地址空间。

它不负责以下事项：

- 不分配新 PID，也不改变父子关系或进程状态；
- 不关闭已打开文件，不改变当前目录；
- 不解析脚本、`PATH`、环境变量或动态链接信息；
- 不提供写时复制、按需装入可执行文件、ASLR 或自动增长栈；
- 不验证完整 ELF 规范，只实现 xv6 用户程序所需的最小子集。

因此 shell 通常先 `fork()`，再由子进程调用 `exec()`；重定向在 `exec` 前设置的文件描述符会被新程序继承。

## 2. 源码地图

| 路径 | 核心符号 | 在本机制中的职责 |
|---|---|---|
| `kernel/exec.c` | `kexec()`、`flags2perm()`、`loadseg()` | ELF 装载、栈构造、映像提交与回滚 |
| `kernel/elf.h` | `struct elfhdr`、`struct proghdr`、`ELF_MAGIC`、`ELF_PROG_LOAD` | 当前 loader 使用的 ELF64 内存布局 |
| `kernel/sysfile.c` | `sys_exec()` | 校验用户指针，把参数暂存到内核页中，再调用 `kexec()` |
| `kernel/syscall.c` | `syscall()`、`fetchaddr()`、`fetchstr()` | 系统调用分派、用户指针数组和字符串复制 |
| `kernel/proc.c` | `proc_pagetable()`、`proc_freepagetable()`、`forkret()` | 创建带 trampoline/trapframe 的页表，释放页表，以及启动首个 `/init` |
| `kernel/proc.h` | `struct proc`、`struct trapframe` | 保存 `pagetable/sz/name` 与新程序的 `epc/sp/a0/a1` |
| `kernel/vm.c` | `uvmalloc()`、`uvmclear()`、`copyout()`、`walkaddr()`、`uvmfree()` | 分配用户页、保护 guard page、写新栈和释放地址空间 |
| `kernel/fs.c` | `namei()`、`readi()`、`ilock()`、`iunlockput()` | 路径解析与 inode 内容读取 |
| `kernel/param.h` | `MAXARG`、`MAXPATH`、`USERSTACK` | 参数数量、路径长度和用户栈页数上限 |
| `user/usys.pl`、`kernel/syscall.h` | `exec` stub、`SYS_exec` | 用户 ABI 入口：`a7 = SYS_exec; ecall` |
| `user/user.ld`、`user/ulib.c`、`Makefile` | 链接布局、`start()`、`_forktest` 特例 | 产生 ELF 入口；普通程序从 `start(argc, argv)` 调用 `main()` |

`kernel/defs.h` 对外声明的是 `int kexec(char *, char **)`。前缀 `k` 用于区分内核实现和用户态 `exec()` API，是本仓库相对一些 xv6 版本的命名差异。

## 3. 两个入口与返回语义

### 3.1 普通用户系统调用

普通程序执行：

```text
user exec(path, argv)
  -> user syscall stub: a7 = SYS_exec; ecall
  -> usertrap(): epc += 4
  -> syscall(): 根据 a7 调用 sys_exec()
  -> sys_exec(): 从旧页表导入 path/argv
  -> kexec(): 构造并提交新映像
  -> syscall(): trapframe->a0 = kexec 返回值
  -> prepare_return/userret: 使用新页表、新 epc、新 sp 返回 U-mode
  -> elf.entry
       |-- 普通 _% 程序: user/ulib.c:start(argc, argv) -> main(argc, argv)
       `-- user/_forktest: main()（Makefile 使用 -e main）
```

失败时，`kexec()` 或 `sys_exec()` 返回 `-1`。`syscall()` 把 `-1` 写入旧 trapframe 的 `a0`，CPU 回到旧程序中 `ecall` 后的系统调用 stub，stub 再 `ret` 给调用者。

成功时，旧调用点不会再次执行。`kexec()` 返回 `argc`，`syscall()` 将它写入 `a0`；与此同时 `kexec()` 已把 `a1` 设为新栈中的 `argv` 地址。因此这次“系统调用返回值”同时成为新程序入口收到的第一个参数。普通用户程序的入口是 `start(argc, argv)`；stub 中原本位于 `ecall` 后的 `ret` 不会执行，因为 `epc` 已被改成 ELF 入口。

### 3.2 首个用户进程的特殊入口

本仓库的 `userinit()` 不把内嵌 initcode 复制到用户页。第一个进程首次进入 `kernel/proc.c:forkret()` 时先执行 `fsinit(ROOTDEV)`，然后直接调用：

```c
p->trapframe->a0 = kexec("/init", (char *[]){"/init", 0});
```

这里没有用户系统调用，也没有 `sys_exec()` 参数导入阶段；参数本来就是可信内核指针。`forkret()` 显式把返回的 `argc` 放入 `a0`，失败则 `panic("exec")`。随后它使用新页表直接返回 `/init` 的用户入口。

这个直接入口也说明 `kexec()` 的接口前置条件：`path` 和 `argv` 必须是可由内核直接解引用、以 NUL 结尾的数据，`argv` 必须由空指针终止，并且非空参数数目必须不超过 31。任意用户指针不能绕过 `sys_exec()` 直接传给它。

## 4. 第一阶段：把不可信参数暂存到内核

`sys_exec()` 在调用 loader 前，必须先复制旧用户地址空间中的所有输入。原因不只是防止直接解引用用户指针：成功提交后旧页表会立即被释放，loader 不能让新栈里的 `argv` 继续引用旧地址。

### 4.1 路径

`argstr(0, path, MAXPATH)` 经 `fetchstr()` 和 `copyinstr()` 把第一个系统调用参数复制到 `char path[MAXPATH]`。当前 `MAXPATH` 为 128，所以成功输入最多包含 127 个非 NUL 字节；在前 128 字节内找不到 NUL 就返回 `-1`。

路径解析发生在 `kexec()` 中：绝对路径从根 inode 开始，相对路径从当前进程的 `cwd` 开始。系统没有可执行权限位或 `PATH` 搜索；`exec("echo", ...)` 只是按普通相对路径查找当前目录中的 `echo`。

### 4.2 `argv` 指针向量

`argaddr(1, &uargv)` 只取出第二个寄存器参数的数值，不在此处判断地址合法性。随后循环通过：

```text
fetchaddr(uargv + i * 8, &uarg)
```

逐个读取 64 位用户指针。`fetchaddr()` 同时检查：

- 指针槽起始地址小于旧 `p->sz`；
- `addr + 8` 不超过旧 `p->sz`，并用两项比较覆盖整数回绕；
- `copyin()` 能从旧页表实际读取这 8 字节。

读到空指针时，内核在自己的 `argv[i]` 写入 `0` 并结束。当前 `argv` 内核数组有 `MAXARG == 32` 个槽，终止空指针也必须占一个槽，所以通过 `sys_exec()` 可成功传入的非空参数最多为 **31 个**。如果 32 个槽都不是空指针，下一轮在读取越界前失败。

### 4.3 参数字符串

每个非空用户参数对应一次 `kalloc()`，即暂时占用一个完整物理页。`fetchstr(uarg, argv[i], PGSIZE)` 要求字符串在该页大小的上限内以 NUL 终止，因此单个字符串最多 4095 个非 NUL 字节。

这不是最终参数总量上限。稍后所有字符串、对齐填充和 `(argc + 1)` 个指针还必须一起装进 `USERSTACK` 页；当前只有一页，所以通常会先触发新栈容量限制。

每个参数使用整页暂存让清理逻辑简单，但会提高成功 `exec` 前的内存峰值。`sys_exec()` 无论 `kexec()` 成功还是失败，都会遍历已分配的槽并 `kfree()`；若某次 `kalloc()` 或 `fetchstr()` 失败，`bad` 分支也会释放此前所有参数页，因此无参数页泄漏。

### 4.4 本分支的 lazy allocation 细节

`fetchaddr()` 使用 `copyin()`；当前 `copyin()` 在地址小于 `p->sz` 但页尚未映射时，可以经 `vmfault()` 补上 lazy page。因此位于合法 lazy 区间内的 `argv[]` 指针槽可能在导入过程中触发分配。

`fetchstr()` 使用的 `copyinstr()` 不会调用 `vmfault()`。所以参数字符串或路径若只处于逻辑上已由 `sbrklazy()` 扩展、但还未实际映射的页中，会失败；用户先触碰该页使其映射后才可成功。这是当前实现差异，不能从 `copyin()` 的行为推断所有用户复制函数都会补页。

## 5. 第二阶段：打开 ELF 并创建临时页表

`kexec()` 首先取得当前进程 `p = myproc()`，但不会立即修改 `p->pagetable`。其顺序是：

```text
begin_op()
  -> namei(path)
  -> ilock(ip)
  -> read ELF header
  -> proc_pagetable(p)
  -> 遍历 program headers，分配并装入 PT_LOAD 段
  -> iunlockput(ip)
end_op()
  -> 建 guard page 和 stack
  -> 复制 argv
  -> Commit to the user image
```

`begin_op()`/`end_op()` 包围路径解析、inode 引用和文件读取阶段。虽然正常 `exec` 只读取文件，`namei()`/`iput()` 所在的文件系统协议要求操作处于事务范围内；若文件在取得引用后被 unlink，最后一个 inode 引用的释放还可能截断并回收它。`begin_op()` 只在短时间持有日志 spinlock，记录 outstanding 操作后便释放，并非整个装载期间一直持有该 spinlock。`end_op()` 若结束的是最后一个 outstanding operation，则可能同步提交共享日志中的已有修改，因此也可能睡眠。

`namei()` 返回带引用但未锁定的 inode。`ilock(ip)` 获取 inode sleeplock，此后 ELF header、所有 program header 和所有段数据都在同一 inode 锁保护下读取，避免装载期间与对该 inode 的普通读写交错。磁盘 I/O 可能睡眠，因此整个函数必须运行在普通进程上下文，不能在中断上下文调用。

`proc_pagetable(p)` 创建一张空的 Sv39 根页表，并加入两项不属于低地址用户映像的固定映射：

- `TRAMPOLINE`：共享 trampoline 代码，`PTE_R | PTE_X`，无 `PTE_U`；
- `TRAPFRAME`：当前进程已有的 trapframe 物理页，`PTE_R | PTE_W`，无 `PTE_U`。

这张页表仍只在 `kexec()` 的局部变量 `pagetable` 中。当前进程继续使用旧 `p->pagetable`，所以到最终提交前的任何错误都不影响旧程序继续执行。

## 6. ELF 数据结构与实际校验范围

### 6.1 `struct elfhdr`

`kernel/elf.h` 的 `struct elfhdr` 对应 ELF64 file header。loader 实际使用的字段只有：

| 字段 | 用途 |
|---|---|
| `magic` | 必须等于 `ELF_MAGIC`，即 little-endian 的 `0x7f 'E' 'L' 'F'` |
| `entry` | 成功后写入 `trapframe->epc` |
| `phoff` | program header table 在文件中的起始偏移 |
| `phnum` | program header 数量 |

`elf[12]`、`type`、`machine`、`version`、`flags`、header 尺寸和所有 section header 字段均未解释或校验。section table 也完全不参与运行时装载。

loader 先要求 `readi()` 恰好返回 `sizeof(struct elfhdr)`，然后只比较 magic。magic 正确不代表文件一定是适用于 RV64 的静态 ELF。

### 6.2 `struct proghdr`

对每个 `struct proghdr`，代码从 `elf.phoff` 开始，以本地 `sizeof(ph)` 为步长读取；它不采用文件头里的 `phentsize`。非 `ELF_PROG_LOAD` 项直接跳过。可装载项使用：

| 字段 | 用途 |
|---|---|
| `type` | 只有值 1，即 `ELF_PROG_LOAD`，才装入地址空间 |
| `flags` | 转成页表的 X/W 权限 |
| `off` | 段的文件数据起始偏移 |
| `vaddr` | 段在用户地址空间的虚拟起点 |
| `filesz` | 从文件复制的字节数 |
| `memsz` | 段在内存中的总字节数 |

`paddr` 和 `align` 被忽略。

每个 `PT_LOAD` 项只执行以下显式校验：

1. program header 必须能完整读取；
2. `memsz >= filesz`，防止文件内容超过段内存范围；
3. `vaddr + memsz` 不能发生 64 位加法回绕；
4. `vaddr` 必须按 `PGSIZE` 对齐；
5. `uvmalloc()` 必须成功；
6. `loadseg()` 必须返回成功；对正常的 32 位 xv6 文件范围，这意味着完整读取 `filesz` 字节。

当前代码**没有**验证：

- ELF class、字节序、ABI、文件类型、machine 或 version；
- `phentsize` 是否等于 `sizeof(struct proghdr)`；
- inode 是否为普通文件；文件系统本身也没有 execute permission 检查；
- program header table 的范围是否以更宽整数安全表示；局部 `off` 是 `int`，而 `phoff` 是 `uint64`；
- `ph.off + ph.filesz` 的显式溢出和文件边界；短读通常使装载失败，但不是完整格式验证；
- 段是否按虚拟地址递增、互不重叠；
- 段终点是否低于 `MAXVA`、是否避开 `TRAPFRAME/TRAMPOLINE`；
- `entry` 是否位于一个已映射且可执行的用户页；
- 是否至少存在一个 `PT_LOAD` 段。

因此该 loader 面向由本仓库 linker 生成、存放在受控文件系统镜像中的 ELF，不是面向敌对二进制的加固解析器。一个 magic 正确但布局异常的文件通常会因短读或内存不足而在 `exec` 中返回 `-1`；若它通过现有检查但 `entry` 不可执行，则 `exec` 可以先成功，进程在首次取指时才被杀死。页表辅助函数中还存在针对内部前置条件的 `panic()`，所以不能把“任意坏 ELF 都经过完整校验并以 `-1` 安全拒绝”视为当前保证。

## 7. 段分配、权限与零填充

### 7.1 `uvmalloc()` 怎样扩展映像

`kexec()` 用 `sz` 记录目前最高的逻辑段终点。对每个 load segment：

```text
newsz = ph.vaddr + ph.memsz
sz1 = uvmalloc(pagetable, sz, newsz, flags2perm(ph.flags))
sz = sz1
loadseg(pagetable, ph.vaddr, ip, ph.off, ph.filesz)
```

`uvmalloc()` 从 `PGROUNDUP(oldsz)` 开始逐页分配，先 `memset(..., 0, PGSIZE)`，再以 `PTE_R | PTE_U | xperm` 映射。因此在本仓库 linker 产生的递增、非重叠段布局中：

- `[vaddr, vaddr + filesz)` 之后由文件内容覆盖；
- `[vaddr + filesz, vaddr + memsz)` 保持为零，实现 `.bss` 语义；
- 最后一页在 `filesz` 后的剩余字节也保持为零；
- 前一段终点到后一段起点之间的空洞会被实际分配并清零，而不是保持 unmapped。

最后一点容易被忽略：`uvmalloc()` 从 `PGROUNDUP(oldsz)` 才开始建立新页。因而前一段实际终点到其所在页末的空洞已经属于旧页，保留前一段的权限；只有从 `PGROUNDUP(oldsz)` 起新建的整页才取得**引起此次增长的当前段权限**。当前 linker 生成按地址递增的常规 text/data 段，并把 `.data` 起点页对齐，所以这种简化可工作；对乱序、重叠或以不同方式对齐的段，loader 没有定义可靠的权限合并规则。若 `newsz < sz`，`uvmalloc()` 直接返回旧 `sz`，`loadseg()` 可能覆盖已有映射，而不会更新那些页的权限。

### 7.2 ELF flags 到 PTE 的映射

`flags2perm()` 的当前映射为：

| ELF flag | 传给 `uvmalloc()` 的额外 PTE 位 |
|---|---|
| execute bit `0x1` / `ELF_PROG_FLAG_EXEC` | `PTE_X` |
| write bit `0x2` / `ELF_PROG_FLAG_WRITE` | `PTE_W` |
| read bit `0x4` / `ELF_PROG_FLAG_READ` | 无额外位 |

无论 ELF 是否声明 read，`uvmalloc()` 都会加入 `PTE_R | PTE_U`。所以所有 load segment 都可被用户读取；W 和 X 才由 ELF flags 决定。若 ELF 同时声明 W 和 X，当前代码会产生用户可读、可写、可执行页，没有 W^X 策略。

只读代码页仍能被 loader 写入，因为 `loadseg()` 不通过用户虚拟写权限复制数据。它用 `walkaddr()` 找到物理页，再调用 `readi(ip, 0, pa, ...)`，其中 `user_dst == 0` 表示目标是可直接写的内核地址。装载完成后，用户态和 `copyout()` 才受 `PTE_W` 约束。

### 7.3 `loadseg()` 的前置条件

`loadseg(pagetable, va, ip, offset, sz)` 要求：

- `va` 页对齐；
- `[va, va + sz)` 涉及的页已由 `uvmalloc()` 映射；
- inode sleeplock 仍由调用者持有；
- `offset` 和 `sz` 表示可由 `readi()` 完整读取的文件范围。

它按页迭代，通过 `walkaddr(pagetable, va + i)` 取得物理页基址，每次读取 `min(PGSIZE, sz - i)` 字节。已分配页找不到时不是普通输入错误，而是 loader 内部前置条件被破坏，所以代码执行 `panic("loadseg: address should exist")`。文件短读则返回 `-1`，交给外层统一回滚。

注意 `loadseg()` 的 `offset` 和 `sz` 形参是 32 位 `uint`，而 ELF program header 的 `off/filesz` 是 64 位。当前 xv6 inode 的 `size` 和文件偏移本来也使用 32 位 `uint`，适合小型文件系统，但不能据此宣称支持任意 ELF64 大文件。

## 8. 用户地址空间最终布局

所有段装入后，`sz = PGROUNDUP(sz)`。`kexec()` 再一次调用 `uvmalloc()`，分配 `(USERSTACK + 1)` 页，权限参数为 `PTE_W`。由于 `uvmalloc()` 自动加入 `PTE_R | PTE_U`，新页最初都是用户可读写、不可执行。

当前 `USERSTACK == 1`，布局为：

```text
低地址
0
| ELF text / rodata             PTE_R | PTE_U | [PTE_X]
| ELF data / bss / 段间填充      PTE_R | PTE_U | [PTE_W/PTE_X]
| 对齐到下一页
+-----------------------------+  guard = PGROUNDUP(last segment end)
| stack guard page            |  PTE_V | PTE_R | PTE_W，但无 PTE_U
+-----------------------------+  stackbase
| one user stack page         |  PTE_V | PTE_R | PTE_W | PTE_U
| argv vector and strings     |
+-----------------------------+  p->sz，初始 sp 从这里向下生长

... 未映射 ...

TRAPFRAME                       supervisor-only，指向 p->trapframe
TRAMPOLINE                      supervisor-only，共享 trampoline 代码
高地址
```

`uvmclear(pagetable, guard)` 只清除 guard PTE 的 `PTE_U`，不会撤销映射或释放物理页；页表中仍保留一个 valid leaf PTE，但任何用户取指、读写都会 fault。在刚完成 exec 的布局中，缺页处理的 `ismapped()` 看到该页已有 `PTE_V` 后不会把它当成 lazy hole 重新映射。

guard page 仍计入 `p->sz`，所以 `p->sz` 是低地址映像的逻辑顶端，不表示 `[0, p->sz)` 每个字节都可由用户访问。页表权限才是最终访问判据。

exec 刚返回时，用户栈固定为一页，不会按 fault 自动向下增长；命中 guard page 会产生权限 fault，尚未通过 `sbrk()` 增长的栈顶上一页则未映射。保护区只有一页；若错误的栈指针一次跨过整页 guard，落入更低且本来可访问的程序/填充页，硬件不会把这次访问识别为栈溢出。

这不是进程余生都保持的布局不变量。当前 `sbrk()` 没有记录或检查 exec 建立的 heap/stack/guard 下界：负增长可以把栈页和 guard 页一起解除映射，随后 eager 或 lazy 正增长又能把原 guard 地址建立为普通用户页；正增长也会从原栈顶继续向高地址建立 heap 页。因此“栈顶上一页不可访问”和“原 guard 永远不可重新映射”都必须限定为尚未用 `sbrk()` 跨越或重建该区域的初始 exec 布局。

## 9. 参数栈与 RISC-V ABI

`sp` 初始设为新栈顶 `sz`，`stackbase = sp - USERSTACK * PGSIZE`。参数构造分两步。

### 9.1 复制字符串

loader 按 `argv[0]`、`argv[1]` 到 `argv[argc-1]` 的顺序处理每个内核暂存字符串：

1. `sp -= strlen(argv[i]) + 1`，为字符串和 NUL 留空间；
2. `sp -= sp % 16`，向下对齐到 16 字节；
3. 若 `sp < stackbase`，失败并回滚；
4. `copyout(newpagetable, sp, argv[i], len + 1)`；
5. 在内核数组 `ustack[i]` 记录该用户虚拟地址。

每个字符串起点都因此为 16 字节对齐，字符串间可能有未使用填充。`copyout()` 先要求目标映射有效且带 `PTE_U`，再检查 `PTE_W`；所以它不会静默写入只读段或 guard page。

当前 `copyout()` 在目标页缺失时有一条面向当前进程 lazy allocation 的 `vmfault()` fallback，但 `exec` 传给它的是尚未安装的临时 `pagetable`，而 `vmfault()` 最终映射的是当前 `p->pagetable`。因此栈复制的正确性依赖前面的 `uvmalloc()` 已经 eager 地映射所有目标栈页；不能删掉该分配并指望 `copyout()` 为临时页表补页。正常 `exec` 路径不会进入这条 fallback。

### 9.2 复制指针向量

字符串全部完成后，内核设置 `ustack[argc] = 0`，再执行：

```text
sp -= (argc + 1) * sizeof(uint64)
sp = align_down(sp, 16)
copyout(newpagetable, sp, ustack, (argc + 1) * 8)
```

最终布局从低地址到高地址大致为：

```text
final sp -> argv[0] pointer
            argv[1] pointer
            ...
            argv[argc - 1] pointer
            NULL
            alignment padding
            argv[argc - 1] string + '\0'
            ...
            argv[1] string + '\0'
            argv[0] string + '\0'
            padding
stack top = p->sz
```

因为 `argv[0]` 最先从栈顶向下复制，它通常位于最高的字符串地址；程序不依赖字符串在内存中的排列顺序，只依赖指针向量的顺序。

新程序入口寄存器为：

| 状态 | 值 |
|---|---|
| `trapframe->epc` | `elf.entry` |
| `trapframe->sp` | 16 字节对齐的最终 `sp`，也就是 `argv[]` 起点 |
| `trapframe->a0` | `argc`，由 `syscall()` 或首进程的 `forkret()` 写入 |
| `trapframe->a1` | 最终 `sp`，即 `argv` 用户地址 |

栈上没有另压一份 `argc`、环境指针、auxiliary vector 或伪返回地址。普通用户程序由 linker 选择 `user/ulib.c:start(int argc, char **argv)` 作为入口；它直接按 RISC-V 调用约定接收 `a0/a1`，调用 `main()`，若 `main()` 返回再调用 `exit()`。`Makefile` 对 `user/_forktest` 使用 `-e main -Ttext 0`，它是直接进入 `main` 的仓库特例，不经过 `start()`。

空参数表 `argv[0] == 0` 是允许的，此时 `argc == 0`，栈上只有一个空指针。loader 也不要求 `argv[0]` 等于 `path`；进程调试名来自路径 basename，而不是参数零。

## 10. 提交点：`Commit to the user image`

在 ELF、栈、字符串和指针向量全部成功后，代码才进入 `kernel/exec.c` 中标记为 `Commit to the user image` 的区域：

```text
oldpagetable = p->pagetable
p->pagetable = pagetable
p->sz = sz
p->trapframe->epc = elf.entry
p->trapframe->sp = sp
proc_freepagetable(oldpagetable, oldsz)
return argc
```

此前还设置了 `trapframe->a1 = sp`，并把 `path` 最后一个 `/` 后的 basename 复制到 `p->name`。从这两项修改开始到释放旧页表之间没有可能返回错误的操作；它们不会产生“参数寄存器已改但 loader 又报告失败”的路径。

提交后不会再尝试回滚。`proc_freepagetable()`：

1. 解除旧页表中的 `TRAMPOLINE` 和 `TRAPFRAME` 映射，但不释放这两个物理对象；
2. 释放旧低地址用户物理页；
3. 递归释放旧页表页。

trapframe 物理页属于进程本身而不是旧用户映像，新页表已把同一个 trapframe 重新映射到固定地址。trampoline 代码也由所有进程共享。因此释放旧页表不会破坏新返回路径。

提交发生在 S-mode、内核页表和进程内核栈上，硬件 `satp` 此时并不指向被释放的旧用户页表。普通系统调用随后由 `userret` 写入新用户 `satp`，并在切换前后执行 `sfence.vma`；首进程的 `forkret()` 也把新页表转换为 `satp` 后调用同一个 `userret`。所以不需要先在当前 CPU 上继续运行旧用户映像，也不会带着旧用户 TLB 项进入新程序。

成功 `exec` 保留：

- `pid`、`parent`、`state`、`killed` 等进程身份和调度状态；
- 内核栈、`struct proc` 和 trapframe 对象本身；
- `ofile[]` 中的所有打开文件、管道和设备；
- `cwd`；
- 与父进程的等待/退出关系。

成功 `exec` 替换或更新：

- `p->pagetable` 根页表和整个低地址用户映像（高地址 trapframe/trampoline 映射被重建），以及 `p->sz`；
- 用户 PC、SP、`a0/a1` 启动参数；
- `p->name` 调试名称。

当前实现不会清零 trapframe 的所有其他用户寄存器；它只覆盖新程序启动所需的字段以及系统调用返回值。新程序不能把未指定寄存器值当作 ABI 输入。

## 11. 两阶段替换与失败回滚

从资源所有权看，`exec` 是“构造临时对象，最后交换”的两阶段替换：

```text
旧页表仍有效
    |
    +-- 导入参数到内核页
    +-- 构造 temp pagetable
    +-- 装 ELF、建栈、复制 argv
    |
    +-- 任一步失败 --> 释放 temp/参数/inode；旧页表继续有效
    |
    `-- 全部成功 --> p->pagetable = temp；释放旧页表
```

### 11.1 `kexec()` 的 `bad` 分支

| 失败发生位置 | 当时可能持有的资源 | 清理动作 |
|---|---|---|
| `namei()` 失败 | 一个 outstanding FS operation | 直接 `end_op()` 并返回 `-1` |
| header/格式/页表/段失败 | inode 引用和 sleeplock、FS operation、部分临时页表 | `proc_freepagetable(temp, sz)`，再 `iunlockput(ip)`、`end_op()` |
| inode 已释放后的栈或 `copyout()` 失败 | 完整/部分临时页表；`ip == 0` | 只释放临时页表，不重复 `end_op()` |
| 全部成功 | 新旧两张页表 | 先安装新页表，再释放旧页表 |

正常读完所有段后，代码先 `iunlockput(ip)`、`end_op()`，再设 `ip = 0`。这个哨兵让后续栈错误共用同一个 `bad` 标签而不会二次解锁 inode 或结束事务。

`uvmalloc()` 自己也有局部回滚：若增长中途分配用户页或 `mappages()` 失败，它通过 `uvmdealloc()` 释放本次调用已成功映射的用户 leaf 页，再返回 0。`walk()` 在失败前可能已经创建了空的中间页表节点；这些节点不由 `uvmdealloc()` 单独回收，而会在外层 `proc_freepagetable()` 的 `freewalk()` 中释放。外层 `sz` 仍表示上一次完整成功的边界，所以清理不依赖一个半完成的新边界。

### 11.2 `sys_exec()` 的清理

`sys_exec()` 的每个 `argv[i]` 页仍由该函数拥有，`kexec()` 只读取它们并把内容复制到新用户栈。无论 loader 返回 `argc` 还是 `-1`，`sys_exec()` 随后都会释放这些暂存页。成功时新栈已有独立副本，因此释放不会留下悬空用户指针。

### 11.3 失败后可依赖的不变量

- `p->pagetable`、`p->sz`、旧 PC/SP 和旧用户内存仍对应同一旧映像；但导入 `argv[]` 时，`copyin()` 可能已把其中一个旧 lazy page 物化，这不改变旧程序的逻辑地址空间或数据语义；
- `p->name` 尚未改变，因为所有可能失败的步骤都位于名称更新之前；
- 打开文件和 `cwd` 无论成功失败都不变；
- 临时页表、临时用户页、inode 引用、inode 锁和 FS operation 均有唯一清理路径；
- 普通 `sys_exec()` 路径返回给旧程序的错误只有 `-1`，不提供区分“文件不存在”“坏 ELF”或“内存不足”的 errno。若进程同时已被 kill，`usertrap()` 会在系统调用后退出而不返回用户态；首进程直接调用失败则由 `forkret()` panic。

两阶段替换的代价是峰值内存：旧地址空间必须一直保留，同时存在新地址空间和最多 31 个参数暂存页。系统可能在“释放旧映像后本可装得下”的情况下仍让 `exec` 返回 `-1`；这换取了失败时可继续运行旧程序的语义。

## 12. 锁、睡眠与并发

### 12.1 持锁关系

- `begin_op()` 可能因日志空间或正在提交而睡眠；它返回后只增加 outstanding 计数，不持续持有 `log.lock`。配对的 `end_op()` 若触发日志提交，也可能执行磁盘 I/O 并睡眠。
- `namei()` 在逐级路径解析时短暂获取目录 inode sleeplock。
- ELF inode 从 `ilock(ip)` 到所有段读取结束始终保持 sleeplock；`readi()` 的前置条件就是调用者持有该锁。
- 磁盘读取、buffer cache 等下层操作可能睡眠。
- `kalloc()`/`kfree()` 短暂获取物理页分配器的 spinlock；`walk()`、`mappages()` 等页表遍历本身不加锁，因为临时页表由当前调用独占。`kexec()` 不跨文件 I/O 持有物理页分配器锁。
- `kexec()` 不获取 `p->lock`。`pagetable`、`sz`、trapframe 和 name 在当前运行进程中属于私有字段；持有 `p->lock` 跨越可睡眠文件 I/O 反而违反调度协议。

### 12.2 为什么不需要锁住旧地址空间

调用 `exec` 的用户线程已经陷入内核，同一 xv6 进程没有多个用户线程，不会有另一个执行流同时修改该进程的用户页表。时钟中断可以让当前进程被抢占，恢复后仍是同一个 `struct proc`；临时页表只由当前内核调用栈持有，其他进程不可见。

提交的几次字段写并不是硬件原子事务，但不存在第二个线程并发运行同一进程。系统中不按锁读取调试字段的代码最多可能短暂观察到更新中的 `name/pagetable/sz`，不能据此建立同步语义。

inode sleeplock保证一次装载过程中 header、program headers 和段字节相互一致。它不提供跨多次独立文件写操作的版本化快照，也没有“执行中的文件不可写”规则；锁释放后，磁盘文件可再被修改，而已经复制到用户物理页的映像不受影响。

### 12.3 killed 状态

`kexec()` 本身不轮询 `p->killed`。如果进程在可睡眠的装载过程中被 kill，系统调用可以先完成；返回 `usertrap()` 后的 killed 检查会按 trap 路径终止进程。不能假定 kill 一定在 ELF 构造中途立即取消 I/O。

## 13. 核心不变量

审阅或修改实现时，应保持以下可检查陈述：

1. 在所有可能失败的分配、读取和 `copyout()` 完成前，`p->pagetable` 必须仍指向旧页表。
2. 临时页表始终由当前 `kexec()` 独占；成功提交转移给 `p`，失败则由 `bad` 分支释放，二者只能发生其一。
3. `proc_pagetable()` 创建的新表必须同时映射当前 trapframe 和 trampoline，否则用户 trap 返回链不完整。
4. `loadseg()` 开始前，目标虚拟页必须存在并带 `PTE_U`；找不到页表示内核自身的构造不变量被破坏。
5. 对正常递增、非重叠的 ELF 段，文件字节只覆盖 `filesz`，其余 `memsz` 依赖 `uvmalloc()` 的先清零行为实现 BSS。
6. exec 提交时的 guard page 必须有有效映射但没有 `PTE_U`；在该初始布局未被 `sbrk()` 删除或重建时，lazy fault 不能直接给这个有效 PTE 重新赋予用户权限。
7. 最终 `sp` 和 `argv` 基址必须 16 字节对齐，`argv[argc]` 必须为 0。
8. 最终 `sp >= stackbase`；参数字符串、填充和指针向量都不能越过 guard page。
9. 成功返回的 `argc` 必须由调用层写入 trapframe `a0`；`kexec()` 自身负责 `a1/epc/sp`。
10. 释放旧页表时不得释放共享 trampoline 或仍被新页表引用的 trapframe 物理页。
11. 任何失败都不得关闭 `ofile[]` 或改变 `cwd`；在未被 kill 的普通系统调用路径中，旧程序必须能继续处理 `exec == -1`。

## 14. 当前实现的限制与容易误读之处

### 14.1 不是完整 POSIX `execve`

用户接口只有 `exec(path, argv)`：没有 `envp`、权限/凭据转换、set-id、close-on-exec、信号 disposition、线程收敛或 errno。系统也没有动态 linker，因此用户程序必须是链接到固定虚拟地址的静态 ELF。

### 14.2 参数限制有三层

1. `sys_exec()` 的 32 个指针槽包含终止 NULL，因此普通调用最多 31 个非空参数；
2. 每个字符串必须在 4096 字节内终止；
3. 所有字符串、逐字符串 16 字节对齐填充、指针向量和最终对齐必须装进一页栈。

shell 自己的 `user/sh.c:MAXARGS == 10` 还会施加更小的前端限制；它为终止 NULL 留槽，所以交互命令通常最多 9 个参数。这个 shell 限制不是内核 ABI。

`kexec()` 内部的 `ustack` 也只有 `MAXARG` 个槽，但其数量检查位于循环体内，终止 NULL 则在循环后写入。`sys_exec()` 已保证最多 31 个非空参数，所以普通系统调用路径安全；新的内核直接调用者也必须保持这个前置条件。若直接传入恰好 32 个非空参数再加 NULL，当前实现会执行越界的 `ustack[32] = 0`，不能依赖 `kexec()` 独立拒绝该输入。

### 14.3 loader 假定规范链接布局

代码要求每个 load segment 的 `vaddr` 页对齐，并隐含假定 program headers 按地址递增且不重叠。段间空洞被映射，ELF read flag 被忽略，W/X 可以共存，entry 也不验证。外部工具生成的常规 ELF 即使 magic 正确，也可能不符合这些更窄的假设。

### 14.4 程序映像是 eager，堆可以 lazy

所有 ELF 段和初始栈在 `exec` 时立即分配物理页并读取；这里没有 file-backed page fault。执行后的 `sbrklazy()` 才能形成逻辑存在、尚未映射的堆页。释放旧映像时，当前 `uvmunmap()` 能跳过不存在的 lazy 页，所以 `proc_freepagetable(oldpagetable, oldsz)` 可处理带洞的旧地址空间。

“能跳过”只表示不会因无效 PTE panic，不表示按已映射页建立稀疏索引。成功路径在交换 `p->pagetable/p->sz` 后、返回调用者前同步扫描旧 `[0, oldsz)`，因而旧地址空间即使只物化少数页，清理仍可花费 `O(oldsz/PGSIZE)`；新的程序已经成为进程身份，但系统调用尚未返回。这也是两阶段替换除峰值内存之外的延迟代价。

### 14.5 进程名只是调试信息

`p->name` 取 `path` 最后一个 `/` 后的部分，经 `safestrcpy()` 截断到 15 个字符加 NUL。它不影响文件身份、`argv[0]` 或进程权限；主要用于 panic、未知系统调用和 `procdump()` 输出。

## 15. 验证与调试

### 15.1 静态检查 ELF

先构建用户程序，再检查 ELF header、入口和 load segments：

```sh
make user/_echo
riscv64-linux-gnu-readelf -h -l user/_echo
```

若工具链前缀不同，使用 Makefile 自动选择的对应 `readelf`。应重点核对：

- magic/class/machine 与预期工具链一致；
- 普通程序的 `Entry point address` 对应 `user/ulib.c:start`；`user/_forktest` 应对应 `main`；
- `LOAD` 段的 `VirtAddr` 页对齐；
- text 段通常为 `R E`，data/bss 段通常为 `RW`；
- `MemSiz >= FileSiz`，BSS 体现为两者差值。

可结合符号表确认入口：

```sh
riscv64-linux-gnu-readelf -s user/_echo | rg ' start$'
```

### 15.2 正常与边界测试

启动 xv6 后可逐项运行：

```text
usertests exectest
usertests bsstest
usertests copyinstr2
usertests copyinstr3
usertests bigargtest
usertests stacktest
usertests nowrite
usertests badarg
usertests execout
```

这些测试分别覆盖：

| 测试 | 主要断言 |
|---|---|
| `exectest` | 成功替换映像，且 `exec` 前重定向的 fd 1 被新 `echo` 继承 |
| `bsstest` | `memsz - filesz` 对应的未初始化数据为零 |
| `copyinstr2` | 过长路径和超过单参数暂存页的字符串被拒绝 |
| `copyinstr3` | 跨越最后有效用户页且无 NUL 的路径不会越界读取 |
| `bigargtest` | 参数总量超过一页栈时失败，旧子进程仍能继续执行 |
| `stacktest` | 栈下方 guard page 的用户读取触发 fault 并杀死子进程 |
| `nowrite` | text、trampoline、trapframe 和非法高地址不可由用户写 |
| `badarg` | 大量无效参数字符串指针不会泄漏 `sys_exec()` 暂存页 |
| `execout` | 几乎耗尽内存并逐步释放少量页后反复尝试 exec；主要断言是内核不 panic，且全局页数检查可发现最终持续漏页；该项属于 slow tests |

`execout` 的父进程使用 `wait(0)` 且忽略子状态，所以它不能证明每个内存额度下 exec 成功，也不能逐一证明所有中间失败点都正确回滚。`usertests` 在每轮前后用可分配页数检测明显物理页泄漏，因此整体成功能支持“没有最终持续页泄漏”，但不能定位某一次尝试的具体回滚行为。

### 15.3 GDB 观察点

用 `make qemu-gdb` 启动后，在另一个终端连接仓库生成的 `.gdbinit`。建议断点：

```gdb
b sys_exec
b kexec
b loadseg
b kernel/exec.c:133
```

最后一个断点应落在源码注释 `Commit to the user image` 附近；行号会随源码变化，若失效应在 `kexec()` 内按注释重新定位。适合观察的值包括：

```gdb
p *p
p/x elf.entry
p/x ph.vaddr
p/x ph.filesz
p/x ph.memsz
p/x ph.flags
p/x sz
p/x sp
p/x stackbase
p argc
p/x p->pagetable
p/x pagetable
```

在提交前，`p->pagetable` 与局部 `pagetable` 应不同；越过赋值后两者相同。提交前故意让 `bigargtest` 触发失败，应看到 `p->pagetable` 从未被替换，并从 `bad` 返回旧程序。调试 `loadseg()` 时，`walkaddr(pagetable, va + i)` 应非零，最后一次读取的 `n` 可能小于一页。

### 15.4 接口复核

修改 loader、ELF 结构、栈页数或参数限制后，应重新检查 `kernel/exec.c`、`kernel/elf.h`、`kernel/sysfile.c`、`kernel/proc.c` 和 `kernel/vm.c` 的接口是否改变。静态复核不能替代上述运行测试，尤其不能证明恶意 ELF 一定被安全拒绝。

## 16. 修改时的审阅清单

- 新增 ELF 校验时，失败是否仍发生在页表提交前？
- 改 `struct elfhdr`/`struct proghdr` 时，是否仍与磁盘 ELF64 字节布局和工具链一致？
- 改段权限时，是否同时考虑 `uvmalloc()` 固定加入的 `PTE_R | PTE_U` 和段间空洞？
- 改 `USERSTACK` 或对齐规则时，`stackbase`、guard page 和 `bigargtest/stacktest` 是否同步？
- 改 `MAXARG` 时，是否给终止 NULL 留槽，并检查 `ustack[argc]` 不越界？
- 改 `copyin/copyinstr/vmfault` 时，路径、指针槽和参数字符串的 lazy-page 行为是否仍一致？
- 在提交区域新增任何可能失败的操作时，是否补上真正可行的旧页表回滚？
- 改页表释放时，是否保留 trapframe 和共享 trampoline 的物理所有权？
- 新增 close-on-exec 等语义时，是否明确其与失败回滚、fd 引用计数的提交顺序？
