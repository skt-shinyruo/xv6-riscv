# Communication and I/O 0.1.0 非作者走查记录

- 教程版本：`0.1.0`；单元状态：`verified`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`3128b81333ebb6fc2cea5e704d144efe675fc61a` 加 #13 候选 diff；
  本记录与晋级元数据在走查通过后纳入同一提交，最终提交由 GitHub #13 closure comment 记录
- 走查单元或连续路径：`core.communication-and-io`、IO-00..IO-08、隔离 `ioflow` fixture
- 匿名入口能力：`IO-C20-R4`；已完成进程、调度、虚拟内存和 COW 前置，未参与本单元编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、GNU Make 4.3、Python 3.12.3、
  `riscv64-linux-gnu-gcc` 13.3.0；ioflow/focused/full `CPUS=1`，quick `CPUS=2`

## 观察到的卡点

首轮审查发现旧 fixture 没有真正触发第 513 字节 writer sleep、丢弃 killed child 的
wait status、遗漏 parent endpoint close，而且只跑两项 filtered regression。源码复核还
纠正了 shell left child 关闭 fd 1、普通 inode read 不持有 log reservation，以及
`argfd` 与 `fdalloc` 的职责边界。修订后的 parent 先用 `iosnapshot` 观察命名 child 的
state/channel/occupancy，再执行 read 或 kill；每个 child status、endpoint 和阶段账本都
进入 host oracle，并增加两次 fixture、6 个 focused、quick 和 full regression。

走查中还捕获了一个只在实际应用后出现的问题：zero-context nested diff 虽通过
`git apply --check`，却把 syscall declaration/dispatch 落到 `kernel/syscall.c` 末尾。
最终资源改回普通上下文 patch，并由 apply 后编译与完整 QEMU 重跑验收。该 literal nested
diff 的外层 whitespace 解释仅对这一文件通过 `.gitattributes` 关闭；nested 内容仍用
`git apply --whitespace=error-all`、reverse 和源码 snapshot 严格检查。

初版 `DEVICE` marker 又把 `console=1/virtio=1` 写成不可观察常量。最终动态结论收窄为
README 的准确 16-byte inode read 与资源恢复；console/UART/PLIC/VirtIO 在本单元只建立
源码 ownership 边界，cache miss/device completion 的运行时 trace 交给后续设备单元。

## Ownership 与 pipeline 证据

| 阶段 | ownership / source edge | runtime observable | 清理责任 |
|---|---|---|---|
| descriptor | `p->ofile[fd] -> struct file` | BASE `fd=3`；pipeline left/right dup 到 1/0 | child/parent 关闭原 endpoint |
| file/pipe | `filedup/fileclose -> pipeclose` | BASE `files=1 refs=9 pipes=0`；每阶段 after 相等 | 最后两端关闭才释放 pipe page |
| empty/full | `piperead/pipewrite -> sleep` | `state=2`，channel=READ/WRITE，occupancy=0/512 | wake 后重查谓词并 wait child |
| EOF/broken | `pipeclose` 改 open flags | empty read=0；无 reader write=-1 | 相反 endpoint close/wakeup |
| killed waiters | `kkill -> wakeup -> pipe loop` | read/write waiter 均先 SLEEPING，再 status=-1 | parent 按 pid/status wait；drain committed 512 |
| capacity rollback | `fdalloc/pipealloc` failure | 13 个 dup 填满 NOFILE，`pipe=-1` | fd/file/pipe/page ledger 不变 |
| inode/device boundary | `fileread -> readi -> bread` | README `read=16` | close fd；cache/device completion 不作动态主张 |

一轮代表性 raw marker 为：

