# 全局不变量与资源边界答案与证据标准

本页给出检查标准，不替代学习者自己的 owner 图和 raw report。答案中的每个结论都必须能回到
pinned source symbol 或 `FD_ROLLBACK` 原始字段。

## INVARIANT-00

完整图至少连接 `proc -> ofile[fd] -> struct file -> inode -> buffer -> log entry/pin ->
VirtIO request -> log/home bytes`。fd slot、file ref、inode ref、buffer identity、log reservation、
descriptor chain 与 durable bytes 是不同边；任何把它们画成一个“file owner”的答案都遗漏了
transfer 与失败边界。

## INVARIANT-01

总数只能排除部分 leak。例如两个 `(dev,blockno)` 同时占 buffer、另一个 block 没有 identity，
buffer 总数仍可等于 `NBUF`；把同一 page 交给两个 PTE 后 allocated-page count 也可能不变。稳定
边界应同时检查 identity 唯一性、每条 owner edge 和相应 ref/reservation；函数内部未发布的短暂
状态要由锁、单线程进程或私有构造期说明。

## INVARIANT-02

`pipealloc()` 依次取得两个 file objects 和一个 page，再把两端初始化为可读/可写 file。
`sys_pipe()` 取得两个 fd slots，并且只有两个 `copyout()` 都成功后才向 caller 返回 0；此时
caller 接管关闭责任。任一 `fdalloc()` 或 `copyout()` 失败都清槽并 `fileclose()` 两端，最后一个
endpoint 释放 pipe page；`pipealloc()` 的 `bad` path 则释放已取得的子集。两次 `copyout()` 不是
事务：user array 的首项或页尾前缀可以保留；`copyout()` 还可能先经 `vmfault()` 物化一个 lazy
page，随后在另一页失败，这个 page 由当前 process 保留到退出或缩容。保留的 bytes/fault page
都不代表已发布 fd，caller 必须忽略整个输出。

## INVARIANT-03

`allocproc()` 的 `USED` slot 尚未发布，trapframe/page-table failure 由构造者直接
`freeproc()`。`RUNNABLE` 后 scheduler 可认领；`kexit()` 关闭运行资源但发布 `ZOMBIE`，pid、status、
slot、trapframe 和 page table 保留给当前 parent；parent 退出时 `reparent()` 可把 zombie 转给
仍存活的 `initproc`。只有 `kwait()` 在成功交付 status 后调用
`freeproc()`；坏 status 地址必须保留 zombie 以便重试。

## INVARIANT-04

`begin_op()` 在 `log.lock` 下按保守不等式增加 aggregate `log.outstanding`，并不创建 per-operation
reservation object；`log_write()` 把 unique home block 登记并 pin identity；最后一个
`end_op()` 把 commit 责任交给 `commit()`。每次 disk request 临时取得三个 descriptor，IRQ 只
发布 completion/wakeup，requester 醒来后 reclaim chain；commit 仍负责 install、unpin 与 header
clear。IRQ context 不能调用可能 sleep 的 buffer/log path。

## INVARIANT-05

标准答案必须逐项落到 caller：`allocproc()` 失败由 `kfork()` 转成 `-1`，而 `userinit()` 没有同样
的受控返回；`filealloc()`/`fdalloc()` 返回空并由相应 syscall 转成 `-1`；`kalloc()` 返回 0，但
lazy hardware fault 可转成 kill，启动路径也可 panic；`iget()` 与
`bget()` 无 victim 直接 panic；`begin_op()` 和 VirtIO descriptor shortage sleep；write/pipe
路径还可保留 partial prefix。相同“容量满”不承诺相同 API 语义。

## INVARIANT-06

PLIC claim token 只描述一次 external interrupt dispatch。device handler ACK/读取设备状态并
发布 request completion，`plic_complete(irq)` 归还 claim token；`wakeup()` 只改变 waiter state。
descriptor、buffer 和 syscall result 仍由醒来的 requester/relevant owner 处理。一个动作不能
替代另一个，也不能由 IRQ 进入 sleeplock 或 log admission path。

## INVARIANT-07

当前关系是 `NOFILE=16`、BASE `fd=3`、`filled=13`、下一次 `pipe=-1`。fixture 在 failure
前后内部比较 fd、active files、refs、pipes、procs、free pages；host 能从公开 marker 复算的
部分是关闭 duplicates 后的 `FD_ROLLBACK AFTER==BASE`，failure-point ledger 本身不是 raw marker。
否则可能只是返回值正确而 file/page 泄漏。第二轮、回归、reverse
和 process cleanup 排除 fixture 污染。由于表已满，第一个 `fdalloc(rf)` 就失败，没有新 fd
被安装；因此它不动态证明 `fd0` 已安装而 `fd1` 失败的 cleanup，也不证明
`NFILE/NINODE/NBUF/log/NUM` 的 exhaustion semantics。

## INVARIANT-08

`proc_mapstacks()` 为所有 `NPROC` slots 预分配 kernel stack，所以扩容立即增加永久页；
`scheduler()`/`wakeup()` 的线性扫描上界随之增长。每个新 process 最多持 `NOFILE` entries，fork
会增加既有 file objects 的 refs，但不会消耗新的 `NFILE` slot；只有扩容后的 workload 继续
`filealloc()` 创建 distinct file objects 时，`NFILE` 才可能先成为瓶颈。page pool 则会直接承受
新增 stacks/process state。答案应给关系式与 source anchors，而不是承诺固定 wall-clock slowdown。

## INVARIANT-09

`LOGBLOCKS` 与 `NBUF` 都由 `MAXOPBLOCKS*3` 派生，但相等不是 headroom proof。必须重新核对
admission inequality、每 operation unique home blocks、`filewrite()` chunk、pin peak、commit
临时 buffers、filesystem log layout 与设备 backpressure。S 可核对公式和 path，B/C 需要受控
capacity/wait trigger，R 需要 crash/tear oracle；普通 `usertests` 只能提供有限 F/B regression。

## INVARIANT-10

合格结论按“陈述 invariant -> 指定 trigger -> 保存 raw observation -> 区分 fixture 内部断言与
host 可复算字段 -> 检查 cleanup ->
列 remaining gaps”闭合。测试通过只能支持已触发路径和配置，不能推出形式证明、公平性、所有
interleaving、真实介质持久性或未触发资源池的失败语义。
