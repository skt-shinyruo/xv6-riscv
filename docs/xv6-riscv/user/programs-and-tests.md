# 用户程序与测试：工具、压力负载和宿主驱动

本文覆盖当前镜像中的用户工具、独立压力程序、`user/usertests.c` 测试框架以及宿主侧 `test-xv6.py`。这些代码既是 xv6 API 的使用示例，也是对进程、VM、文件系统、pipe、`exec` 和崩溃恢复不变量的可执行说明。

源码范围：`user/cat.c`、`user/echo.c`、`user/grep.c`、`user/wc.c`、`user/ls.c`、`user/kill.c`、`user/ln.c`、`user/mkdir.c`、`user/rm.c`、`user/forktest.c`、`user/zombie.c`、`user/stressfs.c`、`user/logstress.c`、`user/forphan.c`、`user/dorphan.c`、`user/grind.c`、`user/usertests.c` 和 `test-xv6.py`。

用户 ABI、启动和 heap 见[用户 ABI 与运行库](runtime-and-abi.md)；shell 如何组合这些程序见[`init` 与 shell](init-and-shell.md)。

## 1. 程序怎样进入镜像

`Makefile:UPROGS` 显式列出全部用户 ELF。宿主构建名形如 `user/_cat`，`mkfs` 写入 `fs.img` 时去掉 `user/` 和 `_`，所以 shell 中执行 `cat`、`usertests` 或 `grind`。源码存在但未列入 `UPROGS` 的程序不会自动出现在 xv6 根目录。

除 `_forktest` 外，各程序静态链接 `ulib.o`、`usys.o`、`printf.o` 和 `umalloc.o`。没有 PATH 搜索、动态库或环境变量；shell 初始 cwd 为 `/`，因此简单名字恰好能找到根目录 ELF。

## 2. 小工具共同约定

这些工具采用简化命令行语义：

- 成功通常 `exit(0)`，明显使用错误通常 `exit(1)`；但部分工具打印操作失败后仍以 0 退出。
- 诊断有的写 fd 2，有的用 `printf` 写 fd 1，并不完全统一。
- 没有 errno，系统调用失败只得到 -1。
- 不处理短写重试的程序必须按其源码限制理解，不能当作完整 Unix 工具。
- shell 不展开通配符，因此 `rm *` 不会得到文件列表。

下面逐项说明实际算法和边界。

## 3. `cat`

`user/cat.c:cat(fd)` 使用一个 512 字节全局缓冲区：

```text
while read(fd, buf, 512) > 0:
  require write(1, buf, n) == n
if final read < 0:
  report error
```

无文件参数时读取 fd 0，适合作为管道右端；有多个路径时依次打开、复制、关闭，输出无分隔。任一 open/read/write 失败立即以状态 1 退出，不再处理后续文件。它把短写视为错误而不循环补写，这在当前普通文件、console 和不超过 pipe buffer 的块上通常成立，但不是通用 POSIX 写法；当前读块大小正好是 `PIPESIZE=512`。

## 4. `echo`

`user/echo.c` 依次 `write(1, argv[i], strlen(argv[i]))`，参数间写一个空格，最后一个参数后写换行。它不解释 `-n`、反斜杠或转义，也不检查 write 返回值。

一个细节是：没有参数时循环完全不执行，因此不会输出通常 Unix `echo` 的空行。这是当前代码语义。

## 5. `grep` 的流式分行

`user/grep.c:grep(pattern, fd)` 使用 1024 字节缓冲区，并保留上次 read 后没有换行的尾部：

```text
m = retained bytes
read into buf+m, leaving one byte for NUL
scan complete '\n'-terminated lines
temporarily replace newline with NUL for regex match
restore newline and write matching original line
move unconsumed suffix to start; repeat
```

无文件参数时读 stdin；有多个文件时依次处理。open 失败立即退出。write 返回值和最终 read 错误没有检查。

当前实现只在找到 `\n` 时处理一行，因此 EOF 前最后一行若没有换行会被丢弃。长度填满缓冲区仍没有换行时，下次 read 的长度变为 0，也会静默丢弃该超长行。

### 5.1 正则匹配器

匹配器来自 Kernighan & Pike，只支持：

