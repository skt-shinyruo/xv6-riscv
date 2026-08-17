# 通信与设备 I/O rubric

`communication.patch` 是 pinned baseline 临时导出的 publication/non-author fixture。它只
提供 `_ioflow`、只读 `iosnapshot(fd,pid,addr)` 观察 seam 和 host runner，不是生产修复或
学习者答案。学习者的报告必须解释源代码 ownership，并逐字段保存 guest 原始 marker；不能
把 `IO PASS` 或超时本身当成证明。

| 维度 | 通过要求 | 失败示例 |
|---|---|---|
| ownership | 分开 descriptor/file/pipe/proc/device queue，写出 ref、endpoint 和 close 规则 | 把 `dup` 当成复制 `struct file` object |
| pipeline | 逐 child 记录 fork/dup/close/exec/read/write/wait，说明 EOF/broken-end 依赖 | 只画字节流箭头，遗漏 parent writer close |
| raw ledger | 保存 `IO BASE` 及各阶段的 fd slots、active files/refs/pipes、procs/free pages、state/channel、occupancy/open flags | 只贴摘要或只说 `make clean` |
| boundary | `FULL` 先观察第 513 字节 writer `SLEEPING/WRITE`，`EMPTY` 先观察 reader `SLEEPING/READ`，再验证 wake/result | 用 timeout、child 自报或 kill 后状态代替前置观察 |
| failures | EOF=0、broken write=-1、read/write waiter kill=-1，parent `wait` 验证同一 pid/status | 丢弃 wait status 或硬编码 marker |
| capacity | `PIPESIZE=512` 的顺序字节、`NOFILE=16` 槽耗尽回滚、阶段 after ledger 回 BASE | 只写少于 512 字节，未观察满条件 |
| device boundary | 静态连接 console/UART/PLIC 与 buffer/VirtIO anchors；动态只要求 README `read=16` 与账本恢复，并明确 cache hit 限制 | 把硬编码字段当 runtime interrupt，或宣称 DMA/crash 顺序 |
| limits | 明确 S/F/B/C/R 有界结论；R=N/A，CPUS=1 不推出公平性或所有交错 | 把一次 QEMU 运行称作形式化证明 |

runner 还必须通过 static/build、两次独立 `ioflow`、focused、CPUS=2 quick、CPUS=1 full、
patch reverse、`make clean`、工作树/`fs.img` 指纹和进程组 cleanup。报告应绑定 fixture、
runner、baseline、教程提交和完整出口报告的 SHA-256。
