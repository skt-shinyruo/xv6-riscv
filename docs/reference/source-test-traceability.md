# 源码、正确性义务与测试追踪矩阵

本文回答“改动一段源码以后，哪些文档结论和测试证据必须一起复核”。追踪关系不是简单的文件目录：一个系统调用通常跨越用户 stub、trap、参数复制、对象生命周期和设备完成路径；一个测试通过也只覆盖它实际观察的结果，不能自动证明锁顺序、资源回收或崩溃原子性。

本文以当前仓库的手写输入为基线。全局证明义务见[全局正确性不变量](../correctness/global-invariants.md)，容量及耗尽结果见[资源失败矩阵](resource-failure-matrix.md)，不可信输入与失败类别见[信任与失败模型](../architecture/trust-and-failure-model.md)，磁盘结构规则见[文件系统一致性](../filesystem/filesystem-consistency.md)，可重复验证方法见[故障注入](../verification/fault-injection.md)，测试本身的实际 oracle 见[用户程序与测试](../user/programs-and-tests.md)。

## 1. 怎样读这张矩阵

每一行包含五条边：

```text
源码文件/关键符号
        -> 解释它的主文档
        -> 改动必须保持的不变量
        -> 能发现一部分违反的现有测试
        -> 仍未被自动验证的空白
```

“测试”列只表示相关证据，不表示完备证明。证据强度按以下标签区分：

| 标签 | 含义 | 典型例子 |
|---|---|---|
| S | 静态核对：编译、链接、符号或文档漂移 | `make`、`docs/check-docs.sh` |
| F | 正常功能路径 | `opentest`、`exectest` |
| B | 参数、容量或错误边界 | `copyin`、`sbrkfail`、`diskfull` |
| C | 并发或调度压力 | `preempt`、`manywrites`、`grind` |
| R | QEMU 强杀后的恢复 | `test-xv6.py crash` |

不存在“P=已证明”的标签。锁图、happens-before、DMA 可见性和持久化顺序仍需源码审阅；有限测试只能反驳错误，不能穷尽所有交错。

## 2. 启动、平台与基础运行时

| 源码与关键符号 | 主文档 | 必须保持的不变量 | 现有证据 | 明确空白 |
|---|---|---|---|---|
| `kernel/entry.S:_entry`、`kernel/start.c:start/timerinit`、`kernel/main.c:main` | [启动与初始化](../architecture/boot-and-init.md)、[平台契约](../architecture/platform-contracts.md)、[entry.S 与 start](../assembly/entry-and-start.md) | hart id 在 `NCPU`/启动栈范围内；machine 到 supervisor 的 CSR 委托完整；boot hart 发布初始化后其他 hart 才进入 scheduler | S: `make`；F: 多 hart 启动至 shell | 没有自动测试 `CPUS>NCPU`、稀疏 hart id、错误 CSR 初值或启动发布 fence 缺失 |
| `Makefile`、`kernel/kernel.ld`、`kernel/memlayout.h` | [构建、链接与镜像](../build/build-link-and-fs-image.md)、[内存](../kernel/memory.md) | 编译/链接参数、生成依赖、链接地址、trampoline、kernel text/data、PHYSTOP 与 QEMU RAM 一致；各对齐断言成立 | S: `make` 和链接成功；F: boot | 没有 ELF 布局快照比较，也不检查 QEMU RAM 小于 `PHYSTOP`；部分 target 的工具/版本前置条件不同 |
| `kernel/riscv.h`、`kernel/types.h`、`kernel/param.h` | [平台契约](../architecture/platform-contracts.md)、[资源失败矩阵](resource-failure-matrix.md) | CSR/PTE 位与 Sv39 契约一致；宽度与容量改变必须传播到布局、循环上界和测试 | S: 编译；B: 若干容量测试 | 宏的语义关系没有机器可读 schema；修改一个容量不会自动重算日志和内存证明 |
| `kernel/string.c`、`kernel/printk.c:printk/panic`、`kernel/defs.h` | [内核运行时](../kernel/kernel-runtime.md) | freestanding 原语不越界；panic 串行化且不被当作可恢复错误；声明与定义一致 | S: `make`；间接由全套测试调用 | 缺少独立 string/format 单元测试；格式化边界与并发 panic 未专测 |

## 3. 锁、进程和调度

