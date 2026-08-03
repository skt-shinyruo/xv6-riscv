# kill 一个阻塞进程：标志、非条件唤醒与协作退出

`kill(pid)` 不异步销毁目标，也不注入用户信号。它设置 `p->killed`，必要时把 `SLEEPING` 改为 `RUNNABLE`；目标在可取消等待点或返回用户态边界观察标志后退出。

## 1. killer 路径

```text
user kill(pid)
  -> SYS_kill -> sys_kill -> kkill(pid)
  -> linear scan proc[0..NPROC)
       acquire candidate p.lock
       if pid matches:
          p.killed=1
          if state==SLEEPING: state=RUNNABLE
       release p.lock
```

返回 0 只表示扫描时找到同 pid 槽并写入标志，不表示目标已经退出或副作用回滚。找不到返回 -1。扫描、pid 比较和状态更新由每槽 `p->lock` 串行化。

## 2. 以阻塞 console read 为例

目标原本持 `cons.lock` 检查 `cons.r==cons.w`，然后 `sleep(&cons.r,&cons.lock)` 发布：

```text
p.chan=&cons.r, p.state=SLEEPING, p.lock handed to scheduler
```

kill 获取 `p->lock` 后设置标志并直接置 `RUNNABLE`。它不持 `cons.lock`，也没有改变 `cons.r/w`；这是“取消唤醒”，不是条件成立唤醒。

若 kill 发生在上述 `SLEEPING` 发布之后，目标恢复后由 `sleep()` 清 chan、重新取得 `cons.lock`。`consoleread()` 再次看到缓冲空，先检查 `killed`，释放锁并返回 -1。系统调用返回到 `usertrap()` 后，最终 killed 检查对普通进程执行 `kexit(-1)`。

这里不能推出“取消唤醒永不丢失”。当前调用者先在持条件锁时检查 `killed`，随后才进入 `sleep()`；killer 不取得 `cons.lock`。因此存在真实窗口：目标检查到 `killed==0`，仍为 `RUNNING`；killer 只置 flag；目标随后取得 `p->lock` 并发布 `SLEEPING`。没有第二次 kill 或真实 console 输入时，它可能一直睡眠。`p->lock` 只保证已经开始发布睡眠状态之后的 wakeup 不丢失，不能封闭调用者检查与 `sleep()` 之间的取消竞态。

## 3. 阻塞点可取消性矩阵

| 等待位置 | 通道 | 恢复后检查 killed | kill 的实际效果 |
|---|---|---|---|
| console 空输入 | `&cons.r` | 是 | 若已发布 SLEEPING，则返回 -1，再于 usertrap 退出；检查后入睡窗口可能延迟到真实输入 |
| pipe 空/满 | `&pi->nread` / `&pi->nwrite` | 是，等待前/循环内 | 若已睡眠则被取消唤醒并返回 -1；检查后入睡窗口仍存在，且可能已有部分字节 |
| UART TX busy | `&tx_chan` | 否 | 被置 runnable 后仍继续发送，必要时再次 sleep；直到写完，才在 usertrap 边界退出 |
| `sys_pause` ticks | `&ticks` | 是 | 若已睡眠则返回 -1；检查后入睡窗口可等到下一 tick |
| `kwait` 无 zombie | `p`（父进程） | 是 | 若已睡眠则 wait 返回 -1；检查后入睡窗口可等到 child 状态变化 |
| `begin_op` 等日志空间/commit | log wait channel | 否 | 被置 runnable 后若条件仍假会再次 sleep，直到日志进展 |
| VirtIO descriptor/完成 | driver channel | 否 | I/O 完成后才继续；kill 不取消 DMA |
| sleeplock | sleeplock channel | 否 | 获锁后才继续 |

`sleep()` 本身不检查 killed。可取消性属于每个调用者的谓词循环；即使调用者有检查，若取消方不取得条件锁、调用者又不在持 `p->lock` 后复查，仍可能命中检查后入睡窗口。新增等待点必须分别定义检查位置、锁关系和该窗口的处理策略。

## 4. 目标状态矩阵

