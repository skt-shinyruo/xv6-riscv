# PLIC、UART 与控制台设备

本文说明 QEMU `virt` 平台上外部中断的路由、16550A UART 的收发协议、console 行缓冲和设备文件分派。VirtIO 的队列算法见[存储栈](storage-stack.md)，trap 汇编入口见[Trap 与中断](traps-and-interrupts.md)。

## 1. 源码地图

| 源码 | 核心符号 | 责任 |
|---|---|---|
| `kernel/plic.c` | `plicinit()`、`plicinithart()`、`plic_claim()`、`plic_complete()` | IRQ priority、每 hart enable、claim/complete |
| `kernel/uart.c` | `uartinit()`、`uartwrite()`、`uartputc_sync()`、`uartintr()` | 16550A 初始化、阻塞发送、同步输出和收发中断 |
| `kernel/console.c` | `cons`、`consputc()`、`consolewrite()`、`consoleread()`、`consoleintr()`、`consoleinit()` | 输入编辑、可读边界发布、用户 read/write 和设备注册 |
| `kernel/printk.c` | `pr`、`printk()`、`panic()`、`panicking`、`panicked` | 通过同步 console 输出内核消息和 panic |
| `kernel/virtio_disk.c` | `virtio_disk_intr()` | 另一条由 PLIC 分派的外设完成中断 |
| `kernel/memlayout.h` | `UART0`、`UART0_IRQ`、`VIRTIO0_IRQ`、`PLIC_SENABLE()`、`PLIC_SPRIORITY()`、`PLIC_SCLAIM()` | MMIO 基址、PLIC context 地址和 IRQ 编号 |
| `kernel/start.c`、`kernel/main.c` | `start()`、`main()`、`started` | supervisor 中断使能、boot hart/其他 hart 的初始化顺序 |
| `kernel/trap.c` | `usertrap()`、`kerneltrap()`、`devintr()` | 外部中断识别、IRQ 分派和 PLIC complete |
| `kernel/vm.c` | `kvmmake()` | UART、VirtIO 与 PLIC MMIO 的内核恒等映射 |
| `kernel/file.c`、`kernel/sysfile.c`、`kernel/file.h` | `devsw[]`、`fileread()`、`filewrite()`、`sys_open()`、`struct file` | `T_DEVICE` inode 到 `FD_DEVICE`、驱动回调的分派 |
| `kernel/proc.c` | `sleep()`、`wakeup()`、`kkill()`、`procdump()` | console/UART 的条件等待、kill 唤醒和诊断输出 |
| `user/init.c` | `main()` | 创建/打开 `console` 节点并建立标准输入、输出、错误 fd |

## 2. QEMU `virt` 的设备地址

本实现直接依赖固定 MMIO：

```text
PLIC    0x0c000000
UART0   0x10000000, IRQ 10
VIRTIO0 0x10001000, IRQ 1
```

`kvmmake()` 对 UART 的一个页面、VirtIO 的一个页面以及从 `PLIC` 开始的 64 MiB 范围做恒等映射，并设置读写权限。启用分页后，地址数值因此同时是物理地址和内核虚拟地址。`consoleinit()` 比 `kvminit()` 更早执行；此时 `satp=0`、分页尚未开启，相同数值直接作为物理地址访问 UART，也同样成立。

UART 的 `ReadReg/WriteReg` 使用 `volatile unsigned char *` 做 8 位 MMIO，PLIC 使用 `uint32 *` 做 32 位 MMIO。这里没有设备树、总线抽象，也没有对这些寄存器访问额外包一层显式内存屏障。

驱动没有设备树解析、总线枚举或动态 probe；换机器模型或地址就必须同步修改 `kernel/memlayout.h` 和页表映射。

## 3. 初始化顺序与中断门控

### 3.1 启动调用链

每个 hart 先在 `start()` 中完成以下与中断有关的准备：

1. 关闭分页，并用 `medeleg/mideleg = 0xffff` 尝试把低 16 个 cause 中平台支持的异常和中断委托给 supervisor mode；
2. 在 `sie` 中设置 `SEIE` 和 `STIE`，允许 supervisor external/timer 两类中断；
3. 设置首次 timer compare，并把 hart id 放入 `tp`，供 `cpuid()` 使用；
4. `mret` 进入 `main()`。此时只是打开了 `sie` 中的分类开关；`start()` 不写 `sstatus.SIE`，当前 QEMU/reset 状态使它在早期 S-mode 启动期间为零。

boot hart 在 `main()` 中按下面的次序初始化：

