`make qemu` 启动后，在进入 `void main()` 之前，大致经历这几步：

1. **Makefile 构建内核和磁盘镜像**

`make qemu` 会先生成：

```text
kernel/kernel
fs.img
```

内核由 `kernel/kernel.ld` 链接，入口符号是：

```ld
ENTRY(_entry)
```

并且链接地址从：

```text
0x80000000
```

开始。

2. **QEMU 加载内核**

Makefile 里 QEMU 参数是：

```text
-machine virt -bios none -kernel kernel/kernel
```

这里 `-bios none` 表示不使用传统 BIOS/OpenSBI 引导，QEMU 直接把 xv6 kernel 加载到 `0x80000000`，然后让每个 hart，也就是 RISC-V CPU 核，跳到那里执行。

3. **执行 `kernel/entry.S` 里的 `_entry`**

`_entry` 是真正最早执行的 xv6 代码：

```asm
_entry:
        la sp, stack0
        li a0, 1024*4
        csrr a1, mhartid
        addi a1, a1, 1
        mul a0, a0, a1
        add sp, sp, a0
        call start
```

它做的核心事情是：**给当前 CPU 设置内核栈 `sp`**。

`stack0` 在 `start.c` 里定义：

```c
char stack0[4096 * NCPU];
```

每个 CPU 分到 4096 字节栈。`mhartid` 是当前 CPU 的编号，所以不同 hart 会拿到不同栈。

4. **跳到 `kernel/start.c` 的 `start()`**

此时还在 **machine mode**，也就是 RISC-V 最高权限级。

`start()` 做几件关键准备：

```c
// 设置 mret 后进入 supervisor mode
mstatus.MPP = Supervisor;

// 设置 mret 返回地址为 main
mepc = main;

// 暂时关闭分页
satp = 0;
```

也就是说，`start()` 并不是直接 `call main()`，而是准备好一次特权级切换：之后用 `mret` 跳到 `main()`。

5. **把异常和中断委托给 supervisor mode**

```c
w_medeleg(0xffff);
w_mideleg(0xffff);
w_sie(r_sie() | SIE_SEIE | SIE_STIE);
```

含义是：之后大部分异常、中断交给 supervisor mode 的 xv6 内核处理，而不是继续留在 machine mode。

6. **设置 PMP，允许 supervisor 访问物理内存**

```c
w_pmpaddr0(0x3fffffffffffffull);
w_pmpcfg0(0xf);
```

PMP 是 Physical Memory Protection。这里 xv6 简单地开放一大片物理内存访问权限，否则 supervisor mode 可能不能访问内存。

7. **初始化定时器中断**

```c
timerinit();
```

里面会开启 supervisor timer compare，并设置第一次时钟中断：

```c
w_stimecmp(r_time() + 1000000);
```

8. **保存当前 CPU 编号到 `tp` 寄存器**

```c
int id = r_mhartid();
w_tp(id);
```

xv6 后面用 `tp` 来实现：

```c
cpuid()
```

所以每个 CPU 都能知道“我是几号 hart”。

9. **执行 `mret`，进入 `main()`**

最后：

```c
asm volatile("mret");
```

`mret` 会根据前面设置好的寄存器：

```text
mstatus.MPP = Supervisor
mepc = main
```

从 machine mode 切换到 supervisor mode，并跳转到：

```c
void main()
```

所以一句话概括：

`make qemu` 后，QEMU 把内核放到 `0x80000000`，先执行 `_entry` 设置栈，再进入 `start()` 做特权级、中断、PMP、定时器、CPU 编号等初始化，最后通过 `mret` 从 machine mode 切到 supervisor mode，正式进入 `main()`。