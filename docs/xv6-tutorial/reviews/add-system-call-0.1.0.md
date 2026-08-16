# Add-system-call 0.1.0 非作者走查记录

- 教程版本：`0.1.0 draft`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`425cdfb7bf0b13fd9e788154f00e579c8d91ec1f` 加 #7 未提交候选 diff；最终提交保留本记录与修正后的完整 diff
- 走查单元或连续路径：`lab.add-system-call`
- 匿名入口能力：`SYSLAB-C19-R1`；已通过 Foundation gate 与前三个 core 单元，未参与候选正文、patch 或 runner 编写
- 使用环境：WSL2 Linux x86-64；Python 3.12.3、Make 4.3、Perl 5.38.2、QEMU 8.2.2、RISC-V GCC 13.3.0；`CPUS=1`、128 MiB、私有镜像

## 观察到的卡点

初始候选能完成动态实验，但两个 manifest anchor 只是通用子串；唯一出口报告
也没有绑定 patch 路径与摘要，且没有按 `F/B` 分别记录允许副作用、资源结果
和 cleanup。静态 oracle 还只检查三个常量与比较表达式存在，把
`sysprobe(values[i])` 换成输入值或缩短循环后仍可能伪通过。

修订将 anchor 收紧为 pinned baseline 中的 `sub entry` 与 `$U/usys.S :`，
报告增加 patch SHA-256 和完整 `F/B` oracle 表，并把循环边界、真实 syscall
调用、逐项比较和失败退出合成一个静态函数体契约。R2 走查只接受修订后文件
哈希，首次运行的旧报告不再作为晋级依据。

## 验收产物

- patch 精确包含 `user/user.h`、`user/usys.pl`、`kernel/syscall.h`、
  `kernel/syscall.c`、`kernel/sysproc.c`、`user/usertests.c` 六个手写文件，
  SHA-256 为 `0d83b885e999958609b453318291f13ba72a0e06a899916cb2623bfb1dea2d74`；
  `user/usys.S` 未进入 patch。
- `SYS_sysprobe=22` 唯一；generator 重放与 Make 产物逐字节一致；反汇编
  stub 为 `li a7,22`、`ecall`、`ret`。handler 只读取第 0 个 `int` 参数并
  返回该值，原 dispatch guard 保持不变。
- missing-dispatch 实例实际得到一次 `unknown sys call 22`、用户返回 `-1`、
  `FAILED`、`SOME TESTS FAILED` 和恢复后的 shell 提示符；没有 panic、重复
  trap 或 timeout。
- final 实例对 `0`、`12345`、`-7` 逐项跨 ABI 往返，精确得到
  `test sysprobe: OK` 与 `ALL TESTS PASSED`。
- `./test-xv6.py sysprobe`、`./test-xv6.py -q usertests` 和
  `./test-xv6.py usertests` 作为三个独立 driver 均通过，未出现 unknown
  诊断或失败 marker。
- `C/R` 均为 `N/A`：handler 不共享可变状态、不阻塞、不分配资源且不写
  持久化数据；这不建立跨 hart、恢复、复杂参数或用户指针结论。

## 修正与复查

R2 完整 runner 耗时 251.26 秒并通过 static、boundary、final、focused、
related、full 和 cleanup。runner SHA-256 为
`4c5dcf2ec753c25f09db67fc97cf7dc155cc3ef2e2b1a461c49a1cccab384d5d`；
走查报告 SHA-256 为
`b022f5f389fe6d127d719240c5c519bf254131e3bef98f5ffc27c21444bc2938`。

暂存检查随后发现 unified diff 的两个空白 context prefix 会被仓库级
`git diff --check` 视为尾随空格。最终资源只把这两个 hunk 改写为最小非空
context；六个目标文件的 new-blob ID 与 R2 完全相同。最终表示再次完整运行
static、boundary、final、focused、related、full 和 cleanup 并通过，报告
SHA-256 为 `7b35790d0b057987388de904a2a600c3d3916151cbe54a1df936f671f700a979`。

每个 driver 的进程组均已消失；临时导出逆向 patch 并 `make clean` 后，逐文件
内容与 mode 回到 baseline。共享 `fs.img` 前后 SHA-256 均为
`c470503efebc0f8b33d67aed363d65a03a0bd6ca1f1389ccbd276b71d2b60f1a`，
原工作树状态不变，无 QEMU、test driver 或 runner 临时目录残留。

普通与 development validator、generated navigation check、5 个 validator
单测、Python 编译、static runner 和 `git diff --check` 均通过。现有旧问题集中
没有由本实验独占的题目，因此本票未迁题，也没有创建同步副本。

## 结果

| 单元 | 结果 | 晋级依据 |
|---|---|---|
| `lab.add-system-call` | 通过 | 六文件边界、生成 provenance、missing-dispatch 安全失败、三个 `int` 往返、三层回归、资源 cleanup 与明确局限共同闭合 `S/F/B` oracle |

本记录只支持 `lab.add-system-call` 晋级为 `verified`。它不替代后续 boot、
interrupt、process、memory、concurrency 或 persistence 单元的证据。