```text
consoleinit()
  -> initlock(cons.lock)
  -> uartinit()
       -> 配置 UART 并打开 UART RX/TX 中断
       -> initlock(tx_lock)
  -> 注册 devsw[CONSOLE]
printkinit() -> initlock(pr.lock)
早期 printk（同步轮询 UART，不依赖设备中断）
kvminit() -> kvminithart()
trapinit() -> initlock(tickslock)       # 全局一次，它本身不写 trap vector
trapinithart() -> stvec = kernelvec     # 每 hart 一次
plicinit() -> plicinithart()
...文件与磁盘初始化...
started = 1
scheduler() 循环 -> intr_on(); intr_off()
```

`uartinit()` 在 `tx_lock` 初始化之前就设置 UART 的 IER，但这段启动代码尚不会进入 UART handler：PLIC priority/context enable 还未配置，`stvec` 还未安装为有效入口，而且 CPU 仍在 S-mode 且 `sstatus.SIE=0`。UART 原因即使此时已经成为 pending，也只会在后续门控全部就绪后才能投递。早期 `printk()` 使用 `uartputc_sync()`，所以 PLIC 未就绪也可以输出。

其他 hart 等待 `started`，之后分别执行 `kvminithart()`、`trapinithart()`、`plicinithart()`，最后进入 scheduler。`started` 只在 boot hart 完成 `tickslock`、PLIC、VirtIO 和首进程初始化后发布，因此其他 hart 使用共享 `tickslock` 或本 hart PLIC context 前，所需全局状态已就绪；它们也不会与 `plicinit()` 的全局寄存器写并发。scheduler 每轮先短暂 `intr_on()` 再 `intr_off()`，这是启动后 S-mode 第一个真正开放 pending 中断的边界，不表示 scheduler 后续一直保持 `SIE=1`。

### 3.2 UART 外部中断的分层门控

一个 UART 原因要成为可被 CPU 接受的 supervisor external interrupt，必须先满足下表的上游条件。这些条件分属 UART、PLIC 和 hart CSR，不能把它们压缩成一个“全局开关”。

| 层次 | 当前代码的配置 | 精确作用 |
|---|---|---|
| UART 原因 | `IER_RX_ENABLE | IER_TX_ENABLE` | 已启用的 RX data-ready 或 THRE 原因才向外部提出 IRQ；IER 不阻止字节到达或 THR 自身变空，只决定这些状态是否请求中断 |
| PLIC source/gateway | IRQ 10 priority = 1 | gateway 接受 source 请求并记录 pending；priority 0 的 source 永远不可投递 |
| PLIC supervisor context | 本 hart IRQ 10 enable = 1，threshold = 0 | 只有已 enable 且 `priority > threshold` 的 pending source 才会使该 context 产生 external notification |
| pending 与委托 | PLIC notification 反映为 `sip.SEIP`，`mideleg.SEI` 由 `w_mideleg(0xffff)` 尝试置位 | pending 说明请求存在；delegation 决定它以 S-mode 为目标。`mideleg` 是 WARL，代码不读回校验，因而依赖 QEMU 支持 SEI 委托 |
| cause 分类开关 | `sie.SEIE = 1` | 允许 supervisor external 这一类中断；它不替代 UART 和 PLIC 的上游门控 |
| 特权级全局条件 | 见下表 | `sstatus.SIE` 只在当前特权级恰为 S-mode 时门控目标为 S-mode 的中断 |

在 pending、delegation 和 `sie.SEIE` 均已满足的前提下，当前 CPU 特权级决定 `SIE` 是否还是一道门：

| CPU 当前模式 | 目标为 S-mode 的中断能否被接受 | `sstatus.SIE` 的作用 |
|---|---|---|
| M-mode | 不会向低特权级的 S-mode 取 trap | 无法使它在 M-mode 期间向下投递 |
| S-mode | 仅 `SIE=1` 时可接受 | 这时是 S-mode 中断全局门 |
| U-mode | 可接受，不要求 `SIE=1` | 目标特权级高于当前特权级，`SIE` 不参与这次投递判定 |

`stvec` 不是“是否接受中断”的门；硬件决定接受后才用它选择入口。但要进入本驱动的正确处理路径，它必须已指向当前地址空间可执行的 `kernelvec` 或 `uservec`；未安装有效入口不会使请求失去资格，而会让已接受的 trap 跳到错误地址。同理，`plic_claim()` 也不是投递门；它是已进入 handler 后用来选取和标记 in-service source 的协议步骤。

### 3.3 PLIC 的全局与每 hart 初始化

`plicinit()` 是 CPU 0 执行一次的全局初始化。它把 UART IRQ 10 和 VirtIO IRQ 1 的 priority 设为 1；priority 为零等同禁用。

`plicinithart()` 由每个 hart 执行：