| 语法 | 语义 |
|---|---|
| `^` | 仅在模式首字符时锚定文本开头 |
| `$` | 仅在模式尾部时锚定文本结尾 |
| `.` | 任意单字符 |
| `c*` | 零个或多个字符 c；`.*` 同理 |

`match()` 对无 `^` 模式从文本每个位置尝试 `matchhere()`，包括空字符串位置。`matchhere()` 先处理模式结束、`*`、尾部 `$` 和普通单字符递归。`matchstar()` 从零次匹配开始尝试剩余模式，再逐字符扩展；这是简洁的回溯实现，没有字符类、转义、分组或 alternation，病理模式可能产生大量递归尝试。

## 6. `wc`

`user/wc.c:wc(fd, name)` 以 512 字节块扫描，维护：

- `c`：每个输入字节加一；
- `l`：遇到 `\n` 加一；
- `inword`：当前是否位于非空白序列；
- `w`：从空白进入非空白时加一。

空白集合是空格、回车、制表、换行和纵向制表。输出为 `lines words bytes name`。stdin 模式的 name 是空字符串。read 失败以状态 1 退出；多文件模式不计算合计行。

## 7. `ls`

`user/ls.c` 首先 open + fstat 路径：

- 普通文件或设备：打印格式化 basename、type、inode number、size。
- 目录：直接以 `read(fd, &de, sizeof de)` 读取原始 `struct dirent` 序列。

目录项 `inum == 0` 被跳过。代码把定长 14 字节名字复制到拼接路径并加 NUL，再对每项调用 `stat()` 取得类型与大小。`fmtname()` 找最后一个 `/`；短名字用空格填充到 `DIRSIZ`，长名字直接返回原路径尾指针。其内部短名 buffer 是静态的，调用结果不能长期保存，但每次只立即用于一次 `printf`。

拼接缓冲区固定 512 字节，`strlen(path) + 1 + DIRSIZ + 1` 超限时拒绝。目录读取出错或出现非整条 dirent 时循环结束但不报告。默认参数是 `.`，所以依赖 cwd。

## 8. 简单控制工具

| 文件 | 行为 | 边界 |
|---|---|---|
| `user/kill.c` | 对每个参数执行 `kill(atoi(arg))` | 忽略 kill 返回值；非数字经简化 `atoi` 可能成为 PID 0 |
| `user/ln.c` | 要求 `old new`，调用 `link()` | link 失败会打印，但仍 `exit(0)` |
| `user/mkdir.c` | 依次 `mkdir(argv[i])` | 首次失败后停止，但仍 `exit(0)`；无参数才返回 1 |
| `user/rm.c` | 依次 `unlink(argv[i])` | 首次失败后停止，但仍 `exit(0)`；无递归/强制选项 |

因此不能仅凭 shell 的退出状态判断 `ln`/`mkdir`/`rm` 的每个操作是否成功，必须观察诊断或随后检查文件系统。

## 9. `forktest`：进程表耗尽必须可恢复

`user/forktest.c` 最多尝试 1000 次 fork。每个子进程立即退出，父进程在 fork 首次返回负数时停止创建，再执行同样次数的 wait：

```text
fork until allocation fails
wait exactly n exited children
require one additional wait == -1
```

测试验证 `kfork()` 在固定 `NPROC` 耗尽时返回错误而不 panic，已经创建的子进程仍能被完整回收，且 `wait()` 不多不少。

最后一个断言失败时会打印源码中的 `wait got too many`。因此该测试不仅要求前 `n` 次 wait 成功，还要求第 `n+1` 次确实返回 -1；只观察“fork 终于失败”不足以判定通过。

它在这些 wait 之后直接打印成功并退出，没有再执行一次 fork。因此它能证明已创建 child 都进入了回收路径，却没有用同一进程直接证明“刚释放的 proc 槽立刻可重新分配”；后续程序仍能 fork 只是间接覆盖。若把槽复用作为验收目标，应在回收一个 child 后立即 fork 并结合 proc/OOM 计数确认失败原因。

Makefile 为 `_forktest` 使用更小的特殊链接规则，只链接 `forktest.o + ulib.o + usys.o`，避免程序映像过大让物理内存先于进程表成为限制。

## 10. `zombie`

