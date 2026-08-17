# 设备中断与 VirtIO 队列 0.1.0 非作者走查记录

- 教程版本：`0.1.0`；单元状态：`verified`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`5c0ad088f45c21438ca0069b704fb643cacdf025` 加 #14 候选 diff；
  本记录、迁题和晋级元数据在走查通过后纳入同一提交，最终提交由 GitHub #14 closure comment 记录
- 走查单元或连续路径：`core.device-io`、DEVICE-01..DEVICE-11、隔离 `devtrace` fixture
- 匿名入口能力：`DEV-C21-R4`；已完成通信与 I/O 前置，未参与本单元编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、GNU Make 4.3、Python 3.12.3、
  `riscv64-linux-gnu-gcc` 13.3.0；设备 trace/quick `CPUS=2`，full `CPUS=1`

## 观察到的卡点

首轮独立审查发现 runtime marker 没有锁上下文，host 会忽略额外 `DEV` marker、重复事件序和
不一致的 PLIC hart mask，失败命令也可能遗留 process group。修订后 marker schema 成为闭集，
记录 `cons.lock`、`vdisk_lock` 与 `condition lock -> p->lock` handoff，并以 19 组自测 mutation
拒绝未知、重复、缺失、非整数、锁、hart、事件序和 cleanup 篡改。

实际 QEMU 又暴露两个仅靠静态检查看不到的问题：扩大的 user snapshot 放在 stack 上会越过
guard page，最终改为静态存储；queue 的事件虽唯一，却未约束 submit、defer、descriptor wait、
release、reclaim 的因果关系。第二轮审查补上 ID-independent 偏序并拒绝三种倒置 transcript。

最后，shared-state guard 被证明遗漏普通 untracked 文件和 index 内容。最终 runner 分开枚举
tracked/普通 untracked 与 ignored 文件，将 `git ls-files --stage` 一并纳入摘要，并在 self-test
中确认 runner 与 fixture 自身被采样。旧报告均不用于晋级；以下证据全部绑定修订后的最终哈希。

## Ownership 与偏序证据

| slice | trigger | owner / lock / channel | runtime relation | cleanup |
| --- | --- | --- | --- | --- |
| console | host 注入 `D14-input\n` | `cons.lock -> p->lock`；`&cons.r` | wait 1 < RX 2 < wake 3 < read 4；IRQ 10 claim/complete | AFTER ledger |
| disk | block 1999 cache miss | `vdisk_lock -> p->lock`；`b` | program 1 < publish 2 < notify 3；IRQ 4；sleep 5 < complete 6 < wake 8 < reclaim 9 | AFTER ledger |
| queue | blocks 1996..1998 | `vdisk_lock -> p->lock`；`&disk.free[0]` | submit 1,3；defer 2,4；wait 5 < release 6 < reclaim 7 | AFTER ledger |

disk 的 `irq_seq < sleep_seq` 是允许交错：设备可在 caller 真正睡眠前完成，但 caller 仍在
`b->disk == 1` 条件循环上正确重查。queue 则由 audit gate 延后前两笔完成，使第三请求在只剩
2 个 descriptor 时进入 SLEEPING；host 先观察 state/channel，再释放 gate，timeout 只作 watchdog。

## 完整 raw evidence

```text
devtrace console
DEV CONSOLE_WAIT generation=1 pid=4 state=2 chan=2147563640 seq=1
D14-input
DEV CONSOLE generation=1 pid=4 irq=10 wait_seq=1 rx_seq=2 wake_seq=3 done_seq=4 claims=67 completes=67 claim_harts=2 complete_harts=2 wait_chan=2147563640 wake_chan=2147563640 rx=10 first=68 last=10 read=10 ring_before=17 ring_after=27 publish_lock=1 condition_before=1 proc_after=1 condition_after=0 wake_proc_lock=1 lock_errors=0 status=0 cleanup=1
DEV AFTER phase=CONSOLE active=0 waiters=0 deferred=0 free_desc=8 info=0 buf_refs=0
$

devtrace disk
DEV DISK generation=2 pid=6 block=1999 sector=3998 submit_hart=0 head=0 d0=0 d1=1 d2=2 d0_addr=2147633744 d0_len=16 d0_flags=1 d0_next=1 d1_addr=2147609112 d1_len=1024 d1_flags=3 d1_next=2 d2_addr=2147633624 d2_len=1 d2_flags=2 queue_desc=2281017344 queue_avail=2281013248 queue_used=2281009152 queue_ready=1 free_before=8 free_programmed=5 free_after=8 avail_before=53 avail_after=54 used_before=53 used_after=54 notify=1 b_owner=1 device_status=0 wait_chan=2147609024 wake_chan=2147609024 program_seq=1 publish_seq=2 notify_seq=3 sleep_seq=5 irq_seq=4 complete_seq=6 wake_seq=8 reclaim_seq=9 irq=1 claims=1 completes=1 claim_harts=2 complete_harts=2 info_after=0 read=0 program_lock=1 publish_lock=1 notify_lock=1 complete_lock=1 reclaim_lock=1 condition_before=1 proc_after=1 condition_after=0 wake_proc_lock=1 lock_errors=0 status=0 cleanup=1
DEV AFTER phase=DISK active=0 waiters=0 deferred=0 free_desc=8 info=0 buf_refs=0
$

devtrace queue
DEV QUEUE generation=3 p0=8 p1=9 p2=10 block0=1996 block1=1997 block2=1998 free_before=8 free_held=2 free_after=8 deferred_held=2 deferred_after=0 third_pid=10 third_state=2 third_chan=2147633600 desc_chan=2147633600 avail_before=54 avail_after=57 used_before=54 used_after=57 submit0_seq=1 submit1_seq=3 defer0_seq=2 defer1_seq=4 wait_seq=5 release_seq=6 reclaim_seq=7 irq=1 claims=3 completes=3 claim_harts=1 complete_harts=1 controller_hart=1 status0=0 status1=0 status2=0 info_after=0 program_lock=1 publish_lock=1 notify_lock=1 complete_lock=1 reclaim_lock=1 condition_before=1 proc_after=1 condition_after=0 wake_proc_lock=1 lock_errors=0 cleanup=1
DEV AFTER phase=QUEUE active=0 waiters=0 deferred=0 free_desc=8 info=0 buf_refs=0
DEV PASS cases=3 cleanup=1
$
```