1. 根据 `cpuid()` 选择 QEMU `virt` 为该 hart 提供的 supervisor context；
2. 向该 context 的第一个 32 位 enable word **整体写入** UART 与 VirtIO 两位；
3. 把 priority threshold 设为零，使严格高于 threshold 的 priority 1 请求可递送。

两个 IRQ 在每个 hart 上都被 enable，没有设备亲和性策略。PLIC 可以向多个符合条件的 context 发出 notification，但同一 source 只能被其中一次 claim 成功取走；其他 hart 随后进入 handler 时可能 claim 到零。这里写的是第一个 enable word，而不是逐位 OR，因此若以后加入同一 word 内的 IRQ，必须把原有位一并写入；IRQ 号达到 32 以上还需要访问后续 word，当前宏和代码没有覆盖这种情况。

`plicinithart()`、`plic_claim()` 和 `plic_complete()` 都通过 `cpuid()` 选 context，而 `cpuid()` 要求本 CPU 的中断已关闭。现有调用满足这一条件：初始化发生在全局中断打开前，claim/complete 发生在 trap 上下文。PLIC 代码自身没有软件锁；全局配置由 boot hart 串行完成，context 寄存器则按 hart 分离。

因此新增设备至少需要同步完成全局 priority、每 hart enable 和 `devintr()` 分派。只改其中一处不会得到完整中断路径。

## 4. 外部中断的 claim/complete 协议

Supervisor external interrupt 的路径是：

```text
device raises IRQ
  -> PLIC selects interrupt for a hart
  -> scause = supervisor external interrupt
  -> uservec -> usertrap()，或 kernelvec -> kerneltrap()
  -> devintr()
  -> plic_claim()
  -> uartintr() or virtio_disk_intr()
  -> plic_complete(irq)
```

`devintr()` 用精确的 `scause=0x8000000000000009` 识别 supervisor external interrupt。读取 claim 寄存器既取得当前最高优先级 IRQ，也表示该 context 开始处理它；返回零表示没有可 claim 的请求。驱动完成设备级确认后，内核在同一 hart、同一 context 把同一个 IRQ 写回 claim/complete 寄存器，允许 PLIC 以后再次递送。具体 handler 不会 sleep 或调度，因此 claim 与 complete 之间不会迁移 hart。

UART 在 handler 内读取 ISR/LSR/RHR，VirtIO 在 handler 内确认设备 MMIO interrupt status；`devintr()` 在这些设备级处理之后才 complete PLIC。代码即使遇到未知非零 IRQ 也会 `printk()` 并 complete，避免永久占住该 PLIC source。claim 返回零时既不调用 handler 也不 complete，但 `devintr()` 仍对这次 external trap 返回 1。timer interrupt 返回 2，只有该返回值会让 trap 层考虑 `yield()`。

`devintr()` 每次 external trap 只执行一次 claim，不循环清空 PLIC。UART handler 可以在这一次 source 服务中排空当前 RX 字节，VirtIO handler可以消费当前全部 used entries；其他 pending source 留给之后的 trap或另一个 hart。对未知的 level-triggered source，complete只结束本次 in-service状态；若驱动没有清除设备级电平，gateway会立即再次置 pending，形成中断风暴，而不是因 complete永久消失。

## 5. UART 初始化

`uartinit()` 直接配置 16550A：

1. 关闭 UART 自身中断；
2. 打开 divisor latch，写入除数 3；源码注释称其为 38.4K，但实际 baud 取决于平台输入时钟/UART 模型，驱动没有发现时钟；
3. 配置 8 data bits、1 stop bit、无 parity；
4. 启用并清空收发 FIFO；
5. 启用 RX 和 TX interrupt；
6. 初始化 `tx_lock`。

寄存器 0 在读时是 RHR、写时是 THR；LCR 的 divisor-latch 位打开时，寄存器 0/1 暂时改作波特率除数。寄存器 2 写时是 FCR、读时是 ISR/IIR。`tx_busy` 和仅取地址作为等待通道的 `tx_chan` 都是静态对象，启动时由 BSS 清零。

串口参数在 QEMU `-nographic` 环境主要影响设备模型协议；内核没有 termios 层。驱动也没有探测 UART 类型或验证寄存器配置是否生效。

本节涉及的 UART、console 和 PLIC 控制状态均采用内核全生命周期：boot hart 初始化一次，不动态分配、不引用计数，也没有 teardown（VirtIO 队列页的分配另见存储栈文档）。`tx_lock` 只保护 `tx_busy` 及其条件等待协议；`tx_chan` 的整数值从不读写，其地址仅作为等待通道标识。`cons.lock` 保护 `cons.buf/r/w/e`。`devsw[]` 在早期单 hart 阶段登记函数指针，之后只读，因此注册过程没有单独的锁。PLIC 的 pending、enable、threshold 和 claim 状态位于硬件 MMIO 中，内核没有对应的软件镜像。