`user/zombie.c` fork 后，父进程 `pause(5)`，子进程立即 exit。暂停窗口让子进程进入 ZOMBIE，便于调试观察。父进程之后也 exit，并未主动 wait；内核退出/reparent 逻辑与 `init` 的 wait 最终负责回收。

该程序不是自动断言测试，它主要制造一个短暂状态供 `ps` 类工具或 GDB 观察。

## 11. `stressfs`

`user/stressfs.c` 通过 fork 链形成五个进程，分别令循环变量为 0 到 4，并操作 `stressfs0` 到 `stressfs4`。每个进程：

1. 创建/打开自己的文件。
2. 连续写 20 个 512 字节块。
3. 关闭后重新只读打开。
4. 连续读 20 个 512 字节块。
5. 调用一次 wait；有子进程的链节点等待下一个节点，最末节点的 -1 被忽略。

多个进程使用不同 inode，但竞争日志、buffer cache、VirtIO 队列和磁盘中断。代码不检查 open/read/write 结果，因此它更像并发负载发生器，而不是严格自校验测试。开头关于 IDE queue 的注释来自更老驱动背景；当前仓库实际使用 VirtIO，不能照注释推断当前实现结构。

## 12. `logstress`

`user/logstress.c` 为每个命令行文件名 fork 一个子进程。子进程打开独立文件，然后执行 250 次、每次请求 2000 字节的 write；父进程 wait 所有孩子并传播非零状态。2000 字节小于当前 `filewrite()` 的 3072 字节 chunk 上限，所以每次 write 通常只建立一组 `begin_op()/end_op()`；压力来自多进程的这些操作反复并发、共享日志容量并被 group commit 合并，而不是单次 2000 字节写被主动分片。

当前源码有一个必须明确记录的 C 越界：

```text
#define BUFSZ 500
char buf[BUFSZ]
enum { N = 250, SZ = 2000 }
memset(buf, ..., SZ)
write(fd, buf, SZ)
```

`memset` 和 write 的源区都超过 500 字节对象边界，属于未定义行为；在当前简单地址空间布局中可能碰巧访问后续映射内存，但它不是内存安全的测试实现。评估日志正确性时不要把由该越界造成的异常误归因于内核日志。

另一个细节是子进程内部复用了外层变量 `i` 作为 250 次写循环计数；fork 后地址空间独立，所以不会影响父进程继续遍历参数。

## 13. `forphan` 与 `dorphan`

这两个程序不是单独运行即可判定成功的测试，而是为宿主强制崩溃制造持久状态。

`user/forphan.c` 的实际删除调用是 `unlink(ff)`，其中 `ff == "file0"`：

```text
open("file0", O_CREATE|O_WRONLY)
fstat to remember inode number
unlink("file0")
verify pathname can no longer open
keep fd open forever with pause loop
```

inode 此时 `nlink == 0`，但打开 file 维持内存引用。宿主杀死整个 QEMU 后内存引用消失，重启必须由 `ireclaim()` 释放它。

`user/dorphan.c`：

```text
mkdir("dd")
chdir("dd")                    cwd holds inode reference
unlink("../dd")                remove directory name
pause forever
```

空目录因 cwd 引用在 unlink 后仍存活；崩溃后同样留下 type 非零、nlink 为 0 的 dinode。两者分别验证 open-file orphan 和 cwd orphan。

## 14. `grind` 的随机并发模型

`user/grind.c` 是无限运行的系统调用混合压力测试。`do_rand()` 实现 Park-Miller 31 位伪随机算法，通过拆分乘法避免溢出；两个 worker 对初始 seed 分别异或 31 和 7177，得到不同但可复现的序列。

`main()` 永久循环：fork 一个 `iter()` 子进程、等待、`pause(20)`，再改变 seed。`iter()` 清理 `a`/`b`，创建两个 worker 执行 `go(0/1)`；若先回收的 worker 非零退出，就 kill 两者，然后回收另一个并退出。

这里的退出状态不是强 oracle：`iter()` 无论两个 worker 的状态如何最终都执行 `exit(0)`，第二次 `wait()` 得到的状态也不检查；外层 `main()` 又忽略 `iter()` 的 wait status。若先回收者失败，kill 只是尽快结束同轮，错误主要靠 worker 已打印的诊断暴露。因而自动运行 `grind` 必须同时监视错误文本、kernel panic 和进展停滞，不能把某轮 `iter` 的 0 状态当成该轮所有操作通过。