| 源码与关键符号 | 主文档 | 必须保持的不变量 | 现有证据 | 明确空白 |
|---|---|---|---|---|
| `kernel/spinlock.c:acquire/release/push_off/pop_off`、`kernel/spinlock.h` | [同步与锁](../kernel/synchronization.md)、[全局不变量](../correctness/global-invariants.md) | acquire/release 建立跨 hart 可见性；持锁期间本 hart 中断关闭；嵌套计数与原始中断状态配对 | C: 全套多核测试和 `grind` 间接覆盖 | 没有弱内存模型 litmus、锁依赖运行期检测或中断嵌套穷举 |
| `kernel/sleeplock.c:acquiresleep/releasesleep`、`kernel/sleeplock.h` | [同步与锁](../kernel/synchronization.md)、[调用上下文契约](../kernel/call-context-contracts.md) | sleeplock 身份由内部 spinlock 保护；等待不持有调用方不允许睡眠的锁 | C: 并发文件测试间接覆盖 | 没有公平性、饥饿或 owner 误用测试 |
| `kernel/proc.c:allocproc/freeproc/kfork/kexit/kwait`、`kernel/proc.h` | [进程与调度](../kernel/processes-and-scheduling.md)、[`fork -> exec -> wait`](../flows/fork-exec-wait.md) | proc 状态只在 `p->lock` 规则下转移；child 完整构造后才 RUNNABLE；ZOMBIE 保留到 wait；失败路径归还未发布资源 | B: 独立 `forktest`、`forkfork`、`forkforkfork`、`exitwait`；C: `twochildren`、`reparent`、`reparent2` | 未确定性耗尽每个 `allocproc` 子分配点；PID 回绕和槽复用没有长期 oracle |
| `kernel/proc.c:scheduler/sched/yield/sleep/wakeup`、`kernel/swtch.S:swtch` | [进程与调度](../kernel/processes-and-scheduling.md)、[同步与锁](../kernel/synchronization.md)、[swtch.S](../assembly/context-switch.md)、[timer 抢占与迁移](../flows/timer-preemption-and-migration.md) | RUNNING 进程至多在一个 hart；`sched` 只带 `p->lock` 且中断关闭；sleep 的谓词改变与 wakeup 使用同一条件锁协议；callee-saved 寄存器和 `sp/ra` 完整移交 | C: `preempt`、`pipe1`、`killstatus`、`manywrites`、`grind` | 没有可重放的指定调度交错；没有逐寄存器 context-switch 自检；无公平性保证 |
| `kernel/proc.c:reparent`、全局 `wait_lock` | [进程与调度](../kernel/processes-and-scheduling.md)、[可扩展性分析](../analysis/scalability.md) | parent 指针生命周期受 `wait_lock` 保护；锁顺序为 `wait_lock -> p->lock`；init 最终回收 adopted zombie | C: `reparent`、`reparent2`、`zombie` 观察程序 | 最坏扫描成本和多代同时退出交错没有确定性覆盖 |

## 4. 物理内存、页表与程序映像

| 源码与关键符号 | 主文档 | 必须保持的不变量 | 现有证据 | 明确空白 |
|---|---|---|---|---|
| `kernel/kalloc.c:kinit/kalloc/kfree` | [内存](../kernel/memory.md)、[资源失败矩阵](resource-failure-matrix.md) | 每个可分配物理页恰在 freelist 或由一个 owner 持有；`kfree` 拒绝非对齐/越界地址，但不检测 double free，调用图必须保证唯一释放；分配失败不损坏链表 | B: `sbrkmuch`、`sbrkfail`、每轮 `countfree`；C: `grind` | 没有精确页 owner 账本；double free 可重复插入 freelist；`countfree` 只能发现净泄漏且受并发影响 |
| `kernel/vm.c:walk/mappages/uvmalloc/uvmunmap/uvmcopy/uvmfree`、`kernel/vm.h` | [内存](../kernel/memory.md)、[可扩展性分析](../analysis/scalability.md) | 叶/非叶 PTE 类型正确；映射与物理页 owner 一致；构造失败回滚；释放只释放归属页且不遗留可访问映射 | B: `sbrkbasic`、`sbrkfail`、`kernmem`、`MAXVAplus`、`nowrite` | 空中间页表页的成本不被测试；没有随机页表模型对照或 TLB shootdown 测试 |
| `kernel/vm.c:copyin/copyout/copyinstr/vmfault` | [内存](../kernel/memory.md)、[lazy fault 流程](../flows/lazy-page-fault.md)、[系统调用](../kernel/system-calls.md) | 用户地址按页验证 PTE/用户位，输出另需写权限；只在当前进程允许的 lazy 路径补页；跨页失败允许复制前缀，结果由调用方传播 | B: `copyin`、`copyout`、`copyinstr1/2/3`、`rwsbrk`、`pgbug`、`lazy_copy` | 不预检完整区间，零长度和 `p->sz` 边界有例外；合法 lazy buffer 的每种方向未逐 API 覆盖；部分副作用无统一 oracle |
| `kernel/exec.c:kexec/loadseg`、`kernel/elf.h` | [`exec`](../kernel/exec.md)、[信任与失败模型](../architecture/trust-and-failure-model.md) | 当前只验证 magic、`memsz>=filesz`、一种 `vaddr+memsz` 回绕、segment 页对齐及分配/读取成功；新地址空间完整后一次提交，失败保留旧映像并保持栈 ABI 对齐 | F/B: `exectest`、`bsstest`、`bigargtest`、`stacktest`、`badarg`、`execout` | class/machine/table 范围、`off+filesz`、overlap、entry 可执行性等未系统验证；无变异 ELF corpus |