## 6. 两条输出路径

### 6.1 用户 write 的可睡眠路径

用户对 console fd 调用 `write()` 后最终进入：

```text
sys_write()
  -> filewrite(FD_DEVICE)
  -> devsw[CONSOLE].write
  -> consolewrite(user_src=1, ...)
  -> uartwrite()
```

`consolewrite()` 每次先用 `either_copyin()` 从用户地址复制最多 32 字节到内核栈 buffer，再调用 `uartwrite()`。复制发生在取得 `tx_lock` 之前，因此页表检查或 lazy allocation 不会发生在 UART 锁内。若某一批复制失败，函数停止并返回此前已经交给 UART 的字节数；第一批就失败时返回 0，而不是 `-1`。

`uartwrite()` 在 `tx_lock` 下逐字节执行：

1. 若 `tx_busy != 0`，调用 `sleep(&tx_chan, &tx_lock)`；sleep 在把当前进程置为 `SLEEPING` 的同时释放 `tx_lock`，醒来后重新取得它；
2. 向 THR 写一个字节；
3. 设置 `tx_busy=1`；
4. 如果还有字节，下一轮必须等 THR-empty interrupt 清 busy 后才能继续。

`LSR_TX_IDLE` 是源码使用的名字，但它对应 LSR bit 5，即 **Transmit Holding Register Empty (THRE)**：THR 已能接收下一字节，不表示前一字节已经完全从串行线路发送完毕；表示整个 transmitter empty 的通常是另一个状态位。相应地，`tx_busy` 只表示“中断式路径写入了一个字节，尚未观察到 THRE”，不是硬件整体忙闲状态。

最后一个字节写入 THR 并设置 busy 后，`uartwrite()` 就可以释放锁并返回；它不等待该字节在线路上发送完。当前分支没有软件 TX ring，对中断式路径而言同时只跟踪一个等待 THRE 的字节。

条件检查、sleep 和 wakeup 都由 `tx_lock` 串起来，避免“检查到 busy 后、中断先 wakeup、进程才睡下”的丢失唤醒窗口。`uartintr()` 的 `wakeup(&tx_chan)` 会唤醒所有在该地址睡眠的进程；它们重新竞争 `tx_lock` 并复查 `tx_busy`。由于 sleep 会释放锁，并发 `uartwrite()` 调用没有整次写入的原子性，唤醒后的竞争可能让不同 writer 按字符交错。

### 6.2 内核同步输出路径

`printk()` 和输入回显不能睡眠，调用：

```text
printk/consoleintr
  -> consputc
  -> uartputc_sync
```

`uartputc_sync()` 忙等 LSR 的 THRE 位，再直接写 THR；它同样只等“可接受下一字节”，不等字符物理发送完毕。`consputc(BACKSPACE)` 会依次同步发送 `\b`、空格、`\b`，从终端画面上擦掉一个字符。

在非 panic 情况下，`uartputc_sync()` 用配对的 `push_off()/pop_off()` 防止同一 CPU 在轮询和写 THR 时被设备中断重入。这只是本 CPU 的中断嵌套控制，不是跨 CPU UART 锁：该函数既不取得 `tx_lock`，也不更新 `tx_busy`。

正常 `printk()` 用 `pr.lock` 保护一次函数调用中的格式化输出；取得这个 spinlock 已关闭本 CPU 中断，`uartputc_sync()` 的 push/pop 只是再嵌套一层。`panic()` 先设置 `panicking=1`，使 panic 输出跳过 `pr.lock` 和 UART push/pop，以免死在已损坏或已持有的锁上；打印完后设置 `panicked=1`。其他 CPU 以后若进入 `uartputc_sync()`，会在检测到 `panicked` 后永久自旋，但该机制不会主动停止仍未尝试输出的 CPU。

同步与中断式路径共享 UART FIFO，却没有统一软件队列或全局输出锁。`pr.lock` 只串行化正常的 `printk()` 调用；console 回显持有的是 `cons.lock`，用户输出持有的是 `tx_lock`。因此 printk、回显、不同用户 writer 之间不保证消息原子性或总顺序，最多依赖各次 MMIO 写和硬件 FIFO 接收字符。

### 6.3 本仓库与常见 xv6 版本的差异

阅读外部 xv6 资料时经常会看到 `uart_tx_buf[]`、读写索引和 `uartstart()` 组成的软件 TX ring。本仓库的 `kernel/uart.c` 没有这些对象：`consolewrite()` 分 32 字节复制只是用户内存搬运批次，真正发送仍由单个 `tx_busy`、`&tx_chan` 和每字节一次 THRE wakeup 推进。分析吞吐量、写入原子性或锁时必须以本仓库这条路径为准，不能套用 TX ring 的结论。

