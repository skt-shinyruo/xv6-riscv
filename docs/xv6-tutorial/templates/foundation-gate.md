# Foundation gate 包

- 教程版本：
- manifest 源码基线：
- 走查时教程提交：
- `git status --short`：
- Linux 或 WSL 环境：
- 宿主 `cc`：
- RISC-V 工具链前缀：
- QEMU：
- GDB：
- `CPUS` 和 GDB 端口：

## 命令行与构建

- [ ] 链接 `environment.md`。
- [ ] 区分源码、目标文件、可执行文件和进程。
- [ ] 记录宿主检查、`make -B kernel/kernel`、退出状态和一次预期失败的阶段。

## C 内存推理

- [ ] 链接 `c-memory.md` 和三节点临时副本 patch。
- [ ] 包含链表/数组内存图、位标志真值表和 set/test/clear 观察。
- [ ] 定位空指针、数组越界和悬空指针的首个非法访问。
- [ ] 确认仓库 foundation 资源恢复并通过原始检查。

## C/RISC-V 调用栈

- [ ] 链接 `machine.md`。
- [ ] 覆盖 `_entry` 到 `call start` 的全部状态变化。
- [ ] 包含 hart 0/2 栈计算及 `pc/sp/ra/a0-a7/tp` 角色表。
- [ ] 把 `memcmp(left, right, 3)` 的参数映射到 `a0/a1/a2`，并说明返回 `a0` 覆盖原参数含义。

## 引导式调试

- [ ] 链接 `debug-trace.md`。
- [ ] 按 `_entry -> start -> main` 记录三个断点。
- [ ] 包含 thread/hart、`pc/sp/ra`、`x/i $pc` 的下一条指令、已建立栈上的内存和 backtrace。
- [ ] 记录错误端口或缺失符号的边界结果。

## 清理与自评

- [ ] QEMU 已终止，没有并行写同一 `fs.img` 的实例。
- [ ] 临时目录已删除，共享教程资源无练习修改。
- [ ] Foundation gate 的五行 rubric 全部满足。

任一项未勾选时，结果写为“不通过”并保留已取得的证据；不要用总分、timeout 或一次成功启动抵消缺项。