## 5. Trap、中断与系统调用 ABI

| 源码与关键符号 | 主文档 | 必须保持的不变量 | 现有证据 | 明确空白 |
|---|---|---|---|---|
| `kernel/trampoline.S:uservec/userret`、`kernel/trap.c:usertrap/prepare_return` | [Trap 与中断](../kernel/traps-and-interrupts.md)、[系统调用往返](../flows/syscall-round-trip.md)、[trampoline.S](../assembly/trampoline.md) | 用户 GPR/`sepc`/`sstatus` 完整保存恢复；`satp`、`stvec` 和 trapframe 只在中断关闭窗口切换；返回前 killed 状态统一处理 | F/B: 所有 syscall、用户 fault 和 `killstatus` | 没有逐寄存器往返测试；嵌套 trap 返回窗口和 CSR 前后态未自动核对 |
| `kernel/kernelvec.S:kernelvec`、`kernel/trap.c:kerneltrap/clockintr/devintr` | [Trap 与中断](../kernel/traps-and-interrupts.md)、[设备](../kernel/devices.md)、[kernelvec.S](../assembly/kernel-trap-vector.md)、[timer 抢占与迁移](../flows/timer-preemption-and-migration.md) | C ABI caller-saved 状态完整；kernel trap 返回原 `sepc/sstatus`；timer 只在可抢占进程上 yield；PLIC claim/complete 配对 | C: `preempt`、多核 I/O 测试 | 未注入每个 kernel 指令点的 timer；未知 level IRQ 重触发没有测试 |
| `kernel/syscall.h`、`kernel/syscall.c:syscall/argraw/argaddr/argstr`、`user/usys.pl`、`user/user.h` | [系统调用](../kernel/system-calls.md)、[用户 ABI](../user/runtime-and-abi.md) | 系统调用号、用户 stub、分发表、handler 和声明一一对应；参数寄存器及返回值宽度一致；未知号不越界 | S: `docs/check-docs.sh` 核对四处名称；F/B: `usertests` | checker 只核对名称，不解析 C 类型或 `sbrk` 的仓库特有双参数 ABI |
| `kernel/sysproc.c:sys_fork/sys_exit/sys_wait/sys_sbrk/...` | [系统调用](../kernel/system-calls.md)、[进程与调度](../kernel/processes-and-scheduling.md)、[kill 阻塞进程](../flows/kill-blocked-process.md) | 参数取得后委托正确内核 primitive；错误/kill/部分副作用符合各 syscall 契约 | B/C: 进程、时间和 sbrk 相关 quick tests | `pause`/`uptime` 精度、溢出和多 hart 时间可见性覆盖较弱 |

## 6. fd、file、pipe 和命名层

