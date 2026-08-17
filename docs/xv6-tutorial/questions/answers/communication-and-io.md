# 通信与设备 I/O 答案与证据标准

## IO-00

descriptor 是进程私有 slot；`fork` 复制 slot，`dup` 占新 slot 并对同一个 file
执行 `filedup`。file object 由 `ftable` 管理，ref 归零才 `fileclose`。pipe object
由两端 file 共享，只有 `readopen==0 && writeopen==0` 才释放。proc 拥有 slots、cwd
和 parent；`wait` 才回收 zombie，而不是 `close`。

## IO-01

`runcmd` 建 pipe 后 fork 两个 child。left 关闭 fd1 和 read endpoint，`dup(p[1])`
到 fd1 后关闭原 p[1]；right 对称地把 p[0] dup 到 fd0。parent 关闭两端再 wait 两个
child。parent 忘记 writer 会让 right 永远等 EOF；left 忘记 reader 会令 broken-end
条件不成立。

## IO-02/03

writer 在 `nwrite == nread + PIPESIZE` 时睡在 `&nwrite`；reader 在空且 `writeopen`
时睡在 `&nread`。写入后 wake `&nread`，读取后 wake `&nwrite`。`pipeclose` 关闭
writer 时 wake reader，关闭 reader 时 wake writer。空且无 writer 的 read 返回 0；
无 reader 的 write 返回 -1。wakeup 只发布 RUNNABLE，恢复者必须重新取得 pipe lock
并重查谓词。

## IO-04

fixture 的 parent 通过 `iosnapshot(fd, pid, addr)` 读取原始 snapshot，要求目标 child
为 `state=SLEEPING` 且 `chan=READ/WRITE`、pipe occupancy 和 open flags 符合谓词；
`pause` 只作为调度机会。随后 parent `kill(pid)`，要求 child read/write 为 -1、
`wait` 得到同一 pid，并再次 snapshot 复算账本。若没有前置状态，timeout 和 kill 都
无法区分 blocked read、普通退出或调度延迟。

## IO-05/06

descriptor 指向 `FD_DEVICE` file，`devsw[CONSOLE]` 选择 file/device dispatch，`cons`
则保护输入 ring；三者不是同一 owner。`consoleread` 睡在 `&cons.r`，但 UART/PLIC 如何
产生 wakeup 是下一单元的完成路径。普通 inode read 没有 log reservation，修改路径才进入
`begin_op/end_op`；`readi` 到 `bread` 只是 buffer/device handoff。当前动态 marker 只证明
准确 inode read 与清理；cache miss、UART/PLIC/VirtIO completion 由下一设备单元建立，
完整 cache/LRU、log persistence 与 crash recovery 由持久化单元拥有。

## IO-07/08

`IO BASE` 与每个阶段的 snapshot 给出 fd slots、活动 file/ref、pipe 数、进程数、
空闲页、目标 state/channel、occupancy 和 endpoint flags；`PIPELINE` 还记录两 child
的 fd 1/0 拓扑，`FULL/EMPTY/KILL_*` 记录先观察到的睡眠状态。host 对字段集合、pid
唯一性、512 容量、结果码和阶段账本逐项复算，并要求 `IO PASS cleanup=1` 及第二次运行
的 BASE/after 相等。这建立 S/F/B/C 的有限事件证据；R 为 N/A。临时 fixture、单 hart
和串口只改变观察条件，不能证明所有交错、公平性、DMA memory order、持久化或形式化
正确性。
