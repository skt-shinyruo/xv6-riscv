# 有界实验：新增最小系统调用

## 问题场景与本单元成果

跟随一条已经存在的 `getpid()` 路径，只能证明学习者会阅读现成的 ABI 链。
本实验新增 `sysprobe(int)`：用户传入一个 C `int`，内核不改变它并原样返回。
功能刻意保持最小，以便把声明、生成 stub、编号、分派、handler、测试注册、
失败中间态和回归责任全部放进一个可审阅边界。

本单元的唯一出口产物是一份 `sysprobe` 有界修改报告包。报告包引用一个只含
六个手写文件的 patch，并在同一份报告中保存缺少 dispatch 的 `B` transcript、
完整实现的 `F` transcript、生成代码证据、定向/相关/完整三层回归和 cleanup。
只提交能编译的 patch，或只写 `ALL TESTS PASSED`，都不构成出口产物。

## 前置单元与暂存黑盒

硬前置：[一次系统调用如何往返](../core/syscall-roundtrip.md)。相关单元：
[用户程序如何成为可运行镜像](../core/user-program-and-abi.md)。前置单元已经
解释 `ecall -> uservec -> usertrap -> syscall -> handler -> sret`；本实验不
复制该说明，而是验证学习者能在这条路径上增加一个新的表项。

用户指针、复杂参数、锁、睡眠、资源分配和持久化不属于本实验。`sysprobe`
不建立新错误码，也不推广 `fork/exec/exit` 的特殊语义。完整 trampoline
页表契约仍由后续进程与内存单元解释。

## 最小模型和关键不变量

最终 patch 的手写路径集合必须精确等于：

```text
user/user.h       : int sysprobe(int) 声明
user/usys.pl      : entry("sysprobe") 生成器输入
kernel/syscall.h  : SYS_sysprobe 22
kernel/syscall.c  : extern + syscalls[] 表项
kernel/sysproc.c  : sys_sysprobe handler
user/usertests.c  : sysprobetest + quicktests 注册
```

`user/usys.S`、`user/usys.o`、`user/usertests.asm` 和 `user/usertests.sym` 都是
构建产物，不进入 patch。`user/usys.pl` 生成包含 `SYS_sysprobe` token 的汇编；
`kernel/syscall.h` 在编译 `user/usys.o` 时提供编号，两条依赖不能混为一条。

当前 pinned baseline 的最大编号是 `SYS_close=21`，因此新编号是唯一的正整数
`22`。handler 的契约只有两步：

```c
argint(0, &value);
return value;
```

当前分支的 `argint()` 返回 `void`，从 trapframe 参数寄存器取得低 32 位；
不要照搬其他 xv6 版本中“检查 `argint()` 返回值”的接口。`uint64` handler
返回 `-7` 时会在寄存器中符号扩展，用户 C 原型再按 `int` 解释低 32 位；因此
测试必须分别比较 `0`、`12345` 和 `-7`，不能只测正数。

失败中间态保留用户声明、stub、编号和测试，但暂时移除
`kernel/syscall.c/kernel/sysproc.c` 两处 dispatch/handler 改动。此时 `22`
超出原 `syscalls[]` 长度，现有短路 guard：

```text
num > 0 && num < NELEM(syscalls) && syscalls[num]
```

必须安全进入 unknown 分支，把 `-1` 写回 `trapframe->a0`。首个
`sysprobe(0)` 随即打印稳定失败诊断并退出，所以 unknown 诊断必须恰好一次，
不能 panic、重复 trap 或等待超时。

## 源码追踪计划

先用 pinned baseline 中存在的稳定 token 复核类比链；manifest 锚点不能指向
尚未应用 patch 的 `sysprobe`：

```sh
rg -n 'getpid|sys_sbrk' user/user.h
rg -n 'entry\("getpid"\)|^sub entry' user/usys.pl
rg -n '^#define SYS_getpid |^#define SYS_close ' kernel/syscall.h
rg -n '^extern uint64 sys_getpid|^static uint64 \(\*syscalls|unknown sys call' kernel/syscall.c
rg -n '^sys_getpid\(|argint\(0,' kernel/sysproc.c
rg -n '^struct test|quicktests|^runtests\(|^drivetests\(' user/usertests.c
rg -n '^def test_usertests|q\.monitor|ALL TESTS PASSED' test-xv6.py
rg -n '\$U/usys\.S|\$U/usys\.o|\$U/_usertests' Makefile
```

应用 patch 后建立三个闭环：

```text
name:   user.h -> usys.pl -> generated global sysprobe -> C caller
number: syscall.h(22) -> generated li a7,22 -> trapframe.a7 -> syscalls[22]
value:  caller int -> argint(0) -> handler return -> trapframe.a0 -> caller int
```

测试函数必须叫 `sysprobetest(char *s)`，不能也叫 `sysprobe`，否则会与用户
系统调用原型冲突。它以字符串 `"sysprobe"` 注册进 `quicktests[]`，因此
`usertests sysprobe`、`usertests -q` 和完整 `usertests` 都经过同一测试实现。

## 观察任务

先读唯一 patch 和 runner，不启动 QEMU：

```sh
sed -n '1,260p' docs/xv6-tutorial/resources/add-system-call/sysprobe.patch
python3 docs/xv6-tutorial/resources/add-system-call/run-lab.py --static-only
```