## 7. UART 中断处理

`uartintr()` 先读取一次源码名为 ISR、在 16550A 语义中通常称 IIR 的寄存器；源码注释把这一步称为 acknowledge，但丢弃读取值，不按中断原因分别分支。不能把这理解为“一次读取清掉所有原因”：THRE 原因可由读取 IIR/写 THR 清除，RX data-ready 则要靠后续读取 RHR 排空。驱动随后先处理 THR-ready 条件，再排空所有当前可读的输入。

### 7.1 THR 可再次接收

`uartintr()` 即使面对纯 RX 中断也会先取得 `tx_lock` 并检查 LSR 的 THRE 位。若 THR 可接受字节：

- 清 `tx_busy`；
- `wakeup(&tx_chan)`。

它不先检查 busy 是否原本为 1，所以启动时或同步输出引发的 THRE 条件也可能执行一次没有实际 waiter 的 wakeup。睡眠的 `uartwrite()` 醒来后重新取得 `tx_lock`、复查 busy，再决定是否写入下一个字节。`wakeup()` 会扫描整个进程表，因此这段工作发生在 UART trap 上下文且仍持有 `tx_lock`。

### 7.2 接收字符

循环调用 `uartgetc()`，只要 LSR bit 0（data ready）有效，就从 RHR 取一个字符并调用 `consoleintr(c)`。一次 UART interrupt 可以排空多个已到达字符；每个字符分别取得和释放一次 `cons.lock`。

UART 中断运行在 trap 上下文，不能调用会睡眠的 `uartwrite()`；`consoleintr()` 的回显使用同步输出正是为了满足这一限制。

驱动未开启或检查 line-status interrupt，也不检查 overrun、parity、framing、break 等错误位；只要 data-ready 有效就把 RHR 的低 8 位当作输入。它也没有 modem-status 处理和接收流控。

## 8. Console 输入缓冲模型

`kernel/console.c` 使用 128 字节环形数组和三个从 BSS 零值开始的 `uint` 计数器。`w` 只在发布输入时追上 `e`；`e` 通常递增，但 Backspace/`Ctrl-U` 可使它回退；`r` 通常随读取递增，但延迟 `Ctrl-D` 到下一次 read 时会回退一格：

```text
r: 下一个交给 read() 的字符
w: 已发布给 read() 的可读边界
e: 当前编辑位置
```

忽略 `uint` 回绕时，可以把概念顺序画成 `r <= w <= e`。真正跨回绕仍成立、且源码可以依赖的不变量应写成无符号距离：

```text
(uint)(w-r) <= (uint)(e-r) <= INPUT_BUF_SIZE
```

数组访问时才对 `INPUT_BUF_SIZE` 取模；计数器不会在每绕一圈时清零。代码不做原始计数器的有序比较，只比较相等性以及无符号差 `e-r`；因为两个距离始终不超过 128，32 位模算术回绕后仍能唯一解释已发布区和编辑区长度。

`r..w` 是已经发布、用户可读的数据，可能包含多行；`w..e` 是最近一次发布可读边界之后、仍可被 backspace 或 kill-line 修改的编辑区。总占用量 `e-r` 同时包括已发布但尚未读取的数据和当前编辑数据。

## 9. `consoleintr()` 的编辑和可读边界发布

所有输入状态由 `cons.lock` 保护。字符处理规则：

| 输入 | 行为 |
|---|---|
| `Ctrl-P` | 调用 `procdump()` 打印进程表，不写输入 buffer |
| `Ctrl-U` | 从 `e` 向 `w` 回退，遇到换行也停止；每删一字节输出退格擦除序列 |
| `Ctrl-H`/Delete | 若 `e != w`，删除一个尚未发布的字符并回显擦除序列 |
| `\r` | 在普通字符路径中先转成 `\n`，再回显、保存并发布可读边界 |
| 非零普通字符 | `e-r < 128` 时先回显，再写入 `buf[e++ % 128]` |
| 换行、`Ctrl-D`、追加后恰好占满 | 设置 `w=e`，调用 `wakeup(&cons.r)` 发布当前所有输入 |
| NUL，或缓冲区已经占满时到达的普通字符 | 不回显、不保存，直接丢弃 |

已经发布到 `r..w` 的字符不会被编辑键修改。第 128 个占用字节会强制发布可读边界，即使没有换行，从而唤醒 reader；在 reader 消费至少一个字节以前，后续普通字符、换行或 `Ctrl-D` 都会被默认分支的容量检查丢弃，不会覆盖旧数据。

