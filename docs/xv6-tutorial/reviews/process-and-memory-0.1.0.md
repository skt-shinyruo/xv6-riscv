# Process-and-memory 0.1.0 非作者走查记录

- 教程版本：`0.1.0 draft`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`5c8ff63e666dd02754af2f64228cb3f6ca75dd3d` 加 #9 未提交候选 diff；最终提交保留本记录与修正后的完整 diff
- 走查单元或连续路径：`core.process-and-memory`、迁移题 `LIFE-00..12` 和隔离 `lifecycle` 资源账本实验
- 匿名入口能力：`PROC-C20-R3`；已通过 Foundation gate、系统观察、用户 ABI、系统调用、boot/trap/interrupt 单元，未参与候选正文、patch 或 runner 编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、GNU Make 4.3、Python 3.12.3；`CPUS=1`、128 MiB、私有 `fs.img`

## 观察到的卡点

首版 runner 对 phase 顺序和容量 file refs 只作宽松判断，QEMU 若在构造函数内
启动失败还可能漏掉进程组；related regressions 也没有执行报告所声称的零
`LIFE` marker 检查。最初的失败 exec 使用 README，在创建临时页表之前就因
ELF magic 被拒绝，不能支持“部分新映像 rollback”的结论。

题集首版又把正常 shell child 与 orphan 都写成由 `/init` 回收，并把
`wait(0)` 误写成 status 交付。它缺少独立的 orphan、NPROC capacity 和全流程
综合问题，对 fork parent 返回值、`sys_exec` argv 预拷贝、`kill(0)` 后果的源码
入口也不完整。

## 验收产物

- S：pinned baseline 上验证完整状态枚举、`userinit -> forkret -> fsinit ->
  kexec("/init")`、fork 发布、exec prepare/commit/bad、exit/wait/reparent/kill
  顺序和 13-path patch scope。`MAXARG=32`、`USERSTACK=1`、`PGSIZE=4096` 与
  `30*300+9` bytes argv 共同证明失败 fixture 越过 4 KiB 新用户栈容量。
- F：同一 pid 在 `EXEC_READY -> EXEC_AFTER` 保留 parent、trapframe、cwd 和
  inherited fd，同时替换 name、pagetable、`sz` 和用户入口；status=37 由同一
  child 交付，回收后五项账本回 BASE。
- B：后期失败 exec 保留旧 pid/`sbrk(0)`/fd 并精确恢复账本；非法 wait 地址
  保留已关闭 fd 的 zombie；blocked target 先观测为 `SLEEPING`，kill 后得到
  status=-1；live orphan 先显示 parent=init，再由 init wait 回收。
- capacity：baseline used=3，创建 61 个 child 后 `used=64` 且仍有 31673 个
  free pages；active files=`1+2=3`、refs=`9+2+61*4=255`、parent links=`2+61=63`，
  `CAPACITY_REAP` 与 FINAL 全部恢复为 BASE。
- regressions：`exitwait/reparent/killstatus` focused、`exectest/reparent2/forktest`
  related、quick 和完整 `usertests` 均精确得到一次 `ALL TESTS PASSED`，控制与
  回归输出均无 `LIFE` marker。
- 非作者最终报告：`/tmp/ticket9-nonauthor-final.md`，SHA-256
  `d2d11cac2219f82480dfe6e00ea71f0e8854bd104875f164c1b1dee8566c38cc`；该轮执行的
  patch SHA-256 为 `632f0b5028acaabda4a51af204f1b7b9ffa38f2a2da0ce61be9d0cb56ff2f62c`，
  runner SHA-256 为 `dc9a46c9d37aae23cdad7a85d6e4c6c7829488f41c9f99ffef13fd80ac308bec`。

## 修正与复查

runner 将完整 phase 列表改为精确序列，并对 EXEC/KILL/ORPHAN/CAPACITY 每段
reap 和 FINAL 的 slots/pages/file objects/file refs/parent links 逐项比较 BASE。
capacity refs 使用精确等式；所有 QEMU/driver 都由独立 process group 管理，
构造失败路径也自清理并确认整个组消失。失败 exec 改用已在上一 phase 成功运行
的 `lifeexec` ELF 和超长 argv；静态门同时检查 `sys_exec` 预拷贝/释放与
`kexec` rollback，而动态门检查旧身份、fd 和账本恢复。

题集把 shell 回收和 init orphan 回收分为两条路径，补齐源码地标、半初始化
状态、parent 返回值、orphan、capacity 和整份报告综合题。最终本地 R3 与非作者
R3 都通过 static/focused/related/quick/full/cleanup。普通与 development
validator、generated navigation、5 个 validator 单测、Python 编译、JSON、
patch apply 和 `git diff --check` 均通过；成功运行创建的临时导出、私有镜像与
进程组均已清理，共享 `fs.img` 前后都不存在。

暂存检查随后发现 patch 文件自身的 inner-diff 空白 context 会触发外层
`git diff --cached --check`。最终资源机械重生成为 `--unified=0` 表示，13 个
目标文件的 applied blob 与 R3 完全相同；最终 patch SHA-256 为
`616a5180d328c3c94812ce10ce164692367d751a50ee8cbc8cafa24599dce42d`。该最终
表示又完整通过 R4 的 static/focused/related/quick/full/cleanup，报告
`/tmp/xv6-process-lifecycle-r4.md` SHA-256 为
`1a9bfd2bb6f22ce6636349795f4be02606f1c563d9b2f5f37eccfad23838fb62`。

## 结果

| 单元 | 结果 | 晋级依据 |
| --- | --- | --- |
| `core.process-and-memory` | 通过 | 显式状态/ownership、fork-exec-wait 身份与 status、late rollback、blocked kill、orphan/init reclaim、NPROC 容量和精确资源恢复共同闭合 `S/F/B` oracle |

本记录不支持 scheduler 锁交接、lost-wakeup happens-before、多 hart migration、
PTE/permission/TLB 或持久化恢复结论；它们分别属于 #10、#11 和后续单元。
