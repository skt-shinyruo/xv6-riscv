# 文件对象、文件描述符与管道

本文说明当前仓库中位于系统调用层与 inode、设备、管道之间的“打开文件”抽象。重点不是重复 `read()`、`write()` 的表面作用，而是回答以下问题：一个小整数 fd 如何找到真正的资源；`dup()`、`fork()`、`close()` 和 `exit()` 怎样改变引用；普通文件、设备和管道如何共用 `struct file` 接口；大写入为什么必须拆成多个受日志配额约束的分块；管道为何不会丢失唤醒，以及关闭、终止和坏用户地址分别返回什么。

本文以当前源码为准。它描述的是 xv6 的教学实现，不承诺完整的 POSIX 文件描述符、管道原子写或错误码语义。

## 1. 文档边界与源码地图

### 1.1 直接覆盖的源码

| 源码路径 | 核心符号或定义 | 本文覆盖内容 |
| --- | --- | --- |
| `kernel/file.c` | `ftable`、`fileinit()`、`filealloc()`、`filedup()`、`fileclose()`、`filestat()`、`fileread()`、`filewrite()` | 全局打开文件表、引用计数、后端分派、共享偏移和日志分批 |
| `kernel/file.h` | `struct file`、`struct inode`、`struct devsw`、设备号宏、`CONSOLE` | 打开文件、内存 inode 和设备分派的接口契约 |
| `kernel/pipe.c` | `struct pipe`、`pipealloc()`、`pipeclose()`、`pipewrite()`、`piperead()` | 管道对象、环形缓冲、阻塞、唤醒、关闭和释放 |
| `kernel/fcntl.h` | `O_RDONLY`、`O_WRONLY`、`O_RDWR`、`O_CREATE`、`O_TRUNC` | `open()` 模式位及当前实现的解释方式 |
| `kernel/stat.h` | `T_DIR`、`T_FILE`、`T_DEVICE`、`struct stat` | 用户可见的文件类型和元数据 ABI |

### 1.2 为解释调用者而引用的源码

| 源码路径 | 与本文的关系 |
| --- | --- |
| `kernel/sysfile.c` | `argfd()`、`fdalloc()` 和 `sys_open()`、`sys_dup()`、`sys_close()`、`sys_fstat()`、`sys_pipe()` |
| `kernel/proc.h` | 每进程 `ofile[NOFILE]` 描述符表 |
| `kernel/proc.c` | `kfork()` 复制引用，`kexit()` 关闭引用，`sleep()`/`wakeup()` 实现管道等待 |
| `kernel/param.h` | `NOFILE=16`、`NFILE=100`、`NDEV=10`、`MAXOPBLOCKS=10` |
| `kernel/fs.c` | `readi()`、`writei()`、`stati()`、`iput()` 的下层行为 |
| `kernel/fs.h` | `BSIZE=1024`、磁盘 inode 和最大文件布局 |
| `kernel/log.c` | `begin_op()`/`end_op()` 的日志空间预留和提交条件 |
| `kernel/console.c` | `devsw[CONSOLE]` 的实际注册和设备回调示例 |
| `user/sh.c` | shell 如何用 `pipe()`、`fork()`、`dup()` 和 `close()` 建立流水线 |
| `user/init.c` | `dup(0)` 如何建立标准输出和标准错误 |
| `user/usertests.c` | `pipe1`、`sharedfd`、`bigwrite`、坏地址和资源回收测试 |

inode 缓存、目录查找、块映射、日志提交与控制台内部算法分别由文件系统、存储栈和设备文档展开。本文只追踪它们与打开文件层相接的边界。

## 2. 总体模型：一个 fd 要经过两级表

用户程序看到的文件描述符只是从 0 开始的小整数。它先索引当前进程的 `ofile[]`，得到全局表中的 `struct file`；后者再根据 `type` 指向 inode 或 pipe，或用 `major` 索引设备分派表。

```text
用户 fd（例如 1）
  |
  | 当前进程 p->ofile[fd]，每进程最多 NOFILE=16 项
  v
全局 struct file，系统最多 NFILE=100 项
  |  ref/readable/writable/off/type
  |
  +-- FD_INODE  --> f->ip -----------------> inode/块缓存/磁盘
  +-- FD_DEVICE --> f->ip + f->major ------> devsw[major].read/write
  +-- FD_PIPE   --> f->pipe ---------------> 内存环形缓冲
  `-- FD_NONE   --> 未分配槽，不得执行 I/O