| 源码与关键符号 | 主文档 | 必须保持的不变量 | 现有证据 | 明确空白 |
|---|---|---|---|---|
| `kernel/file.c:filealloc/filedup/fileclose/fileread/filewrite`、`kernel/file.h` | [文件与管道](../kernel/files-and-pipes.md)、[资源失败矩阵](resource-failure-matrix.md) | `file.ref` 等于稳定边界上的 fd/临时引用总和；最后 close 按类型转移并释放 owner；inode write 分片满足日志预算；共享 offset 的串行化前提明确 | F/B: `sharedfd`、`writetest`、`bigwrite`、`badwrite` | 没有独立耗尽 `NFILE` 的确定性测试；device/pipe 没有统一 offset 锁 |
| `kernel/fcntl.h`、`kernel/stat.h` | [系统调用](../kernel/system-calls.md)、[用户 ABI](../user/runtime-and-abi.md)、[文件与管道](../kernel/files-and-pipes.md) | 用户/内核共享的 open flags、字段宽度、布局和返回语义一致；复制给用户的完整对象已初始化 | S: 用户程序编译；F/B: open/fstat 相关 tests | checker 不解析 C ABI 布局；当前 `struct stat` padding 未清零的问题需要独立回归 |
| `kernel/sysfile.c:argfd/fdalloc/sys_dup/sys_open/sys_close/sys_pipe` | [系统调用](../kernel/system-calls.md)、[文件与管道](../kernel/files-and-pipes.md) | fd 槽安装与 file ref 在系统调用稳定边界匹配；多阶段失败清除已安装槽；create 后 fd 耗尽允许名字这一已记录副作用 | B: fd 类 tests、`pipe1`、`manywrites`；C: `grind` | 未逐故障点检查 open/pipe 构造回滚；`NOFILE` 与 `NFILE` 原因不可由 `-1` 区分 |
| `kernel/pipe.c:pipealloc/pipewrite/piperead/pipeclose` | [文件与管道](../kernel/files-and-pipes.md)、[pipeline fd 拓扑](../flows/pipeline-fd-topology.md) | `readopen/writeopen` 与 file 引用生命周期匹配；buffer 计数单调且不越容量；条件改变和 wakeup 在 pipe 锁协议内；broken pipe 保留已写前缀 | B/C: `pipe1`、`sharedfd`、`grind` pipeline | 多 writer 的精确交错、零长度行为和 killed partial write 未独立断言 |
| `kernel/sysfile.c:create/sys_link/sys_unlink/sys_mkdir/sys_chdir` | [文件系统](../kernel/filesystem.md)、[文件系统事务](../flows/filesystem-transaction.md)、[文件系统一致性](../filesystem/filesystem-consistency.md)、[unlink 与 orphan 恢复](../flows/unlink-crash-orphan-recovery.md) | 所有会 `iput` 的命名路径在事务内；目录 `.`/`..`、parent nlink 和目标 nlink 一致；禁止目录硬链接和删除 `.`/`..` | B/C: `subdir`、`rmdot`、`dirfile`、`linktest`、`linkunlink`、`concreate`、`createdelete` | 测试只从 API 观察，未离线扫描完整目录图、重复名字或错误 nlink |

## 7. inode、buffer、日志和块设备

| 源码与关键符号 | 主文档 | 必须保持的不变量 | 现有证据 | 明确空白 |
|---|---|---|---|---|
| `kernel/fs.c:iget/idup/ilock/iunlock/iput`、`kernel/fs.h` | [文件系统](../kernel/filesystem.md)、[全局不变量](../correctness/global-invariants.md)、[文件系统一致性](../filesystem/filesystem-consistency.md) | `(dev,inum)` 在 cache 中身份唯一；`ref` 保护身份、sleeplock 保护内容；最后 `ref` 且 `nlink==0` 才截断；cache 耗尽当前会 panic | B/C: `iput`、`exitiput`、`openiput`、`iref` | `NINODE` 活跃槽耗尽无直接测试；`openiput` 默认调度不能可靠制造注释中的交错 |
| `kernel/fs.c:balloc/bfree/bmap/itrunc/readi/writei` | [文件系统](../kernel/filesystem.md)、[资源上界](../kernel/resource-bounds.md)、[文件读写](../flows/file-read-write.md)、[文件系统一致性](../filesystem/filesystem-consistency.md) | 已引用数据/间接块唯一且 bitmap 已置位；释放恰一次；size 与可寻址块范围一致；短写和已提交前缀语义明确 | B: `writebig`、`bigfile`、`bigwrite`、`truncate1/2/3`、`diskfull` | 没有离线 block owner 对照；`diskfull` 未证明清理后所有块均可复用 |
| `kernel/fs.c:dirlookup/dirlink/namex/ireclaim` | [文件系统](../kernel/filesystem.md)、[文件系统一致性](../filesystem/filesystem-consistency.md)、[unlink 与 orphan 恢复](../flows/unlink-crash-orphan-recovery.md) | 目录项 inode 号有效；稳定目录内名字唯一；路径持有/释放 inode 引用；恢复后扫描并回收 `type!=0 && nlink==0` orphan | B: `fourteen`、`subdir`；R: `forphan`/`dorphan` + `test-xv6.py crash` | 恶意目录环、重复 dirent、坏 inode 号会被内核高度信任；orphan 测试只看打印行 |
| `kernel/bio.c:bget/bread/bwrite/brelse/bpin/bunpin`、`kernel/buf.h` | [存储栈](../kernel/storage-stack.md)、[全局不变量](../correctness/global-invariants.md)、[可扩展性分析](../analysis/scalability.md) | 每个 `(dev,blockno)` 至多一个 cache identity；ref/pin 阻止复用；sleeplock 保护 data/valid；无 victim 当前 panic | C: `fourfiles`、`manywrites`、`stressfs` 间接覆盖 | 没有控制所有 buffer 被 pin/持有的测试，也无 cache identity 运行期审计 |
| `kernel/log.c:begin_op/log_write/end_op/commit/recover_from_log` | [存储栈](../kernel/storage-stack.md)、[事务流程](../flows/filesystem-transaction.md)、[资源上界](../kernel/resource-bounds.md)、[故障注入](../verification/fault-injection.md) | admission 预留上界成立；同一 home block absorption；提交严格为 log data -> 非零 header -> home -> 清 header；恢复可重复安装 | B/C: `manywrites`、`logstress`；R: `test-xv6.py log` | 强杀由延时决定，不能选择每个 commit 点；没有坏/越界 log header 测试或 flush/FUA 持久性证明 |
| `kernel/virtio_disk.c:virtio_disk_init/virtio_disk_rw/virtio_disk_intr`、`kernel/virtio.h` | [存储栈](../kernel/storage-stack.md)、[设备](../kernel/devices.md)、[可扩展性分析](../analysis/scalability.md)、[故障注入](../verification/fault-injection.md) | descriptor 三元组所有权唯一；avail 发布先于 notify；used/status 对 CPU 可见后才唤醒；IRQ 不睡眠；请求者醒后归还 chain | C: 并发文件测试、`stressfs` | 没有可观测的“最多两笔在途/第三笔等待”oracle；设备错误、超时、reset 和 DMA 故障均未覆盖 |
| `mkfs/mkfs.c` | [构建、链接与镜像](../build/build-link-and-fs-image.md)、[文件系统](../kernel/filesystem.md)、[文件系统一致性](../filesystem/filesystem-consistency.md) | 生产出的 superblock、bitmap、dinode、dirent 与内核 ABI 一致；所有输入文件落在布局边界内；根目录 `.`/`..` 正确 | S/F: `make fs.img` 后可启动、列目录和 exec | 构建器缺少通用 fsck；不检查所有重复最终名字、容量溢出或宿主输入变异 |

