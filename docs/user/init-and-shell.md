# `init` 与 shell：第一个用户进程和命令执行器

本文说明当前仓库中 `user/init.c` 与 `user/sh.c` 的实际实现。它们代码量不大，却把启动、文件描述符、进程、`exec`、管道和孤儿回收连接在一起。阅读时需要区分三个角色：内核创建的首进程、由它反复启动的 shell，以及 shell 为每一行命令创建的执行进程。

相关专题：

- [启动与初始化](../architecture/boot-and-init.md)
- [进程与调度](../kernel/processes-and-scheduling.md)
- [`exec` 装载](../kernel/exec.md)
- [文件与管道](../kernel/files-and-pipes.md)
- [`fork -> exec -> wait` 执行链](../flows/fork-exec-wait.md)

## 1. 总体进程关系

本分支不是从内核内嵌的 `initcode` 启动用户态。`kernel/proc.c:userinit()` 先创建一个没有用户程序映像的进程；该进程第一次经 `forkret()` 运行时，内核完成 `fsinit(ROOTDEV)`，再调用 `kexec("/init", ...)` 从文件系统装入 `/init`。

正常启动后的核心关系是：

```text
kernel userinit process
  `- exec /init                 PID 1, initproc
       `- fork + exec sh        foreground shell
            `- fork             one input-line runner
                 |- exec tool   simple command
                 |- fork ...    pipe/list/background descendants
                 `- exit
```

这里通常把首进程称为 PID 1，但代码真正依赖的是内核全局指针 `initproc`，而不是硬编码 PID。所有失去原父进程的子进程都会被 `reparent()` 转交给它。

## 2. `init` 建立标准文件描述符

`user/init.c:main()` 首先确保控制台设备存在并打开：

```c
if (open("console", O_RDWR) < 0) {
  mknod("console", CONSOLE, 0);
  open("console", O_RDWR);
}
dup(0); // stdout
dup(0); // stderr
```

这段代码依赖文件描述符分配“选择当前进程最低空闲槽位”的规则：

1. 第一次成功的 `open()` 返回 `0`，成为标准输入。
2. 第一次 `dup(0)` 返回 `1`，成为标准输出。
3. 第二次 `dup(0)` 返回 `2`，成为标准错误。

三个 fd 引用的是同一个全局 `struct file`，因此共享文件对象和偏移。控制台设备不依赖普通文件偏移，所以这种共享正合适。若 `console` 尚不存在，`mknod("console", CONSOLE, 0)` 创建一个设备 inode；主设备号 `CONSOLE` 使后续读写经 `devsw` 分派到控制台驱动。

这里没有检查第二次 `open()` 或两个 `dup()` 的返回值。系统刚启动、资源表为空是它的隐含前提；若这些操作仍失败，后续诊断输出也可能不可见。

## 3. `init` 是 shell 守护者

完成 fd 初始化后，`user/init.c` 永久执行两层循环：

```text
outer loop:
  print "init: starting sh"
  pid = fork()
  child: exec("sh", {"sh", 0})

  inner loop:
    wpid = wait(0)
    if wpid == shell pid:
      break and restart shell
    if wpid < 0:
      call exit(1), then kernel panics because initproc may not exit
    otherwise:
      reap an adopted orphan and keep waiting
```

子进程成功 `exec` 后，继承 fd 0、1、2 和当前目录 `/`。`exec` 只替换地址空间，不关闭普通 fd，所以 shell 可以立即通过控制台读写。若 `exec("sh", argv)` 失败，子进程打印错误并 `exit(1)`；父进程随后回收它并再次启动 shell。

`init` 不会在 shell 异常退出后停止系统，而是回到外层循环重新创建 shell。这是它作为用户态守护进程的主要职责。

“永久循环”描述的是正常路径，不代表源码没有错误出口。`fork()` 失败或用户代码实际观察到 `wait()` 返回负数时，都会打印诊断并主动 `exit(1)`；`kexit()` 一进入便检查 `p == initproc` 并 `panic("init exiting")`，检查发生在关闭 fd、释放 cwd 和 reparent 之前。因此这些分支不是可恢复的用户态关机，也不会留下另一个进程接替收养者，而是直接使内核停止。