```

这两级结构解决的是两个不同问题：

- `p->ofile[]` 给进程提供局部名字空间。两个进程的 fd 3 可以指向完全不同的对象。
- 全局 `ftable.file[]` 保存“打开实例”。多个 fd 可以指向同一个实例，从而共享访问模式和当前偏移。
- inode 表示文件系统对象；同一 inode 可以被多次独立 `open()`，得到多个 `struct file`，每个打开实例拥有自己的 `off`。
- pipe 没有路径或 inode。`pipe()` 直接创建一个内存对象和两个方向相反的 `struct file`。

因此不能把 fd、`struct file.ref` 和 `inode.ref` 当作同一个计数。它们位于不同层次，结束条件也不同。

## 3. 固定容量与资源消耗

`kernel/param.h` 给出三个直接相关的上限：

| 常量 | 当前值 | 限制对象 |
| --- | ---: | --- |
| `NOFILE` | 16 | 单个进程 `ofile[]` 中可用的 fd 槽数 |
| `NFILE` | 100 | 全系统同时存活的 `struct file` 数量 |
| `NDEV` | 10 | `devsw[]` 可索引的主设备号范围 |

资源消耗规则并不相同：

- 一次成功的普通 `open()` 消耗一个进程 fd 槽和一个全局 file 槽。
- 一次成功的 `pipe()` 消耗两个进程 fd 槽、两个全局 file 槽和一个物理页分配器提供的页面。`struct pipe` 实际远小于一页，但 `kalloc()` 的粒度是一页。
- `dup()` 只消耗一个新的进程 fd 槽；它复用原 `struct file` 并增加 `ref`。
- `fork()` 给子进程填充对应 fd 槽，同时增加每个非空 `struct file` 的 `ref`；它不新建全局 file 槽。
- 对同一路径再次 `open()` 会新建 `struct file`，即使底层最终是同一个 inode。

全局表采用固定数组和线性扫描，没有动态扩容、空闲链表或每用户配额。资源耗尽统一以 `-1` 向系统调用层报告，不提供 `EMFILE`、`ENFILE` 等细分错误码。

## 4. `struct file`：打开实例的状态

`kernel/file.h` 定义：

```c
struct file {
  enum { FD_NONE, FD_PIPE, FD_INODE, FD_DEVICE } type;
  int ref;
  char readable;
  char writable;
  struct pipe *pipe;
  struct inode *ip;
  uint off;
  short major;
};
```

字段的有效性由 `type` 决定：

| 字段 | `FD_PIPE` | `FD_INODE` | `FD_DEVICE` | 保护与含义 |
| --- | --- | --- | --- | --- |
| `ref` | 有效 | 有效 | 有效 | 由 `ftable.lock` 保护，统计指向该打开实例的有效引用 |
| `readable` | 有效 | 有效 | 有效 | 打开时确定，此后不改变 |
| `writable` | 有效 | 有效 | 有效 | 打开时确定；管道还借它区分关闭的是哪一端 |
| `pipe` | 有效 | 无效 | 无效 | 指向唯一的 `struct pipe` |
| `ip` | 无效 | 有效 | 有效 | 持有一个 inode 引用，最后一次 `fileclose()` 时交给 `iput()` |
| `off` | 无效 | 有效 | 当前 I/O 不使用 | 同一 `struct file` 的共享文件偏移 |
| `major` | 无效 | 无效 | 有效 | 索引 `devsw[]`，不使用 `off` 做通用定位 |

`FD_*` 表示打开实例选择哪个 I/O 后端，`T_*` 表示 inode/stat 中的文件系统对象类型，两组枚举不能因部分数值碰巧相同而混用。`FD_INODE` 可承载 `T_FILE`，也可承载以只读方式打开的 `T_DIR`；`ls` 正是把目录内容当作 `dirent` 序列读取。`T_DEVICE` 打开后使用 `FD_DEVICE`。pipe 没有 inode，因此也没有对应的 `T_PIPE` 或可返回的 `struct stat`。

`filealloc()` 只把空槽的 `ref` 从 0 改为 1，并不会清零整个结构。首次启动时静态数组本身为零；槽被最终关闭时 `fileclose()` 会把 `type` 改回 `FD_NONE`。分配者必须在把对象暴露给用户前初始化与新类型有关的所有字段，不能依赖其他旧字段恰好为零。

### 4.1 `struct inode` 在打开文件层的角色

`kernel/file.h` 同时定义内存 inode：

| 字段 | 保护方式 | 含义 |
| --- | --- | --- |
| `dev`、`inum` | inode 表槽的身份，在 `itable.lock` 下认领 | 唯一标识后端磁盘上的 inode |
| `ref` | `itable.lock` | 内核中指向该内存 inode 的引用数；file 只持有其中一个 |
| `lock` | 自身是 sleeplock | 允许磁盘 I/O 期间睡眠，并保护其后所有字段 |
| `valid` | `ip->lock` | 为 0 表示磁盘 inode 内容尚未装入缓存槽 |
| `type` | `ip->lock` | `T_DIR`、`T_FILE`、`T_DEVICE`；磁盘上 0 表示未分配 |
| `major`、`minor` | `ip->lock` | `T_DEVICE` 的设备编号 |
| `nlink` | `ip->lock` | 磁盘目录硬链接数，不是打开引用数 |
| `size` | `ip->lock` | inode 当前逻辑字节数 |
| `addrs[NDIRECT+1]` | `ip->lock` | 直接块地址和一个间接块地址 |

`valid` 到 `addrs` 是磁盘 `struct dinode` 的内存缓存。`ilock()` 首次装载，`iupdate()` 把修改写回日志；file 层不直接解释块地址，只在 I/O 和 stat 周围取得 inode 锁。`fileclose()` 只交还 inode 引用，真正是否清空缓存槽或释放磁盘块由 `iput()` 根据 `ref` 与 `nlink` 决定。

### 4.2 `struct devsw` 接口

设备分派项只有两个函数指针：

```c
struct devsw {
  int (*read)(int user_dst, uint64 dst, int n);
  int (*write)(int user_src, uint64 src, int n);
};
```

`kernel/file.c` 提供 `devsw[NDEV]` 存储，具体驱动在初始化时按主设备号填写。例如 `consoleinit()` 写入 `devsw[CONSOLE]`，其中 `CONSOLE` 在 `file.h` 中定义为 1。回调的首参数说明地址属于用户还是内核；`fileread()`/`filewrite()` 都传 1。通用接口没有 file 指针、偏移或 minor 参数，因此这些状态若有需要，只能由具体设备实现另行管理。

## 5. 三种引用与所有权必须分开

### 5.1 fd 槽不是独立对象

`p->ofile[fd]` 是一个裸指针槽。当前 xv6 每个进程只有一个执行线程，所以 `sys_close()`、`sys_dup()` 等不会与同一进程中的另一个线程并发修改 `ofile[]`；该数组没有独立锁。

`fdalloc(f)` 从 0 开始找第一个空槽并写入 `f`。源码注释明确规定：成功时它接管调用者已经持有的 file 引用，不会自动调用 `filedup()`。失败时它不接管引用，调用者仍负责清理。

这解释了 `sys_dup()` 的顺序：

```text
argfd(oldfd) -> f
fdalloc(f)   -> 把同一指针安装到新槽
filedup(f)   -> 为新槽增加一个引用
return newfd
```

`fdalloc()` 失败时尚未安装新槽，也没有增加引用，因此直接返回 `-1` 即可。

### 5.2 `struct file.ref` 统计共享打开实例

以下事件会增加 `ref`：

- `filealloc()` 把新槽从 0 变为 1；
- `dup()` 为新 fd 调用 `filedup()`；
- `kfork()` 为子进程继承的每个非空 fd 调用 `filedup()`。

以下事件会减少 `ref`：

- `close(fd)` 清空进程槽后调用 `fileclose()`；
- `kexit()` 遍历并关闭所有非空槽；
- `open()`、`pipe()` 的失败回滚关闭已经分配但不能交付的对象。

`exec()` 不清空文件描述符。本实现没有 close-on-exec 标志，因此 shell 在 `fork()` 后建立的重定向和管道端点会穿过 `exec()` 保留下来。

### 5.3 inode 引用属于底层文件系统

`sys_open()` 从 `namei()` 或 `create()` 得到一个带引用的 inode。成功构造 `FD_INODE`/`FD_DEVICE` 后，这个引用由 `struct file.ip` 持有；`sys_open()` 只解锁 inode，不执行 `iput()`。

只有最后一个 file 引用关闭时，`fileclose()` 才执行 `iput(ff.ip)`。一个 inode 可以同时被多个独立 `struct file` 引用，所以 inode 的 `ref` 不等于任意一个 file 的 `ref`。磁盘硬链接数 `nlink` 又是第三种概念：它统计目录项，不统计打开描述符。

这也是“打开后再 `unlink()`”仍可继续 I/O 的原因：删除路径会减少 `nlink`，但打开 file 持有的 inode `ref` 仍使内存 inode 和磁盘数据保持可达；最后一个 file 关闭后，`iput()` 才可能看到 `nlink==0` 并真正截断、回收。`usertests unlinkread` 覆盖这一生命周期。

### 5.4 pipe 端点存活由 file 最后引用决定

`pipealloc()` 为读端和写端各创建一个独立 `struct file`。对某一端执行 `dup()` 或 `fork()` 只增加该端 file 的 `ref`。只要写端 file 还有一个引用，`pipeclose()` 就不会收到“写端关闭”通知，`writeopen` 仍为 1；读端同理。

这使 `readopen`/`writeopen` 两个布尔值足够使用：它们表示各端对应的唯一 file 对象是否已到最后一次关闭，而不是描述符的精确数量。

## 6. 全局 file 表与 `ftable.lock`

`kernel/file.c` 声明静态全局表：

```c
struct {
  struct spinlock lock;
  struct file file[NFILE];
} ftable;
```

启动 CPU 在 `main()` 中调用 `fileinit()`，后者只初始化 `ftable.lock`。表本身依赖 BSS 初始清零，使所有 `ref` 初始为 0。

### 6.1 `filealloc()`

`filealloc()` 在 `ftable.lock` 下从头扫描数组。看到 `ref == 0` 时立即写成 1，再释放锁并返回。先认领再解锁保证两个 CPU 不会拿到同一空槽。

如果 100 个槽都在使用，它释放锁并返回空指针。调用者必须把这转换为系统调用失败并回滚其他资源。

### 6.2 `filedup()`

`filedup()` 在同一把锁下检查 `ref >= 1`，随后加一。对已经释放的对象调用它属于内核不变量破坏，代码选择 `panic("filedup")`，而不是向用户返回错误。

计数没有显式溢出检查；受固定进程表和每进程 fd 数限制，正常系统状态远达不到 `int` 溢出。

### 6.3 `fileclose()` 的两阶段关闭

`fileclose()` 先在 `ftable.lock` 下减引用：

1. 若原 `ref < 1`，说明重复关闭或内核指针错误，执行 `panic("fileclose")`。
2. 若减一后仍大于 0，只释放锁并返回；底层资源仍被其他 fd 使用。
3. 若降到 0，把整个 `struct file` 复制到栈上局部变量 `ff`。
4. 把共享槽的 `ref` 保持为 0、`type` 设为 `FD_NONE`，释放 `ftable.lock`。
5. 根据快照 `ff.type` 关闭 pipe 或释放 inode 引用。

先复制再释放锁有两个作用：

- 后续的 `begin_op()`、`iput()` 和 `pipeclose()` 都会进入更深层的锁；其中前两者还可能睡眠。它们都不应在持有 `ftable` 自旋锁时执行。`pipeclose()` 本身不睡眠，但会获取 `pipe.lock` 并调用 `wakeup()`。
- 共享槽在锁释放后可以立刻被另一 CPU 的 `filealloc()` 复用；旧资源清理由局部快照完成，不再读取可能已被复用的槽。

对 `FD_PIPE`，它调用 `pipeclose(ff.pipe, ff.writable)`。对 `FD_INODE` 和 `FD_DEVICE`，它在 `begin_op()`/`end_op()` 内调用 `iput(ff.ip)`，因为最后一个 inode 引用且 `nlink==0` 时可能截断 inode、释放磁盘块并更新日志。`FD_NONE` 不需要底层清理。

## 7. `open()` 如何构造一个普通或设备 file

`sys_open()` 的关键流程如下：

```text
读取 path 和 omode
  -> begin_op()
  -> O_CREATE ? create() : namei()+ilock()
  -> 检查目录写打开和设备 major
  -> filealloc()
  -> fdalloc(f)
  -> 按 inode 类型填写 f
  -> 可选 itrunc()
  -> iunlock(ip)
  -> end_op()
  -> 返回 fd