## 8. PLIC、UART 与 console

| 源码与关键符号 | 主文档 | 必须保持的不变量 | 现有证据 | 明确空白 |
|---|---|---|---|---|
| `kernel/plic.c:plicinit/plicinithart/plic_claim/plic_complete` | [设备](../kernel/devices.md)、[平台契约](../architecture/platform-contracts.md) | source priority/enable/context 与 QEMU virt 映射一致；每次非零 claim 最终 complete；设备先清源再 complete | F/C: console 与磁盘 I/O | 未测试 unknown source、错误 context 或持续 level IRQ；一次 trap 仅 claim 一次 |
| `kernel/uart.c:uartinit/uartputc_sync/uartintr` | [设备](../kernel/devices.md) | THRE 等待谓词和唤醒配对；每写一字节的 IRQ 状态不丢；RX drain 及时；IRQ handler 不睡眠 | F: shell/所有输出；C: 多进程输出 | 无 UART overrun、THRE 中断丢失、设备缺席或传输超时注入 |
| `kernel/console.c:consoleintr/consoleread/consolewrite` | [设备](../kernel/devices.md)、[一次按键到 shell](../flows/console-keystroke-to-shell.md) | `r<=w<=e` 累计索引保持容量；行、EOF 和满缓冲发布语义一致；producer/reader 用相同 console 锁条件协议 | F: 交互 shell、`cat`；B: `copyin/copyout` 间接 | 无自动按键/编辑序列、多 reader 或满缓冲丢字符 oracle |

## 9. 用户 ABI、shell、工具和测试输入