`consoleintr()` 在设置 `w=e` 后仍持有 `cons.lock` 调用 `wakeup()`，与 reader 的 sleep 协议共同避免丢失唤醒，并一次唤醒所有 console reader。多个 reader 之后竞争 `cons.lock`，输入不绑定到某个特定进程。

`Ctrl-P` 在持有 `cons.lock` 时调用 `procdump()`；`procdump()` 自身有意不获取各 `p->lock`，避免系统已经锁死时诊断也被卡住，所以输出可能是不完全一致的快照。其内部 `printk()` 会形成 `cons.lock -> pr.lock` 的锁顺序，并同步轮询 UART，故 `Ctrl-P` 可显著延长这次中断和 `cons.lock` 持有时间。

## 10. `consoleread()` 的行语义

`consoleread(user_dst, dst, n)` 在 `cons.lock` 下读取最多 `n` 字节：

1. 当 `r==w`，说明没有已发布输入；若进程被 kill 返回 `-1`，否则 `sleep(&cons.r, &cons.lock)`；
2. 取 `buf[r++ % size]`；
3. 普通字符通过 `either_copyout()` 写到用户或内核目标；
4. 读到换行后返回这一行；
5. 读到 `Ctrl-D` 按 EOF 规则处理。

`Ctrl-D` 的细节用于模拟 Unix EOF：

- 若它是本次 read 遇到的第一个字符，消费 `Ctrl-D` 并返回 0；
- 若本次已经复制了普通字符，先把 `r` 减一，返回已有字符；下次 read 再消费同一个 `Ctrl-D` 并返回 0。

这样 `abc Ctrl-D` 在 read 缓冲区足够大时会表现为一次返回 `abc`，下一次返回 EOF，而不会丢失前面的数据。

kill 只在 `r==w` 的等待循环中检查。`kkill()` 会把任意 sleeping 进程改回 `RUNNABLE`，因此空缓冲区上的 reader 醒来后会返回 `-1`；如果醒来时已经有已发布数据，或调用之初就有数据，它会先读数据，本函数不会在每个字符前再次检查 killed。

`sleep(&cons.r, &cons.lock)` 先取得当前进程的 `p->lock`，再释放 condition lock，醒来后恢复 `cons.lock`。`consoleintr()` 在相同 condition lock 下改变 `w` 并 wakeup，因而“检查空缓冲区”和“真正进入睡眠”之间不存在丢失输入发布事件的窗口。

这条无丢失保证只针对 `consoleintr()` 发布输入；`kkill()` 不取得 `cons.lock`。若 reader 在检查 `killed==0` 后、`sleep()` 取得 `p->lock` 前被 kill，killer 看到的仍是 `RUNNING`，reader 仍可能随后发布 `SLEEPING`，直到下一次输入或第二次 kill 才再次运行。设备条件锁因此不能自动把 kill 变成无窗口的取消协议，完整竞态见 [kill 阻塞进程](../flows/kill-blocked-process.md)。

`either_copyout()` 也在 `cons.lock` 内执行。用户目标页尚未映射时，`copyout()` 可能调用 `vmfault()` 做 lazy allocation，并在此过程中取得页分配器锁，但不会 sleep。

复制错误语义需要特别注意：代码先执行 `c = buf[r++ % 128]`，再调用 `either_copyout()`。如果用户目标地址无效，那个输入字节已经从环形缓冲区消费且不会放回（只有遇到 `Ctrl-D` 的专门分支可能回退 `r`）；函数返回此前成功复制的字节数。若第一个普通字节就复制失败，返回值是 0，调用者无法仅靠返回值把它与 EOF 区分。`user_dst=0` 的内核目标则直接 `memmove()`，没有这个失败分支。

## 11. Console 作为设备文件

`consoleinit()` 初始化输入锁、调用 `uartinit()`，然后注册：

```text
devsw[CONSOLE].read  = consoleread
devsw[CONSOLE].write = consolewrite
```

`CONSOLE` major number 为 1，`devsw` 共预留 `NDEV=10` 个 major 槽位。文件系统中的 `console` 是 `T_DEVICE` inode，保存 major/minor 而不保存普通文件内容；console 驱动只按 major 分派，不读取 minor。

`sys_open()` 遇到 `T_DEVICE` 时先验证 major 在 `[0, NDEV)` 内，再创建持有 inode 引用、major、读写权限的 `FD_DEVICE` `struct file`。open 阶段不要求相应 `devsw[major]` 回调非空；真正 `fileread()/filewrite()` 时才同时检查 major 范围和函数指针，然后分别以 `user_dst=1` 或 `user_src=1` 调用驱动。设备 I/O 不使用也不推进 `f->off`。

