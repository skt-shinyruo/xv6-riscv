# 文件描述符、管道、控制台与设备 I/O

## 问题场景与本单元成果

执行 `printf hi | wc` 时，字节不是从一个“文件”直接流到另一个“文件”。用户进程
持有 descriptor table entries；每个 entry 指向可共享的 `struct file` object；pipe
object 再拥有缓冲区和端点；它没有单独的 channel 字段，等待身份是 `&nread` 或
`&nwrite`，存入 sleeping process 的 `p->chan`。`fork()` 复制 descriptor entries，
`dup()` 复制 entry 而增加 file ref，`close()` 只在最后一个 endpoint 消失时释放
pipe。控制台把同一 file 接到 `consolewrite/consoleread`，UART 中断负责推进传输；
VirtIO 设备则以 buffer-cache 和 DMA descriptor 完成磁盘请求。

本单元的唯一出口是一个可独立复核的“通信与设备 I/O 账本报告包”。报告必须把
descriptor、file、pipe、process、sleep channel 和 device queue 分层，绘制一次 shell
pipeline 的 fork/dup/close/blocked/wakeup/EOF/reclaim 时间线，并用隔离的
`ioflow` bounded lab 验证正常传输、满/空、断端、被 kill 的等待者和最终清理。

## 前置单元与暂存黑盒

硬前置：[Copy-on-Write fork 证据项目](../experiments/copy-on-write.md)。相关单元是
[进程生命周期与回收](process-and-memory.md)与[调度、同步与等待](scheduling-and-synchronization.md)。
本单元解除 `console-device-path`，并拥有 descriptor/file/pipe/device ownership。
文件系统 inode、日志事务、buffer-cache 的持久化顺序留给后续 `core.persistence`；
这里只追踪普通 read 的 `fileread -> readi -> bread` 与修改路径的
`begin_op/end_op` 边界。DMA memory ordering、
调度公平性和所有 possible interleavings 不是本单元的结论。

## 最小模型和关键不变量

### 四层 ownership

| 层 | 代表 | 复制/释放规则 | 可观察字段 |
|---|---|---|---|
| descriptor | `p->ofile[fd]` | `fork` 复制 entry；`dup` 占一个新 slot；`close` 清 slot | fd 数、读/写权限 |
| file object | `struct file`、`ref` | 多个 entries/进程共享；最后一次 `fileclose` 才调用 endpoint close | `type/ref/pipe/ip/off` |
| communication object | `struct pipe` | `pipealloc` 一次分配两个 file 和一页 pipe；两端都关闭才 `kfree` | `nread/nwrite/readopen/writeopen`；其地址可作 channel identity |
| process/wait | `struct proc`、`p->chan` | blocked reader/writer 由 `sleep` 把 `&nread/&nwrite` 存入 proc，`wakeup` 只发布 RUNNABLE；`wait` 回收槽 | pid/state/chan/parent |

`filedup` 不增加 `struct file` object；它只增加 `ref`。pipe 的容量是
`PIPESIZE=512`，满时 writer 睡在 `&nwrite`，空且 `writeopen` 时 reader 睡在
`&nread`。关闭 writer 后，reader 在空缓冲区得到 EOF (`read==0`)；关闭 reader
后，writer 得到 `-1`。任何等待都必须在条件锁下重新检查谓词，不能把一次 wakeup
当作资源转移。

### shell pipeline 的两条边

`user/sh.c:runcmd` 先 `pipe(p)`，分别 fork left/right child；每个 child 关闭不需要
的端点，用 `dup` 把 pipe 端接到 fd 1/0，然后关闭原始 fd，最后 `exec`。parent 关闭
两个端点并 wait 两个 child。控制边是 `fork -> dup -> close -> exec -> read/write ->
exit -> wait`；ownership 边是 `descriptor -> file ref -> pipe endpoint -> buffer`。
若 parent 忘记关闭 writer，right child 永远看不到 EOF；若 child 忘记关闭 reader，
left writer 不能得到 broken-end 结果。

### console、PLIC、UART 与 VirtIO

`user/init.c:main` 打开 `console` 并通过 `dup` 保证 fd 0/1/2。`sys_read/sys_write`
经 `argfd` 找到 file object；`fdalloc` 只在 `dup`、`open` 和 `pipe` 等创建新
descriptor 的路径选择空 slot。`FD_DEVICE` 进入 `devsw[CONSOLE]`，
`consolewrite` 批量调用 `uartwrite`，`consoleread` 在 `cons.r == cons.w` 时睡在
`&cons.r`。`uartintr` 既清除 transmit busy 并 wake writer，也把接收字符交给
`consoleintr`；newline、`^D` 和完整输入缓冲区才唤醒 reader。PLIC 只负责 claim/
complete 中断源，不拥有用户 descriptor。

磁盘路径是另一条 ownership 链：普通 inode read 从 `fileread -> readi -> bread`
取得 buffer sleeplock；修改型路径才在相应 syscall 或 `filewrite` 中用
`begin_op/end_op` 包围事务。`virtio_disk_rw` 把 buffer 挂进 descriptor chain，
设备完成中断后 `virtio_disk_intr` wake waiter，随后 `brelse` 归还 cache。buffer、
log、DMA descriptor 都不是 pipe/file object；后续持久化单元才讨论 crash ordering。

## 源码追踪计划