当前 `wait(0)` 没有 status copyout 失败的可能；在 shell 仍是 child 时，“没有 child”也违反该循环的不变量。若 init 是因为被 kill 而使内核 `kwait()` 返回 -1，`usertrap()` 会在系统调用结束后、返回用户态前发现 kill 标志并直接调用 `kexit(-1)`，所以 `init.c` 的 `wpid < 0` 分支反而看不到这次返回，内核仍会以同一个 `panic("init exiting")` 停止。该分支主要是用户代码的防御性终局。

## 4. 为什么 `init` 必须回收“不是 shell”的进程

当任意进程退出时，`kernel/proc.c:reparent()` 会把它的所有直接子进程改挂到 `initproc`。循环只检查 `pp->parent == p`，不会排除已经退出但尚未回收的 ZOMBIE：活着的 adopted child 以后退出时成为 init 的 zombie，已经是 ZOMBIE 的 child 则在转交后可被 init 立即回收。`reparent()` 对每个转交项唤醒 init 尤其重要，因为后一类 child 不会再次执行 exit 来发送通知。

`wait()` 返回 proc table 扫描中第一个已退出的直接 child，而不只返回 shell，也不保证按退出时间排序。

因此内层循环必须判断：

- `wpid == pid`：前台 shell 已退出，应该重启；
- `wpid > 0 && wpid != pid`：回收了一个被收养的孤儿，继续等待 shell；
- `wpid < 0`：理论上已经没有子进程，与 shell 仍应存在的约束矛盾，视为致命错误。

后台命令展示了这条路径。shell 的命令执行器为后台命令再 `fork()`，随后外层命令执行进程退出；后台孙进程失去父进程后被交给 `init`，最终由 `init` 回收。

## 5. shell 启动时校正 fd 0、1、2

`user/sh.c:main()` 不能假定自己总由正常的 `init` 启动，因此再次执行：

```c
while ((fd = open("console", O_RDWR)) >= 0) {
  if (fd >= 3) {
    close(fd);
    break;
  }
}
```

只要 0、1、2 中还有空位，`open()` 就填充最低空位；当返回值达到 3，说明前三个槽位都已占用，临时的 fd 3 随即关闭。正常情况下 `init` 已准备好三项，循环只打开并关闭一次 fd 3。

这段逻辑只保证“前三个 fd 已打开”，不保证它们分别具有理想的读写权限；正常继承自 `init` 才提供完整语义。

## 6. shell 的顶层读取循环

`getcmd()` 向 fd 2 写 `$ `，清空 100 字节静态缓冲区，再用 `gets()` 从 fd 0 读取一行。返回空字符串表示 EOF，shell 随即退出，`init` 会重启它。

顶层循环只在当前 shell 进程中直接处理一个内建命令：

```text
cd path
```

`chdir()` 必须由 shell 本身调用。若像普通命令一样先 `fork()`，子进程改变的 `cwd` 会随子进程退出而消失，父 shell 的工作目录不会改变。当前识别条件是前三个字符恰为 `c`、`d`、空格；它不支持单独的 `cd`、`cd\tpath`、HOME 展开或带引号路径。

除空行与 `cd ` 外，每行命令都遵循：

```c
if (fork1() == 0)
  runcmd(parsecmd(cmd));
wait(0);
```

因此交互 shell 本身不会被普通命令的 `exec()` 替换；解析、重定向和进一步派生都发生在“一行命令执行进程”内。即使语法错误触发 `panic()`，通常也只终止这个子进程，主 shell 仍能读取下一行。

这里两层 `fork1()` 的失败范围不同。若主 shell 在创建 line runner 时 fork 失败，`fork1() -> panic() -> exit(1)` 终止的是主 shell，init 随后识别其 pid 并重启 shell。若已经创建的 runner 在执行 `LIST`、`PIPE` 或 `BACK` 时 fork 失败，终止的只是 runner；主 shell 的 `wait(0)` 回来后继续读下一行，init 不会重启它。部分派生已经成功时没有事务式回滚，例如 pipe 左侧已创建而右侧 fork 失败，runner 退出会关闭自己的 pipe fd，留下的 child 被 reparent 给 init；它可以继续运行，而后续写入在所有读端已关闭时得到错误。

100 字节输入缓冲区还有一个容易误判的边界。`gets(buf, 100)` 最多读取 99 个字节且不报告“尚未遇到换行”；更长的一条物理输入行会被顶层循环当成多条命令依次执行。若首段以 `cd ` 开头且因容量或 EOF 没有以换行结尾，`cmd[strlen(cmd)-1] = 0` 仍无条件执行，会删掉路径的最后一个真实字符，剩余输入随后又会被当成新命令。这里没有“整行过长则拒绝”的机制。