静态检查从 `curriculum.json` 读取 pinned baseline，在一个临时导出中验证：

- patch path 集恰好是六个手写文件，且不含 `user/usys.S`；
- 编号 `22` 唯一，原最大编号为 `21`，安全 dispatch guard 未改；
- 声明、generator entry、extern、表项、handler、测试和 quick 注册各出现一次；
- `perl user/usys.pl` 的 stdout 与 Make 生成的 `user/usys.S` 逐字节一致；
- `_usertests` 反汇编中的 `sysprobe` stub 实际执行 `li a7,22`、`ecall`、
  `ret`；两个 kernel/user 目标以 `-Werror` 构建成功；
- 逆向 patch 并 `make clean` 后，临时导出的逐文件哈希和 mode 回到原值。

这些检查证明生成来源和闭合映射，不证明 guest 中的失败或返回值；动态证据
属于下一段的有界修改任务。

## 有界修改任务

准备一个仓库外的报告目录，然后运行完整实验：

```sh
REPORT_DIR=$(mktemp -d /tmp/xv6-sysprobe-report.XXXXXX)
python3 docs/xv6-tutorial/resources/add-system-call/run-lab.py \
  --report "$REPORT_DIR/sysprobe.md"
sed -n '1,240p' "$REPORT_DIR/sysprobe.md"
```

runner 只写一个 pinned-baseline 临时导出和它的私有 `fs.img`，使用
`CPUS=1`。它先完整应用 [`sysprobe.patch`](../resources/add-system-call/sysprobe.patch)，
再用同一 patch 只逆向 `kernel/syscall.c` 与 `kernel/sysproc.c`，构建受控
missing-dispatch 状态。这个阶段不能调用 `./test-xv6.py sysprobe`：现有 driver
只等待成功 marker，预期失败会白等 600 秒。runner 直接驱动 QEMU，要求完整
transcript 为以下结构，其中 pid 可变：

```text
usertests sysprobe
usertests starting
test sysprobe: <pid> usertests: unknown sys call 22
sysprobe: sysprobe(0) returned -1, expected 0
FAILED
SOME TESTS FAILED
$
```

随后 runner 从同一个 patch 恢复两处 kernel 改动、全新构建，并精确要求：

```text
usertests sysprobe
usertests starting
test sysprobe: OK
ALL TESTS PASSED
$
```

最后分别运行现有 driver 的三个命令；它们是三个独立验收项，不能互相替代：

```sh
./test-xv6.py sysprobe
./test-xv6.py -q usertests
./test-xv6.py usertests
```

runner 给每个 driver 建立独立进程组和 watchdog；无论成功、失败或超时都清理
该组，避免 driver 只结束父 `make` 而遗留 QEMU。完整回归通过后，它执行
`make clean`、逆向完整 patch、比较临时树快照，并核对原工作树状态和共享
`fs.img` 哈希前后相同。检查报告后删除仓库外的目录：

```sh
rm -r -- "$REPORT_DIR"
git status --short
```

## Oracle、证据、失败路径和局限

- `S`：patch path 集精确等于六个手写文件；`SYS_sysprobe=22` 唯一；名称链
  各出现一次；generator 重放一致；生成 stub 的数值指令是 `li a7,22` 后接
  `ecall/ret`；dispatch guard 未改。
- `F`：`sysprobetest` 对 `0/12345/-7` 逐项做精确 `int` 比较；final transcript
  只有 `test sysprobe: OK` 和权威终止 marker；focused driver 再次通过。
- `B`：只移除 dispatch/handler 后，编号、stub 和测试仍在；unknown 诊断中的
  编号为 `22` 且恰好一次，用户取得 `-1`，测试明确失败，shell 提示符恢复。
  panic、重复诊断、无提示符或 timeout 都是失败，不是边界证据。
- 相关回归：`./test-xv6.py -q usertests` 独立得到 `ALL TESTS PASSED`。
- 完整回归：`./test-xv6.py usertests` 独立得到 `ALL TESTS PASSED`。
- cleanup：临时树的内容和 mode 完全恢复；原 `git status --short`、共享
  `fs.img` 哈希不变；无 QEMU 或 runner 临时目录残留。
- `C` 不适用：handler 不共享可变状态、不加锁、不阻塞；`CPUS=1` 的结果不
  建立跨 hart 并发结论。
- `R` 不适用：handler 不分配资源、不写磁盘、不定义 crash point；删除私有
  镜像是 cleanup，不是恢复证据。

quick 和完整回归不能排除声明/编号错配恰好相互抵消，所以不能替代 `S`；静态
闭环也不能证明 guest 返回值，所以不能替代 `F/B`。本实验只支持一个整数
参数和普通返回，不支持用户指针、阻塞、持久化或新的错误协议。

## 退出产物与后续单元

提交一份填好的 `sysprobe` 有界修改报告包。包内必须引用唯一六文件 patch，
记录环境与 pinned baseline、生成器/反汇编证据、missing-dispatch 与 final
完整 transcript、三层 driver 结果、`C/R` 不适用理由、cleanup 快照和证据
局限。正常与失败只是同一报告的两个分节，不是两份出口产物。

现有 `docs/questions/` 没有只由本实验拥有的独立问题，因此本单元不迁题，也
不创建同步副本。完成后进入 manifest 中的后继单元
`core.process-and-memory`，解释系统调用往返中仍保留的进程状态、内核栈和
页表生命周期。