```

整个路径查找、创建、截断和失败 `iput` 都位于文件系统事务中。成功后 inode 保持引用但不保持锁，后续 I/O 按次加锁。

### 7.1 `fcntl.h` 模式位

| 宏 | 值 | 当前解释 |
| --- | ---: | --- |
| `O_RDONLY` | `0x000` | 只读；值为 0，所以“只读”是缺少写位，不是一个可检测的独立位 |
| `O_WRONLY` | `0x001` | 只写 |
| `O_RDWR` | `0x002` | 可读写 |
| `O_CREATE` | `0x200` | 不存在时创建普通文件；已存在的普通文件或设备可继续打开 |
| `O_TRUNC` | `0x400` | 成功打开 `T_FILE` 后在同一事务内调用 `itrunc()` |

权限字段按以下表达式设置：

```c
f->readable = !(omode & O_WRONLY);
f->writable = (omode & O_WRONLY) || (omode & O_RDWR);
```

因此 `O_RDWR` 可读可写，`O_WRONLY` 只写，零模式只读。实现没有完整校验任意未知位组合，也没有 append、nonblocking、close-on-exec、权限掩码或每文件访问控制。

`O_TRUNC` 的执行条件只检查该位和 `ip->type == T_FILE`，没有再要求 `f->writable`。所以当前源码允许 `open(path, O_RDONLY | O_TRUNC)`（数值上就是只带 `O_TRUNC`）先清空普通文件，再返回一个不可写、可读的 fd。类似地，`O_CREATE` 不自动包含写权限；只带 `O_CREATE` 可以创建文件并得到只读打开实例。这些是当前位解析的结果，不是完整 Unix 权限语义。

截断改变共享 inode 的块映射和 `size`，但不会遍历其他 `struct file` 重置它们的 `off`。新打开实例自己的偏移刚被设为 0；此前打开的实例保留原数值，可能暂时位于新 EOF 之后。`usertests truncate1` 会从多个打开实例观察这一点。

目录只有在 `omode == O_RDONLY`，也就是数值严格等于 0 时允许打开。带 `O_TRUNC` 等额外位的目录打开不满足这个条件，即使截断逻辑本身只作用于 `T_FILE`。

### 7.2 普通 inode 与设备 inode

普通文件设置：

- `f->type = FD_INODE`；
- `f->ip = ip`；
- `f->off = 0`。

设备 inode 设置：

- 先验证 `0 <= ip->major < NDEV`；
- `f->type = FD_DEVICE`；
- `f->major = ip->major`；
- 同样保存 `f->ip = ip`，供生命周期和 `fstat()` 使用。

设备的 `minor` 保存在 inode/stat 信息里，但通用 `devsw` 回调只按 `major` 分派，函数签名没有传入 `minor`。当前控制台使用主设备号 `CONSOLE=1`。

### 7.3 `open()` 的失败回滚

| 失败点 | 已取得资源 | 回滚动作 |
| --- | --- | --- |
| 路径复制、查找或创建失败 | 可能只有日志操作名额 | `end_op()`，返回 `-1` |
| 尝试写方式打开目录 | 一个已锁 inode 引用 | `iunlockput(ip)`，`end_op()` |
| 设备主编号越界 | 一个已锁 inode 引用 | `iunlockput(ip)`，`end_op()` |
| `filealloc()` 失败 | 一个已锁 inode 引用 | `iunlockput(ip)`，`end_op()` |
| `fdalloc()` 失败 | inode 引用和一个 ref=1 的 file | `fileclose(f)`，再 `iunlockput(ip)` 和 `end_op()` |

在最后一种路径中，file 尚未接管 inode，因而 `fileclose(f)` 不得替代后面的 `iunlockput(ip)`。此时新 file 的 `type` 仍为 `FD_NONE`，关闭只归还全局槽。

## 8. fd 系统调用层的最短路径

### 8.1 `argfd()`

`argfd(n, &fd, &f)` 从系统调用参数取整数，并验证：

- `fd >= 0`；
- `fd < NOFILE`；
- `myproc()->ofile[fd] != 0`。

成功后可返回整数和指针中的任意一个。它不增加 file 引用；系统调用执行期间依赖当前单线程进程不会并发关闭自己的槽。

### 8.2 `close()`

`sys_close()` 先把 `p->ofile[fd]` 清零，再调用 `fileclose(f)`。清槽表示进程从此不再拥有这个引用；即使底层最后关闭需要睡眠，该 fd 已经可被后续 `open()` 或 `dup()` 重用。

### 8.3 `dup()` 与最低可用 fd

`fdalloc()` 总是从 0 向上扫描，因此 `dup(oldfd)` 返回当前最低的空 fd。shell 正是利用这一点实现重定向：先 `close(1)`，再 `dup(pipe_write_end)`，新描述符必然落到 1。

`dup()` 不复制 `struct file`。新旧 fd 共享 `readable`、`writable`、`off` 和后端对象。

### 8.4 `fork()`、`exit()` 与 `exec()`

`kfork()` 对父进程每个非空 `ofile[i]` 执行：

```c
np->ofile[i] = filedup(p->ofile[i]);
```

父子因此共享同一打开实例和普通文件偏移。`kexit()` 遍历 16 个槽，对每个非空项调用 `fileclose()` 并清零。进程成为 zombie 前已经不再持有文件或管道端点。

`kexec()` 替换地址空间和程序映像，但不修改 `ofile[]`。这既允许 shell 把管道接到子程序的 fd 0/1，也要求 shell 主动关闭不应由新程序继承的所有端点。

## 9. `fileread()`：按后端分派

所有读取先检查 `f->readable`。不可读立即返回 `-1`，不会进入后端。

### 9.1 管道

`FD_PIPE` 直接调用：

```c
piperead(f->pipe, user_addr, n)
```

它不使用 `f->off`，消费位置保存在共享 pipe 的 `nread` 中。

### 9.2 设备

`FD_DEVICE` 验证主设备号范围，并检查 `devsw[major].read` 非空；任一不满足都返回 `-1`。有效时调用：

```c
devsw[f->major].read(1, addr, n)
```

第一个参数 1 表示目标是用户地址。打开文件层不持 inode 锁，也不维护设备偏移；设备驱动负责自己的锁、阻塞和返回值语义。`consoleinit()` 把 `CONSOLE` 对应的回调注册为 `consoleread()`/`consolewrite()`。

### 9.3 普通 inode

`FD_INODE` 的路径为：

```text
ilock(f->ip)
  -> readi(ip, user_dst=1, addr, f->off, n)
  -> 若返回值 r > 0，f->off += r