首个用户程序 `user/init.c` 先调用 `open("console", O_RDWR)`。代码对任何 open 失败都尝试 `mknod("console", CONSOLE, 0)`，并再次 open；它没有 errno，因而不能确认第一次失败一定是“节点不存在”，也没有检查第二次 open 的返回值。正常情况下这是进程的首个 fd，所以得到 fd 0，随后两次 `dup(0)` 建立 fd 1 和 2。

设备节点、open-file description 和驱动表是三种不同对象。已打开的 `struct file` 保留 inode 引用和 major；unlink 目录中的设备节点不会立刻使已有 fd 失效，最后一次 close 才通过 `iput()` 释放 inode 引用。

## 12. VirtIO 中断在设备层的位置

PLIC 对 VirtIO 和 UART 一视同仁，只按 IRQ number 分派。不同之处是：

- UART RX/TX 中断发布字符或 `tx_busy` 条件；
- VirtIO handler 在 `vdisk_lock` 下先把 `INTERRUPT_STATUS & 0x3` 写入设备的 `INTERRUPT_ACK`，再消费 used ring、清 `b->disk` 并用 `wakeup(b)` 发布完成；
- PLIC complete 必须在具体驱动完成设备级 ack 后执行。

`virtio_disk_rw()` 以 `b` 为等待通道、`vdisk_lock` 为 condition lock 等待 `b->disk` 清零，协议与 UART/console 的条件睡眠形式相同。`virtio_disk_intr()` 返回并释放 `vdisk_lock` 后，`devintr()` 才 complete IRQ 1。VirtIO 的 MMIO feature negotiation、三 descriptor 请求链和内存 fence 详见[Buffer Cache、日志与 VirtIO](storage-stack.md)。

## 13. 锁和中断不变量

| 状态 | 保护锁 | 谁等待 | 谁唤醒 |
|---|---|---|---|
| UART `tx_busy` | `tx_lock` | `uartwrite()` | `uartintr()` |
| console `r/w/e/buf` | `cons.lock` | `consoleread()` | `consoleintr()` |
| 一次正常 `printk()` 调用 | `pr.lock`（panic 跳过） | 其他 printk 调用 spin | 前一个打印者 release |
| PLIC context | 硬件 claim/complete | 无软件 sleep | 具体 handler 完成后 complete |

Spinlock 获取会通过 `push_off()` 关闭当前 CPU 中断。因此中断处理程序可与进程上下文共享 `tx_lock/cons.lock`，而不会在同一 CPU 上持锁时被 UART 中断重入；另一个 CPU 上的 handler 则会在同一锁上自旋。

实际锁嵌套如下：

- `uartwrite()`：`tx_lock -> 当前 p->lock`，后者由 `sleep()` 临时取得；
- `uartintr()`：`tx_lock -> 各 p->lock`，后者由 `wakeup()` 扫描进程表时逐个取得；
- `consoleread()`：`cons.lock -> 当前 p->lock`，来自 `killed()` 检查或 `sleep()`；用户目标页需要 lazy allocation 时还会在 `cons.lock` 内取得页分配器锁；
- `consoleintr()` 发布输入：`cons.lock -> 各 p->lock`，来自 `wakeup()`；
- `consoleintr()` 处理 `Ctrl-P`：`cons.lock -> pr.lock`；
- 普通 `printk()`：`pr.lock` 后只做同步 MMIO，不取 `cons.lock` 或 `tx_lock`；
- `uartintr()` 在开始接收字符前已经释放 `tx_lock`，不会同时持有 `tx_lock` 和 `cons.lock`。

这些路径中不存在反向取得上述锁的实现。condition waiter 必须在保护条件的锁内检查条件，并通过 `sleep(chan, lock)` 原子交接到 `p->lock`；waker 则应持相同 condition lock 更新条件并调用 `wakeup(chan)`。

虽然原则上 spinlock 临界区应短，当前教学实现有两个显著的长路径：`wakeup()` 持 condition lock 扫描全部 `NPROC` 项，`consoleintr()` 还会持 `cons.lock` 同步回显或执行 `procdump()`。若 UART 长时间不置 THRE，这会无限延长中断关闭和锁持有时间；实现没有 timeout。

## 14. 失败与限制

