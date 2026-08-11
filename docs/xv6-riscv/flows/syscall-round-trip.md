# 一次系统调用往返：`ecall` 到 `sret`

本文从用户 C 调用开始，按指令和数据所有权追踪一次系统调用如何进入内核、分派、取参、写回结果并返回。核心源码是 `user/usys.pl`、生成的 `user/usys.S`、`kernel/trampoline.S`、`kernel/trap.c`、`kernel/syscall.c` 和具体 `sys_*` handler。

专题背景见[用户 ABI 与运行库](../user/runtime-and-abi.md)、[Trap 与中断](../kernel/traps-and-interrupts.md)和[系统调用](../kernel/system-calls.md)。

## 1. 总体时序

以 `getpid()` 为最简单例子：

```text
user C caller
  -> user stub: a7=SYS_getpid; ecall
  -> hardware trap to stvec=uservec
  -> trampoline uservec
       save user registers to TRAPFRAME
       load kernel sp/tp/satp/entry
       switch to kernel page table
  -> usertrap
       save sepc; advance epc; enable interrupts
  -> syscall
       num=trapframe.a7
       call sys_getpid
       trapframe.a0=return value
  -> prepare_return
       stvec=uservec; fill kernel fields; set sstatus/sepc
  -> trampoline userret(user_satp)
       switch user page table; restore registers
  -> sret
  -> user stub ret
  -> C caller sees a0 result
```

从用户视角是普通函数调用；真正跨特权级的只有 `ecall`/`sret`，中间的 C 与汇编协作由 trapframe 连接。

## 2. 用户调用约定

标准 RISC-V ABI 提供八个整数/指针参数寄存器 `a0..a7`，函数返回值放在 `a0`。当前 xv6 系统调用 ABI 只接收六个参数：`argraw()` 只读取 `a0..a5`，`a7` 专门保存系统调用号，`a6` 不参与分派。因而新增七参数系统调用不能只增加 C 原型，还必须扩展内核取参约定或改为传递参数结构体。

`user/usys.pl` 为每项生成：

```asm
.global read
read:
  li a7, SYS_read
  ecall
  ret
```

C 编译器已经把 `read(fd, buf, n)` 参数放入 `a0/a1/a2`；stub 只设置 `a7`，不会搬运参数。`ecall` 后，若系统调用正常返回，`a0` 已被内核替换为结果，`ret` 使用恢复的用户 `ra` 回到 C caller。

## 3. `sbrk` 的特殊命名

生成器对 `sbrk` 输出符号 `sys_sbrk`，因为 `user/ulib.c` 需要提供两个策略 wrapper：

```c
sbrk(n)     -> sys_sbrk(n, SBRK_EAGER)
sbrklazy(n) -> sys_sbrk(n, SBRK_LAZY)
```

内核仍只看到调用号 `SYS_sbrk` 和两个寄存器参数。生成文件 `user/usys.S` 不应直接编辑，重新运行 Perl 生成器会覆盖它。

## 4. `ecall` 的硬件动作

用户态执行 `ecall` 时，RISC-V 硬件至少完成：

- 将当前用户 PC 写入 `sepc`，它指向 `ecall` 本身；
- 将 trap 原因写入 `scause`，来自 U-mode 的 environment call 为 8；
- 把先前特权级记录到 `sstatus.SPP`，把原 `SIE` 保存到 `SPIE`，再清 `SIE`；
- 切换到 supervisor mode；
- 从 `stvec` 取入口 PC。

硬件不会自动切页表、换内核栈或保存通用寄存器。这些必须由 trampoline 完成。

## 5. 为什么入口必须是 trampoline

trap 发生瞬间仍使用用户页表，但即将执行的入口代码必须能继续工作并切到内核页表。`kernel/trampoline.S` 所在物理页被用户页表和内核页表都映射到相同虚拟地址 `TRAMPOLINE`，因此切换 `satp` 前后 PC 仍有效。

`prepare_return()` 在上一次返回用户态前已把 `stvec` 设置为：

```text
TRAMPOLINE + (uservec - trampoline)
```

TRAPFRAME 页同样固定映射在每个用户页表的 `TRAPFRAME` 虚拟地址，但没有 `PTE_U`，用户代码不能直接访问。

## 6. `uservec` 保存用户现场

入口首先：

```asm
csrw sscratch, a0
li a0, TRAPFRAME
```