## 7. AST 数据结构

shell 把命令解析为五种带公共首字段的结构：

| 类型 | 结构 | 语义 |
|---|---|---|
| `EXEC` | `struct execcmd` | 程序名与最多 9 个参数 |
| `REDIR` | `struct redircmd` | 包装子命令，并替换一个 fd |
| `PIPE` | `struct pipecmd` | 左、右命令通过管道并发运行 |
| `LIST` | `struct listcmd` | 左命令完成后运行右命令 |
| `BACK` | `struct backcmd` | 派生子进程后不等待 |

所有结构的第一个成员都是 `int type`，所以 `struct cmd *` 可以作为手写的 tagged union 使用。`runcmd()` 先检查 `type`，再转换为相应具体结构。构造器通过 `malloc()` 分配节点并整体清零。

`MAXARGS` 为 10，但数组最后一个槽位必须存放空指针，所以一条简单命令最多有 9 个非空参数项，包括 `argv[0]`。超过限制会触发 shell 的 `panic("too many args")`。

## 8. 词法扫描：指针区间而非字符串复制

`gettoken()` 在原始输入缓冲区内移动 `*ps`，用 `[q, eq)` 返回 token 的起止地址：

- 空格、制表、回车、换行和纵向制表属于空白；
- `< | > & ; ( )` 是单字符符号；
- `>>` 被编码为特殊 token `'+'`；
- 其他连续字符返回类型 `'a'`，表示普通参数；
- 到缓冲区结尾返回 `0`。

扫描阶段不复制参数，也不会立即写 `NUL`。`execcmd.argv[]` 保存 token 起点，`eargv[]` 保存终点；重定向节点同样保存 `file` 与 `efile`。全部解析成功后，`nulterminate()` 遍历 AST，把每个终点字符改为 `0`，原始输入缓冲区才变成一组可交给 `exec()`/`open()` 的 C 字符串。

这一设计减少了复制和分配，但让 AST 的字符串生命周期依赖顶层静态 `buf`。本实现会在该缓冲区被下一行覆盖前完成执行，因此有效。

不支持的词法能力包括引号、反斜杠转义、变量、通配符和命令替换。符号无法通过转义成为普通文件名字符。

## 9. 递归下降语法与优先级

解析入口是 `parsecmd()`，主要调用关系为：

```text
parsecmd
  `- parseline                 handles & and ;
       `- parsepipe            handles |
            `- parseexec       words, redirects, (...)
                 |- parseredirs
                 `- parseblock -> parseline
```

可以把支持的语法近似写成：

```text
line  := pipe ('&')* (';' line)?
pipe  := exec ('|' pipe)?
exec  := block-or-words-with-redirections
block := '(' line ')' redirection*
redir := '<' word | '>' word | '>>' word
```

由调用层次可得：

1. 单词、括号和重定向结合最紧。
2. `|` 次之，并因递归右部而构造为右结合 AST。
3. `&` 包装已经解析完成的 pipe。
4. `;` 最弱，右侧递归解析整条 line。

例如：

```text
a | b ; c &
```

概念 AST 是：

```text
LIST
|- PIPE(a, b)
`- BACK(c)
```

`parsecmd()` 最后确认输入已完全消费；存在无法归入语法的剩余字符时打印 `leftovers` 并报语法错误。

## 10. `EXEC`：用 `exec` 替换执行进程

`runcmd(EXEC)` 对空命令直接退出，否则调用：

```c
exec(ecmd->argv[0], ecmd->argv);
```

成功时不会返回，当前命令执行进程的地址空间被目标 ELF 替换，但继承的 fd 和 cwd 保留。失败时打印 `exec ... failed`，落到统一的 `exit(0)`；因此这个教学 shell 的退出状态不能可靠区分 `exec` 失败和成功命令，父 shell 也不检查状态。

shell 不搜索 `PATH`。用户必须给出当前目录可解析的名字或绝对/相对路径；初始文件系统把工具放在根目录，初始 cwd 也是 `/`，所以输入 `ls` 能解析为 `/ls`。

## 11. `REDIR`：利用最低可用 fd 规则

重定向节点保存目标 fd、文件名和打开模式。执行时：

```text
close(target fd)
open(file, mode)
runcmd(child)
```

因为 `open()` 分配最低空闲 fd，关闭 0 或 1 后，成功打开的文件应恰好占据该编号：

- `< file`：`O_RDONLY`，替换 fd 0；
- `> file`：`O_WRONLY | O_CREATE | O_TRUNC`，替换 fd 1；
- `>> file`：`O_WRONLY | O_CREATE`，替换 fd 1。

实现只检查 `open() < 0`，没有验证返回值确实等于 `rcmd->fd`。该不变量依赖执行进程没有其他更低编号的空洞。

需要特别注意：当前内核没有 `O_APPEND`，`sys_open()` 也不会在 `O_CREATE` 时把 `off` 移到文件末尾。因此 shell 虽把 `>>` 解析成不同模式，它实际上只是“不截断后从偏移 0 写”，会覆盖文件开头，而不是真正的追加。这是当前源码语义，不应按完整 Unix shell 理解。

多个重定向通过嵌套节点执行；外层先打开，内层随后可能再次关闭同一 fd。它们的最终效果取决于 AST 包装顺序，本实现没有为复杂重复重定向提供 POSIX 兼容保证。

## 12. `LIST`：顺序执行

`runcmd(LIST)` 先为左节点创建子进程并等待其退出，再在当前执行进程中尾调用右节点：

```text
runner
  |- fork left -> run left -> exit
  |- wait left
  `- run right in runner
```