```text
IO BASE fd=3 files=1 refs=9 pipes=0 procs=3 free=32530
IO PIPELINE left=4 right=5 left_fd=1 right_fd=0 bytes=641 eof=0 cleanup=1
IO EMPTY child=6 state=2 chan=1 occupancy=0 wake=1 cleanup=1
IO FULL child=7 state=2 chan=2 occupancy=512 wake=1 cleanup=1
IO EOF occupancy=0 read=0 cleanup=1
IO BROKEN occupancy=0 write=-1 cleanup=1
IO KILL_READ child=8 state=2 chan=1 result=-1 cleanup=1
IO KILL_WRITE child=9 state=2 chan=2 committed=512 result=-1 cleanup=1
IO FD_ROLLBACK filled=13 pipe=-1 cleanup=1
IO DEVICE read=16 cleanup=1
IO PASS cleanup=1
```

BASE、PIPELINE、EMPTY、FULL、EOF、BROKEN、KILL_READ、KILL_WRITE、FD_ROLLBACK、
DEVICE 和 PASS 各有一个 `IO AFTER`；host 逐字段确认全部恢复为
`fd=3 files=1 refs=9 pipes=0 procs=3 free=32530`。第二次运行使用不同 pid，但得到
相同关系和账本。

## 验收产物

- fixture：`resources/communication-and-io/communication.patch`，SHA-256
  `741ac9f2f2de62952b715613c5b37b24a40a12081d0bd5a56b355a6ed79e64f5`
- runner：`resources/communication-and-io/run-lab.py`，SHA-256
  `dbd8062296aa5d0f946a7c7b370ddb9aa92cd74e56c125c2eb4bf522f81cdce3`
- 主机器报告：`/tmp/ticket13-final-candidate.md`，SHA-256
  `df1ce6558559edadcec919e2096f17eb235b678ec90c41d51bb91249d13e1d3b`
- 非作者报告：`/tmp/ticket13-nonauthor-final.md`，SHA-256
  `dfc31c93f3e5fbbae2179a1d6d2dfc1389c44e4475e674c6a60da01823f0d24a`

| 层级 | 命令 / 配置 | 结果 |
|---|---|---|
| static | `run-lab.py --static-only` | 13-path apply/build/reverse/snapshot PASS |
| focused | `pipe1/preempt/killstatus/sharedfd/copyout/exectest`，CPUS=1 | 每项唯一 `ALL TESTS PASSED`，无 IO marker 泄漏 |
| quick | `test-xv6.py -q usertests`，CPUS=2 | PASS |
| full | `test-xv6.py usertests`，CPUS=1 | PASS |
| publication | normal/development validator、navigation、5 tests、Python/JSON/diff check | PASS |

主报告 focused transcript SHA-256 依次为 `6e6e176f...`、`18c9e066...`、
`83264e94...`、`e49cf4cc...`、`d9a663e0...`、`ae6ef55b...`；quick/full 为
`d69ecad2...`/`e8720036...`。完整值保存在机器报告中。

## 修正与复查

临时导出在 `make clean` 后逆向 patch，before/after source snapshot 一致；所有 QEMU/
driver process group 消失，共享 `fs.img` 前后均为 `missing`。所有 child 已 wait，pipe
endpoint 已关闭，第二次 fixture 的 BASE 与第一次一致。

S/F/B/C 已覆盖源码 ownership、正常流、边界/失败和受控 waiter 顺序。C 只证明这些命名
事件与 CPUS=2 回归，不证明公平性、所有 interleaving 或 multi-hart ordering；R 为 N/A。
本单元不证明 cache miss、外部 console input、UART/PLIC/VirtIO completion、DMA ordering
或 crash recovery。旧 `docs/questions/filesystem-and-storage.md` 是后续持久化单元拥有的混合
问题组，因此本次不迁移或复制；新 IO 问题由本 verified 单元独立拥有。

## 结果

| 单元 | 结果 | 晋级依据 |
|---|---|---|
| `core.communication-and-io` | verified | pipeline ownership、full/empty/closed/killed oracle、raw ledger、回归与隔离清理均由非作者复核 |

学习者签名：`IO-C20-R4`。

非作者 reviewer 签名：`IO-NONAUTHOR-R1`。