用户 `a0` 暂存到 `sscratch`，从而可以用 `a0` 作为 trapframe 基址。随后按 `struct trapframe` 固定偏移保存 ra、sp、gp、tp、临时寄存器、callee-saved 寄存器和 `a1..a7`，最后从 `sscratch` 取回原用户 a0 存到 trapframe。

这里保存了除恒为零的 `x0` 外全部 31 个用户通用寄存器；CSR 不在这个保存集合中。它不同于 `swtch.S` 只保存 `ra/sp/s0..s11` 的精简内核上下文。系统调用可能调度、睡眠或跨越任意 C 调用，返回时必须能恢复 trap 发生点的用户现场。

## 7. 从 trapframe 取得内核执行环境

`prepare_return()` 预先写入 trapframe 顶部四个内核字段：

```text
kernel_satp    current kernel page table token
kernel_sp      this process's permanent kernel stack top
kernel_trap    address of usertrap()
kernel_hartid  current hart id for tp/cpuid
```

`uservec` 加载它们，将 sp 换为进程内核栈、tp 换为 hart id，保留 usertrap 地址，并执行两次 `sfence.vma` 包围 `csrw satp`。切到内核页表后通过 `jalr` 进入 C 函数 `usertrap()`。

这四个字段中没有 kernel `gp`。保存 trapframe 中的用户 `gp` 不会改变硬件寄存器，`uservec` 也没有在进入 C 前替换它；当前路径因最终内核产物不含 C 生成的 `gp` 相对访问才成立。工具链或编译选项变化后必须按[平台契约](../architecture/platform-contracts.md#5-psabi系统调用-abi-与汇编边界)重新审计。

此时通用用户状态已安全落在 trapframe，C 编译器可正常使用内核栈。

## 8. `usertrap()` 建立内核 trap 环境

`usertrap()` 首先验证 `sstatus.SPP == 0`，确保来源确为用户态。随后立即把 `stvec` 改为 `kernelvec`：从此若内核执行时发生中断，必须走保存内核寄存器的 kernel trap 路径，不能再次用 uservec 覆盖用户 trapframe。

函数取得当前进程并保存：

```c
p->trapframe->epc = r_sepc();
```

对 `scause == 8` 的系统调用：

1. 若进程已 killed，直接 `kexit(-1)`，不执行调用。
2. `epc += 4`，跳过固定 4 字节的 `ecall` 指令。
3. 在已经读完 sepc/scause/sstatus 后 `intr_on()`。
4. 调用 `syscall()`。

如果不推进 epc，返回后会再次执行同一 ecall，形成无限系统调用循环。页故障则不能推进，因为修复映射后必须重试原访存指令。

## 9. 为什么到这里才开中断

进入 trap 时硬件关闭中断。`usertrap()` 需要先保存用户 PC、读取 cause并把 `stvec` 切到 kernelvec；否则定时器中断可能覆盖 `sepc/scause/sstatus` 或进入错误入口。

完成这些一次性保存后允许中断，使可能很慢的文件系统调用、pipe 等待或页面分配不必阻塞该 hart 的设备与时钟处理。若 handler 睡眠，scheduler 可运行其他进程。

## 10. 调用号分派

`kernel/syscall.c:syscall()` 从 trapframe 而不是当前活寄存器取号：

```c
num = p->trapframe->a7;
```

`a7` 先被赋给 32 位 `int num`，然后才验证 `num > 0`、数组范围内且表项非空，再调用 `syscalls[num]()`。在当前工具链上，这意味着畸形的 64 位 `a7` 会先按低 32 位转换，而不是先作为完整 64 位值被拒绝；正常 ABI 调用号都是 1 到 21，不受影响。函数指针数组用 designated initializer 将 `SYS_fork` 到 `SYS_close` 映射到 21 个 handler。

handler 的 `uint64` 返回值统一写回：

```c
p->trapframe->a0 = syscalls[num]();
```

未知号码打印 pid、进程名和号码，并写入 64 位 -1。没有 errno 或权限分派层。

## 11. 参数提取

handler 不接收 C 形参，而是调用 helper 从保存的 trapframe 读取：

- `argraw(n)`：返回 `a0..a5` 的 64 位原值，其他 n panic。
- `argint(n, &x)`：把 64 位槽转换为 32 位 `int`；当前 RISC-V 工具链实际保留低 32 位并按有符号数解释。
- `argaddr(n, &va)`：只取得 64 位地址，不立即判断合法性。
- `argstr(n, buf, max)`：通过 `fetchstr`/`copyinstr` 复制 NUL 字符串。
- `argfd`：额外验证 fd 范围和 `ofile[fd]` 非空。

将地址校验推迟到实际 `copyin`/`copyout` 很重要：只有复制长度已知时才能逐页处理跨页范围和 lazy 页。但“推迟校验”不等于先验证完整区间，也不提供事务语义；helper 可能复制若干页后才在后续页失败。

## 12. 整数、地址和字符串的校验差异

`fetchaddr(addr, &value)` 读取用户内存中的一个 64 位值，例如 exec 的 argv 指针。它先要求整个 8 字节区间位于 `p->sz` 且加法不溢出，再 `copyin()`。

`fetchstr(addr, buf, max)` 交给 `copyinstr()` 查找 NUL。`copyinstr` 逐页 walk，只接受有效用户映射，遇到未映射页立即失败，不调用 `vmfault()`。它没有 `fetchaddr()` 的 `p->sz` 区间检查：缩容后仍映射的最后部分页中，逻辑 break 之后的字节只要仍带 `PTE_U` 且能在 `max` 内找到 NUL，字符串提取就可成功。

当参数是当前进程的 `p->pagetable` 时，普通 `copyin()`/`copyout()` 遇到没有有效用户映射的页会调用 `vmfault()`；后者以当前进程的 `p->sz` 为上界，分配一张零页并映射为 `R|W|U`。`walkaddr()` 本身检查 `PTE_V|PTE_U`，`copyout()` 还额外拒绝没有 `PTE_W` 的叶子；`copyin()` 没有单独检查 `PTE_R`，因为它经物理地址由内核读取。

这个 lazy fallback 隐含“传入页表就是当前进程页表”的前提：`vmfault()` 虽接收 `pagetable` 参数，却最终把新页映射到 `p->pagetable`。例如 `kexec()` 构造的临时页表不能依赖该 fallback；当前实现先 eagerly 映射新栈，所以其 `copyout()` 正常路径不会触发这一问题。

因此同一个 lazy 地址：

- 作为 read/write 数据 buffer 可能自动补页；
- 作为 open/exec 路径字符串不会补页，会返回 -1。

这不是通用 RISC-V 规则，而是当前 VM helper 的实现差异。两种普通复制还都按页推进：跨页失败时，已写入用户区或内核目标区的前缀不会回滚，先前分配的 lazy 页也会保留。调用者必须自行决定把它转换成 `-1`、短计数还是其他结果。

## 13. handler 执行期间

简单 handler 如 `sys_getpid()` 直接返回字段；复杂 handler 可能：

- 获取进程、inode、file 或 pipe 锁；
- `begin_op()` 加入文件系统事务；
- 在 `copyin/out` 时分配 lazy 页；
- `sleep()` 并被其他 CPU 唤醒；
- 因定时器中断被调度出去后再继续；
- 关闭资源或改变地址空间。

无论经历多少次内核上下文切换，用户寄存器仍保存在该进程自己的 trapframe，进程恢复后 syscall C 栈继续执行。

## 14. 返回值和 killed 检查

普通 handler 返回后，`syscall()` 已更新 trapframe a0。`usertrap()` 再检查 `killed(p)`；若 killed 标志在这次检查前已经可见，就 `kexit(-1)`，不返回用户态。阻塞在 `sleep()` 中的进程会被 `kkill()` 改回 `RUNNABLE`，不少阻塞 handler 也会检查 killed 并提前返回，随后仍由这里统一退出。

kill 是协作式标志而不是立即撤销：若另一 hart 恰好在这次最终检查之后、`sret` 之前设置 killed，当前代码不会再次检查，进程可能短暂返回用户态，直到下一次 trap 才退出。timer 用户 trap 在 killed 检查后执行 `yield()`，若进程在该调度间隙被 kill，也存在同样窗口。

系统调用 cause 分支的 `which_dev` 仍为 0，所以不会因这次 trap 自身调用 `yield()`。但 handler 执行期间可以睡眠，或被嵌套 timer interrupt 的 `kerneltrap()` 调度。

## 15. `prepare_return()`

返回用户态前先 `intr_off()`，因为即将把 `stvec` 从 kernelvec 改回 uservec；在真正 `sret` 前若发生内核中断，走 uservec 会把当前内核现场误当用户现场。

随后：

1. 设置 `stvec` 为 trampoline uservec 地址。
2. 刷新 trapframe 的 kernel_satp/sp/trap/hartid，为下一次 trap 做准备。
3. 清 `sstatus.SPP`，令 `sret` 进入 U-mode。
4. 置 `SSTATUS_SPIE`；`sret` 会把它复制到 `SIE`，并把 `SPIE` 重新置 1。
5. 把保存的 `trapframe->epc` 写到 `sepc`。

函数不在 C 中直接切用户页表，因为切换后普通内核地址和当前 C 栈不再可访问；必须跳到双映射 trampoline 完成。

## 16. `userret()` 切回用户页表

`uservec` 的 `jalr t0` 把紧随其后的 `userret` 地址写入内核 `ra`，所以 `usertrap()` 的普通 C 返回会直接落到 trampoline 的 `userret`；返回的用户 satp token 位于 `a0`。它：

```text
sfence.vma
satp = user token
sfence.vma
a0 = TRAPFRAME
restore every register except a0
restore user a0 last
sret
```

恢复 a0 最晚，因为此前它还用作 trapframe 基址和 satp 参数。`sret` 根据 sepc 回到 `ecall` 后一条指令，并根据 sstatus 降到用户态。

用户 stub 随即执行 `ret`，C caller 从 a0 取得 handler 结果。普通路径会把其余通用寄存器恢复为执行 `ecall` 时的值；其中 `a7` 已是 stub 刚装入的系统调用号，而不是调用 stub 之前的值。ABI 只要求被调用者保持 callee-saved 寄存器，用户程序不应依赖这个实现顺带保留其余 caller-saved 寄存器。

## 17. 四种不同终局

### 普通返回

如 read/getpid/open：handler 返回数值，原地址空间、epc 仅推进 4 字节，stub `ret` 回原 caller。

### `fork` 的父子双返回

父进程在 `kfork()` 返回后继续当前 `syscall()` C 栈，`syscall()` 把子 pid 写入父 trapframe 的 a0。子进程得到父 trapframe 的副本，其中 epc 已指向父 `ecall` 的下一条指令，但 `kfork()` 把子 a0 改为 0；子进程并不复制父内核栈，也不会从父 handler 中返回。

子进程第一次被调度时从 `forkret()` 开始，调用 `prepare_return()` 后直接跳到 trampoline `userret`。因此父子最终都在各自地址空间执行同一 fork stub 的 `ret`，但父 caller 看见子 pid，子 caller 看见 0。

### 成功 `exec`

`sys_exec()` 先从旧地址空间复制 path、argv 指针数组和参数字符串；`kexec()` 在临时页表中装载 ELF 和新栈，直到所有可失败步骤完成后才提交 `p->pagetable`、`sz`、`trapframe->epc/sp/a1`。随后 `sys_exec()` 返回 argc，`syscall()` 把它写入新现场的 a0。仍走同一个 prepare/userret，但 `sret` 进入新 ELF entry，a0 是 argc、a1 是新 argv，旧地址空间及其中的 exec stub 已被释放。

`kexec()` 不会把整个 trapframe 清零：除 `epc/sp/a1` 和随后写入的 a0 外，`ra/gp/tp/a2..a7/s*/t*` 仍是旧程序执行 `ecall` 时的值，`userret` 会照常恢复它们。正常启动只把 pc、sp、a0、a1 当作有效入口契约；尤其旧 `ra` 的数值仍在寄存器中，但其原目标代码已经随旧页表消失，新的 `ulib.c:start()` 也不会靠它返回。

失败 `exec` 不提交临时地址空间，原 `epc` 已在 `usertrap()` 中推进到旧 stub 的 `ecall` 后一条指令，最终以 a0=-1 回旧 caller。不过，读取旧地址空间中的 argv 指针使用 `fetchaddr()`/`copyin()`，可能物化合法范围内的 lazy 页；若 executable inode 损坏并在 size 内含 hole，`kexec()` 的事务中还可能由 `readi()->bmap()` 分配并登记磁盘块。失败不承诺用户页表或异常文件系统输入完全无副作用。

### `exit`

`sys_exit()` 把 64 位参数槽经 `argint()` 转为 32 位状态后调用 `kexit()`。`kexit()` 关闭文件、释放 cwd 引用、托管子进程并把自身置为 ZOMBIE，随后进入 scheduler；它永不回到 `syscall()`/`usertrap()`，地址空间和 trapframe 要等 parent `wait()` 才回收。唯一特例是 `initproc` 调用 `kexit()` 会触发 `panic("init exiting")`，系统不允许负责收养孤儿的 init 退出。

阻塞调用如 wait/read pipe 是普通返回的延迟版本：handler 睡眠，条件满足后在原内核栈继续，最终走相同返回链。

## 18. 错误路径

| 错误 | 处理位置 | 用户结果 |
|---|---|---|
| 未知 syscall number | `syscall()` | 打印诊断，a0=-1 |
| fd 非法/关闭 | `argfd()` | handler 返回 -1 |
| 用户地址坏 | 具体 copy helper | 返回 -1 或对象定义的短计数；已复制前缀和已分配 lazy 页不回滚 |
| 路径不终止 | `copyinstr()` | `argstr`/handler 返回 -1 |
| 进入 syscall 前已 killed | `usertrap()` | exit(-1)，不返回 |
| handler 期间被 killed | handler 自检或返回后的检查 | 若检查已观察到标志则 exit(-1)；最终检查后的 late kill 延至下次 trap |
| 意外用户异常 | usertrap default | 标记 killed并 exit(-1) |

系统调用返回错误通常不等于“什么都没发生”。例如 `pipe()` 若第二个 fd 的 `copyout()` 失败，会关闭刚建的描述符，但第一个 fd 数字可能已经留在用户数组中；跨页 `copyout()` 可能已经覆盖目标前缀。具体 handler 还可能在发现后续错误前改变文件偏移、对象内容或缓存状态，所以错误后的可见副作用必须按各 handler 的契约分析，系统调用分派层没有统一回滚机制。

## 19. 与中断的交错

handler 开中断运行期间，timer/设备中断从 supervisor mode 进入 `kernelvec -> kerneltrap`。`kernelvec` 在当前内核栈上保存 `ra`、`gp` 以及 `t0..t6/a0..a7`；`sp` 由栈帧本身体现，`s0..s11` 依靠 C ABI 由被调用代码保持。保存/恢复 `gp` 只保持原值，并不建立 kernel global pointer。它刻意不恢复 `tp`：若 `yield()` 后进程迁移到另一 hart，`tp` 必须保留新 hart id。

`kerneltrap()` 把被打断内核代码的 `sepc/sstatus` 存入 C 局部变量，读取 `scause`，处理设备并可能 `yield()`，最后恢复 `sepc/sstatus`；`scause` 无需为 `sret` 恢复。这个栈上现场独立于进程 trapframe。

它不会覆盖用户 epc，因为用户 PC 已保存到 trapframe。`stvec` 在整个 handler 阶段指向 kernelvec，直到 prepare_return 关中断后才改回 uservec。

## 20. 核心不变量

1. uservec 开始时用户页表必须同时映射 trampoline 和当前进程 trapframe。
2. 切内核页表前必须先从 trapframe取得 kernel satp、sp、tp 和入口。
3. 用户寄存器只能保存到当前进程 trapframe，不能依赖共享临时区。
4. 系统调用 epc 必须推进 4 字节；可修复 page fault 的 epc 不推进。
5. `stvec=uservec` 与“正在运行用户态”相配，`stvec=kernelvec` 与“正在运行内核态”相配；过渡期关闭中断。
6. 内核 C 代码不能直接解引用用户 VA；必须使用 copy helper，并把失败视为可能已复制前缀、已分配 lazy 页的非原子结果。
7. 返回值最终必须写入 trapframe a0，userret 恢复它后再 sret。

## 21. 调试方式

```gdb
b usertrap
b syscall
b sys_read
b prepare_return
```

在 `usertrap` 检查 `scause == 8`、`sepc` 和 `trapframe->a7/a0..a5`；在 `syscall` 前后比较 `trapframe->a0`；在 `prepare_return` 检查新 epc、sstatus 和 stvec。汇编单步时注意切 `satp` 后地址解析变化，trampoline 是刻意保持虚拟地址不变的唯一桥梁。

## 22. 核心结论

一次系统调用由三份状态协作完成：硬件 CSR 描述 trap 原因和返回权限，trapframe持久保存用户寄存器与下次进入内核所需字段，进程内核栈承载可睡眠的 C 调用链。trampoline 的职责不是业务分派，而是在两套页表都可执行的位置把这三份状态安全接起来。