右侧无需额外 `fork()`，因为整个 runner 本来就是交互 shell 为这一行创建的临时进程。右侧完成后 runner 退出，顶层 shell 的 `wait()` 返回。

分号不是条件执行：无论左侧退出状态为何都会运行右侧。本 shell 没有 `&&` 或 `||`。

## 13. `PIPE`：两个并发端点

`runcmd(PIPE)` 调用 `pipe(p)` 获得读端 `p[0]` 和写端 `p[1]`，再创建两个子进程：

```text
left child:                 right child:
  close(1)                    close(0)
  dup(p[1]) -> fd 1           dup(p[0]) -> fd 0
  close(p[0], p[1])           close(p[0], p[1])
  run left AST                run right AST

pipe runner:
  close(p[0], p[1])
  wait left
  wait right
  exit
```

左右两侧必须并发运行。如果先等待左侧再启动右侧，左侧写满 512 字节管道缓冲区后会睡眠，而无人读取，形成死锁。

所有不再使用的端点都必须关闭。特别是 pipe runner 必须关闭自己的写端，否则右侧即使在左侧退出后也仍看到一个 writer 引用，`piperead()` 无法得到 EOF。子进程在 `dup()` 后关闭原始端点，避免同样的引用泄漏。

管道节点等待两个直接子进程，但不区分返回顺序，也不传播退出状态。多级管道来自右结合 AST；每一层 runner 创建自己的管道和两侧进程，最终仍能形成完整数据流。

## 14. `BACK`：脱离当前等待链

`runcmd(BACK)` 创建子进程执行被包装的 AST，父 runner 不等待，直接走到 `exit(0)`。顶层交互 shell 等到的只是 runner，于是很快显示下一个提示符。

后台子进程仍继承控制台和 cwd，并没有会话、进程组或作业控制。它可以继续与前台命令竞争控制台输入、交错输出。runner 退出后它被 `init` 收养，最终由 `init` 的内层 `wait()` 回收。

## 15. 括号的作用

`parseblock()` 解析 `( line )`，随后允许在右括号后附加重定向。括号创建语法分组，但 AST 没有单独的 SUBSHELL 类型。顶层命令本来就在子进程中运行，管道/list/background 又按各自规则派生，所以分组可以通过现有进程边界获得足够的教学 shell 语义。

例如：

```text
(cat README ; echo done) > out
```

形成包裹整个 `LIST` 的 `REDIR`，两个子命令都继承被替换的 fd 1。

## 16. 资源所有权和退出路径

一行普通命令涉及的资源可以按下面追踪：

| 资源 | 创建者 | 转移/继承 | 释放者 |
|---|---|---|---|
| AST 节点 | line runner 的解析器 | 只在同一地址空间使用 | 成功 `exec` 时随旧页表释放；若进程 `exit`，则要等父进程 `wait()` 进入内核 `kwait()/freeproc()` 才释放整个地址空间，代码不逐节点 `free` |
| 输入缓冲区 | shell 静态区 | `fork` 后私有副本 | 进程退出或下一轮覆盖 |
| 程序 fd | `init`/重定向/管道 | `fork` 复制引用，`exec` 保留 | 显式 `close`，或 `kexit()` 在进程变为 `ZOMBIE` 前逐项 `fileclose()` |
| 管道对象 | `pipe()` | 两端 fd 引用 | 所有读写端引用关闭后释放 |
| 子进程槽位 | `fork()` | parent 指针确定等待者 | 父进程 `wait()` 回收；孤儿由 `init` 回收 |

