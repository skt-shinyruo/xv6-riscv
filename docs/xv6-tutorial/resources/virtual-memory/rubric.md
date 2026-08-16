# Virtual memory 与 lazy-NX 报告 rubric

提交物是一份地址空间生命周期与 lazy-NX 证据报告包。publication fixture 只给
出可重复验收 seam；学习者必须独立解释源码关系，不能把 `VM PASS` 或完整 patch
当作答案。

## 必须通过

| 项目 | 通过标准 |
| --- | --- |
| baseline | 分开记录 manifest 的 pinned baseline、走查时教程 commit、patch/runner/report SHA-256 与工具版本 |
| translation | 从 `satp/PX/walk` 解释 L2/L1/L0、leaf flags、TLB 和 PA，不以绝对地址代替关系 |
| ownership | 区分 kernel root、per-process root/intermediate、普通 data PA、共享 trampoline PA 和 proc-owned trapframe PA |
| trampoline | 证明 trampoline 在两 roots 同 VA/PA，TRAPFRAME 只在 user root 的固定 VA；完整追踪 uservec/prepare_return/userret |
| lifecycle | 连接 allocproc、ELF/eager/lazy、fork holes、exec commit/bad、exit/wait/freewalk |
| normal F | load/store/copyout/copyin 从 absent leaf 到 `V|R|W|U,X=0`，来源/cause/status 与非零/零 payload 正确 |
| boundary B | invalid、mapped text permission、NX cause 12、同一 valid ret 的 X mutation 全部可区分 |
| OOM B | data/L1/L0 三个 failpoint 无 valid leaf；即时 data rollback 与最终 intermediate cleanup 分开记账 |
| exec B | 有效 ELF + 两个合法长 argv 在临时映像建立后失败；旧 marker/pid 可继续且状态 42 区分成功 exec |
| regression | focused/related、quick 与完整 `usertests` 均有精确成功 marker，且无 `VM` marker 泄漏 |
| cleanup | 每个 child wait 后 free-page 总账恢复；临时 build/patch/process group 清理；共享工作树和 `fs.img` 不变 |
| limits | S/F/B 声明准确；C/R N/A；说明 A/D mask、模拟 OOM、CPUS=1 和有限场景不能推出的结论 |

## 直接不通过

- 把 timeout、boot prompt、guest `PASS` 或完整 `usertests` 单独当正确性 oracle。
- 用坏 opcode/零页执行失败声称 NX，或 mutation 后仍不要求同一 `ret` 成功返回。
- 只报告 OOM 后 child 被 kill，不检查 leaf、分配序号和 wait 后页账本。
- 要求第 3 个 failpoint 后整棵 page table 立即逐位恢复，忽略 empty intermediate
  由 `freewalk()` 最终回收的当前契约。
- 用不存在文件/坏 ELF 代替 late exec rollback，或允许成功 `echo` 的 status=0
  冒充旧映像返回。
- 把 kernel table 的数值 `TRAPFRAME` VA 写成 process trapframe 映射；只有 user
  table 有该 leaf，kernel 通过 direct map 访问 PA。
- 把 `CPUS=2` 回归写成 remote TLB shootdown、并发正确性或形式化证明。
- 将 test-only syscall、failpoint 或 `PTE_X` mutation 留在 executable baseline。

## 复核提示

复核者应独立运行 `run-lab.py --report <path>`，重新解析 raw marker 并计算报告
digest；不要只接受作者给出的摘要。任何 fixture 内容变化都需要重新跑静态、
focused、related、quick、full 和 cleanup。