iunlock(f->ip)
```

inode sleeplock同时保护 inode 内容和同一 inode 上 I/O 的关键区。由于共享 `struct file` 的 `off` 也在这段锁内读取并更新，父子进程或 `dup()` 出来的 fd 不会读到重叠的同一偏移区间。

两个独立 `open()` 得到两个 `struct file`，因此它们的 `off` 独立；inode 锁仍会串行化实际 inode 数据访问。

当前 `readi()` 若在某个块向用户 `copyout` 失败，会返回 `-1`，即使此前可能已把较早块复制到用户缓冲区。`fileread()` 只在 `r>0` 时推进偏移，因此该错误路径不推进 `f->off`。用户缓冲区的部分修改不会回滚。

未知的 file 类型不是用户错误，而是内核状态损坏，`fileread()` 执行 `panic("fileread")`。

## 10. `filestat()` 与 `stat.h`

只有 `FD_INODE` 和 `FD_DEVICE` 支持 `filestat()`。函数锁住 `f->ip`，由 `stati()` 生成内核栈上的快照，解锁后再 `copyout()` 到用户地址。

`struct stat` 的用户可见字段为：

| 字段 | 类型 | 来源 |
| --- | --- | --- |
| `dev` | `int` | inode 所属文件系统的后端磁盘设备号；不是设备 inode 的 `major` |
| `ino` | `uint` | inode 编号 |
| `type` | `short` | `T_DIR`、`T_FILE` 或 `T_DEVICE` |
| `nlink` | `short` | 磁盘目录硬链接计数 |
| `size` | `uint64` | inode 当前字节数；内存 inode 中的 `uint` 被扩展写入 |

管道没有 inode 元数据，所以 `fstat(pipefd, ...)` 返回 `-1`。设备 fd 返回其设备 inode 的 stat，而不是驱动私有状态。

`copyout()` 失败同样返回 `-1`；已经生成快照不会改变任何文件状态。

`kernel/file.h` 还提供设备号编码宏：`mkdev(major, minor)` 把两个 16 位部分组合成 `uint`，`major(dev)` 和 `minor(dev)` 再拆分。设备 inode 自身另有 `ip->major`/`ip->minor`；当前打开文件分派只复制前者到 `f->major`。不能从 `stat.dev` 推断这个设备 inode 的驱动主编号。

## 11. `filewrite()` 的三种后端

所有写入先检查 `f->writable`。不可写返回 `-1`。

- `FD_PIPE` 调用 `pipewrite()`。
- `FD_DEVICE` 验证 `major` 和写回调，再调用 `devsw[major].write(1, addr, n)`。
- `FD_INODE` 在日志容量约束下分批调用 `writei()`。
- 其他类型触发 `panic("filewrite")`。

设备和 pipe 的具体返回值直接上送；打开文件层不会强制把部分写转换成全长成功。

## 12. 普通文件写入为何按 3072 字节分批

文件数据写入可能修改数据块、位图块、inode 块和间接索引块，必须位于 `begin_op()`/`end_op()` 日志协议内。一次任意大的用户 `write()` 不能无限占用固定日志。

当前上限计算为：

```c
int max = ((MAXOPBLOCKS - 1 - 1 - 2) / 2) * BSIZE;
```

代入 `MAXOPBLOCKS=10` 和 `BSIZE=1024`：

```text
max = ((10 - 1 - 1 - 2) / 2) * 1024
    = 3 * 1024
    = 3072 字节