| 命中状态 | `kkill` 更新 | 何时退出 |
|---|---|---|
| `SLEEPING` | flag + `RUNNABLE` | 被调度、调用点处理取消，或之后的 usertrap 边界 |
| `RUNNABLE` | 只置 flag | 某 hart 运行后到 killed 检查 |
| `RUNNING` | 只置 flag，无 IPI | 通常在下一 trap/系统调用检查；若正处于“检查后、发布 sleep 前”，可先睡眠并延迟到真实条件变化 |
| `USED` 发布窗口 | 只置 flag | 路径依赖初始化阶段；`forkret` 本身不在入口先检查 killed |
| `ZOMBIE` | 置 flag但无行为变化 | 仍由 parent/init `wait` 回收 |
| `UNUSED` 且 `pid==0` | `kill(0)` 会错误命中并置 flag，状态不变 | 当前 `allocproc()` 认领该槽时不先清 `killed`，新进程继承污染 |

pid 不是 capability；猜中尚处 `USED` 的正 pid 也可能标记成功。更严重的是当前 `kkill(0)` 会在第一个 `UNUSED/pid==0` 槽返回成功并留下 `killed=1`，而 `allocproc()` 只设置新 pid/state，不清这个字段；随后认领该槽的 child 会在之后的 killed 检查中异常退出。只有 `freeproc()` 才会清 `killed`，所以这不是无害的空槽写入。`initproc` 又是特殊例外：若最终进入 `kexit()`，内核会 panic，而不是允许 init 成为 zombie。

## 5. 所有权与副作用

kill 不展开内核栈、不释放 fd/inode/page，也不撤销正在进行的系统调用。真正清理发生在目标调用 `kexit()`：关闭 fd、释放 cwd 引用、reparent children、发布 `ZOMBIE`；页表和 trapframe直到 parent `kwait()` 回收。

因此：

- pipe/file write 在观察 killed 前写入的前缀保留；
- 已提交的文件系统事务不会撤销；已加入但尚未提交的操作仍按日志协议完成；
- 已提交给 VirtIO 的 DMA 请求必须完成，buffer 所有权不能因 kill 提前归还；
- lazy `copyin/out` 已分配的页保留到进程回收；
- kill 与 close/unlink 等并发结果由各对象锁和事务顺序决定，而非 kill 优先级。

## 6. 调度延迟

`kkill()` 不发送 reschedule IPI。即使把目标设成 `RUNNABLE`，也只是让后续 scheduler scan 可见。若目标在另一 hart 运行，killer 不能立即打断它；依靠该 hart timer 或自然 trap。若目标由不可取消等待重新 sleep，则还依赖真正条件进展。

## 7. 确定性测试

应给内核测试构建加入命名 checkpoint 和一次性 barrier，而不是用随机延时：

1. `sleep_after_state_publish`：目标已是 SLEEPING、尚未 swtch；此时 kill，验证已发布睡眠的取消不会遗失。
2. `after_killed_check_before_sleep`：用持条件锁且 `CPUS>=2` 的原子 gate 停在检查之后，令另一 hart kill，再放行目标发布睡眠。当前实现的 oracle 是稳定观察到 `state=SLEEPING,killed=1`；随后用真实 producer 或第二次 kill 清场。修复实现后，此负例应变为无需外部进展即可退出。
3. `kill_after_wakeup_before_run`：目标已 RUNNABLE，验证 flag 保留且最终退出。
4. `pipe_after_k_bytes`：写入固定前缀后阻塞再 kill，核对 reader 只看到该前缀。
5. `virtio_after_submit`：kill 等待 I/O 的目标；完成中断只置 `b->disk=0` 并唤醒，请求线程随后清 `disk.info`、释放 descriptor chain，调用方最终 `brelse()`，之后目标才退出。
6. 对 ZOMBIE 调 kill，应返回当前实现结果但不得重复清理或改变 wait status。
7. 单独运行 `kill(0)` 回归：确认它当前污染第一个 UNUSED 槽、下一次 `fork` 复用同一槽并继承 killed；修复后同一测试应要求 `kill(0)==-1` 且 child 正常运行。
8. 每例在 parent `wait` 后核对空闲页、file/inode 引用和 pipe page 回到基线；状态超时必须打印最后的 `state/chan/killed/hart`。
