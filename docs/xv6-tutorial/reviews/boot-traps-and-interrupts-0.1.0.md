# Boot-traps-and-interrupts 0.1.0 非作者走查记录

- 教程版本：`0.1.0 draft`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`5e393a9a378c53a22e53fcb264bd9a49b3a265ac` 加 #8 未提交候选 diff；最终提交保留本记录与修正后的完整 diff
- 走查单元或连续路径：`core.boot-traps-and-interrupts`、迁移题 `BOUNDARY-01/02/03/04` 和隔离 `irqtrace` 实验
- 匿名入口能力：`KBOUND-C20-R2`；已通过 Foundation gate、系统观察、用户 ABI 与系统调用往返，未参与候选正文、patch 或 runner 编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、RISC-V GCC 13.3.0、Python 3.12.3；`CPUS=1`、128 MiB、私有 `fs.img`

## 观察到的卡点

初始静态 runner 把 `uservec` 与独立的 `userret` 入口错误地当成一段连续指令，
还写错了 `sscratch` 指令 token。正文首版的若干 `rg` 命令又假定 C 返回类型和
函数名在同一行，实际执行会退出 1；`_entry` 栈公式遗漏 `(hartid + 1)`，第二个
fence 和四个 trace marker 也没有绑定到真实控制位置。

走查进一步纠正了三类易形成错误模型的表述：每 hart 的 timer 在 M-mode
`start()` 中、`mret` 前设置，不在 `started` gate 之后；用户 timer 经
`usertrap -> devintr -> clockintr`，内核 timer 才经 `kerneltrap`；只有 hart 0
更新全局 ticks，而 `yield()` 是 `devintr()` 返回 2 后由 trap caller 调用。
最后一次审计发现 patch 的新 blob `index` 元数据仍指向修改前摘要。

## 验收产物

- S：从 pinned baseline 复核 `_entry -> start -> mret`、MPP/mepc/delegation/
  PMP/timer/tp、hart-0 发布与 secondary-hart fence 顺序、`prepare_return` 和
  PLIC UART/VirtIO 路由。`uservec/userret`、256 字节 `kernelvec` 帧（包括
  hart-local `tp` 不恢复）和 `swtch(ra/sp/s0-s11)` 的寄存器/栈契约均由
  exact register/offset 集合检查。
- B：控制命令 `usertests reparent` 精确得到 `OK` 和 `ALL TESTS PASSED`，且
  `IRQTRACE` marker 为零；timeout 只作 watchdog。
- F：同一 pid 在 user `sepc` 收到 cause `0x8000000000000005`，事件严格为
  `USERTRAP -> CLOCK -> YIELD -> RESUME`，ticks 恰好加一；`yield()` 两侧实际
  `p->state` 都由 patch 断言为 `RUNNING`。
- 回归：focused transcript、`./test-xv6.py -q usertests` 和完整
  `./test-xv6.py usertests` 全部通过。
- 非作者 R3 报告：`/tmp/ticket8-nonauthor-walkthrough-r3.md`，SHA-256
  `50d59b003773303e98711880300ae044cde8596fb0d9ab6ae8e680ca78fdc203`；该轮确认
  应用后 `kernel/trap.c` blob 为 `60ba7bdb9f3cc1ad1e077f7be605db8e7862316c`。
- 最终 R6 报告：`/tmp/xv6-boot-traps-report-r6.md`，SHA-256
  `44e253b88f615d90206c7ee5aaf95046e0a8c8c210ccf8108761a818f1781e06`；最终
  patch SHA-256 为 `c2f4289ba8fe5d0f7d00ef8b051cb45e84636524fd19925879c4785f0ea3c927`。

## 修正与复查

runner 将 pinned-baseline 检查与 applied-patch 检查分成两个阶段；并将
trampoline 拆成 `uservec`/`userret` 两个区段，精确比较两端的
寄存器/offset 集合和 `kernelvec` save/restore 对称性，并完整验证
`(hartid+1)*4096` 栈计算、secondary-hart `while -> fence -> per-hart init`
顺序，并把四个 marker 与 `devintr`、单 tick 检查和 `yield` 两侧绑定。patch
又在 `yield()` 前后读取真实 `p->state`；最终 `index` 更新为
`684e288..60ba7bd`，只修正 patch 元数据，不改变 R3 已运行的生成源码。

最终 R6 再次通过 baseline-static/instrumentation/focused/quick/full/cleanup；
报告加入稳定 `path:symbol` 源码图和显式 `userret -> sret` 边界。临时树在
`make clean` 后逆向 patch 并恢复，QEMU/driver 进程组消失，原工作树状态与共享
`fs.img` SHA-256
`c470503efebc0f8b33d67aed363d65a03a0bd6ca1f1389ccbd276b71d2b60f1a` 前后
一致。普通/development validator、generated navigation、5 个 validator
单测、Python 编译、JSON、patch apply 和 `git diff --check` 均通过。

## 结果

| 单元 | 结果 | 晋级依据 |
| --- | --- | --- |
| `core.boot-traps-and-interrupts` | 通过 | 启动/CSR/per-hart/vector 静态链、确定性单 hart timer 事件、真实状态断言、控制组、两层回归和完整资源清理共同闭合 S/F/B oracle |

本记录只支持启动、trap/vector、timer/device 路由边界和三类汇编契约。它不
证明多 hart happens-before、调度公平性、完整 trampoline PTE、设备 DMA 或
持久化/恢复；这些边界保留给后续单元。