`go()` 每轮取 `rand() % 23`。0 是空操作，1 到 22 混合：

- 带 `.`、`..`、绝对/相对形式的 create/open/unlink/chdir；
- 普通文件与目录同名竞争、已 unlink cwd、已 unlink open file；
- link/unlink 往返；
- 持久 fd 的 read/write/close；
- fork/wait、多级 fork、kill/self-kill；
- eager `sbrk` 增长和回缩；
- pipe 在多次 fork 后的读写；
- 创建/写入/fstat 检查确保 inode、fd、block 资源没有逐步泄漏；
- 手工构建 `echo hi | cat` 双管道，检查两个 exit status 和结果 `hi\n`。

每 500 轮输出 A/B 表示进展。它没有固定结束条件；通过持续运行、没有 panic/死锁/错误诊断来建立置信度。出现失败后 seed 和最近操作序列对复现很重要，但源码没有保存完整 trace，因此调试通常需要临时增加日志。

## 15. `usertests` 框架结构

`user/usertests.c` 包含 3000 多行回归测试。所有测试通过：

```c
struct test {
  void (*f)(char *);
  char *s;
};
```

登记在 `quicktests[]` 或 `slowtests[]`。`run()` 为每项 fork 独立子进程：子进程调用测试函数，正常返回后 exit(0)；父进程 wait status 并打印 OK/FAILED。独立进程提供三项隔离：地址空间破坏不会污染 runner、测试遗留 fd 会在 exit 关闭、预期 fault 只杀测试子进程。

`drivetests()` 负责按命令行选择 quick/slow 数组、执行轮次并汇总结果；`-c` 与 `-C` 的继续策略也是在这一层实现，而不是单个测试函数自行循环。

测试仍共享同一个文件系统镜像，因此每项应清理自己创建的名字。失败中断时可能留下文件，后续测试或重新运行可受影响；宿主 `test-xv6.py` 默认先重置镜像。

## 16. `usertests` 运行模式

命令行语义：

| 命令 | 行为 |
|---|---|
| `usertests` | quick + slow 各运行一次，首个失败停止 |
| `usertests -q` | 只运行 quick tests |
| `usertests name` | 只运行匹配的单项；仍会在相应数组查找 |
| `usertests -c` | 连续重复整套，某项失败停止 |
| `usertests -C` | 连续重复，失败也继续 |

`runtests()` 对名称做精确 `strcmp`，不是正则。指定不存在的名字会打印 `NO TESTS EXECUTED` 并失败。

每轮前后 `countfree()` 反复 eager `sbrk(PGSIZE)` 直到失败，再缩回原 break，用能分配的页数估算空闲物理页。若测试后页数小于开始值，报告 page leak。这个测量会瞬时耗尽可用物理页，依赖其他进程负载稳定；它能发现明显泄漏，不是并发系统中的严格内存计量 API。

最终成功标志是精确行：

```text
ALL TESTS PASSED
```

宿主驱动依赖该输出。

## 17. quick tests：用户拷贝和参数边界

| 用例 | 核心断言 |
|---|---|
| `copyin` | file/console/pipe write 对内核、高地址、溢出地址等坏用户源返回失败而非 panic |
| `copyout` | file/pipe read 对 0、内核和越界用户目标安全失败 |
| `copyinstr1` | 路径字符串坏指针被拒绝 |
| `copyinstr2` | 路径恰无空间容纳 NUL、exec 参数总量超过栈页时失败并回滚 |
| `copyinstr3` | 字符串越过用户地址空间末页时失败 |
| `rwsbrk` | 已通过 sbrk 归还的页不能再作为 read/write 缓冲区 |
| `validatetest` | 以页为步长遍历从地址 0 到约 1.1 MiB，把每个地址仅作为 `link("nosuchfile", bad_new_path)` 的第二个路径字符串；要求每次返回 -1 而不是破坏内核 |
| `argptest` | 对 `read(fd, sbrk(0)-1, -1)` 做存活性回归；它不检查返回值或缓冲区，因此只证明该组合没有让测试进程/内核异常终止，不证明负长度被拒绝。当前 inode 路径会把 `-1` 转成 `uint`，可能钳制到 EOF 并在后页失败前写出目标前缀 |
| `badarg` | 50000 次含坏 argv 指针的 exec 不泄漏内核页 |
| `pgbug` | 极大 64 位地址传给 exec/pipe 不因截断触发内核页故障 |