```

这是保守预算：为 inode、可能的间接块和非对齐写入余量预留块数，剩余预算按数据块及其分配记账成对估算。重要结论是每轮至多请求 `writei()` 写 3072 字节，而不是把整个用户请求放进一个事务。

每一轮真实顺序为：

```text
选择 n1 = min(剩余字节, 3072)
  -> begin_op()                 可能等待日志空间
  -> ilock(f->ip)               可能睡眠
  -> writei(ip, 1, addr+i, f->off, n1)
  -> 若 r>0，f->off += r
  -> iunlock(f->ip)
  -> end_op()                   最后一个 outstanding op 才触发提交
  -> r==n1 ? 继续 : 结束
```

这里的 `begin_op()`/`end_op()` 界定一轮写的日志空间预留范围，不等于承诺“一轮就是一笔独立的磁盘提交”。全局日志会把同时 outstanding 的多个范围合并成同一个 group transaction。

这里有四个不能省略的语义：

1. 一个用户 `write()` 被拆成多个日志预留范围，整个调用没有被一个 `begin_op()`/`end_op()` 包围，因而不具备调用级事务原子性。
2. 没有其他 outstanding 操作时，每轮 `end_op()` 会同步提交后才进入下一轮；存在并发时，相邻轮次可能与其他操作进入同一个 group，直到全局最后一个 outstanding 操作结束才提交。
3. 每轮都会释放 inode 锁。多个进程通过同一个共享 `struct file` 执行大写入时，不会重叠使用同一偏移，但两个系统调用可以在 3072 字节边界附近交错。
4. 若任一轮 `writei()` 返回短写，循环停止。只有累计 `i == n` 才返回 `n`，否则 `filewrite()` 返回 `-1`。

第 2 点还意味着系统调用返回不是持久化屏障：若别的文件系统操作仍 outstanding，本次 write 的所有轮次都可能已经返回 `end_op()`，甚至整个 `sys_write()` 已返回用户态，但所属 group 的提交头仍要等最后一个 outstanding 操作结束后才写盘。

最后一点意味着“返回 `-1`”不保证零字节写入。此前完整轮次已经更新偏移并可能提交，失败轮次若返回正的短写也会推进偏移；接口不会把已写字节数返回给用户。文档和调用者不能假定失败会自动回滚整个 write。

`writei()` 遇到坏用户地址时还可能已经通过 `bmap()` 分配块，随后因 `copyin` 失败返回短写；它仍调用 `iupdate()` 记录 inode 状态。这类块在文件最终删除/截断时应被回收，`usertests badwrite` 专门反复验证不会因此耗尽磁盘块。

还有一个更细的部分副作用：`copyin()` 按用户页复制，一次块内复制若跨过用户页边界，可能先改写 `bp->data` 的前缀，再因后一页无效返回 `-1`。该次循环会在调用 `log_write(bp)` 之前退出，所以这段前缀不计入 `writei()` 返回值，`f->off` 也不为它推进；但缓存 buffer 已经被部分修改。若该块因本轮 `bmap()`/`bzero()` 或同一日志组中的更早操作而已经登记，提交仍可能取得 buffer 的最新内容；否则这段未登记修改只在缓存中暂时可见，不具备崩溃持久性保证。这是当前简单用户复制错误路径的真实限制。

## 13. 共享偏移的不变量与粒度

对 `FD_INODE`，可检查的不变量是：

- `f->off` 表示该打开实例下一次 I/O 的起点，不是 inode 的全局游标。
- `dup()` 和 `fork()` 共享同一个 `struct file`，所以共享 `off`。
- 独立 `open()` 创建不同 file，哪怕 `ip` 相同也有独立 `off`。
- 对一次 `readi()` 或一次分批写中的单轮 `writei()`，读取偏移、访问 inode 和推进偏移都位于同一 inode sleeplock 临界区。
- `ftable.lock` 不保护 `off`；它只保护 file 槽认领和 `ref` 变化。
- 大写入的整个系统调用并不持续持有 inode 锁，所以只保证各轮不会覆盖相同共享偏移，不保证整次调用的数据连续且不交错。

`usertests sharedfd` 在 `fork()` 后让父子用同一 fd 各写 1000 个 10 字节块。最终必须恰有 10000 个 `p` 和 10000 个 `c`。这个测试验证共享偏移和 inode 锁共同阻止覆盖；它没有证明大于 3072 字节的整次 write 原子。

## 14. `struct pipe` 与环形缓冲

`kernel/pipe.c` 定义：

```c
#define PIPESIZE 512

struct pipe {
  struct spinlock lock;
  char data[PIPESIZE];
  uint nread;
  uint nwrite;
  int readopen;
  int writeopen;
};
```

`nread` 和 `nwrite` 是累计计数，而不是始终位于 `[0, 511]` 的数组下标。访问数组时才取模：

```text
读槽  = data[nread  % PIPESIZE]
写槽  = data[nwrite % PIPESIZE]
```

先把计数视为不会回绕的逻辑整数，则在 `pipe.lock` 保护下应始终满足：

```text
nread <= nwrite <= nread + PIPESIZE
可读字节数 = nwrite - nread
空          = nread == nwrite
满          = nwrite == nread + PIPESIZE
```

实际字段是无符号 `uint`，累计超过 `UINT_MAX` 后会回绕。源码从不使用普通的 `<`/`>` 比较两个计数，只用空、满两个等式和取模下标；这些等式在无符号模运算下仍成立。回绕后的可检查形式是 `(uint)(nwrite - nread) <= PIPESIZE`，不能在 GDB 中看到 `nread > nwrite` 就直接断定损坏。只要生产者从不越过 512 字节容量、消费者从不越过生产者，槽复用仍与逻辑占用对应。

`readopen`/`writeopen` 也受 `pipe.lock` 保护。它们不是“当前有几个 fd”的计数，而是读端/写端 file 对象是否还存在。

## 15. `pipealloc()`：三个资源的构造与回滚

`pipealloc(&rf, &wf)` 按以下顺序构造：

1. 先把两个输出指针设为 0，便于统一清理。
2. `filealloc()` 分配读端 file。
3. `filealloc()` 分配写端 file。
4. `kalloc()` 分配保存 `struct pipe` 的页面。
5. 初始化 `readopen=1`、`writeopen=1`、两个计数为 0 和 `pipe.lock`。
6. 读端设置为 `FD_PIPE`、可读不可写并指向 `pi`。
7. 写端设置为 `FD_PIPE`、不可读可写并指向同一 `pi`。

任一步失败都跳转到 `bad`：已分配页面由 `kfree()` 释放，已分配 file 分别由 `fileclose()` 归还。由于只有完成 pipe 初始化后才把 file 类型设置为 `FD_PIPE`，构造中途关闭 `FD_NONE` file 不会误调用 `pipeclose()`。

成功返回时，调用者各拥有读端和写端的一个 file 引用。

### 15.1 `sys_pipe()` 安装两个 fd

`sys_pipe()` 先构造两个 file，再依次为读端和写端调用 `fdalloc()`。成功后把两个整数分别 `copyout()` 到用户数组。

失败回滚分两类：

- 任一 fd 分配失败：若读 fd 已安装，先把对应 `ofile` 槽清零；随后对读、写 file 各 `fileclose()` 一次。
- 任一整数 `copyout()` 失败：两个 `ofile` 槽都清零，再关闭两个 file。

两个整数是分两次复制的。如果第一个复制成功、第二个失败，内核资源会完整回滚，系统调用返回 `-1`，但用户内存中第一个整数可能已经改变；用户缓冲区不具备事务性。

## 16. `pipewrite()`：满缓冲、破管道和坏地址

写端取得 `pipe.lock` 后逐字节处理请求。真实循环顺序是：

```text
while i < n:
  若 readopen==0 或当前进程 killed -> 解锁并返回 -1
  若缓冲已满:
    wakeup(&nread)               提醒可能等待数据的读者
    sleep(&nwrite, &pipe.lock)   等待可写空间，返回时重新持锁
  否则:
    copyin 用户的一个字节
    成功则写 data[nwrite % 512]，nwrite++，i++
