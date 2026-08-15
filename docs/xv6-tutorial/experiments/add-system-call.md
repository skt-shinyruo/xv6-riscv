# 有界实验：新增最小系统调用

## 问题场景与本单元成果

阅读现有系统调用只能证明你能跟随一条已经存在的路径。本实验要求新增 `sysprobe(int)`：用户传入一个 `int`，内核原样返回。功能很小，目的是让 ABI 全链路、失败中间态和回归责任保持可审阅。

出口产物是一份有界 patch、一个注册到 `usertests` 的定向测试和完整实验报告。

## 前置单元与暂存黑盒

硬前置：[一次系统调用如何往返](../core/syscall-roundtrip.md)。相关单元：[用户程序如何成为可运行镜像](../core/user-program-and-abi.md)。

用户指针、复杂参数、锁和持久化不属于本实验。`sysprobe` 不访问全局可变状态，不分配资源，也不建立新的错误码。

## 最小模型和关键不变量

需要改变的链路：

```text
user/user.h: sysprobe 声明
user/usys.pl: sysprobe stub
kernel/syscall.h: 唯一编号
kernel/syscall.c: extern + 分派表
kernel/sysproc.c: sys_sysprobe handler
user/usertests.c: 定向测试 + quicktests 注册
```

handler 的预期形状：读取第 0 个整数参数并把它作为返回值。当前分支的 `argint` 返回 `void`，它从 trapframe 的参数寄存器读取低 32 位；不要套用其他版本中 `argint` 返回错误码的接口。

关键不变量：

- 编号为当前最大编号之后的新唯一正整数。
- C 声明、生成器名称、`SYS_` 宏、extern、表项和 handler 名称完全一致。
- handler 没有资源副作用；所有测试值应原样往返为 C `int`。
- 未注册表项的中间状态必须安全返回 `-1`，不能跳到错误函数。

## 源码追踪计划

先对照现有 `getpid` 链：

1. `user/user.h:getpid`
2. `user/usys.pl:entry("getpid")`
3. `kernel/syscall.h:SYS_getpid`
4. `kernel/syscall.c:syscalls`
5. `kernel/sysproc.c:sys_getpid`
6. `user/usertests.c:quicktests`

然后为 `sysprobe` 建立一一对应的锚点。不要直接编辑生成的 `user/usys.S`。

## 观察任务

分阶段实现，并保留每阶段结果：

1. 只加入用户声明、生成器条目、编号和测试调用，但暂不加入分派表。构建应成功；运行定向测试时内核应打印未知系统调用，调用返回 `-1`，测试失败。这是受控的 `B` 证据。
2. 加入 handler 声明和分派表，确认 `sysprobe(0)`、`sysprobe(12345)` 和 `sysprobe(-7)` 原样返回。
3. 检查重新生成的 `user/usys.S`，确认 stub 设置新编号、执行 `ecall` 并 `ret`。

定向测试函数必须接收 `char *s`，失败时打印稳定诊断并 `exit(1)`，然后以名称 `sysprobe` 注册到 `quicktests`。

## 有界修改任务

完成上述 `sysprobe(int)` 全链路。修改范围限制为六个手写文件；`user/usys.S` 只是构建生成物，不纳入 patch。

运行顺序：

```sh
./test-xv6.py sysprobe
./test-xv6.py -q usertests
./test-xv6.py usertests
```

先运行定向测试，再运行 quick 回归，最后完整回归。不要用完整回归替代定向 oracle。

## Oracle、证据、失败路径和局限

- `S`：六个手写位置形成闭合映射；编号唯一，生成文件没有手改。
- `F`：三个代表值精确往返，定向测试打印通过，正常构建无警告。
- `B`：缺少表项的受控中间状态返回 `-1` 并输出未知编号；最终实现不再出现该诊断。
- `C/R`：`N/A`。handler 不共享可变状态、不阻塞、不写持久化数据；报告必须明确写出这一理由。
- quick 和完整回归通过不能证明所有 ABI 错配都不存在；静态链路核对仍是必要证据。

## 退出产物与后续单元

提交 patch、[有界修改报告](../templates/bounded-change-report.md)、定向/quick/完整回归结果，以及中间未知编号失败的观察。当前实验仍为 `draft`；非作者按同一基线走通并修正卡点后才可标记 `verified`。