这些测试特别区分 `copyin`/`copyout` 可为合法 lazy 地址补页，而 `copyinstr` 不补页；“合法但尚未映射”和“跨到下一整页/接近 trampoline”的结果不同。硬件 fault 把原始 `stval` 传给 `vmfault()`，原始地址 `>=p->sz` 时不能补页；`copyin`/`copyout` 却先传向下取整的页首，所以原始 copy 地址虽已越过 break，只要仍在页首 `<p->sz` 的最后一页，就可能首次物化该页。若 PTE 已经存在，helpers 又会直接使用映射而不检查 byte-granular `p->sz`。这些坏高地址用例没有覆盖“越 break 的 copy 首次物化最后一页”和“已映射页尾”两个例外。

## 18. quick tests：文件、inode 和目录

| 用例 | 核心断言 |
|---|---|
| `truncate1` | `O_TRUNC` 立即把同一 inode 的 size 置 0；截断前已打开的 fd 看到新 EOF，随后写入后各 fd 仍按各自旧 offset 读取 |
| `truncate2` | 一个 fd 的 offset 在另一个 open 截断后越过新 EOF时，当前 xv6 再写返回 `-1` 而不是 panic |
| `truncate3` | 一个进程反复 open/write/read，另一个并发 create+truncate+write；两者返回值正确、无崩溃，child 状态为 0 |
| `iput`、`exitiput` | 最后引用释放、exit 关闭等路径不会死锁或泄漏 inode |
| `openiput` | 目录写打开失败与并发 unlink 的 inode 释放路径；只有按源码注释在 `sys_open()` 的 `namei()` 后临时插入多次 `yield()`，才会可靠制造目标交错，未修改内核时运行不构成该竞态的充分验证 |
| `opentest` | 只验证已有路径 `echo` 能打开、路径 `doesnotexist` 不能打开；不覆盖重复 open 或模式边界 |
| `writetest`、`writebig` | 基本读写和跨直接块写入 |
| `createtest`、`dirtest` | 大量创建与 mkdir/chdir 基本语义 |
| `fourfiles` | 四进程并发写不同文件并校验内容 |
| `createdelete` | 并发反复创建/删除名称 |
| `unlinkread` | unlink 后打开 fd 仍可读，名字可重新创建为新 inode |
| `linktest`、`linkunlink` | nlink、别名、并发 link/unlink 和最后删除 |
| `concreate` | 多进程并发创建同一组名字不会产生重复/损坏项 |
| `subdir` | `.`/`..`、嵌套路径、非空目录删除和多种错误路径 |
| `bigwrite`、`bigfile` | 日志分批与直接/间接块边界 |
| `fourteen` | 14 字节目录名及更长名字的截断语义 |
| `rmdot` | 禁止 unlink `.`、`..` |
| `dirfile` | 文件/目录类型不能被错误互换或当作中间目录 |
| `iref` | inode cache 引用压力与路径失败释放 |

这些用例共同覆盖目录项、dinode nlink、内存 ref、事务回滚和磁盘块资源账本。

## 19. quick tests：进程、调度、pipe 与 fd

| 用例 | 核心断言 |
|---|---|
| `exectest` | 基本 exec 可装入并运行 echo |
| `pipe1` | 大量 pipe 数据的生产/消费、顺序和 EOF |
| `killstatus` | killed 进程向 wait 报告 -1 状态 |
| `preempt` | CPU-bound 子进程不会阻止其他进程运行，kill 能终止它们 |
| `exitwait` | 多种 exit status 被 wait 正确复制 |
| `reparent`、`reparent2` | 父进程提前退出后的 adopted children 生命周期 |
| `twochildren` | 两个子进程并发退出/等待无竞态 |
| `forkfork`、`forkforkfork` | 多层派生和 reparent/wait |
| `forktest` | quick suite 的大映像进程反复 fork，通常先在 `uvmcopy()` 处耗尽物理页，并检查失败后已创建子进程可回收 |
| `sharedfd` | fork 后共享同一 `struct file` offset |

