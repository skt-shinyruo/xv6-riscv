# 启动、陷阱、中断与汇编边界

## 问题场景与本单元成果

QEMU 复位后并不是直接进入用户程序。每个 hart 先经过机器模式的入口和
`mret`，再进入 supervisor 初始化；之后同一份内核还要区分用户陷阱、内核
陷阱、时钟抢占和外部设备中断。若把这些入口都称为“trap handler”，就会
遗漏页表、栈、特权级和寄存器保存位置的边界。

本单元的主要成果是能从源码和一次隔离运行中解释启动、向量安装、timer
preemption 和汇编保存契约。唯一的出口是一个可独立复核的“启动、陷阱、中断
与汇编边界报告包”，其中包含静态源码图、控制组否定证据、受控 timer 事件
轨迹、回归结果、资源/清理账本和证据局限。报告中的 S、B、F 分节共同构成
一个出口产物，不是多份互相独立的提交物。

## 前置单元与暂存黑盒

硬前置：[一次系统调用如何往返](syscall-roundtrip.md)。相关基础：[从 QEMU
启动到 shell 提示符](observe-system.md)和[从 C 调用栈到基础 RISC-V](../foundation/machine-and-riscv.md)。

本单元解除启动入口、每 hart 初始化、三类汇编边界和 timer/device 路由的
黑盒。以下内容只作为边界出现在报告中，不在这里完整展开：

- 进程创建、调度策略、睡眠/唤醒、生命周期和回收由
  `core.process-and-memory` 解释；`yield()` 这里只用于观察抢占交接。
- 用户页表中 trampoline 的完整 PTE 映射、权限和地址空间建立由后续进程与
  内存单元解释；这里验证同址汇编入口的职责，不声称证明映射安全性。
- PLIC 的队列、公平性、锁所有权和 VirtIO DMA 由通信与设备 I/O 单元解释；
  这里仅追踪 claim、设备路由和 complete 的边界。

## 最小模型和关键不变量

### 从 reset 到 S mode

`kernel/entry.S:_entry` 用 `mhartid` 为每个 hart 选择独立的 `stack0` 区域，
然后调用 `kernel/start.c:start()`。`start()` 在 M mode 中把 `mepc` 指向
`main`，设置 `MSTATUS_MPP_S`、medeleg/mideleg、PMP、supervisor timer 外部
中断使能和每 hart 的 `tp`，最后 `mret`。因此 `mret` 是一次特权级交接，而
不是普通 C 返回。

进入 S mode 后，hart 0 在 `kernel/main.c:main()` 完成全局初始化、trap
向量和 PLIC 初始化，再以 `__ATOMIC_SEQ_CST` fence 发布 `started=1`；其他
hart 等待并执行同样的 seq_cst fence 后才继续自己的 `kvminithart()`、
`trapinithart()` 和 `plicinithart()`。每个 hart 的 `timerinit()` 更早发生在
M-mode `start()` 中、`mret` 之前，不受 `started` gate 控制。这里描述的是
当前强顺序 fence 的发布/获取角色，不把源码改写成另一种内存序。发布变量和
栅栏共同构成“其他 hart 不使用半初始化全局状态”的不变量。

### 三个汇编入口各自拥有不同契约

| 入口 | 进入前提 | 保存/切换职责 | 返回边界 |
| --- | --- | --- | --- |
| `kernel/trampoline.S:uservec` / `userret` | 用户 trap 时仍在用户页表，代码在两套页表同一虚拟地址 | 先用 `sscratch` 保存用户 `a0`，写 `TRAPFRAME`，加载 `kernel_*`，切 kernel `satp`/栈；返回时切回 user `satp` 并恢复用户寄存器 | `sret` 回 U mode |
| `kernel/kernelvec.S:kernelvec` | 已在 kernel `satp` 和当前内核栈，发生 supervisor trap | 分配固定 256 字节帧并保存 `ra/gp/t0-t6/a0-a7`；`sp` 由帧增减恢复，`tp` 是 hart-local、不能随进程恢复旧值，`s0-s11` 由 C ABI 的 callee-saved 契约保护；C 代码保存/恢复 `sepc/sstatus` | `sret` 回原 supervisor 上下文 |
| `kernel/swtch.S:swtch` | 调度器已经决定交换两个 kernel context | 只保存/恢复 `ra`、`sp`、`s0-s11`；不切特权级，也不负责用户页表 | 从恢复的 `ra` `ret` |

