# Virtual memory 0.1.0 非作者走查记录

- 教程版本：`0.1.0 draft`；单元状态：`verified`
- 源码基线：`e6fc75076de152c5446f2c6be9bb80b848e7cc5d`
- 走查时教程提交：`386ee5c8cfe0933bf7db0587670a50ef2004005d` 加候选 diff；晋级元数据在走查通过后原子加入
- 走查单元或连续路径：`core.virtual-memory`、`VM-00..VM-12`，以及旧 `BOOT-06` 合并到 trampoline 题
- 匿名入口能力：`VM-C20-R4`；已通过前置进程生命周期、Foundation 和边界单元，未参与本单元正文、patch 或 runner 编写
- 使用环境：WSL2 Linux x86-64；QEMU 8.2.2、GNU Make 4.3、Python 3.12.3、`riscv64-linux-gnu-gcc` 13.3.0；focused/full 使用 `CPUS=1`，quick 使用 `CPUS=2`，128 MiB，临时导出和私有 `fs.img`

## 观察到的卡点

走查者先从 manifest 的 pinned baseline 与教程提交分栏开始，执行 static gate，再
独立运行完整 runner。它没有使用共享工作树的 `fs.img`，也没有把仓库中的
implementation-reference 文档当作教程依赖。

## 验收产物

- patch：`resources/virtual-memory/lazy-nx.patch`，SHA-256
  `c91da69ebf02caac2f880f670946ef2394f20435157e6e3cf4bfa078ce4b7698`
- runner：`resources/virtual-memory/run-lab.py`，SHA-256
  `fc13d90cb0095f29e306260806d53dcb8aada1d9c161ddfaa8b6802ad5fd8de3`
- 独立报告：`/tmp/ticket11-nonauthor-final-report.md`，SHA-256
  `ce62c2451cc2c4c5fca374437eef82a31e03d428e9a86753b34e55a176334e5e`
- runner 结果：`static, F/B, focused, related, quick, full, cleanup`

S 静态门确认 Sv39 walk、kernel/user roots、`TRAMPOLINE` 同 VA/PA、user
`TRAPFRAME` 的 proc ownership、kernel 数值 `TRAPFRAME` guard hole、完整
`uservec/prepare_return/userret` `satp`/`sfence.vma` 契约、lazy `p->sz`、fault
分类、fork holes、exec commit/bad、`freewalk` teardown 和 12-path patch scope。

F 记录了四种首次物化：load/store 的真实 13/15 fault 分类、`copyout/copyin`
的 helper 路径、`V|R|W|U` 且无 `X` 的 leaf、非零/零 pipe payload 保真，以及
child status=0。特殊映射报告证明 roots 不同、trampoline PA 相同、user
trapframe PA 等于 owner PA，kernel 的同数值 trapframe 不可映射。

B 逐字段复核 invalid、已映射只读 text、NX、同一有效 `ret 0x00008067` 的
`PTE_X` mutation、data/L1/L0 三档模拟 OOM 和 late exec rollback。NX 的
`scause=12`、`stval=epc`、无 X、status=-1 与 mutation 后 status=0 均成立；
OOM 的即时债为 0/0/1，所有 child wait 后 `base==after`；有效 ELF 加两个约
3000-byte argv 的 late failure 保留旧映像并返回 status=42。

回归覆盖 `lazy_alloc/lazy_unmap/lazy_copy/lazy_sbrk/sbrkfail/sbrkbugs/
sbrklast/execout`、quick 和完整 `usertests`；每组有唯一 `ALL TESTS PASSED`
且无 VM marker 泄漏。runner 最后逆向 patch、`make clean`、回收进程组并比较
共享工作树/索引/内容和 `fs.img`；共享 `fs.img` 前后均为 `missing`，无 QEMU 或
driver 残留。

## 修正与复查

首轮技术复审指出 runner 的报告路径可写回仓库、工具链前缀硬编码、trampoline
静态门缺少切换前 fence，以及 late exec 仍 arm audit seam。修正后，runner 会
拒绝仓库内报告路径、从 Makefile 数据库解析工具链、逐项检查 GPR、
`prepare_return` 和切换前后 fence；late exec 不再 arm seam。顺带移除未使用
常量和 magic case 编号，并把任务准确称为 bounded change lab。最终非作者走查
在固定 patch/runner 哈希上重新执行完整 runner、独立解析所有 raw marker，未再
发现 blocking 或 non-blocking finding。

S/F/B 已闭合；C/R 对本单元为 `N/A`。`CPUS=2` 只是不 arm seam 的回归，不证明
remote TLB shootdown、共享地址空间并发或持久化恢复；A/D 位只在 host oracle
中 mask，OOM 是确定性模拟，有限 VA/ELF 场景不是形式化证明。

## 结果

| 单元 | 结果 | 晋级依据 |
| --- | --- | --- |
| `core.virtual-memory` | 通过 | 地址空间 ownership、full trampoline、lazy-NX fault 分类、OOM/exec rollback、回归和隔离清理均有独立报告与可重跑 runner |