- 未知非零 PLIC IRQ：内核打印后 complete；若设备级 level 原因仍有效，会立即再次 pending并可能形成中断风暴。claim 为零时不 complete，但这两种 external trap 都返回“已识别设备中断”。每次 trap只 claim一个 source，没有动态驱动注册。
- UART 永远不出现 THRE：同步输出永久忙等；当 `tx_busy=1` 时，后续字节或其他 writer 依赖 TX interrupt 清条件，也没有 timeout。
- 同步输出不取 `tx_lock`、不更新 `tx_busy`；中断式输出只看 `tx_busy`，写 THR 前不检查 LSR。并发时不仅消息可按字符交错，sync 写入后 async writer 还可能在 THR 尚未 ready 时直接写，某次 THRE interrupt 也可能清除并非由同一输出者对应的 busy。启用的硬件 FIFO 能吸收有限字节，但驱动没有软件层面的统一所有权或溢出处理。
- 输入占用达到 128 字节时会强制发布可读边界，但 reader 释放空间之前的新字符被丢弃；驱动没有 overrun、parity、framing 等接收错误报告。
- console 的用户地址复制失败只产生短计数/零返回；read 还会丢掉已经 `r++`、但未能 copyout 的那个字符。
- `sys_read()/sys_write()` 不先拒绝负的 `n`；对 console 而言两个 `while (n > 0)`/`while (i < n)` 循环都会跳过，结果是返回 0，而不是 `-1`。
- `uartwrite()` 不检查 killed。`kkill()` 可把它从 sleep 唤醒，但它会继续循环、必要时再次 sleep，直到写完或其他故障；系统调用返回到 `usertrap()` 后，进程才因 killed 退出。
- console 是 canonical-like 行读取，但不实现完整终端信号、回显模式或 ioctl。
- panic 协调只依赖两个 volatile 标志：它让其他 CPU 在下一次同步 UART 输出时自旋，不是停止所有 CPU 的 SMP stop 协议。
- PLIC 配置写死 QEMU `virt` 的 supervisor context 地址公式，把 UART/VirtIO enable 到所有 hart，并只覆盖 enable bitmap 的第一个 word。

## 15. 验证

### 15.1 静态检查

在仓库根目录运行：

```sh
rg -n '^(uartinit|uartwrite|uartputc_sync|uartintr)\(' kernel/uart.c
rg -n '^(consoleinit|consoleintr|consoleread|consolewrite)\(' kernel/console.c
rg -n '^(plicinit|plicinithart|plic_claim|plic_complete)\(' kernel/plic.c
```

三次符号检查应分别找到本文描述的 UART、console 和 PLIC 入口。还应人工确认 `kernel/trap.c:devintr()` 对每个非零 claim 都在具体 handler 返回后调用 `plic_complete(irq)`。

### 15.2 交互检查

- 在 shell 输入普通行，确认换行会发布可读边界；
- 输入字符后用 Backspace 和 `Ctrl-U`，确认只能编辑尚未发布的部分；
- 用 `Ctrl-D` 在空行产生 EOF；shell 会退出，`init` 随后打印启动消息并重启 shell。也可在字符后输入 `Ctrl-D`，观察“先返回字符、下一次 read 返回零”；
- 用 `Ctrl-P` 输出进程状态；
- 运行会持续打印测试进度的 `usertests -q`，观察 UART 发送在多次 sleep/wakeup 后仍能推进。

### 15.3 并发和中断检查

```sh
usertests -q
usertests preempt
usertests manywrites
```

- `preempt()` 的源码注明该用例面向最多两个 CPU；做定向验证时应以 `make CPUS=2 qemu` 启动，再运行 `usertests preempt`。它用两个 CPU-bound 进程、一个 pipe writer 和 timer preemption 验证调度仍能推进；进度打印同时经过 console 输出，但该测试不覆盖 console 输入中断；
- `usertests manywrites` 专门用并发文件写尝试触发 VirtIO 驱动死锁，覆盖 IRQ 1、设备 ACK、`b->disk` wakeup 和 PLIC complete 路径；
- 三条命令都应最终打印 `ALL TESTS PASSED`。默认 `CPUS=3` 下并发运行会打印的程序，检查没有内核 panic；预期输出可能在消息或字符边界交错，不要把顺序当成调度保证。

### 15.4 GDB 观察点

```gdb
break plic_claim
break plic_complete
break uartintr
break consoleintr
break consoleread
break uartwrite
```

在 UART interrupt 中检查 `scause`、claim 的 IRQ、`tx_busy`、`cons.r/w/e`，并用 `x/1xb 0x10000005` 读取 LSR：bit 0 是 RX ready，bit 5 是 THRE。`plic_complete` 应发生在 `uartintr()` 或 `virtio_disk_intr()` 返回之后，并收到同一个非零 IRQ。调试输入睡眠时，reader 应为 `SLEEPING` 且 `p->chan == &cons.r`；字符使 `w` 前移后，`wakeup()` 把它改为 `RUNNABLE`，但它只有在 scheduler 选中后才重新取得 `cons.lock` 并继续。