host 独立复算 descriptor 0/1/2 的 flags 为 1/3/2、length 为 16/1024/1、next 为
1/2；三张 queue 页对齐且互异。单请求 free bitmap 为 `8 -> 5 -> 8`，avail/used 各 `+1`；
容量场景为 `8 -> 2 -> 8`，avail/used 各 `+3`。claim/complete 次数与 hart mask 分别相等，
每个 lock/handoff 字段为 1 且 `lock_errors=0`。

## 验收产物

- fixture：`resources/device-io/device-audit.patch`，SHA-256
  `22cccd302aed3888477d57608cfaa104445e635965489bb4b20adde41877a24b`
- runner：`resources/device-io/run-lab.py`，SHA-256
  `4508765b8f82c78a5d321bc03749434985fb8a8165be36ca9ae0a188015ab472`
- 最终非作者机器附录：`/tmp/ticket14-nonauthor.md`，SHA-256
  `9c2d58be42fd5c023d41125c0eae7e912b55992e448dcdbc2f2daf758673e84a`
- shared repo/index/content digest：
  `8132c63d40a580bf4fdf16d6209c0e92bd8dd073d1a1d931f8d0526a0a2f8cd9`

| 层级 | 命令 / 配置 | 结果 |
| --- | --- | --- |
| static | `run-lab.py --static-only` | 15-path apply/build/reverse/snapshot PASS |
| focused | `pipe1/writebig/bigfile/manywrites`，CPUS=1 | 每项唯一 `ALL TESTS PASSED`，无 DEV marker 泄漏 |
| quick | `test-xv6.py -q usertests`，CPUS=2 | PASS，SHA-256 `30fa6b08...` |
| full | `test-xv6.py usertests`，CPUS=1 | PASS，SHA-256 `fb8ecb4d...` |
| publication | normal/development validator、navigation、5 tests、Python/JSON/diff check | PASS |

focused transcript SHA-256 依次为 `6e6e176f...`、`eaff7910...`、`7520cde1...`、
`fefed0a8...`；完整值保存在最终机器附录中。另以 12 个针对最终动态 transcript 的独立
mutation 复核因果倒置、重复 seq、hart/lock/cleanup 篡改均被拒绝。

## 修正与复查

临时导出在 `make clean` 后逆向 patch，before/after source snapshot 一致；所有 QEMU/driver
process group 消失，共享 `fs.img` 前后均为 `missing`。每个 AFTER 都恢复
`active=0 waiters=0 deferred=0 free_desc=8 info=0 buf_refs=0`；cache 可保留 valid data，
但没有被持有的 buffer ref。

S/F/B/C 已覆盖源码 ownership、正常 console/disk 流、descriptor 容量边界和 CPUS=2 命名
事件偏序。C 不证明所有 interleaving、设备固件实现、跨设备 DMA memory model 或 remote
ordering；R 为 N/A，不证明写回、host persistence 或 crash recovery。`kernel/bio.c` 与
`kernel/buf.h` 的完整 cache/LRU ownership 留给 `core.persistence`，本单元只作 secondary 使用。

旧 `FS-10` 与 `SYNC-02` 在本记录、review metadata 和 verified 状态同一变更中收缩为
DEVICE-10/DEVICE-08 兼容入口；正文不保留第二份同步副本。

## 结果

| 单元 | 结果 | 晋级依据 |
| --- | --- | --- |
| `core.device-io` | verified | PLIC/UART/VirtIO ownership、raw lock/channel/hart/seq、容量边界、回归与隔离清理均由非作者复核 |

学习者签名：`DEV-C21-R4`。

非作者 reviewer 签名：`DEV-NONAUTHOR-R2`。
