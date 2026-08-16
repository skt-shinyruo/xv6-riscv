# 虚拟内存问题：答案与证据标准

先提交[问题页](../virtual-memory.md)要求的图和隔离报告。本页给出判定标准；固定
pid、绝对 PA 或一次 `VM PASS` 都不能替代关系检查。

## VM-00

合格全景从 `allocproc()` 的 trapframe PA 和 `proc_pagetable()` 的 user root 开始，
经首次 `kexec("/init")` 建普通 leaf；用户执行依赖 per-process user root，trap 后
切 shared kernel root。正 lazy growth 只提交 `p->sz`，首次访问再取得 PA/leaf；
fork 创建另一 root 并复制现有 leaf、跳过 holes；exec 在同一 proc 内事务替换
root；exit 留 zombie，wait 才 freewalk。图必须区分 root/intermediate/leaf/target
PA 与 proc identity，不能把“页表”当成一个不可分对象。

## VM-01

`[0,p->sz)` 内缺失/invalid leaf 是 logical hole。`uvmcopy()` 在缺失中间页或
invalid leaf 时继续，child 保留 hole；负 shrink 的 `uvmunmap()` 容忍 hole；
`uvmfree()` 先按 rounded `sz` 扫普通 leaf，再 `freewalk()` 递归页表；exec commit
释放旧表，bad 保留旧表。稀疏只减少 PA/复制量，扫描仍可与逻辑 size 成正比。

## VM-02

直接 load/store 的 `usertrap()` 传原始 `stval`，cause 分别为 13/15；成功
`vmfault()` 后不改 epc，重试同一指令。`copyin/out()` 传页首并在 kernel syscall
内继续复制，不产生第二次 user trap；`copyinstr()` 遇缺页直接 `-1`。因此 break
落在部分页中时，helper 可能以页首 `<p->sz` 物化略越 break 的地址，而硬件首次
触碰用原始地址拒绝。报告的 access 1/2/3/4 与 cause 13/15/0/0 必须一致。

## VM-03

`copyout()` 每轮先取得当前页 PA、检查 W、计算 `n`，随即 `memmove()` 并推进
参数；没有保存 undo buffer。第二页失败时第一页已经提交，不回滚。上层语义由
调用者决定：某些 syscall 返回 -1，pipe/file path 也可能报告 0 或已完成数量；
不能从 helper 的 `-1` 推出统一用户返回值。

## VM-04

有效 ELF 通过 header 后才创建临时 `proc_pagetable()`，逐 segment `uvmalloc/
loadseg`，再建 guard/stack 并 copy argv。commit 才替换 root/sz/epc/sp 并释放
旧表；`bad` 释放临时表。不存在文件和坏 ELF 都在临时表建立前失败，覆盖不了
后期 rollback。fixture 的两个约 3000-byte 合法 argv 超过一页 user stack；
exec 返回旧映像后验证 marker/pid，并用 status=42 排除“成功 exec 到 echo 退出”。

## VM-05

旧 user root/leaf 只由旧 image 拥有，commit 后当前硬件运行在 kernel root，故可
释放；kernel stack 在 shared kernel root，trapframe PA 单独由 proc 拥有，fd/cwd/
pid/parent 也不属于 user image。`sys_exec()` 先用 `fetchaddr()->copyin()` 读取
argv pointer slots，可能在旧表补 lazy page；`fetchstr()->copyinstr()` 不补页。
这是 failure 前已经提交给旧 proc 的允许副作用。

## VM-06

PTE_U 控制 U-mode，S-mode 的 `uservec` 仍可访问无 U 的特殊 leaf。trampoline 在
两个 roots 中同 VA/PA、R|X；user TRAPFRAME 是固定 VA 到当前 trapframe PA、
R|W，kernel root 同数值 VA 为 guard hole。`uservec` 用 sscratch 保存 a0、保存
GPR、加载 kernel_sp/hartid/trap/satp，以 `sfence; satp; sfence` 切表并 jalr；
`prepare_return()` 刷新这些字段与 sepc/sstatus，`userret` 同样围住 user satp
写入、恢复 GPR、sret。linker 断言 trampoline 一页，才能保持切表期间 PC 有效。

## VM-07

当前每个 proc 一次只由一个 hart 执行；context migration 在重新进入该 proc 时
使用它的 satp，本地 satp/sfence 清理当前 hart。不存在两个用户线程同时使用
同一 root 并无锁改 leaf，因此没有 remote invalidation 路径。共享地址空间线程、
COW write-protect 或跨 hart 修改 active table 会需要 shootdown/同步。`CPUS=2`
quick 只说明未 arm test seam 未造成观察到的回归，不证明该协议。

## VM-08

普通 V leaf 由 `uvmcopy()` 分配新 PA、复制整页并复用原 flags；lazy hole 被跳过；
guard leaf 有 V 但无 U，仍被复制并保留 flags。COW 需要把可写 leaf 变只读+COW、
增加 PA ref、在 user store 与 kernel copyout write fault 上拆页，并在 unmap/exec/
exit/fork rollback 中递减引用，还要处理可见 PTE 与 TLB。这些是下一单元要求，
不是当前实现事实。

## VM-09

当前 `vmfault()` 用参数 `pagetable` 执行 `ismapped()`，但 `mappages()` 固定写
`myproc()->pagetable`，合法性也来自当前 `p->sz`。所以参数必须就是当前 proc
table。exec 的临时 table stack 已由 eager `uvmalloc()` 建好，`copyout()` 通常
不会走缺页 fallback；这不是 helper 可安全服务任意离线 table 的证明。

## VM-10

valid hole：before=0，load/store cause=13/15 或 helper cause=0，result=1，leaf
mask=V|R|W|U、X=0、status=0。invalid：无 leaf、allocation=0、cause 13/15、
status=-1。permission：before/after 是同一 R|X|U text leaf、W=0、allocation=0、
status=-1。NX：写入有效 `ret 0x00008067` 并 `fence.i`，cause=12、stval=epc=target、
leaf 仍 X=0、allocation=0、status=-1；随后同 leaf 临时加 X、`sfence.vma` 后同一
ret 返回 0。执行零页只会产生 illegal instruction，不能区分 NX。

## VM-11

high VA 的 first leaf 先取 data PA，再由 `walk()` 取 L1/L0 页。fail 1：data 未
取得；fail 2：data 已取得后释放、L1 未取得；两者 free_before/free_after 相同。
fail 3：data 与 L1 已取得、L0 失败，data 释放但空 L1 仍由 proc tree 拥有，故
立即少一页。三者都必须 after_flags=0、child=-1，parent wait 后 base==after；
后一个等式才证明 intermediate 最终由 freewalk 回收。

## VM-12

综合答案先用 SPECIAL 固定 roots/special mappings，再把 normal cases 连接
`p->sz -> vmfault -> leaf -> retry/helper continuation`；invalid/permission/NX
分别证明三类拒绝，mutation 验证 NX 判别力；OOM 分离立即 leaf/data rollback 与
最终 intermediate cleanup；exec status=42 验证临时映像 rollback；focused/
quick/full 与 patch reverse 收束到同一共享状态。S 支持源码关系，F 支持正常
物化/回归，B 支持 fault/OOM/rollback；C/R 明确 N/A。A/D 被 mask、OOM 是模拟、
CPUS=1 focused 和有限 VA/ELF 都必须列入局限。

## 证据边界

全部 case 的 parent `base==after`、patch reverse、临时 snapshot 和共享状态 digest
是清理必要条件。free-page 总账不能单独给每页分类，guest 自报 PASS 不能替代
host 解析，multi-hart 回归不能升级成 shootdown 证明。