不要把 quick suite 中的 `forktest` 与独立程序 `_forktest` 混为一谈。后者由精简的 `user/forktest.c` 单独链接，映像小，专门先耗尽固定的进程槽并检查槽位可回收；前者运行在较大的 `_usertests` 映像中，源码注释明确预计物理内存先耗尽。

`sharedfd` 很关键：fork 复制 fd 引用而不是复制打开文件对象，所以父子共享同一个 offset。`struct file` 本身没有 offset lock；普通 inode 的 `filewrite()` 在持有 inode sleeplock 时读取、使用并更新 `f->off`，由该锁把本测试中的共享写串行化。这个结论不能推广为 pipe/device 或任意 file 类型都拥有统一的 file 层 offset 锁。

## 20. quick tests：内存、保护和 lazy allocation

| 用例 | 核心断言 |
|---|---|
| `mem` | malloc/free 与 sbrk 基本回收 |
| `sbrkbasic`、`sbrkmuch` | eager 扩缩、内容保持、物理内存耗尽恢复 |
| `kernmem`、`MAXVAplus` | 用户不能访问内核物理区或超 Sv39 合法地址 |
| `sbrkfail` | 内存不足时父子地址空间/失败回滚正确 |
| `sbrkarg` | 两次 eager `sbrk(PGSIZE)` 所得页面可分别作为 `write()` 的用户源缓冲区和 `pipe()` 写回两个 fd 的用户目标缓冲区 |
| `bsstest` | ELF BSS 被清零 |
| `bigargtest` | exec argv 超过单栈页时失败且旧映像继续运行 |
| `stacktest` | 用户栈下 guard page 不可访问 |
| `nowrite` | text、trampoline、内核/越界地址不可写 |
| `sbrkbugs` | break 缩到 0、首个页中间和同页小幅收缩不 panic |
| `sbrklast` | 跨页缩小后最后部分页仍可用于用户拷贝 |
| `sbrk8000` | 32 位符号/回绕参数不绕过上界 |
| `lazy_alloc` | 1 GiB 逻辑扩展只为触及的稀疏页分配并保持内容 |
| `lazy_unmap` | 收缩 lazy 区域释放已触及页，随后访问被 kill |
| `lazy_copy` | 对 lazy 字符串调用一次 `open()`，检查坏高地址上的 `read/write` 失败，并检查异常大的负缩容不破坏 break |
| `lazy_sbrk` | lazy break 可推进到 trapframe 下边界，eager 页清零，越界增长失败 |

`lazy_copy` 忽略那次 `open()` 的返回值，不能据此证明 `copyinstr()` 的具体行为；它也没有用合法 lazy buffer 验证 `copyin()/copyout()` 补页，更无法直接观察 PTE。这里列出的只是源码实际 oracle，而不是测试名可能暗示的完整覆盖。

`lazy_alloc` 每 64 页触及一页，`lazy_unmap` 使用更稀疏步长并在子进程缩回后访问验证映射消失。详见 [lazy page fault 流程](../flows/lazy-page-fault.md)。

## 21. slow tests

`slowtests[]` 有六项。下表严格区分压力意图和源码实际 oracle：

| 用例 | 实际操作与强断言 | 不能据此声称 |
|---|---|---|
| `bigdir` | 为一个文件创建 500 个硬链接，再逐个 unlink；每次 link/unlink 失败都会使测试失败 | 不枚举所有目录块内容，也不模拟崩溃恢复 |
| `manywrites` | 四个进程反复创建、写、关闭、删除不同文件，检查子状态 | 运行一次不能证明不存在其他调度交错下的 VirtIO/日志死锁 |
| `badwrite` | 反复以坏用户指针写新文件，随后删除；失败主要由 panic、后续创建失败或全局 page-leak 检查暴露 | 不直接读取 bitmap 证明每次失败写都立即回滚了哪个 block |
| `execout` | 几乎耗尽物理内存，逐步释放 0 到 14 页后在子进程调用 exec；主要 oracle 是整个过程不 panic | 父进程调用 `wait((int *)0)` 并忽略子状态，不能证明每个内存额度下 exec 都成功或所有分配阶段逐项回滚 |
| `diskfull` | 写文件直到短写/创建失败，继续尝试扩展目录，最后删除创建过的名字 | `mkdir("diskfulldir")` 意外成功只打印诊断、不 `exit(1)`；测试也不在清理后重新写满验证所有 block 已恢复 |
| `outofinodes` | 最多尝试 1024 个创建，遇到失败即停止，然后 unlink 全部候选名字 | 不断言创建确实因 dinode 耗尽而失败，也不在清理后重新创建验证 inode 可恢复 |