`uservec`、`kernelvec` 和 `swtch` 都保存寄存器，但保存集合和执行前提不同；
把 `swtch` 当成 trap 返回路径，或把 `kernelvec` 当成用户 trampoline，都会
推导出错误的栈和页表模型。

### 中断路由和 timer 抢占

用户态 timer 的路径是 `uservec -> usertrap -> devintr -> clockintr`；内核态
timer 才走 `kernelvec -> kerneltrap -> devintr -> clockintr`。
`kernel/trap.c:devintr()` 根据 supervisor external interrupt 调用
`kernel/plic.c:plic_claim()`，再把 UART IRQ 交给 `kernel/uart.c:uartintr()`、
VirtIO IRQ 交给 `kernel/virtio_disk.c:virtio_disk_intr()`，最后调用
`plic_complete()`。timer cause 进入 `clockintr()`；只有 hart 0 更新全局
ticks 和唤醒等待者。`devintr()` 返回 2 后，`usertrap()`，或处于进程上下文
且 `myproc() != 0` 的 `kerneltrap()`，才调用 `kernel/proc.c:yield()` 交还
调度器。一次受控实验只要求观察这条 happens-before 链，不把串口输出延迟
当成实时保证。

## 源码追踪计划

使用稳定的 `path:symbol`，先建立控制边，再补寄存器和所有权边：

```sh
rg -n '^_entry:|mhartid|call start' kernel/entry.S
rg -n '^start\(\)|^timerinit\(\)|MSTATUS_MPP_S|w_mepc|w_medeleg|w_mideleg|mret' kernel/start.c
rg -n 'volatile static int started|__atomic_thread_fence|started = 1' kernel/main.c
rg -n 'MSTATUS_MPP_S|SSTATUS_SPP|SSTATUS_SPIE' kernel/riscv.h
rg -n '^trapinithart\(void\)|^usertrap\(void\)|^kerneltrap\(\)|^prepare_return\(void\)|^clockintr\(\)|^devintr\(\)' kernel/trap.c
rg -n '^uservec:|^userret:|sscratch|csrw satp|sret' kernel/trampoline.S
rg -n '^kernelvec:|addi sp|call kerneltrap|sret' kernel/kernelvec.S
rg -n '^swtch:|sd ra|sd sp|sd s0|ld s11|ret' kernel/swtch.S
rg -n '^struct context|uint64 s11' kernel/proc.h
rg -n '^yield\(void\)' kernel/proc.c
rg -n '^plic_claim\(void\)|^plic_complete\(int irq\)' kernel/plic.c
rg -n '^uartintr\(void\)' kernel/uart.c
rg -n '^virtio_disk_intr\(\)' kernel/virtio_disk.c
```

把静态链记录成三条互不混淆的路径：

```text
reset -> _entry -> start -> mret -> main -> trapinithart
user timer: uservec -> usertrap -> devintr -> clockintr -> yield -> scheduler -> userret
kernel/device: kernelvec -> kerneltrap -> devintr -> plic_claim -> device handler -> complete
```

源码计划只声明入口和调用关系；具体栈地址、`scause` 和 ticks 必须来自本次
隔离运行的报告，不能把某一次构建的绝对地址写成长期契约。

## 观察任务

先记录 manifest 中的 pinned 源码基线和走查时教程提交，再在基线导出中做
静态检查：

```sh
rg -n '"baseline_commit":' docs/xv6-tutorial/curriculum.json
git rev-parse HEAD
python3 docs/xv6-tutorial/resources/boot-traps-and-interrupts/run-lab.py --static-only
```

静态门必须验证 `_entry -> start -> mret`、MPP/delegation/timer、hart-0 发布
顺序、三种汇编保存集合、timer/device cause 和 PLIC 路由。它还必须在临时
导出中构建 `kernel/kernel` 与 `user/_irqtrace`，并检查生成的反汇编存在；
不能把工作树的构建产物当作证据。

完整 runner 使用 pinned baseline 的一个临时源码导出、一个私有 `fs.img` 和
`CPUS=1`。它先运行控制命令 `usertests reparent`，要求精确通过且没有任何
`IRQTRACE` marker；随后运行同一 shell 中的 `irqtrace`：

```sh
python3 docs/xv6-tutorial/resources/boot-traps-and-interrupts/run-lab.py \
  --report /tmp/boot-traps-and-interrupts-report.md
```

`irqtrace` 先由 `uptime()` 在命名进程中 arm，关闭中断并记录 ticks，然后在
用户态执行有界循环。runner 只接受以下顺序和关系：