循环结束或 copyin 失败:
  wakeup(&nread)
  解锁
  返回 i
```

### 16.1 为什么满时等待 `&nwrite`

从不回绕的逻辑计数看，写者等待的条件是 `nwrite == nread + PIPESIZE`，继续条件是小于这个上界；实际源码只用满条件的无符号等式，避免回绕后的普通大小比较。代码用 `&pi->nwrite` 作为等待通道；`piperead()` 完成消费后对同一地址执行 `wakeup(&pi->nwrite)`。

等待通道只是一个稳定地址标识，不表示等待该变量自身被谁直接赋值。

### 16.2 关闭读端

每轮写入前都检查 `readopen`。最后一个读端引用关闭后，`pipeclose()` 把它设为 0，并唤醒等待在 `&nwrite` 的写者。写者恢复、重新持有 pipe 锁后看到无读者，返回 `-1`。

当前实现没有发送 Unix `SIGPIPE`，也没有 `EPIPE`；只有整数 `-1`。

若写者在此前已经写入若干字节，随后发现读端关闭，代码仍返回 `-1`，不会返回部分字节数。已经进入缓冲的数据不会回滚。

### 16.3 killed 与坏用户地址

进程被 `kkill()` 标记时，若正在 `sleep()`，`kkill()` 会把它改成 `RUNNABLE`。写者恢复后在循环顶部检查 `killed(pr)` 并返回 `-1`。即使此前已经写入一部分，返回值仍为 `-1`。

`copyin()` 失败采用另一种语义：循环直接 `break`，最终返回已经复制的字节数 `i`；首字节就失败时返回 0，而不是 `-1`。`usertests copyin` 因而接受 pipe 坏地址写返回 `-1` 或 0，但拒绝正数。

### 16.4 写入原子性边界

写者在有空间时持续持有 `pipe.lock`，其他写者无法插入。但缓冲满时 `sleep()` 会释放锁；多个写者可能在唤醒和再次填充之间交错。当前源码没有独立 `PIPE_BUF` 承诺，也不保证任意长度 write 作为整体不可分割。

特别是大于 512 字节的写必然至少等待一次，除非读者同步消费。文档不应把单写者测试 `pipe1` 推广成多写者原子性保证。

## 17. `piperead()`：空缓冲、EOF 和部分读取

读端取得 `pipe.lock` 后，首先处理空缓冲：

```text
while nread == nwrite && writeopen:
  若当前进程 killed -> 解锁并返回 -1
  sleep(&nread, &pipe.lock)
```

这里必须同时检查“空”和“写端仍开着”：

- 空且仍有写端：未来可能有数据，阻塞。
- 空且所有写端已关闭：未来不可能再有数据，返回 EOF，即 0。
- 写端已关闭但缓冲仍有数据：先把剩余数据读完；下一次空读才返回 0。

离开等待循环后，函数最多读取用户请求的 `n` 个字节，也会在当前缓冲被读空时提前返回。它不会为了填满整个用户请求继续等待下一批数据。

pipe 是字节流而不是消息队列。一次 `write()` 的边界不存入缓冲，一次 `read()` 可以只取某次 write 的一部分，也可以连续取得多次 write 留下的数据。

每个成功字节按以下顺序处理：

1. 从 `data[nread % PIPESIZE]` 取到局部变量；
2. `copyout()` 到用户地址；
3. 只有复制成功才执行 `nread++`。

因此坏用户地址不会消费尚未成功交付的那个字节。若第一个字节就 `copyout` 失败，返回 `-1`；若此前已经成功复制若干字节，则返回该部分字节数。结束前总会 `wakeup(&nwrite)`，使等待空间的写者重新检查条件。

## 18. 管道关闭与对象释放

`pipeclose(pi, writable)` 的第二个参数来自最终关闭的 file 快照：

- `writable != 0` 表示写端最后引用关闭：设 `writeopen=0`，唤醒 `&nread` 上的读者，使其读取剩余数据或看到 EOF。
- `writable == 0` 表示读端最后引用关闭：设 `readopen=0`，唤醒 `&nwrite` 上的写者，使其返回 `-1`。

修改标志和判断两端状态都在 `pipe.lock` 下完成。若两端均关闭，函数先释放自旋锁，再 `kfree(pi)`；否则只释放锁。

释放条件可写成：

```text
读端 file.ref == 0
且写端 file.ref == 0
  => readopen == 0 && writeopen == 0
  => 不会再有合法 fd 到达 pi
  => 可以释放 pipe 页面
