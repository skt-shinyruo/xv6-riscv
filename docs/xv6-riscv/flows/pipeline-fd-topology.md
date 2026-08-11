# 完整 pipeline 的 fd 拓扑与 EOF 证明

以 `left | right` 为例，本文追踪 shell AST 执行期间每个 fd、全局 `struct file` 和 `struct pipe` 的引用。背景见[文件与管道](../kernel/files-and-pipes.md)和[`init` 与 shell](../user/init-and-shell.md)。

## 1. 进程层级

交互 shell 先 fork 一个命令子进程 `P`，`P::runcmd(PIPE)` 创建 pipe，再 fork `L` 和 `R`：

```text
shell
  `- P: owns pipe while constructing topology, waits L and R
       |- L: runcmd(left), stdout -> pipe write end
       `- R: runcmd(right), stdin  -> pipe read end
```

`P` 不 exec；关闭自身 pipe fd、等待两个孩子后退出。shell 等待 `P`。嵌套 pipeline 会递归重复这一模式，每个 `PIPE` AST 节点有自己的 pipe object 和中间管理进程。

## 2. 三层引用

```text
proc.ofile[fd] -> global struct file (ref counted by filedup/fileclose)
global read-file  -> same struct pipe, readable=1
global write-file -> same struct pipe, writable=1
pipe.readopen/writeopen describe whether corresponding global file is still alive
```

`fork()` 复制 fd 槽并对每个非空 file 执行 `filedup`。`dup()` 新增 fd 槽并增加同一 file 的 ref；不会创建新的 pipe endpoint object。

## 3. 精确引用变化

假设新 pipe fd 是 `p[0]=3,p[1]=4`，`fr/fw` 是两端全局 file。下表选择一个合法交错：P 完成两次 fork 后，L、R 再依次执行 close/dup；实际 scheduler 可让 child 更早关闭，所以中间 ref 数可能更低，但每个进程的增减和最终拓扑相同：

| 阶段 | `fr.ref` | `fw.ref` | 关键 fd |
|---|---:|---:|---|
| `P: pipe(p)` 后 | 1 | 1 | P: 3=fr,4=fw |
| fork L 后 | 2 | 2 | P 与 L 各有 3/4 |
| fork R 后 | 3 | 3 | P、L、R 各有 3/4 |
| L `close(1); dup(4)` | 3 | 4 | L: 1=fw,3=fr,4=fw |
| L close 3/4 | 2 | 3 | L 只留 1=fw |
| R `close(0); dup(3)` | 3 | 3 | R: 0=fr,3=fr,4=fw |
| R close 3/4 | 2 | 2 | R 只留 0=fr |
| P close 3/4 | 1 | 1 | 只剩 L writer、R reader |

表中还未计入进程原有 stdin/stdout/stderr 所指的 console file refs；关闭 0/1 会相应减少它们。`dup` 返回最低空 fd，所以先 close 标准 fd 是把 endpoint 精确安装到 0 或 1 的前提。当前 shell 未检查 `dup` 返回值，依赖该槽确实成为最低空位。

在这条正常拓扑里，`NOFILE` 已满也不能让两次 dup 失败：child 刚关闭目标标准 fd，已经保证至少有一个空槽，源 pipe fd 又仍有效，`fdalloc()` 必然把最低空槽装回 0/1。故障注入若要覆盖未检查的返回值，必须破坏源 endpoint/标准 fd 前提或改变操作顺序，不能把它描述成普通 fd 数量耗尽路径。

## 4. 数据与阻塞

L 的 `write(1,buf,n)` 进入 `pipewrite`，持 `pi->lock` 每次从用户复制 1 字节并增加 `nwrite`。容量为 512；满时唤醒 readers，再 `sleep(&pi->nwrite,&pi->lock)`。因为 writer 持锁时 reader 不能同步消费，任何成功写入超过 512 字节的单次 write 至少睡眠一次。

R 的 `read(0,buf,n)` 在空且 `writeopen` 时睡在 `&pi->nread`。读出字节后推进 `nread` 并唤醒 writers。`nread/nwrite` 是 32 位 `uint`，分别递增并最终回绕；数组索引用 `%512`。在 `pi->lock` 下，应按模 \(2^{32}\) 的无符号差解释容量，而不是把原始计数当作永久单调整数：

