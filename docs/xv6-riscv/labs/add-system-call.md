# 实验：新增 `sysinfo` 系统调用

## 1. 目标

新增：

```c
int sysinfo(struct sysinfo *out);
```

返回一个固定宽度快照，至少包含当前空闲物理页数、非 `UNUSED` 进程槽数和已分配全局 file 槽数。实验重点是贯穿用户 ABI、生成 stub、分派表、并发快照和 `copyout`；它不是精确监控系统，也不要求三个计数来自同一全局原子时刻。

建议 ABI：只用 `uint64` 字段并显式 `memset` 整个结构，保留若干置零字段供扩展。用户/内核必须包含同一份公开结构定义，不能复制两个可能漂移的定义。

## 2. 前置阅读与触点

- [一次系统调用往返](../flows/syscall-round-trip.md)；
- `kernel/syscall.h`：分配唯一 syscall number；
- `kernel/syscall.c`：声明、designated table 和参数分派；
- `kernel/sysproc.c` 或独立实现文件：handler；
- `kernel/defs.h` 及 allocator/file/proc 模块：只暴露最小计数 helper；
- `user/user.h`、`user/usys.pl`：原型和生成 stub；
- `user/usys.S` 是生成物，不直接编辑；
- `user/usertests.c` 或独立测试程序、`UPROGS`。

## 3. 设计约束

1. 系统调用号、用户声明、stub、内核声明和分派表必须一一对应；禁止靠数组下标偶然对齐。
2. 每个计数 helper 在其拥有者模块内取锁。不要从 `sysproc.c` 直接暴露或遍历私有 freelist。
3. 统计 proc 时逐槽短暂持有 `p->lock`；统计 file 时持 `ftable.lock` 或由 file 模块提供 helper。
4. 不同时持 `kmem.lock`、`ftable.lock` 和 `p->lock`。分别采样到局部变量，接受弱一致快照，避免新建全局锁序。
5. 所有统计锁在 `copyout` 前释放。当前 `copyout` 可能为 lazy 页分配内存并失败，但不会睡眠；把它移出临界区仍可缩短关中断时间、避免把未来实现细节加入锁契约，而且本实验不需要跨子系统原子快照。
6. 结构体先完整清零再赋字段，避免把 C padding 或旧栈内容泄给用户。
7. bad user pointer 返回 -1；统计动作无持久副作用，所以不需要回滚计数。

## 4. 分阶段任务

### A. 固定 ABI

建立共享头，明确 version/size 或保留字段策略。用编译期断言锁定 `sizeof(struct sysinfo)`，并说明未来扩展是新增 syscall/version，还是由 caller 传入 size；不要默认为任意扩展都 ABI 兼容。

### B. 最小分派链

先让 handler 对合法指针返回全零结构。反汇编生成的 user stub，确认 `a7` 装载新 number，参数仍在 `a0`。检查未知 number 和相邻 syscall 未被覆盖。

### C. 模块内计数

依次加入 free-page、proc、file 计数。每加一项都写出锁前置条件、复杂度和计数含义，例如 `USED/SLEEPING/RUNNABLE/RUNNING/ZOMBIE` 是否都算“已用槽”。

### D. 用户测试

在无并发的基线状态验证范围，再制造一个 child、打开一个 file、分配若干 eager pages，检查方向变化；并发时只断言上下界和结构一致，不能断言跨对象的瞬时等式。

## 5. 验收条件

- 系统调用号、用户声明、生成 stub、内核分派和 handler 经人工/静态核对一致；
- 合法调用返回 0，所有 reserved 字段为 0，数值在容量范围内；
- null、`MAXVA`、只读用户页和跨有效/无效页的输出地址返回 -1，内核不 panic；
- 合法 lazy 输出页按当前 `copyout` 策略物化并成功；
- 调用前后无锁遗留、无物理页/file/proc 槽净泄漏；
- 32/64 位字段在用户/内核两侧尺寸相同，`sizeof` 有断言；
- 定向测试、系统调用相关 tests 和完整 `usertests` 通过。

## 6. 故障注入与负例

| 注入 | 预期结果 | oracle |
|---|---|---|
| `copyout` 第一页 PTE 缺失且 `kalloc` 第 1 次失败 | syscall -1，进程可继续 | free-page回基线、无锁持有 |
| 输出跨页，第二页补页 OOM | syscall -1，第一页面可能已有前缀 | reserved/计数只检查已写前缀，内核状态不变 |
| 统计期间并发 fork/exit/open/close | 返回某个弱一致快照 | 每字段独立在合法范围，无死锁 |
| 故意漏掉结构清零 | 测试必须检测 reserved 非零 | 先把内核栈 poison，再重复调用 |
| 故意让计数 helper 在一个注入分支漏掉 `p->lock` 的 release | syscall 退出前的测试断言必须失败 | 记录 `noff` 和 lock owner；不能只等待系统随机挂起 |

负例只在 mutation test 构建启用；验收提交必须删除/关闭 mutation，并让同一测试通过。

## 7. 调试与清理

trace `(pid, syscall number, user VA, each counter, copyout result)`，但不要在持 spinlock 时打印。完成后确认 `user/usys.S` 由生成规则产生、没有手工差异；清除测试 hook，保留断言和文档中的 ABI 说明。