| 源码范围 | 主文档 | 必须保持的不变量 | 现有证据 | 明确空白 |
|---|---|---|---|---|
| `user/user.ld`、`user/ulib.c`、`user/printf.c`、`user/umalloc.c` | [用户 ABI](../user/runtime-and-abi.md) | `start`/`main`/`exit` 入口链一致；syscall 包装类型正确；malloc block 链无重叠且 sbrk 失败可恢复 | S: 用户程序链接；F/B: 所有程序、`mem` | 无用户库独立单元测试；printf 和 allocator 病理输入覆盖有限 |
| `user/init.c`、`user/sh.c` | [`init` 与 shell](../user/init-and-shell.md)、[启动与初始化](../architecture/boot-and-init.md)、[一次按键到 shell](../flows/console-keystroke-to-shell.md)、[pipeline fd 拓扑](../flows/pipeline-fd-topology.md) | fd 0/1/2 拓扑稳定；init 持续 wait；shell AST ownership 和 fork/exec/pipe/redirect close 顺序不泄漏 fd | F: 启动、手工命令；C: `grind` 的 pipeline | parser 错误多为 shell panic；无完整语法 corpus、fd 拓扑快照或孤儿数量 oracle |
| `user/cat.c`、`user/echo.c`、`user/grep.c`、`user/wc.c`、`user/ls.c` | [用户程序与测试](../user/programs-and-tests.md) | 工具只保证源码明确检查的返回值和简化语义；不能外推 POSIX 行为 | F: 手工运行和 shell 组合 | 没有自动 golden-output 套件；grep 无换行尾行、超长行等已知边界未回归 |
| `user/kill.c`、`user/ln.c`、`user/mkdir.c`、`user/rm.c`、`user/zombie.c` | [用户程序与测试](../user/programs-and-tests.md) | 参数及退出状态按当前简化实现解释；`zombie` 是观察负载而非自断言测试 | F: 手工运行 | 多个工具忽略 syscall 失败或仍返回 0，不能作为严格测试 oracle |
| `user/forktest.c` | [用户程序与测试](../user/programs-and-tests.md)、[资源失败矩阵](resource-failure-matrix.md) | 小映像先耗尽 proc 槽；已创建 child 全部可 wait；额外 wait 返回 -1 | B: 独立 `forktest` | 未直接读取失败原因；仍可能受其他进程/内存状态干扰 |
| `user/stressfs.c`、`user/logstress.c`、`user/grind.c` | [用户程序与测试](../user/programs-and-tests.md) | 它们主要是负载发生器；只有显式返回值/内容检查构成 oracle | C: 长期运行；R: logstress 被宿主强杀 | stressfs 忽略多项返回值；logstress 有已记录的 500/2000 buffer 越界；grind 无完整可重放 trace |
| `user/forphan.c`、`user/dorphan.c`、`test-xv6.py` | [用户程序与测试](../user/programs-and-tests.md)、[unlink 与 orphan 恢复](../flows/unlink-crash-orphan-recovery.md)、[故障注入](../verification/fault-injection.md) | 制造已 unlink 但仍有内存引用的 inode；强杀后不重置镜像；恢复先 redo log 再 ireclaim | R: `./test-xv6.py crash` | 延时/SIGKILL 不能精确选 crash point；宿主脚本失败路径本身有已记录缺陷 |
| `user/usertests.c` | [用户程序与测试](../user/programs-and-tests.md) | 每项在子进程隔离；测试名注册后文档必须记录实际 oracle；整轮前后 page leak 检查仅作近似 | F/B/C: quick 和 slow suites | 不覆盖任意交错、恶意磁盘或全部资源；若测试本身不检查结果，runner 仍会报告 OK |

## 10. `usertests` 注册项反向索引

下面列出当前所有注册名。`docs/check-docs.sh` 从源码提取注册项，并要求每个名字在文档中出现；新增函数但忘记登记仍不会被发现，因此增加测试时必须同时修改数组。

| 领域 | 注册测试 |
|---|---|
| 用户复制与参数 | `copyin`, `copyout`, `copyinstr1`, `copyinstr2`, `copyinstr3`, `rwsbrk`, `validatetest`, `bigargtest`, `argptest`, `pgbug`, `badarg` |
| inode、目录与文件 | `truncate1`, `truncate2`, `truncate3`, `openiput`, `exitiput`, `iput`, `opentest`, `writetest`, `writebig`, `createtest`, `dirtest`, `fourfiles`, `createdelete`, `unlinkread`, `linktest`, `concreate`, `linkunlink`, `subdir`, `bigwrite`, `bigfile`, `fourteen`, `rmdot`, `dirfile`, `iref` |
| 进程、fd 与调度 | `exectest`, `pipe1`, `killstatus`, `preempt`, `exitwait`, `reparent`, `twochildren`, `forkfork`, `forkforkfork`, `reparent2`, `sharedfd`, `forktest` |
| eager VM 与保护 | `mem`, `sbrkbasic`, `sbrkmuch`, `kernmem`, `MAXVAplus`, `sbrkfail`, `sbrkarg`, `bsstest`, `stacktest`, `nowrite`, `sbrkbugs`, `sbrklast`, `sbrk8000` |
| lazy VM | `lazy_alloc`, `lazy_unmap`, `lazy_copy`, `lazy_sbrk` |
| slow 文件系统/内存 | `bigdir`, `manywrites`, `badwrite`, `execout`, `diskfull`, `outofinodes` |