quick 模式跳过它们，因此 `-q` 通过不能替代完整磁盘/低内存压力验证。

源码还保留一个未登记到 `quicktests[]` 或 `slowtests[]` 的 `fsfull()`，所以任何正常 `usertests` 命令都不会调用它。函数上方注释仍称“`balloc` panics”，但当前 `kernel/fs.c:balloc()` 在找不到空闲块时打印 `balloc: out of blocks` 并返回 0；这是过时注释，不能当作当前测试禁用原因。真正被登记的磁盘耗尽覆盖来自 `diskfull`，而它的 oracle 限制如上表所示。

## 22. `test-xv6.py` 的 QEMU 封装

宿主脚本 `test-xv6.py` 接受一个必需的 `testrex` 和可选 `-q`。`class QEMU`：

- 可选 `reset=True` 时先构建 `kernel/kernel`，删除 `fs.img` 并重新 `make fs.img`；
- 以 `subprocess.Popen(["make", "qemu"])` 启动，stdin/stdout 都用 pipe；
- `cmd()` 向 xv6 shell 写命令；
- `read()` 从 QEMU stdout 读取最多 4096 字节并用 replacement 解码；
- `match()` 用 `re.match` 按行匹配累积输出；
- `monitor()` 每秒读一次，直到目标 regex 或 timeout；
- `stop()` terminate make 进程。

普通 usertests 路径先重置镜像，发送 `usertests`、`usertests -q` 或单项名字，等待行首 `ALL TESTS PASSED`。默认 timeout 600 秒，quick 为 300 秒。

## 23. 宿主测试选择规则

脚本通过 `inspect.getmembers()` 找本模块中所有名字以 `test` 开头的函数，并用用户提供的正则搜索函数名。例如：

```text
./test-xv6.py crash       -> matches test_crash
./test-xv6.py log         -> matches test_log
./test-xv6.py usertests   -> matches test_usertests
```

若正则没有匹配任何宿主 test 函数，就把原字符串作为 `usertests <name>` 的单项名字。因此宿主层选择是 regex，进入 xv6 后单项选择是精确名字。

`-q` 在 `test_usertests()` 中优先于单项参数：例如 `./test-xv6.py -q lazy_sbrk` 实际发送的是 `usertests -q`，会运行整套 quick tests，而不是只运行 `lazy_sbrk`。要运行一个单项不能同时带 `-q`。

一个宽正则可能匹配并顺序运行多个宿主函数。每个函数自己决定是否重置镜像，不能假设所有匹配项共享或隔离状态。

## 24. redo log 强制崩溃测试

`test_log()` 最多尝试五次：

```text
crash_log:
  reset filesystem
  boot QEMU
  run "logstress f0 f1 f2 f3 f4 f5"
  wait 2 seconds
  SIGKILL QEMU child

recover_log:
  boot without resetting fs.img
  look for line beginning "recovering"
  if recovery occurred, run ls and require f5 appears
```

强杀时机未与日志 commit 精确同步，所以一次可能没撞到有效已提交 header；因此最多重试五次。测试验证启动恢复能安装完整事务并留下可遍历文件系统，不验证每个文件的完整 500000 字节内容。

## 25. orphan 强制崩溃测试

`test_forphan()`/`test_dorphan()`：

1. 重置镜像并启动对应用户程序。
2. 等待其打印 `wait for kill and reclaim`。
3. SIGKILL QEMU，让内存 inode 引用瞬间消失。
4. 不重置镜像重启。
5. 要求输出行首 `ireclaim`。

`test_crash()` 依次运行 log、file orphan 和 directory orphan 三类。测试会反复删除/重建和修改 `fs.img`，属于破坏性测试，不能用于需要保留当前镜像数据的工作区，也不应与另一个 QEMU 实例共享镜像并行运行。

## 26. `test-xv6.py` 当前缺陷