```

`dup()` 和 `fork()` 增加 file 引用，所以某个进程关闭自己的副本不会过早改变 open 标志。只要任何进程仍持有一个继承的写端，读者就不会收到 EOF；这是 shell 必须关闭无用端点的根本原因。

## 19. `sleep()`/`wakeup()` 为何不会丢失管道事件

管道等待采用条件锁模式。以读者为例：

1. 持有 `pipe.lock` 检查 `nread == nwrite && writeopen`。
2. 调用 `sleep(&nread, &pipe.lock)`。
3. `sleep()` 先取得当前进程的 `p->lock`，再释放 `pipe.lock`，设置 `chan` 和 `SLEEPING` 并调度。
4. `wakeup(&nread)` 扫描进程时要取得目标 `p->lock`。

从“持有条件锁检查为空”到“在 `p->lock` 下声明睡眠”之间没有唤醒可以悄悄穿过：写者要取得 `pipe.lock` 才能改变 `nwrite`，而唤醒者与睡眠者又通过 `p->lock` 同步状态。

被唤醒不代表条件必然成立。`sleep()` 返回时已经重新取得 `pipe.lock`，读写两侧都使用 `while` 或循环顶部再次检查数据、空间、open 标志和 killed 状态。这也处理了多个等待者同时被 `wakeup()` 唤醒的情况。

`wakeup()` 会唤醒匹配通道的所有睡眠进程，随后由调度器和 `pipe.lock` 竞争决定谁先推进。实现没有读者、写者排队或公平性保证；长期竞争下不能从源码推出严格先到先服务。

锁和等待通道汇总如下：

| 状态 | 保护锁 | 等待通道 | 谁唤醒 |
| --- | --- | --- | --- |
| file 槽的 `ref/type` 认领状态 | `ftable.lock` | 无 | 不适用 |
| inode 内容与单轮普通文件偏移推进 | `ip->lock` sleeplock | sleeplock 内部通道 | inode 解锁者 |
| pipe 数据、计数和 open 标志 | `pi->lock` | `&pi->nread`、`&pi->nwrite` | 对端读写或关闭 |
| 日志容量和 outstanding 操作 | `log.lock` | `&log` | `end_op()`/提交结束 |

任何可能睡眠的 inode/log/pipe 底层清理都不在 `ftable.lock` 持有期间执行。

## 20. shell 流水线中的完整生命周期

以 `left | right` 为例，`user/sh.c` 的 `runcmd()` 执行：

```text
父 shell: pipe(p) -> p[0] 读端，p[1] 写端
  |
  +-- fork 左子进程
  |     close(1)
  |     dup(p[1])       -> 最低空槽为 1
  |     close(p[0])
  |     close(p[1])     -> fd 1 仍持有写端引用
  |     exec(left)
  |
  +-- fork 右子进程
  |     close(0)
  |     dup(p[0])       -> 最低空槽为 0
  |     close(p[0])
  |     close(p[1])
  |     exec(right)
  |
  `-- 父进程
        close(p[0])
        close(p[1])
        wait(any child)
        wait(any child)
```

左程序退出或关闭 fd 1 后，写端 file 的最后引用才能降为 0，右程序在读空缓冲后得到 EOF。若父 shell 忘记关闭 `p[1]`，`writeopen` 会保持为 1，右程序可能永久等待。父进程的两次 `wait(0)` 回收任意已退出子进程，不保证先左后右。

`user/init.c` 的 `dup(0)` 两次则把最初打开的 console file 安装到 fd 1 和 2。三个 fd 指向同一设备 file；设备 I/O 不使用普通文件 `off`，但引用计数和退出关闭规则完全相同。

## 21. 返回值与失败路径矩阵

### 21.1 打开文件层

| 操作/条件 | 当前结果 | 状态后果 |
| --- | --- | --- |
| fd 越界或槽为空 | `-1` | 无变化 |
| `read()` 作用于不可读 file | `-1` | 偏移和后端不变 |
| `write()` 作用于不可写 file | `-1` | 偏移和后端不变 |
| 设备 major 越界或回调为空 | `-1` | 不调用驱动 |
| `fstat()` 作用于 pipe | `-1` | 无变化 |
| `fstat()` 用户地址坏 | `-1` | 用户内存可能未完整更新，文件不变 |
| inode read 的 `copyout` 失败 | `-1` | `f->off` 不推进；用户缓冲区可能已部分改变 |
| inode write 某轮短写 | 整个 `filewrite()` 返回 `-1` | 已完成字节和偏移不回滚 |
| 未知 `struct file.type` 进入 I/O | panic | 表示内核不变量已破坏 |
| `close()` 仍有其他引用 | `0` | 只减 `ref`，底层仍打开 |
| `close()` 最后引用 | `0` | 关闭 pipe 端或事务性 `iput()` |

### 21.2 pipe 层

| 条件 | `pipewrite()` | `piperead()` |
| --- | --- | --- |
| 正常完成 | 返回写入字节数 | 返回读出字节数 |
| 请求数为 0 | 0 | 0，只要无需先等待；空且写端开时当前代码会先进入等待循环，因为等待发生在 `for` 前 |
| 满/空且对端仍开 | 睡眠等空间 | 睡眠等数据 |
| 所有读端关闭 | `-1`，即使先前有部分写入 | 不适用 |
| 所有写端关闭且仍有数据 | 不适用 | 读出剩余数据 |
| 所有写端关闭且已空 | 不适用 | 0，表示 EOF |
| 进程 killed | `-1` | 仅在空等待路径检查并返回 `-1`；已有数据时仍可先读取 |
| 首字节用户复制失败 | 0 | `-1` |
| 若干字节后用户复制失败 | 已写字节数 | 已读字节数 |

零长度 pipe read 有一个值得特别记录的当前行为：`piperead()` 在进入 `for (i=0; i<n; i++)` 前先执行“空且写端开”的等待循环，所以 `n==0`、空管道且仍有写端时会阻塞，而不是立即返回 0。这是源码现状，不应在文档中用常见 POSIX 语义覆盖它。

系统调用层没有统一拒绝负的 `n`。不同后端对负值的结果并不一致：pipe 写循环直接返回 0；空且写端仍开的 pipe 读甚至会先等待，之后因 `i < n` 为假返回 0；inode 写通常返回 `-1`；inode 读把 `int` 隐式转换为 `uint` 传给 `readi()`，可能因加法回绕返回 0，也可能被钳制到 EOF 后读取剩余内容。设备路径则由具体驱动决定。正常用户程序必须传入非负长度；这里记录的是接口校验的教学实现限制，不是建议依赖的 ABI。

## 22. 核心不变量清单

审阅或修改本模块时，应逐项保持以下不变量：

1. `ftable.file[i].ref == 0` 表示该槽可重新分配；认领和引用变化只能在 `ftable.lock` 下进行。
2. 每个已安装的 `p->ofile[fd]` 对应 `struct file.ref` 中恰好一个引用。
3. `fdalloc()` 成功只转移引用，不新建引用；`dup()`/`fork()` 才调用 `filedup()`。
4. 最后一次 `fileclose()` 不得在持有 `ftable.lock` 时调用可能睡眠的底层关闭。
5. `FD_INODE`/`FD_DEVICE` 的 file 在存活期间持有一个 inode 引用，最终关闭在日志事务内 `iput()`。
6. 共享 `struct file` 的普通文件偏移在 inode 锁保护下读取和推进。
7. 逻辑占用始终在 0 到 `PIPESIZE` 之间；考虑 `uint` 回绕时等价检查为 `(uint)(nwrite-nread) <= PIPESIZE`。生产者只在未满时增加 `nwrite`，消费者只在非空时增加 `nread`。
8. pipe 的数据、计数和 open 标志只能在 `pipe.lock` 下检查或修改。
9. 最后写端关闭必须唤醒读者，最后读端关闭必须唤醒写者。
10. 只有 `readopen==0 && writeopen==0` 时才能释放 pipe 页面。
11. `sleep()` 返回后必须重新检查条件；一次 `wakeup()` 不是条件成立的证明。
12. `sys_pipe()` 失败时不能在 `ofile[]` 留下任何指向已关闭 file 的槽。
13. 大 inode 写入的每个分块都必须由成对的 `begin_op()`/`end_op()` 包围。
14. 错误返回不自动意味着零副作用；用户复制、偏移推进和已提交分块可能已经部分发生。