源码中的 `fsfull()` 没有注册到两个数组，因此不属于默认或单项可运行集合。追踪脚本检查“已注册名字有文档”，不把任意形似测试的函数误判为可运行项。

## 11. 手写输入文件完整清单

本节是文件级基线。检查脚本动态扫描同类扩展名；新增手写实现文件若没有在任一文档中以完整路径出现会失败。生成物、目标文件和磁盘镜像不进入基线。

### 11.1 内核

```text
kernel/bio.c             kernel/buf.h             kernel/console.c
kernel/defs.h            kernel/elf.h             kernel/entry.S
kernel/exec.c            kernel/fcntl.h           kernel/file.c
kernel/file.h            kernel/fs.c              kernel/fs.h
kernel/kalloc.c          kernel/kernel.ld         kernel/kernelvec.S
kernel/log.c             kernel/main.c            kernel/memlayout.h
kernel/param.h           kernel/pipe.c            kernel/plic.c
kernel/printk.c          kernel/proc.c            kernel/proc.h
kernel/riscv.h           kernel/sleeplock.c       kernel/sleeplock.h
kernel/spinlock.c        kernel/spinlock.h        kernel/start.c
kernel/stat.h            kernel/string.c          kernel/swtch.S
kernel/syscall.c         kernel/syscall.h         kernel/sysfile.c
kernel/sysproc.c         kernel/trampoline.S      kernel/trap.c
kernel/types.h           kernel/uart.c            kernel/virtio.h
kernel/virtio_disk.c     kernel/vm.c              kernel/vm.h
```

头文件也属于契约：`kernel/stat.h`/`kernel/fcntl.h` 跨用户 ABI，`kernel/elf.h` 跨宿主产生的 ELF，`kernel/buf.h`/`kernel/file.h`/`kernel/proc.h` 跨 C 模块，不能因没有独立目标文件而遗漏。

### 11.2 用户态、镜像和宿主入口

```text
user/cat.c               user/dorphan.c           user/echo.c
user/forktest.c          user/forphan.c           user/grep.c
user/grind.c             user/init.c              user/kill.c
user/ln.c                user/logstress.c         user/ls.c
user/mkdir.c             user/printf.c            user/rm.c
user/sh.c                user/stressfs.c          user/ulib.c
user/umalloc.c           user/user.h              user/user.ld
user/usertests.c         user/usys.pl             user/wc.c
user/zombie.c            mkfs/mkfs.c              Makefile
test-xv6.py
```

`user/usys.S` 由 `user/usys.pl` 生成，不是手写真源；`kernel/kernel`、`*.o`、`*.d`、`*.asm`、`*.sym`、`user/_*`、`mkfs/mkfs` 和 `fs.img` 同样是生成物。根目录 `README` 是 `fs.img` 内容输入，`.gdbinit.tmpl-riscv` 与 `.vscode/*.json` 是调试配置；它们分别由[构建文档](../build/build-link-and-fs-image.md)和[调试文档](../build/vscode-debug.md)维护，但不被实现源码扫描器当作内核行为输入。

## 12. 当前覆盖空白总表

| 空白 | 为什么现有测试不够 | 最小补强方向 |
|---|---|---|
| 精确 OOM/表槽故障点 | 只能通过自然耗尽，难以区分先耗尽哪层 | 按[故障注入](../verification/fault-injection.md)给 `kalloc/filealloc/iget/bget` 加可计数 failpoint，逐分配点验证回滚和再次成功 |
| 指定调度交错 | `yield` 和压力运行不可重放 | 按[故障注入](../verification/fault-injection.md)设置双栅栏调度点；用[丢失唤醒实验](../labs/lost-wakeup.md)验证反例和修复 |
| 精确日志 crash point | 宿主等待固定秒数后 SIGKILL | 按[故障注入](../verification/fault-injection.md)在四个提交阶段设置一次性停点，由宿主等握手后杀 QEMU |
| 恶意文件系统镜像 | `mkfs` 只产生预期良构镜像 | 离线变异 superblock/log/dinode/dirent/bitmap，并按[离线 fsck 实验](../labs/offline-fsck.md)建立诊断 oracle |
| 内存模型和 DMA | QEMU 上压力未失败不代表 RVWMO/设备发布证明 | 对关键 fence 建 litmus/事件 trace；分别标注 CPU、TLB、DMA、磁盘持久性边界 |
| cache/queue 联合耗尽 | 现有 I/O 压力没有观测 buffer pin 和 descriptor owner | 导出只读计数器，构造 NBUF 持有与第三笔 VirtIO 请求等待；buffer 设计见[缓存改造实验](../labs/buffer-cache.md) |
| console/IRQ 故障 | 交互输入无法稳定复现满缓冲、丢 IRQ、未知 source | 用 QEMU monitor/测试设备或内核注入点脚本化字符和 IRQ 序列 |
| syscall ABI 类型漂移 | 当前 checker 只比名称 | 从单一描述生成号、stub、声明和分派，编译 ABI 静态断言 |
| 性能和活性 | 功能通过不提供复杂度、公平性或延迟上界 | 按[可扩展性分析](../analysis/scalability.md)记录扫描次数、锁等待和 I/O 放大；固定工作量/CPU 数做回归阈值 |