脚本的失败路径有三处直接缺陷；它们不影响匹配成功的主路径，但会遮蔽原始失败：

1. `QEMU.error()` 直接引用不在该方法作用域中的 `regexps`。第一次匹配失败或 timeout 会先抛 `NameError`，因此通常还到不了后面的输出保存和停止逻辑。
2. 即使修正上一项，`save_output()` 仍写 `self.out`，而类实际维护的是 `self.output`；随后会抛 `AttributeError`，无法保存预期的 `test-xv6.out`。
3. `crash()` 找不到 QEMU 子进程时调用 `os.exit(1)`，Python `os` 模块没有 `exit`；应是 `sys.exit` 或 `os._exit`，当前会抛 `AttributeError`。

此外，`crash()` 用 `ps --ppid <make pid>` 取直接子进程并杀列表第一项，假定它就是 QEMU；构建已完成后的常规路径通常成立，但这是进程树启发式。`build_xv6()` 和 `reset_fs()` 只捕获并打印 `CalledProcessError`，不会阻止后续启动，因而后续现象可能来自旧内核、缺失或未成功重建的镜像。

`monitor()` 每轮调用的 `read()` 是对 stdout pipe 的阻塞式 `os.read()`，没有 `select` 或 non-blocking 设置；如果 QEMU 保持运行却不再输出，调用可以卡在读取中，使得 300/600 秒 deadline 无法按时被重新检查。异常退出时 `stop()`/清理也未由 `finally` 保证，可能遗留 QEMU 或占用 `fs.img`。

这些事实应在解释测试失败时先排除，不能看到宿主 traceback 就直接判断内核错误。

## 27. 测试层次与可证明范围

| 层次 | 适合发现 | 不能单独证明 |
|---|---|---|
| 小工具手工运行 | 基本 API 和数据路径 | 并发、资源耗尽、崩溃一致性 |
| 独立 stress 程序 | 长时间竞态、死锁、泄漏征兆 | 确定性覆盖、严格输出正确性 |
| `usertests -q` | 快速功能/边界回归 | slow 磁盘与低内存压力 |
| 完整 `usertests` | 广泛系统调用和资源回归 | 任意调度交错、真实掉电模型 |
| `test-xv6.py crash` | QEMU 进程强杀后的持久恢复 | 硬件缓存/设备断电的全部行为 |

压力测试“运行很久未失败”只提高置信度。锁正确性、日志协议和资源所有权仍需结合源码不变量审阅。

## 28. 推荐执行顺序

在允许重建/改写 `fs.img` 的工作区中：

```sh
./test-xv6.py -q usertests
./test-xv6.py usertests
./test-xv6.py crash
```

定位单项：

```sh
./test-xv6.py lazy_sbrk
./test-xv6.py sharedfd
./test-xv6.py diskfull
```

长期压力可从 xv6 shell 运行 `grind`、`stressfs` 或 `logstress names...`，但 `grind` 不自行结束，需从外部终止。运行前确认没有另一个 QEMU 占用同一 `fs.img`。

## 29. 变更代码时怎样扩展测试

新增或修复核心逻辑时，测试至少应包含：

1. 正常成功路径与用户可见结果。
2. 每个分配点失败后的回滚和可再次使用性。
3. 坏用户地址、负数和溢出参数。
4. fork 后共享引用以及 exit/close/wait 清理。
5. 多 CPU 并发下相同对象和不同对象操作。
6. 文件系统操作在强杀并恢复后的持久不变量。
7. 测试前后物理页、inode、block、fd/proc 表资源没有净损失。

单项应尽量在独立子进程中执行、使用唯一临时名字、检查所有系统调用返回值，并在成功/失败路径都清理；压力负载与严格断言测试最好分开，以便错误可定位和复现。

## 30. 阅读结论

这些程序展示了 xv6 的设计尺度：`cat`/`wc` 用很少代码组合 read/write，shell 和 `grind` 把 fd、pipe、fork、exec 串成复杂拓扑，`usertests` 用隔离子进程覆盖失败边界，`test-xv6.py` 再从宿主控制真正的 QEMU 生命周期和持久镜像。理解测试结果时应始终区分“被测内核不变量”“用户测试自身假设”和“宿主驱动可靠性”三层。
