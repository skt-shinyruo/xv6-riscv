# 文件系统命名、inode 与数据路径 0.1.0 非作者走查记录

- 教程版本：`0.1.0`；单元状态：`verified`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`2d84235059b284a51e2c418fb911f1be54e54992` 加 #15 候选 diff；
  本记录、迁题和晋级元数据在走查通过后纳入同一提交，最终提交由 GitHub #15 closure comment 记录
- 走查单元或连续路径：`core.filesystem`、FILESYS-01..FILESYS-14、隔离 `fstrace` fixture
- 匿名入口能力：`FS-C22-R4`；已完成 device-I/O 前置，未参与本单元编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、GNU Make 4.3、Python 3.12.3、
  `riscv64-linux-gnu-gcc` 13.3.0；fixture/focused/full `CPUS=1`，quick `CPUS=2`

## 观察到的卡点

首轮走查发现 zero-context nested patch 会把 Makefile 项应用到文件末尾；最终 fixture 改用
普通上下文，并以 apply 后编译而不只是 `git apply --check` 验证位置。随后 raw oracle 从
guest 汇总值收紧为实际 path open fd、held block/inode ledger、failpoint eligible/fired 和
before/after counts；repository guard 也在 apply 前验证 manifest anchors，防止 fixture 自己
补出缺失的 baseline symbol。

独立 Spec 复核又发现两个发布 blocker：答案曾把 host 计算的 delta 写成 raw 字段，TRUNC
只输出一个 inode 和 guest 归约的 `same_inum`。最终 schema 输出 raw
`inum_fd0/inum_fd1`，host 要求两者非零且相等；ALLOCFAIL 输出 raw before/after block/inode
账本并由 host 计算 delta。修订后的 self-test 从 22 增至 23 个 mutation，新增 inode identity
篡改拒绝。两个独立 reviewer 都在最终哈希上重跑 publication/static 门，确认无剩余内容或
oracle finding；此前 non-author 全量运行只作为修正来源，不替代最终哈希的机器附录。

## Source worksheet

| slice | `path:symbol` | owner / state | 可复核关系 | 边界 |
| --- | --- | --- | --- | --- |
| image | `Makefile:fs.img`、`mkfs/mkfs.c:main` | host builder 拥有初始 image | block 1 superblock、ROOTINO、dirent、bitmap | 不替代 runtime allocator |
| lookup | `kernel/fs.c:namex/dirlookup` | caller 持有当前 inode ref | lock current、取得 next ref、`iunlockput` current | last parent ref 由 caller 释放 |
| inode | `kernel/fs.c:iget/ilock/iput` | itable identity 与 inode lock 分离 | `nlink`、inode ref、file ref 分层 | `NINODE=50` 不等于 `NINODES=200` |
| data | `kernel/fs.c:writei/bmap/readi` | inode mapping 拥有 data/indirect block | 12305 bytes = 13 data + 1 indirect metadata | `bread/log_write` 是后续单元边界 |
| namespace | `kernel/sysfile.c:sys_link/sys_unlink/create/sys_open` | dirent、nlink、open ref 分层 | unlink-open、O_TRUNC、late failure 副作用 | 不声称 crash atomicity |

## 最终 raw evidence