AST 不调用 `free()` 不是跨命令的持续泄漏：解析发生在很快退出或 `exec` 的临时 runner 中。成功 `exec` 会立即释放旧页表；`exit` 只先把进程变为 `ZOMBIE`，其地址空间随后由父进程的 `wait()` 回收。只有内建 `cd` 不解析 AST。

## 17. 失败和边界语义

| 情况 | 当前行为 |
|---|---|
| init 创建 shell 的 `fork()` 失败 | init 打印错误并主动 `exit(1)`；内核随即 `panic("init exiting")` |
| init 的 `wait()` 错误分支被执行 | init 打印错误并主动 `exit(1)`，内核 panic；若 init 已被 kill，通常在返回用户态前就已 panic |
| 主 shell 创建 line runner 的 `fork()` 失败 | `fork1()` 经 `panic()` 退出主 shell；init 回收该 pid 后重启 shell |
| runner 内 `LIST/PIPE/BACK` 的 `fork()` 失败 | `fork1()` 经 `panic()` 只退出 runner；主 shell 继续，已创建的后代可能由 init 收养 |
| `exec` 失败 | 当前执行进程打印错误后退出，原地址空间仍有效到退出为止 |
| 重定向 `open` 失败 | 打印错误并以状态 1 退出该 runner |
| `pipe()` 失败 | `panic("pipe")` 终止 runner |
| 空行 | 顶层 shell 忽略，不派生进程 |
| fd 0 EOF | shell 退出，`init` 随后重新启动它 |
| 输入行超过 99 字节 | 被分段解析和执行，不报告截断；截断的 `cd ` 还会误删首段最后一个真实字符 |
| 参数过多、缺文件名、括号不配对 | 解析进程打印错误并退出 |
| 命令返回非零 | 父 shell 不解释状态，仍继续下一轮 |

该 shell 没有信号处理、环境变量、权限、用户身份、作业控制、历史记录或退出状态展开。它的目标是直接展示内核原语组合，而不是提供完整命令语言。

## 18. 关键不变量

调试 `init` 和 shell 时，优先检查以下条件：

1. init 的正常路径持续调用 `wait()` 回收 adopted children；源码错误分支会主动 `exit(1)`，但内核把任何 `initproc` 退出升级为 panic。
2. 正常 shell 的 fd 0、1、2 都指向 console；重定向只在子执行进程中改变它们。
3. 除 `cd` 外，顶层 shell 不直接运行命令，因而不会被 `exec` 替换。
4. `exec` 成功后 fd 与 cwd 保留，AST 和旧用户内存全部消失。
5. 管道每个进程只保留实际需要的端点；所有写端关闭是读端看到 EOF 的必要条件。
6. `argv[]` 和文件名指针只有在 `nulterminate()` 后才是合法 C 字符串。
7. `wait()` 回收的是直接子进程；后台后代最终依靠 reparent 到 `init`。

## 19. 建议的源码跟踪与验证

启动链断点：

```gdb
b forkret
b kexec
b usertrap
b kfork
b kwait
```

shell 执行链可在 `user/sh.c:runcmd()`、`parsecmd()`、`parsepipe()` 和 `nulterminate()` 处断点，并观察 `cmd->type`。由于用户程序链接在虚拟地址 0，切换不同进程时要确认当前符号文件与地址空间。

运行级验证可以覆盖：

```sh
echo hello
cat README | wc
echo one ; echo two
(echo grouped ; cat README) > out
cat < out
echo background &
```

本镜像未必包含示例中的所有外部程序；应以 `Makefile:UPROGS` 为准。验证管道卡死时，可检查每个相关进程的 fd 表以及 pipe 的 `readopen`、`writeopen`、`nread`、`nwrite`，通常能定位未关闭端点或睡眠条件问题。

## 20. 阅读落点

理解本模块后，应能从一条输入命令回答：谁解析、谁 `fork`、哪个进程改变 fd、哪个进程被 `exec` 替换、父进程等待谁、后台进程最终由谁回收。后续可继续阅读 [文件与管道](../kernel/files-and-pipes.md)追踪 fd 的内核表示，或沿 [`fork -> exec -> wait`](../flows/fork-exec-wait.md)观察完整跨层调用链。
