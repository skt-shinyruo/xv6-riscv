# 设备 I/O 学习单元验收 rubric

## 交付边界

学习者提交一个设备 I/O 报告包，源码/runtime worksheet 作为其中一节，不是第二份产物。
`device-audit.patch` 是只应用于 pinned
baseline 临时导出的 test-only fixture：它增加一个 `devprobe` audit seam、raw snapshot、
console/DMA 事件记录和可释放的 completion gate；它不是要求学习者照抄的生产 driver 实现。
实现可以改变内部命名，但必须保留 runner 读取的 guest command 与 raw marker contract；不可把
marker 常量当作 oracle，host 会逐字段解析并重算关系。

## Evidence dimensions

- `S`：15 个 patch path、manifest `path:symbol` anchors、PLIC/UART/console/VirtIO source trace、
  descriptor flags/length、fence/notify 与 cleanup scope。
- `F`：console 10-byte 外部输入、单个只读 block read、caller status=0、IRQ claim/complete。
- `B`：两 deferred completion + free=2 时第三请求 `SLEEPING`；释放后三请求完整回收。
- `C`：CPUS=2、submit/claim/complete/controller hart mask、gate 前后事件 seq；不可用 timeout
  或随机 sleep 代替。
- `R`：N/A。不得把私有 fs.img 读操作写成 host persistence 或 crash recovery 证据。

## Raw marker contract

必须按顺序输出 `DEV CONSOLE_WAIT`、`DEV CONSOLE`、`DEV AFTER phase=CONSOLE`；disk 输出
`DEV DISK` 与对应 AFTER；queue 输出 `DEV QUEUE`、对应 AFTER、`DEV PASS cases=3 cleanup=1`。
字段集合是闭集：缺失、重复、未知字段、非整数和额外 PASS 都失败。host 至少复核：

- console：state=2、wait/wake channel 相等、rx/read=10、first=68、last=10、ring delta=10、
  IRQ10 claims=completes、claim/complete hart mask 相同、seq order，以及 publish/handoff/wake
  lock context；
- disk：三 index 互异且在 0..7、next 链、16/1024/1 lengths、flags 1/3/2、queue addresses
  page-aligned、free 8->5->8、avail/used +1、b owner/status、same channel；program->publish
  ->notify，notify 同时先于 sleep 与 IRQ->complete，sleep 和 complete 都先于 wake->reclaim；
  program/publish/notify/complete/reclaim 的 vdisk lock 与 sleep/wake 的 proc-lock handoff 都成立；
- queue：three positive distinct pids、blocks 1996..1998、free=8/2/8、deferred=2/0、第三 pid
  state=2 且 `third_chan=desc_chan`、avail/used +3、release after all pre-release seq、
  statuses/info/cleanup zero，seq 唯一且 lock ledger zero-error；
- every AFTER：`active=0 waiters=0 deferred=0 free_desc=8 info=0 buf_refs=0`；valid cache
  data 可以保留，但所有 buffer refcnt 必须归零。

## Commands and cleanup

```sh
python3 docs/xv6-tutorial/resources/device-io/run-lab.py --self-test
python3 docs/xv6-tutorial/resources/device-io/run-lab.py \
  --report /tmp/xv6-device-io-report.md
```

runner 必须在临时导出中执行 `make kernel/kernel user/_devtrace`、私有 `fs.img`、设备 trace、
focused `pipe1/writebig/bigfile/manywrites`、quick `CPUS=2` 和 full `CPUS=1` usertests；所有
QEMU/driver 处于可回收 process group。结束时 reverse patch、`make clean`、源码 snapshot 恢复，
共享 worktree/index 和 `fs.img` digest 不变。

## 评定等级

- **通过**：runner static/F/B/C/focused/quick/full/cleanup 全部 exit=0；独立 host 复算 raw marker；
  单一报告包内嵌 worksheet，含 baseline、patch/runner SHA-256、机器附录的外部 SHA-256、命令/
  状态、资源 ledger、限制和 non-author 记录。
- **退回**：只有 `ALL TESTS PASSED`、一次终态、timeout、guest 自报布尔值、未观察 sleeping/gate、
  或没有清理/共享状态保护。任何生产 kernel/user 修改、跨目录实现参考链接或未声明的 DMA/
  persistence 结论也退回。
