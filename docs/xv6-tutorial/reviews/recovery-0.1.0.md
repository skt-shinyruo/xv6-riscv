# 崩溃恢复与离线一致性 0.1.0 非作者走查记录

- 教程版本：`0.1.0`；单元状态：`verified`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`c4302cf6d8dd9f8de96425857066a3adda1a2251` 加 #17 候选 diff；
  本记录、迁题和晋级元数据在走查通过后纳入同一提交，最终提交由 GitHub #17 closure comment 记录
- 走查单元或连续路径：`project.crash-recovery`、RECOVERY-00..RECOVERY-11、隔离
  `recoverycase` fixture、只读 `fsck.py`
- 匿名入口能力：`RECOVERY-C23-R1`；已完成 `core.persistence`，能阅读 log、inode、bitmap 和 QEMU
  启动路径，未参与本单元编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、Python 3.12.3、
  `riscv64-linux-gnu-gcc` 13.3.0；crash/focused/full `CPUS=1`，quick `CPUS=2`

## 观察到的卡点

首轮走查发现机器附录只保留一个 recovery image digest，没有 second-stop digest、crash target、
payload、fsck JSON 或 synthetic mutation 的输入摘要。runner 也只按状态接受 `home-half` 和
`header-restore`，没有显式复核 cleared header 与 offline clean。最终报告逐 case 保留 crash、first、
second image SHA-256、marker、header/target/payload/home relation 和 fsck 摘要；tear 记录 source、
mutated、first/second digest、精确 offset/length/value，并在 boot 后显式调用 offline oracle。

offline checker 的 mutation audit 又发现普通文件 logical hole、EOF 后 trailing block 和目录缺失 `.`
会被接受。修订后所有 allocated inode 都检查 size 所需 block 与尾块，目录必须恰有正确的 `.` 和
`..`；metadata bitmap、duplicate owner、reachability、nlink 与 orphan 同样进入负向矩阵。

最终失败路径审查发现 initial replay marker 解析和 `pidfd_open()` 位于 cleanup 边界外，异常时可泄漏
QEMU；`pidfd_send_signal()` 的一般 `OSError` 也没有 fallback。修订后进程从 `Popen` 起由单一生命周期
管理，三种注入失败分别通过 exact-pid kill 或 process-group cleanup，且均无 QEMU 残留。

## 验收产物

- fixture：`resources/recovery/recovery-audit.patch`，SHA-256
  `82ba3e78ac813e5e521e132a94390cb55abed4aa338b5781efb843b7f587ae2a`
- scenario：`resources/recovery/scenarios.json`，SHA-256
  `d6bdf3e61f66018c8c03dfed12b6ab31523d9f3cc279d2950fb1eb84237e5d24`
- offline oracle：`resources/recovery/fsck.py`，SHA-256
  `0a1289ea72ab098a58878c61f9ae99043fab97223adfa3630a7c1bb9812782f1`
- runner：`resources/recovery/run-project.py`，SHA-256
  `39d9654dcfc033cccfc24fb6cce615d4aa68f26f250dbf81c01c0e24748085a0`
- 仓库外 candidate：`/tmp/xv6-recovery-candidate.patch`，SHA-256
  `5fcbe25c6d6bc5e4c5460ea59aaf5d49de3b75ce647189653e07ab26ff972b1a`
- author 机器附录：`/tmp/xv6-recovery-report-final-v3.md`，SHA-256
  `eb4edf2aa1eb15ec5f613f1e0ba2479853ef97f94c7bcbf669d7c06f6de1ecff`
- non-author 机器附录：`/tmp/xv6-recovery-report-nonauthor-full-v2.md`，SHA-256
  `81cf70f0d9b6381e2702d829d1b4c9e63214d4db6d8dda7dd24f16c419792c4e`

| 层级 | 命令 / 配置 | 结果 |
| --- | --- | --- |
| self-test | `run-project.py --self-test` | 2 个 accepted relation；15 个 mutation 精确拒绝 |
| static | `run-project.py --static-only --candidate ...` | source/scope/build/reverse/offline PASS |
| recovery | candidate + 四个 stable point | 10 -> before；20/30/40 -> after；first/second boot PASS |
| synthetic | 六个声明式 tear profiles | rollback/mixed rejection、redo/idempotence、非法 log 诊断 PASS |
| focused | 四个 filesystem usertests + `logstress f0` | PASS；无未 arm marker 泄漏 |
| quick/full | `test-xv6.py`，CPUS=2/1 | 两层均 PASS |
| publication | validator、navigation、5 tests、JSON/diff check | PASS |

## 独立复算

non-author 从 raw JSON 独立复核 point 10/20 的 header、target、replay、offline 与 first/second digest；
四个 point 的 first-stop 与 second-stop SHA-256 均相等。`payload-half` 的 mutated image 被判为
`mixed`，不能作为 postcommit；`header-restore` 首启 replay 2 blocks、二启 replay 0 blocks，最终 digest
相等且 header cleared。bitmap missing 返回 `E_BITMAP_UNMARKED:block=48`，orphan 返回
`E_ORPHAN:inum=2`。这些关系由报告中的输入 digest 和 diagnostic 支持，不依赖同步答案副本。

QEMU marker 只支持 guest logical order；pidfd kill 后重新打开的 image 支持 host-visible bytes；六种
synthetic tears 只支持列出的 byte mutations。三层都不能推出 physical power-loss durability、sector
atomicity、故障概率或未枚举 tear space。`fsck.py` 是 publication acceptance oracle，不是 learner
candidate、repair tool 或 production fsck。

## 修正与复查

每个 crash point 使用独立 image；first/second boot 后 header 为 0、home non-mixed、offline clean，
第二次启动 `seen_n=0` 且 digest 不变。临时 baseline export、private images、QEMU process group 和 pidfd
均回收；共享 `fs.img` 前后为 `missing`，共享 checkout/index digest 在稳定运行窗口内不变。

旧 `FS-09` 在本记录、review metadata 和 `verified` 状态同一变更中收缩为 RECOVERY-09 兼容入口；
正文不保留同步副本。

## 结果

| 单元 | 结果 | 晋级依据 |
| --- | --- | --- |
| `project.crash-recovery` | verified | stable crash ABI、before/after、first/second boot、synthetic/offline、回归与 cleanup 均由非作者复核 |

学习者签名：`RECOVERY-C23-R1`。

非作者 reviewer 签名：`RECOVERY-NONAUTHOR-R1`。
