# 深入实验

这些实验面向当前分支，不假定上游 xv6 的函数名、首进程、lazy allocation、orphan recovery 或 UART 实现相同。每项都要求先写不变量和失败 oracle，再改代码；“启动成功”或一次 `usertests` 通过不构成验收。

| 实验 | 核心能力 | 主要风险 |
|---|---|---|
| [新增 `sysinfo` 系统调用](add-system-call.md) | ABI 全链路、锁内快照、用户指针 | 映射漂移、padding 泄漏、锁序或锁泄漏 |
| [证明并加固 lazy heap NX](lazy-nx.md) | PTE 权限、fault 分类、行为测试 | 把 instruction fault 错当 lazy allocation |
| [Copy-on-write fork](copy-on-write.md) | PTE 软件位、物理页 refcount、TLB、回滚 | UAF、双重释放、stale writable TLB |
| [确定性制造丢失唤醒](lost-wakeup.md) | 条件锁、`p->lock` 交接、调度 gate | 随机复现、测试永久挂起 |
| [可并行 buffer cache](buffer-cache.md) | 唯一性、分桶、victim迁移、pin | duplicate buffer、锁环、日志死锁 |
| [离线只读 fsck](offline-fsck.md) | 磁盘格式、shadow replay、全局图检查 | 用坏元数据寻址、误修复 live image |

## 共用规则

1. 每个实验单独分支，记录基线 commit、工具链、QEMU版本、`CPUS`、内存和 `fs.img` 哈希。
2. 故障注入只在测试构建启用；正常构建默认关闭，hook 有命名、作用域、触发次数和 trace。
3. 需要破坏镜像的测试只操作从已知基线复制的临时镜像。不得在 QEMU 正以可写方式运行时执行离线工具。
4. timeout 只是 watchdog，不是正确性 oracle；超时必须输出目标 pid/state/chan、锁、注入点和最近事件。
5. 可恢复的 guest 失败在调用返回或 parent `wait` 后检查进程/file/inode/buffer 槽、物理页、日志 header 和 VirtIO descriptor 回到基线。预期 panic 必须在独立 QEMU/临时镜像运行，以 panic 前 owner trace 和重启后镜像检查为 oracle，不能要求已崩溃实例继续计数。纯宿主工具则检查输入哈希不变、无越界和临时文件清理，不套用 guest 槽位账本。
6. 最少运行实验定向测试、相关 `usertests`，再运行完整回归；对并发实验至少覆盖 `CPUS=1` 和多 hart。

故障框架和 crash 编号规范见[故障注入](../verification/fault-injection.md)，跨子系统证明见[全局不变量](../correctness/global-invariants.md)。
