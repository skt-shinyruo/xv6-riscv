# 文件系统命名与数据路径验收 rubric

## 交付边界

学习者提交一个文件系统报告包；source/runtime worksheet 是其中一节，不是第二份产物。
`filesystem-audit.patch` 只应用于 manifest pinned baseline 的临时导出，增加 test-only
`fsaudit` snapshot/fault seam 和 `_fstrace`。它不是 production kernel 修改，也不能把 guest
marker 中的常量当作 oracle；host runner 必须解析 raw fields 并重算关系。

## Evidence dimensions

- `S`：manifest `path:symbol` anchors、`mkfs`/superblock/layout、pathname/dirent、dinode/inode、
  direct/indirect mapping、file/buffer/disk-block boundary 和 patch scope。
- `F`：`12305` byte round-trip、13 data blocks、link/open-unlinked lifecycle、two-block
  `O_TRUNC`、same-inum 3-byte rewrite。
- `B`：fd exhaustion after create、late `dirlink` rollback、second eligible `balloc` failure；
  每个触发器必须命名且只触发一次。
- `C`：N/A。本单元不评估 concurrent pathname 或 buffer-cache scalability。
- `R`：N/A。private image 是隔离设施，不是 commit、durability 或 recovery 证据。

## Raw marker contract

必须严格按以下顺序输出：

```text
FS BASE
FS RW
FS AFTER phase=RW
FS LINK
FS AFTER phase=LINK
FS TRUNC
FS AFTER phase=TRUNC
FS FDFAIL
FS AFTER phase=FDFAIL
FS LINKFAIL
FS AFTER phase=LINKFAIL
FS ALLOCFAIL
FS AFTER phase=ALLOCFAIL
FS PASS cases=6 cleanup=1
```

字段集合是闭集；缺失、重复、未知字段、非整数、阶段乱序和额外 PASS 都失败。host 至少复核：

- `BASE/AFTER`：`blocks/inodes/itable_active/itable_refs/file_objects/file_refs/buf_refs`；每个
  AFTER 逐字段等于 BASE，BASE 合法且 `buf_refs=0`；
- `RW`：generation 1、path_steps 2、`T_FILE/nlink=1`、write/read/size 12305、checksum 867834、
  13 data blocks、四个互异非零 block address、八个唯一递增 layer-event sequence，以及
  direct0 对应的 locked/valid/referenced buffer；
- `LINK`：generation 2、`nlink 1/2/1/0`、两个 path 的实际 `open()` 都返回 `-1`、仍打开的
  fd 为非负并读出五个 `hello` bytes、held block/inode ledger 分别等于 BASE+1；
- `TRUNC`：generation 3、raw `inum_fd0/inum_fd1` 均非零且相等，size `2048 -> 0 -> 3`、
  blocks `2 -> 0`、peer size 0；
- `FDFAIL`：generation 4、12 个 fixture fd 后 create `-1`，随后 path 的实际 open fd 非负、
  nlink=1、size=0，并由 raw held ledger 重算 blocks=BASE、inodes=BASE+1；
- `LINKFAIL`：generation 5、`fail_at=eligible=fired=1`、link 与 target open 都返回 `-1`、
  nlink 不变，并由 raw before/after blocks/inodes 重算 rollback；
- `ALLOCFAIL`：generation 6、`fail_at=2/eligible=2/fired=1`、write `-1`、size 保持 12288、
  新 indirect metadata block 非零、first indirect data entry 为 0，并由 raw before/after ledger
  重算 block delta=1、inode delta=0。

每个场景还必须给出 `status=0 cleanup=1`；这些字段只作为 fixture 状态，不能替代上述 host
关系检查。

## Commands and cleanup

```sh
python3 docs/xv6-tutorial/resources/filesystem/run-lab.py --self-test
python3 docs/xv6-tutorial/resources/filesystem/run-lab.py --static-only
python3 docs/xv6-tutorial/resources/filesystem/run-lab.py \
  --report /tmp/xv6-filesystem-report.md
```

runner 必须在临时导出和私有 `fs.img` 中 build/run，执行 fixture、focused filesystem tests、
quick 与 full `usertests`。结束时终止并回收整个 QEMU/process group、reverse patch、`make clean`、
删除 private image，确认导出源码 snapshot 恢复，并确认共享 worktree/index 与共享 `fs.img`
digest 不变。timeout 仅为 watchdog。

## 评定等级

- **通过**：static/self-test/runtime/focused/quick/full/cleanup 全部 exit 0；独立 host 复算 closed
  raw schema；单一报告包内含 worksheet、baseline、patch/runner SHA-256、机器附录 SHA-256、
  六个场景、七项 ledger、限制和 review disposition。
- **退回**：只有 QEMU boot、guest PASS、一次终态、timeout 或 full-test 成功；缺少 failure
  delta、逐阶段 ledger、隔离/cleanup，或者声称 cache concurrency、log commit、durability、
  crash atomicity。任何 shared checkout/image 污染或 production source 修改也退回。

晋级以 manifest 的 review record、独立 non-author walkthrough 与 publication gates 为准；
rubric 本身不构成晋级证据。