## 23. 常见误读与当前实现限制

- “两个 fd 数字相同”不表示共享文件；必须先确认是否属于同一进程，以及 `ofile[]` 最终是否指向同一 `struct file`。
- “两个 fd 指向同一 inode”也不表示共享偏移；只有共享同一 `struct file` 才共享 `off`。
- `file.ref` 不是 inode `ref`，更不是磁盘 `nlink`。
- `close()` 不一定关闭底层对象；它通常只释放一个引用。
- pipe EOF 由所有写端引用消失触发，不由某一个写进程结束单独触发。
- `write()` 返回 `-1` 不等于调用原子失败。普通文件分块和 pipe 部分写都可能留下数据。
- 管道是 512 字节内存缓冲，不经过 inode、buffer cache 或磁盘日志。
- 普通文件写有日志保护，但一个大 write 被拆成多个 `begin_op()`/`end_op()` 范围；范围可分别提交，也可因并发被 group commit 合并，均不能把整个 write 提升为调用级原子操作。
- 当前模式位没有权限、owner、append、seek、非阻塞或 close-on-exec 语义。
- file 表和 fd 表都是固定数组线性扫描，适合教学，不适合高并发或大规模描述符工作负载。

## 24. 验证方法

### 24.1 静态结构检查

在仓库根目录确认关键定义和调用点仍存在：

```sh
rg -n 'struct file|struct devsw|FD_PIPE|FD_INODE|FD_DEVICE' kernel/file.h
rg -n 'filealloc|filedup|fileclose|fileread|filewrite' kernel/file.c
rg -n 'struct pipe|pipealloc|pipeclose|pipewrite|piperead' kernel/pipe.c
rg -n 'argfd|fdalloc|sys_open|sys_dup|sys_close|sys_pipe' kernel/sysfile.c
rg -n 'filedup|fileclose' kernel/proc.c
```

检查常量推导：

```sh
rg -n 'NOFILE|NFILE|NDEV|MAXOPBLOCKS' kernel/param.h
rg -n 'BSIZE' kernel/fs.h
rg -n 'int max =' kernel/file.c
```

当前结果应能推出每进程 16 个 fd、全局 100 个 file、10 个设备分派槽和 3072 字节的 inode 写分块。

### 24.2 构建与快速回归

先构建内核和用户程序：

```sh
make
```

再启动系统：

```sh
make qemu
```

在 xv6 shell 中分别运行：

```sh
usertests pipe1
usertests sharedfd
usertests bigwrite
usertests copyin
usertests copyout
usertests truncate1
```

预期每项输出 `OK` 和最终 `ALL TESTS PASSED`：

- `pipe1` 写入 5 组、每组 1033 字节，远大于 512 字节管道容量，验证满时睡眠、读者唤醒、顺序和 EOF。
- `sharedfd` 验证 `fork()` 后共享 file 偏移不会相互覆盖。
- `bigwrite` 从 499 字节递增测试到超过日志容量的写请求，验证 3072 字节分批。
- `copyin`/`copyout` 覆盖普通文件、console 和 pipe 的坏用户地址路径。
- `truncate1` 验证 `O_TRUNC` 和不同打开实例的读取行为。

完整快速套件为：

```sh
usertests -q
```

### 24.3 慢速、资源和回收测试

以下用例更适合在核心逻辑修改后运行：

```sh
usertests badwrite
usertests manywrites
usertests diskfull
usertests outofinodes
```

关注点分别是坏地址写后的块回收、并发写与磁盘路径死锁、磁盘块耗尽和 inode 耗尽。测试通过不仅要求系统调用返回合理，还要求内核不 panic、不泄漏到后续操作无法继续。

### 24.4 shell 端到端观察

在 xv6 shell 中执行：

```sh
echo hello | cat
echo first > fd-demo
echo second >> fd-demo
cat fd-demo
rm fd-demo
```

第一条应输出 `hello` 后返回提示符；若某处遗留写端引用，`cat` 会等待 EOF 而无法返回。后四条同时验证最低 fd 重用、重定向、`O_TRUNC` 与不截断写；注意 xv6 的 `>>` 只是以不带 `O_TRUNC` 的写方式重新打开，并没有真正的 `O_APPEND` 原子语义，独立打开的偏移仍从 0 开始，因此这里的结果应结合 `user/sh.c` 当前实现观察，不能按完整 Unix shell 假设。

### 24.5 GDB 断点

使用 `make qemu-gdb` 启动等待调试器的 QEMU 后，可在另一个终端连接并设置：

```gdb
b filealloc
b filedup
b fileclose
b filewrite
b pipealloc
b pipewrite
b piperead
b pipeclose
```

推荐观察：

- `dup()` 或 `fork()` 前后 `f->ref` 的变化；
- `sharedfd` 中父子是否拿到同一个 `struct file *`，以及 `f->off` 是否连续推进；
- `bigwrite` 中 `n1` 是否始终不超过 3072；
- `pipe1` 写满时 `nwrite == nread + 512`，睡眠返回后条件是否重新检查；
- 最后写端 `fileclose()` 是否以 `ff.writable==1` 进入 `pipeclose()`，读者随后是否得到 0；
- `sys_pipe()` 人为传入坏用户数组时，两个 file 的引用是否都归还。

调试阻塞路径时同时查看 `p->state`、`p->chan`、`pi->nread`、`pi->nwrite`、`readopen` 和 `writeopen`。断点会改变多 CPU 时序；判断正确性应以不变量和重复测试为主，而不是仅凭一次停顿顺序。

## 25. 修改本模块时的审阅顺序

1. 先确定改变的是 fd 槽所有权、file 引用、inode 引用还是 pipe 端点状态。
2. 为每个成功路径写出引用的来源和最终接收者。
3. 为每个失败分支列出已经取得的 file 槽、fd 槽、inode 引用和物理页。
4. 检查持有 `ftable.lock` 或 `pipe.lock` 时是否新增了可能睡眠的调用。
5. 若改变 pipe 条件，成对检查等待通道、唤醒通道、关闭唤醒和 `while` 重检。
6. 若改变 `filewrite()` 分块，重新推导最坏日志块数，不能只调大常量。
7. 若改变返回值，分别审阅零长度、负长度、首字节失败、部分完成、killed 和对端关闭。
8. 运行本节列出的定向测试，再运行 `usertests -q` 复核整体行为。

完成这些检查后，审阅者应能从任意用户 fd 追踪到其全局 file、底层资源、保护锁、等待点和最后释放条件，并能明确说明每条错误路径已经发生与尚未发生的状态变化。
