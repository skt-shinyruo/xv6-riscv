# 虚拟内存问题

本组问题关注逻辑地址空间与实际页表映射的差异、lazy allocation、用户拷贝、exec 提交以及并发页表修改的前提。

## 问题

1. **VM-01** 为什么 `p->sz` 范围内可以没有有效 PTE？`fork`、缩容和进程退出如何正确处理这些 lazy holes？
2. **VM-02** 为什么普通 `copyin()/copyout()` 可以触发 lazy allocation，而 `copyinstr()` 不会？这会造成哪些用户可见差异？
3. **VM-03** `copyout()` 跨两页时第二页分配失败，第一页的写入是否回滚？系统调用应该返回什么？
4. **VM-04** `kexec()` 如何构造临时页表并实现提交前失败不破坏旧进程映像？
5. **VM-05** exec 成功后为什么能立刻释放旧用户页表，而不会释放当前正在使用的内核栈？
6. **VM-06** trapframe 和 trampoline 为什么存在于用户页表中，却不能带 `PTE_U`？
7. **VM-07** 当前实现为什么不需要跨 hart TLB shootdown？加入用户线程或共享地址空间后哪项前提失效？
8. **VM-08** 当前 `fork` 如何分别处理普通有效页、lazy hole 和 guard page？改成 COW 后哪几个路径都必须增加引用计数处理？
9. **VM-09** `vmfault(pagetable, ...)` 为什么不能安全用于任意临时页表？它对当前进程页表有什么隐含假设？

## 源码入口

- [`kernel/memlayout.h`](../../kernel/memlayout.h)
- [`kernel/riscv.h`](../../kernel/riscv.h)
- [`kernel/kalloc.c`](../../kernel/kalloc.c)
- [`kernel/vm.c`](../../kernel/vm.c)
- [`kernel/trap.c`](../../kernel/trap.c)
- [`kernel/exec.c`](../../kernel/exec.c)
- [`kernel/proc.c`](../../kernel/proc.c)

## 配套文档

- [虚拟内存](../xv6-riscv/kernel/memory.md)
- [`exec`](../xv6-riscv/kernel/exec.md)
- [一次 lazy page fault](../xv6-riscv/flows/lazy-page-fault.md)
- [Copy-on-write fork 实验](../xv6-riscv/labs/copy-on-write.md)
- [平台契约](../xv6-riscv/architecture/platform-contracts.md)
