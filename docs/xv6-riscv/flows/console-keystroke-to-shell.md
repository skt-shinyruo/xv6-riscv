# 一次按键到 shell：UART、PLIC、console 与 `read`

本文追踪一个主机按键如何最终成为 shell 输入缓冲区中的字节。它连接 QEMU `virt` UART、PLIC、内核 trap、console 行规程、设备 inode、文件描述符和进程调度。设备细节见[设备与控制台](../kernel/devices.md)，用户侧见[`init` 与 shell](../user/init-and-shell.md)。

## 1. 起始状态

shell 的 fd 0/1/2 指向 console 设备。它在 `getcmd()` 中打印 `$ `，随后 `gets()` 反复调用 `read(0,...)`。内核路径为：

```text
sys_read -> fileread(FD_DEVICE) -> devsw[CONSOLE].read
         -> consoleread(user_dst=1, ...)
```

若 `cons.r == cons.w`，`consoleread()` 持 `cons.lock` 检查 `killed`，再执行 `sleep(&cons.r,&cons.lock)`。`sleep` 先持有 `p->lock`，才释放条件锁并发布 `chan=&cons.r,state=SLEEPING`，从而与生产者的同锁更新组成无丢失唤醒协议。

## 2. 从主机到 PLIC

```text
host keyboard/terminal
  -> QEMU injects byte into emulated 16550A RX register
  -> UART raises source UART0_IRQ=10
  -> PLIC marks source pending for enabled S-mode contexts
  -> a hart observes supervisor external interrupt
```

中断能否到达还要求 UART IER、PLIC source/context enable、PLIC threshold 和 `sie.SEIE` 允许；若当前正在 S-mode，`sstatus.SIE` 也必须为 1。当前在 U-mode 时，目标特权级更高，`SIE` 不是这次投递的额外门。`CONSOLE=1` 是设备 major，`ROOTDEV=1` 是文件系统设备，PLIC IRQ 10 又是第三个编号空间，数值不能混用。

## 3. trap 与设备处理

如果中断打断内核，硬件进入 `kernelvec`；如果打断用户态，则先经 `uservec -> usertrap`。二者最终调用 `devintr()`：

1. `plic_claim()` 只取一个最高优先级 source；
2. 对 UART 调用 `uartintr()`；
3. `uartintr()` 循环 `uartgetc()`，处理当时所有可读字节，而非只读一个；
4. 每个字节传给 `consoleintr()`；
5. handler 返回后 `plic_complete(irq)`。

所以是“一次 trap、一个 PLIC source、零到多个 UART 字节”。如果设备的 level 原因未被清除，complete 后 source 会重新 pending；未知 level IRQ 当前只打印并 complete，可能造成重复中断。

## 4. console 编辑状态机

`cons` 使用三个会回绕的 32 位 `uint` 计数；每次前进或回退都按模 \(2^{32}\) 运算：

```text
r  下一个由 reader 消费的位置
w  已发布给 reader 的边界
e  当前编辑边界
(uint)(w-r) <= (uint)(e-r) <= INPUT_BUF_SIZE(128)
```

因为两个无符号距离始终不超过 128，这个局部距离关系在回绕处仍有意义；原始 `r/w/e` 的普通整数大小关系和永久单调性都不是不变量。

`consoleintr()` 持 `cons.lock` 解释字符：

| 输入 | 状态变化 | 是否发布 |
|---|---|---|
| 普通字符 | 回显，写 `buf[e%128]`，`e++` | 否 |
| `\r` | 转成 `\n` 后同上 | 是 |
| newline | 追加并回显 | `w=e; wakeup(&cons.r)` |
| Ctrl-D | 追加 EOF 标记 | `w=e; wakeup` |
| Ctrl-H/Delete | 若 `e!=w`，撤销一个尚未发布字符并回显擦除 | 否 |
| Ctrl-U | 回退当前未发布行直到 newline/`w` | 否 |
| Ctrl-P | 调用无锁诊断 `procdump()` | 否 |
| 缓冲满 | 普通字符触发 `w=e` | 是 |

写 `buf`、推进 `e`、发布 `w=e` 和调用 `wakeup` 都在同一 `cons.lock` 临界区。reader 在睡醒并重新取得该锁后，必然看到发布前的字符。

## 5. 唤醒到 shell 继续运行

`wakeup(&cons.r)` 扫描全部 `NPROC` 槽并逐个取 `p->lock`，把匹配的 `SLEEPING` 改为 `RUNNABLE`。它不会：

- 指定哪个 reader 获得这一行；console 是全局字节流，多 reader 会竞争；
- 直接把目标放到当前 CPU 上执行；
- 向 idle 的其他 hart 发送 reschedule IPI；
- 改变等待谓词或清 `chan`。

若中断发生在 idle scheduler 所在 hart，返回后它会继续扫描；若打断普通内核/用户执行，非 timer 设备 trap 本身不 `yield`，shell 可能等到之后的调度点或本地 timer。

shell 恢复后，`sleep()` 先清 `chan`，释放 `p->lock`，重新取得 `cons.lock`。`consoleread()` 每次先推进 `r`，再 `copyout` 一个字节，遇 newline 返回。一行进入用户缓冲后，`gets()` 终止，`parsecmd()` 构造 AST，shell `fork()` 子进程执行命令并 `wait()`。

## 6. Ctrl-D、kill 与失败副作用

- 若 Ctrl-D 是本次 read 的第一个字节，返回 0 表示 EOF；若此前已复制普通字节，则 `r--` 留住 Ctrl-D，让下一次 read 返回 0。
- 若 reader 已发布 `SLEEPING`，`kkill()` 会把它无条件改为 `RUNNABLE`；恢复后循环先检查 killed 并返回 `-1`，随后 `usertrap()` 的最终 killed 检查令普通进程 `kexit(-1)`。但 kill 不持 `cons.lock`：它若恰在 reader 检查 killed 后、`sleep()` 取得 `p->lock` 前到达，只会给仍为 `RUNNING` 的进程置位，reader 随后仍可能睡到下一次输入或第二次 kill。
- `copyout` 失败发生在 `r++` 之后，当前字符已从全局输入流消费；返回短计数或 0，不回滚 `r`。合法 lazy 用户缓冲可能由 `copyout` 补页，OOM 则留下这一部分副作用。
- `n==0` 时外层 `while (n>0)` 根本不进入，立即返回 0，不等待输入也不检查目标地址；负长度同样因循环条件为假返回 0。这个边界与空 pipe 的零长度 read 不同，后者当前会先进入“空且写端仍开”的等待循环。
- 满缓冲会强制发布没有 newline 的 128 字节片段；调用者不能假定每次 read 都含完整终止行。
- 同步回显使用 polling 输出路径，可能延长持有 `cons.lock` 和中断 handler 的时间。

## 7. 确定性验证

1. 在 `consoleread` 睡眠前、`consoleintr` 写字符后、`w=e` 前后设置测试 checkpoint，强制“先检查空、后到 newline”的交错，验证无丢失唤醒。
2. trace 每次 UART claim、读出的字节数和 PLIC complete，验证一次 source 可批量消费字节且 claim/complete 配对。
3. 启动两个 console reader，输入带唯一编号的行，验证每个字节只被一个 reader 消费，而不要求固定 reader。
4. 把目标 buffer 跨到无效页，验证已复制前缀和被消费字符与文档一致。
5. 分别在进程处于 `SLEEPING`、刚被置 `RUNNABLE` 和 `RUNNING` 时 kill，记录退出延迟；测试 oracle 是最终状态和返回语义，不是固定调度顺序。
