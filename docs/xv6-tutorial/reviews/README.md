# 走查记录

只有完成技术检查并经过非作者走查的单元才能标记为 `verified`。走查记录使用 `../templates/walkthrough-review.md`，不保存姓名、联系方式或其他身份信息。

当前 `0.1.0` 发布版仍为 `draft`。已登记的增量验证记录：

- [Foundation 0.1.0 非作者走查](foundation-0.1.0.md)：基础路径五个单元按 `requires` 顺序通过。
- [Observe-system 0.1.0 非作者走查](observe-system-0.1.0.md)：首个核心单元的启动时间线、配置实验和清理证据通过。
- [User-program-and-ABI 0.1.0 非作者走查](user-program-and-abi-0.1.0.md)：用户程序构建、ELF/ABI、镜像路径和隔离实验通过。
- [Syscall-roundtrip 0.1.0 非作者走查](syscall-roundtrip-0.1.0.md)：`getpid` 正常与未知编号的特权级、页表、栈、trapframe 和返回轨迹通过。
