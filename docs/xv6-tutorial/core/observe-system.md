# 从 QEMU 启动到 shell 提示符

## 问题场景与本单元成果

第一次运行 xv6 时，屏幕会从 QEMU 启动信息走到 shell 提示符。这个结果横跨机器入口、内核初始化、调度、首进程、文件系统和用户程序；若一开始就展开所有细节，读者会失去主线。

本单元只建立一条带时间戳的可观察骨架。出口产物是一份从 `_entry` 到 `$ ` 的事件表，其中每个已解释步骤都有源码锚点，每个未解释机制都被明确标为黑盒。

## 前置单元与暂存黑盒

硬前置：[Foundation gate](../foundation/gate.md)。

本轮暂存两个黑盒：

- `first-user-process`：空首进程怎样获得 `/init` 地址空间并进入用户态。
- `shell-image-path`：`init`、`sh` 和其他用户程序怎样进入 `fs.img`，又怎样成为内存中的程序。

调度锁、页表、文件系统恢复、设备中断和多 hart 顺序都不在本单元证明范围内。

## 最小模型和关键不变量

先使用这一条骨架：

```text
QEMU
  -> kernel/entry.S:_entry
  -> kernel/start.c:start
  -> kernel/main.c:main
  -> kernel/proc.c:scheduler
  -> kernel/proc.c:forkret
  -> kexec("/init")
  -> user/init.c:main
  -> user/sh.c:main
  -> "$ "
```

`main()` 在 hart 0 初始化共享子系统、创建首进程并发布 `started=1`；其他 hart 等待该发布后只做每 hart 初始化。所有 hart 最终进入 `scheduler()`，但打印顺序不是正确性协议。

### Branch delta

本分支的 `userinit()` 只分配一个空首进程并把它设为 `RUNNABLE`。第一次 `forkret()` 在普通进程上下文中运行 `fsinit()`，随后直接 `kexec("/init", ...)`。不要套用“内核把一段 initcode 复制到首进程”的常见 xv6 路径。

关键不变量：

- `_entry` 调用 C 前，每个 hart 已有独立且对齐的栈。
- 共享初始化完成前，其他 hart 不能使用共享子系统。
- `scheduler()` 只运行 `RUNNABLE` 进程。
- shell 提示符出现说明这次执行已经到达用户态并完成了足够的文件系统、进程和控制台路径；它不证明这些机制在失败和并发下正确。

## 源码追踪计划

按顺序定位：

1. `Makefile:qemu`：QEMU 参数、CPU 数和磁盘镜像。
2. `kernel/entry.S:_entry`：每 hart 第一段仓库代码。
3. `kernel/start.c:start`：machine mode 到 supervisor mode 的交接。
4. `kernel/main.c:main`：共享与每 hart 初始化。
5. `kernel/proc.c:scheduler`：选择可运行进程。
6. `kernel/proc.c:forkret`：首进程第一次恢复及 `/init` 装载。
7. `user/init.c:main`：建立 console fd、启动并回收 shell。
8. `user/sh.c:main`：打印提示符并读取命令。

本单元只要求能把这些锚点排成因果顺序，不展开每个函数内部的全部调用。

## 观察任务

从没有正在运行的 QEMU 实例开始：

```sh
make qemu
```

看到 `$ ` 后输入：

```text
echo tutorial-observation
```

使用 QEMU 的 `Ctrl-a x` 退出。把以下事件写入追踪工作表：内核启动行、其他 hart 启动行、`init: starting sh`、shell 提示符、echo 输出。只记录实际观察顺序，不把一次输出顺序写成并发保证。

随后使用 `make qemu-gdb`，在 `_entry`、`main`、`scheduler` 和 `forkret` 设置断点。多 hart 可能多次命中入口和调度器；每次记录 `$tp` 或 hart 信息，不把“第一次命中”误认为全局唯一事件。

## 有界修改任务

不改源码，只改变一个明确配置：

```sh
make CPUS=1 qemu
```

与默认 `CPUS=3` 比较启动输出。Oracle 不是“输出完全相同”，而是两种配置最终都到达 shell；单 hart 不应出现其他 hart 的启动行。退出第一个 QEMU 后才能启动第二个，避免两个实例同时写 `fs.img`。

## Oracle、证据、失败路径和局限

- `S`：骨架中的每条边都有一个源码锚点，且本分支首进程路径写为 `forkret -> kexec("/init")`。
- `F`：默认和 `CPUS=1` 都到达 shell，echo 精确打印输入文本。
- `B`：工具链、QEMU 版本或磁盘镜像构建失败时，记录最早失败命令和退出状态；timeout 只负责结束等待。
- 本单元没有 `C/R` 结论。多 hart 成功启动不证明无竞态；正常退出也不证明崩溃恢复。

## 退出产物与后续单元

提交启动事件表、四个断点的最小追踪、单/多 hart 对比，以及两个显式黑盒。随后进入 [用户程序如何成为可运行镜像](user-program-and-abi.md)，解除 `shell-image-path`。