```text
(uint)(nwrite - nread) <= PIPESIZE(512)
```

## 5. EOF 与释放证明

当 L exec 后正常退出，`kexit()` 关闭 fd 1。若这是 `fw.ref` 的最后一个引用，`fileclose()` 调 `pipeclose(writable=1)`：

```text
pi.writeopen=0; wakeup(&pi.nread)
```

R 在已读尽缓冲且 `writeopen==0` 时返回 0，即 EOF。R 最后关闭读端使 `readopen=0`；两端都为 0 时 `pipeclose` 在锁外归还 pipe page。这里安全依赖 endpoint 全局 file ref 与 `readopen/writeopen` 一一对应：只有最后一个 file ref 才关闭方向。

若 P、L 或 R 遗留任意 write-end fd，`fw.ref` 不归零，R 会在空缓冲上永久等待 EOF。关闭“看起来不用的每个副本”是 pipeline 正确性的一部分，不只是资源优化。

## 6. 失败语义

- `pipe()` 可能因两个全局 `NFILE` 槽、一个 pipe 物理页或当前进程不足两个 `NOFILE` fd 槽返回 -1；shell 的 `panic` 只终止命令管理进程。
- `sys_pipe()` 向用户 fd 数组的任一次 `copyout` 也可失败。内核会清除已安装 fd 并关闭两端；若第一次 copyout 成功、第二次跨页失败，用户数组的第一个元素会留下一个已经失效的完整 fd 数值，第二个元素的有效页尾还可能有 1 至 3 个字节前缀，调用者只能依据返回 -1 丢弃整个数组。
- 第二次 fork 失败会触发 shell `panic`。管理进程退出时关闭继承端点，但不会执行后面的两个 `wait`；已经创建的左孩子被 reparent 给 init，最终由 init 回收，并在端点关闭后看到 EOF/无 reader，而不是由局部代码回滚。
- 任一叶命令 exec 失败时，`runcmd(EXEC)` 打印错误后执行函数末尾的 `exit(0)`；endpoint 仍由退出清理，因此 EOF 会推进，但失败状态被伪装成 0，管理进程也不检查两次 wait status。
- 所有 readers 关闭后，writer 在下一次循环检查 `readopen==0` 返回 -1；没有 Unix `SIGPIPE`。
- bad `copyin` 可留下已写前缀；bad `copyout` 可留下已消费前缀。pipe I/O 不是事务。
- writer 被 kill 时可能先写入前缀；reader 被 kill 时已复制字节保留。
- `PIPESIZE` 不是 write 原子性常量；多个 writers 可在睡眠间隙交错大 write。

## 7. 多级 pipeline

对 `a | b | c`，parser 构造右递归 AST `PIPE(a, PIPE(b, c))`。外层 pipe 把 `a` 的输出接到右子树输入；执行外层右子树的进程随后为 `PIPE(b,c)` 创建内部 pipe。验证拓扑时，不要只看叶进程：右递归产生的中间管理进程会继承外层端点，必须沿每个 `fork/dup/close` 证明最终没有多余 writer。

一种机械检查方法是给每个 pipe 分配 debug id，并在 `pipealloc/filedup/fileclose/pipeclose` 输出 `(pid,fd,file,pipe,ref,readopen,writeopen)`；在每个 exec 前断言叶进程只保留语义需要的 pipe 方向。

## 8. 验收测试

1. `yes-like producer | head-like consumer`：consumer 早退，producer 必须看到 read end 关闭并退出，而非永久 sleep。
2. 写入 513 字节，checkpoint 在第 512 字节，证明 writer 睡眠后 reader 才推进。
3. 三段 pipeline 传输带序号记录，核对顺序、总数和每个 pipe 最终释放一次。
4. 故意保留 P 的 write fd 作为负例，验证 reader 卡在 EOF；测试 harness 打印拓扑后杀掉整棵临时进程，不能污染常规镜像。
5. 在 `pipe` 的 `NFILE/NOFILE/page/copyout`、第二 fork 和 exec 故障点注入失败；对 dup 则用专门 hook 破坏源 endpoint 或顺序，因为正常 close-target 拓扑已保证一个空槽。成功建立两 child 时由管理进程 wait，第二 fork 失败时 trace 左 child 被 reparent 并由 init 最终回收。两种路径都要核对 file table、pipe page 和空闲页恢复基线。