## 13. 自动漂移检查的设计边界

仓库根目录运行：

```sh
./docs/check-docs.sh
```

`.github/workflows/docs.yml` 在每次 push 和 pull request 上运行同一命令，并把权限收窄为只读仓库内容。CI 不生成或提交文档；矩阵仍由维护者基于源码语义更新，脚本负责拒绝已知的文件级断边。修改 checker 时必须先在仓库根目录和仓库外工作目录各运行一次，避免依赖调用者的当前目录。

脚本执行四类保守检查：

1. 用户清单中的 P0/P1/P2 文档、核心既有文档和 CI workflow 都作为必备基线存在。
2. 对 Markdown 中目标以 `.md` 结尾或带 `#fragment` 的简单相对链接，只检查目标文件存在；fragment 被剥离但不验证锚点。
3. 动态枚举 `kernel/*.{c,h,S}`、`kernel/*.ld`、`user/*.{c,h}`、`user/*.ld`、`user/*.pl`、`mkfs/*.c`、`Makefile` 和 `test-xv6.py`，要求完整路径出现在本文第 2 至 9 节的语义矩阵中；第 11 节的静态清单不能自证覆盖。
4. 从 `kernel/syscall.h` 读取 `SYS_name`，检查 `user/usys.pl` 的 entry、`kernel/syscall.c` 的分发表、`user/user.h` 的名字和系统调用文档；并从 `usertests.c` 提取注册字符串，要求每项在 `user/programs-and-tests.md` 有 oracle 入口，而不是只在本文反向索引中出现。

为了减少误报，脚本刻意不做以下事情：

- 不验证 Markdown 标题到 anchor 的 renderer 特定规则；
- 不解析带标题、换行、转义或空格目标的复杂 Markdown link，也不访问 HTTP 链接；
- 不把一次字符串出现当作语义正确，不证明矩阵行描述仍与实现一致；
- 不运行编译、QEMU、测试、fsck 或性能测量；
- 不比较生成文件，因为生成文件应由其真源和构建规则重建。

因此 checker 通过仅表示“文件级追踪边没有明显断裂”。语义审阅和动态验证仍是变更验收的一部分。

## 14. 维护规则

修改实现时按以下顺序更新，不接受只补最后一个链接的做法：

1. 在提交说明中列出改变的 owner、状态、容量、锁、等待谓词、失败结果和持久化边界。
2. 更新对应专题文档；若跨两个以上子系统，再更新全局不变量或信任/失败矩阵。
3. 在本文对应行更新关键符号、不变量、现有证据和空白。不能把新增测试写成它没有观察的结论。
4. 新增系统调用必须同时更新号、用户声明、stub、分派、handler、ABI 文档和至少一个成功/失败测试。
5. 新增 `usertests` 项必须登记数组、使用唯一资源名、检查所有关键返回值、清理成功和失败路径，并在测试文档记录实际 oracle。
6. 修改磁盘格式、日志容量或 VirtIO ring 时，必须更新生产者和消费者两端以及离线/崩溃验证；成功 boot 不是格式兼容证明。
7. 运行 `./docs/check-docs.sh`、`make`、相关单项、quick suite；涉及资源、并发或持久化时再运行完整 suite、确定性故障注入与 crash 恢复。
8. 测试失败要保存源码 commit、QEMU/CPU 数、镜像来源、命令、seed/failpoint/crash point 和完整输出，使结果可复现。

追踪矩阵的价值不在于让每格看起来“已覆盖”，而在于让未覆盖项保持可见。新增一条弱压力测试不应删除证明空白；只有加入能稳定触发目标状态且拥有强 oracle 的验证后，才能缩小对应空白。