```text
FS BASE blocks=965 inodes=23 itable_active=2 itable_refs=4 file_objects=1 file_refs=9 buf_refs=0
FS RW generation=1 path_steps=2 inum=25 type=2 nlink=1 size=12305 write=12305 read=12305 checksum=867834 direct0=966 direct11=977 indirect=978 indirect0=979 data_blocks=13 open_seq=1 file_write_seq=2 inode_write_seq=3 direct_seq=4 buffer_seq=5 indirect_seq=6 file_read_seq=7 inode_read_seq=8 buffer_block=966 buffer_valid=1 buffer_ref=2 buffer_locked=1 status=0 cleanup=1
FS AFTER phase=RW blocks=965 inodes=23 itable_active=2 itable_refs=4 file_objects=1 file_refs=9 buf_refs=0
FS LINK generation=2 inum=24 nlink0=1 nlink1=2 nlink2=1 nlink3=0 path_old=-1 path_alias=-1 read=5 first=104 last=111 open_fd=3 blocks_held=966 inodes_held=24 status=0 cleanup=1
FS AFTER phase=LINK blocks=965 inodes=23 itable_active=2 itable_refs=4 file_objects=1 file_refs=9 buf_refs=0
FS TRUNC generation=3 inum_fd0=24 inum_fd1=24 before_size=2048 before_blocks=2 after_size=0 after_blocks=0 peer_size=0 rewrite=3 final_size=3 status=0 cleanup=1
FS AFTER phase=TRUNC blocks=965 inodes=23 itable_active=2 itable_refs=4 file_objects=1 file_refs=9 buf_refs=0
FS FDFAIL generation=4 filled=12 create=-1 check_fd=15 nlink=1 size=0 blocks_held=965 inodes_held=24 status=0 cleanup=1
FS AFTER phase=FDFAIL blocks=965 inodes=23 itable_active=2 itable_refs=4 file_objects=1 file_refs=9 buf_refs=0
FS LINKFAIL generation=5 fail_at=1 eligible=1 fired=1 link=-1 target=-1 nlink_before=1 nlink_after=1 blocks_before=965 blocks_after=965 inodes_before=24 inodes_after=24 status=0 cleanup=1
FS AFTER phase=LINKFAIL blocks=965 inodes=23 itable_active=2 itable_refs=4 file_objects=1 file_refs=9 buf_refs=0
FS ALLOCFAIL generation=6 fail_at=2 eligible=2 fired=1 write=-1 size_before=12288 size_after=12288 indirect_before=0 indirect_after=977 indirect_data=0 blocks_before=977 blocks_after=978 inodes_before=24 inodes_after=24 status=0 cleanup=1
FS AFTER phase=ALLOCFAIL blocks=965 inodes=23 itable_active=2 itable_refs=4 file_objects=1 file_refs=9 buf_refs=0
FS PASS cases=6 cleanup=1
```

host 独立复算 12305-byte checksum、13 个 data blocks、四个互异 block address、八层事件
顺序、link `1 -> 2 -> 1 -> 0`、两个 TRUNC inode identity、fd exhaustion 的 namespace
副作用、dirlink rollback 和 empty-indirect `block delta=1`。六个 AFTER 的七项 ledger 都
逐字段等于 BASE，不能由 guest 的 `status=0` 或最终 PASS 代替。

## 验收产物

- fixture：`resources/filesystem/filesystem-audit.patch`，SHA-256
  `263397859abc8e1a1bc4c64bddd81edbcf9cc5e0032435eb1467aef417f3a349`
- runner：`resources/filesystem/run-lab.py`，SHA-256
  `e26b5d41945bdde68eef08d1645074e4e3cf54d743370f9f8681152e5abffaca`
- 最终机器附录：`/tmp/ticket15-author-final-r3.md`，SHA-256
  `023be323baee2febc5499ba718c27fb2c499315c3157ef300dc58c0c4ac2872a`
- 修正前 non-author 全量附录：`/tmp/ticket15-nonauthor-final.md`，SHA-256
  `3e4fbca8083e421373bc9041c682a1414708e99d624050e6de4796c6a239333f`；
  它用于暴露修正点，不作为最终 fixture/runner 哈希证据

| 层级 | 命令 / 配置 | 结果 |
| --- | --- | --- |
| static | `run-lab.py --static-only` | 15-path apply/build/reverse/snapshot PASS；23 mutations rejected |
| focused | `unlinkread/linktest/createdelete/subdir/dirfile/iref/writebig/bigfile` | 每项唯一 `ALL TESTS PASSED`，无 FS marker 泄漏 |
| quick | `test-xv6.py -q usertests`，CPUS=2 | PASS，SHA-256 `4e162e28...` |
| full | `test-xv6.py usertests`，CPUS=1 | PASS，SHA-256 `2c127846...` |
| publication | normal/development validator、navigation、5 tests、JSON/diff check | PASS |

## 修正与复查

临时导出在 `make clean` 后逆向 patch，source snapshot 精确回到 baseline；QEMU/driver process
group 消失，共享 `fs.img` 前后均为 `missing`，共享 worktree/index/content digest 未变。
S/F/B 覆盖源码 ownership、正常 mapping/lifecycle 和三个命名 failure；C/R 为 N/A。本记录
不证明 arbitrary pathname interleaving、buffer eviction、log commit、host durability、tear 或
recovery；这些黑盒由 `core.persistence` 接手。

旧 `FS-01/02/03/08` 与 `SYNC-06` 在本记录、review metadata 和 verified 状态同一变更中
收缩为 FILESYS-06/08/09/11 兼容入口；正文不保留同步副本。

## 结果

| 单元 | 结果 | 晋级依据 |
| --- | --- | --- |
| `core.filesystem` | verified | image/namespace/inode/data anchors、closed raw oracle、完整资源账本、回归与隔离清理由非作者复核 |

学习者签名：`FS-C22-R4`。

非作者 reviewer 签名：`FS-NONAUTHOR-R2`。