```text
IRQTRACE ARM
1 USERTRAP: timer cause=0x8000000000000005, user sepc, same pid
2 CLOCK: ticks n -> n+1
3 YIELD: same pid, RUNNING
4 RESUME: same pid, RUNNING
USER: user_before <= n, user_after >= n+1, user_after > user_before
```

`CLOCK` marker 中的 `n -> n+1` 是被 instrumentation 捕获的单次
`clockintr()`；用户的两个 `uptime()` 样本包围这个事件，但可能还包围额外
timer tick，不能强制也显示为恰好加一。

之后分别运行 `./test-xv6.py -q usertests` 和完整 `./test-xv6.py usertests`，
再 `make clean`、逆向 patch、比较临时树快照，并检查原工作树状态与共享
`fs.img` 哈希没有变化。报告是唯一出口；不要另提交一份只复制控制台的日志。

## 有界修改任务

本单元的实验是一个 tutorial-only、CPUS=1 的最小 timer instrumentation patch，
资源为 [`irqtrace.patch`](../resources/boot-traps-and-interrupts/irqtrace.patch)
和 [`run-lab.py`](../resources/boot-traps-and-interrupts/run-lab.py)。它只在隔离
源码树中增加一个用户程序和临时 trace gate；不修改共享工作树、系统调用表、
设备驱动或调度策略。runner 在临时树中先做 preflight，再实际应用 patch：

```sh
git apply --check --unidiff-zero docs/xv6-tutorial/resources/boot-traps-and-interrupts/irqtrace.patch
git apply --whitespace=error-all --unidiff-zero docs/xv6-tutorial/resources/boot-traps-and-interrupts/irqtrace.patch
```

第一条只是检查；第二条才是实际应用动作，共享工作树不执行这两条命令。

F 的允许副作用仅包括临时树构建产物、私有镜像和短寿命 QEMU/driver 进程；B
是未 arm 的控制命令，要求零 marker。focused 与每个 regression 完成时先终止
并回收各自进程组；所有运行结束后依次 `make clean`、逆向 patch、比较快照，
最后由临时目录清理私有镜像和导出。`irqtrace` 的 printk、runner 启停和用户
循环会改变时序，因此实验不测量真实 timer latency 或调度公平性。

## Oracle、证据、失败路径和局限

| 维度 | 触发器 | 必须观察到 | 允许副作用与资源结果 | 不能推出 |
| --- | --- | --- | --- | --- |
| S | pinned baseline 静态检查，随后加 patch 的 instrumentation 静态检查 | 启动、hart fence、vector、保存集合、PLIC 路由和实验 gate 契约成立 | 只写临时导出；静态构建物随后清理 | 映射权限、锁图或多 hart 安全性 |
| B | `usertests reparent`，未 arm | 完整回归 transcript，`IRQTRACE` 次数为 0 | 控制命令结束；同一 focused QEMU/私有镜像保留给 F，B/F 后统一退出并删除 | timer 永不抢占其他进程 |
| F | arm 后用户循环 | `USERTRAP -> CLOCK -> YIELD -> RESUME`，同 pid，ticks 恰增 1，用户观察到变化 | 一个 trace 进程组和私有镜像，退出并清理 | 实时延迟、公平性、DMA 顺序 |

失败定位按边界缩小：静态契约失败先查对应 `path:symbol`；控制组出现 marker
先查 gate 是否只在命名程序 arm；缺少 `USERTRAP` 不能用 timeout 代替成功，
应保留 transcript 并检查 timer 窗口是否太短；pid/ticks/order 任一关系不符
都不能晋级。回归失败或清理不完整时，F 结果无效，即使 trace transcript 看似
正确。

`C` 和 `R` 在本单元为 N/A：CPUS=1 刻意不建立跨 hart happens-before，也没有
持久化写入、crash point 或恢复主张。设备 handler 只验证路由 token；PLIC
队列、DMA、锁所有权和恢复由后续单元负责。

## 退出产物与后续单元

提交一份填好的启动、陷阱、中断与汇编边界报告包，必须包含：pinned baseline
与教程提交分栏、S 静态契约、B 控制 transcript、F 精确事件序列、focused/
related/full 回归、patch digest、临时资源和清理结果，以及 `C/R` 限制。报告
中不得用固定绝对地址替代关系断言，也不得把 timeout 或“能启动”当作 oracle。

本单元解除启动/向量/timer 的边界，随后由后续的进程、调度与地址空间单元解释
`yield` 背后的 context、用户地址空间、生命周期和多 hart 交接；通信与设备
I/O 单元再展开 PLIC、UART、VirtIO 的所有权和阻塞路径。