```sh
rg -n '^struct file|^struct devsw' kernel/file.h
rg -n '^filealloc\(|^filedup\(|^fileclose\(|^fileread\(|^filewrite\(' kernel/file.c
rg -n '^pipealloc\(|^pipeclose\(|^pipewrite\(|^piperead\(' kernel/pipe.c
rg -n '^argfd\(|^fdalloc\(|^sys_read\(|^sys_write\(|^sys_dup\(|^sys_close\(|^sys_pipe\(' kernel/sysfile.c
rg -n '^runcmd\(|case PIPE|^main\(' user/sh.c user/init.c
rg -n '^consolewrite\(|^consoleread\(|^consoleintr\(' kernel/console.c
rg -n '^uartwrite\(|^uartintr\(' kernel/uart.c
rg -n '^plic_claim\(|^plic_complete\(' kernel/plic.c
rg -n '^bread\(|^brelse\(' kernel/bio.c
rg -n '^begin_op\(|^end_op\(' kernel/log.c
rg -n '^virtio_disk_rw\(|^virtio_disk_intr\(' kernel/virtio_disk.c
rg -n 'struct io_snapshot|iosnapshot\(|fileaudit\(|pipeaudit\(|procaudit\(|freepagecount\(' \\
  docs/xv6-tutorial/resources/communication-and-io/communication.patch
```

## 观察任务

先运行静态门和生成检查：

```sh
python3 docs/xv6-tutorial/resources/communication-and-io/run-lab.py --static-only
python3 docs/xv6-tutorial/tools/validate.py
python3 docs/xv6-tutorial/tools/generate_navigation.py --check
```

把 `printf x | cat` 画成一张表：每一行注明 pid、fd、`struct file` ref、pipe
endpoint、sleep channel、wakeup 触发器和释放动作。随后在 QEMU 中执行
`ioflow`，再运行 `usertests pipe1`、`usertests preempt` 和 `usertests killstatus`。

## 有界修改任务

资源 [`communication.patch`](../resources/communication-and-io/communication.patch)
只在 pinned baseline 的临时导出中加入 `_ioflow` 用户 fixture 和一个只读
`iosnapshot(fd, pid, addr)` audit syscall；它不是 kernel 修复，也不是学习者答案。
该 syscall 返回临时 `struct io_snapshot` 的原始账本字段：当前进程 fd slot 数、活动
`struct file` 数与总 ref、pipe 数、活动进程数、空闲页数、目标进程的 state/channel，
以及所选 pipe 的 occupancy/readopen/writeopen。`ioflow` 建立一个 pipe，并顺序验证：

1. 正常 fork/dup/close/read/write，父子各自只保留必要 endpoint；
2. 先写满 512 字节，再观察第 513 字节的 writer 进入 `SLEEPING`；reader 消费后
   writer 被唤醒，原 512 字节与尾随字节顺序不变；
3. writer 关闭后的空读返回 `0`；reader 关闭后的写返回 `-1`；
4. child 在空 pipe 上阻塞，parent 先观察 `SLEEPING` 再 `kill`，child 的 read 返回
   `-1`，随后 parent `wait`；
5. `dup` 槽耗尽时 `pipe()` 失败且账本不变；普通 inode read 返回准确的 README
   字节并恢复账本；所有 fd、child 和 pipe endpoint 关闭，第二次独立运行仍通过。

runner 从 guest 的原始 `IO BASE/PIPELINE/EMPTY/FULL/EOF/BROKEN/KILL_READ/KILL_WRITE/FD_ROLLBACK/DEVICE/PASS`
marker 逐字段复算 fd、file ref、pipe occupancy/endpoints、process state/channel、512 容量、
writer/reader blocked 前置、EOF、broken-end、killed waiter、inode read 和两次 cleanup；它同时
静态检查 `runcmd` 的 close/dup 拓扑、console/UART/PLIC/VirtIO anchor 以及 patch 只含 fixture
文件。`DEVICE read=16` 不声称每次都发生 VirtIO 请求：第二次运行可能命中 cache；外部
console read 和 device completion 的动态 trace 由后续“设备中断与队列”单元接手。
超时不是 oracle。

## Oracle、证据、失败路径和局限

| 维度 | 触发器 | 必须观察到 | 不能推出 |
|---|---|---|---|
| S | source anchors + fixture scope | 四层 ownership、pipe 条件循环、console/UART/PLIC/VirtIO 静态路径 | 运行时 device completion 或形式化 lock graph |
| F | normal `ioflow` transfer | byte sequence、descriptor closure、child wait | arbitrary scheduler fairness |
| B | full/empty/closed/killed waiter | `512` 后 writer sleep，EOF `0`，broken `-1`，killed status `-1` | all timing interleavings |
| C | `iosnapshot(fd,pid,addr)` 先读到目标 `SLEEPING` 与 channel，再 read/kill/wait | 命名 waiter 的 `SLEEPING -> RUNNABLE -> exit -> wait`，并复算 pipe occupancy/open flags 与资源账本 | multi-hart ordering、所有交错或公平性 |
| R | N/A | no persistent claim; private image removed | crash/recovery or DMA durability |

允许副作用仅为临时导出、build artifacts、私有 `fs.img` 和短寿命 QEMU。runner 最后
执行 `make clean`，确认 patch 可逆、共享工作树和共享 `fs.img` 不变。任何 fd/endpoint
未关闭、pid 未 wait、阶段账本未回到 BASE、marker 字段不符或回归失败都使报告无效。

## 退出产物与后续单元

报告包应包含 pipeline ownership 图、每阶段原始 marker 与 BASE/after 账本、
`ioflow` 两次 transcript、focused/quick/full 回归、patch/runner digest、cleanup 和
`C/R` 局限。下一张设备 I/O ticket 接手外部 console/disk event 的 runtime completion trace；
后续 `core.persistence` 接手 inode、buffer、log 和 crash/recovery，不重复本单元的
descriptor/pipe 解释。
