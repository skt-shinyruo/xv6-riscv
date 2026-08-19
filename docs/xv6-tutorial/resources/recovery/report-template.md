# 崩溃恢复与离线一致性报告模板

这是一份单一出口报告包。完整 learner candidate 保存在仓库外；只记录 path/digest。runner 生成的
machine appendix 必须原样绑定到本报告，但不能代替 source model、逐点 matrix、offline ownership、
evidence limits 与签名。

## 1. 可复现性记录

    unit: project.crash-recovery
    pinned baseline commit:
    tutorial checkout commit:
    host / kernel toolchain:
    QEMU version:
    run timestamp / timezone:
    CPUS crash / quick / full: 1 / 2 / 1
    candidate absolute path (outside repository):
    candidate SHA-256:
    recovery-audit.patch SHA-256:
    run-project.py SHA-256:
    scenarios.json SHA-256:
    fsck.py SHA-256:
    machine appendix SHA-256:
    final report SHA-256:

记录 pristine image、每个 case image、临时 baseline export 和 exact QEMU PID/pidfd 的生命周期；共享
checkout/index/`fs.img` 的 before digest 另列在清理段。

## 2. Source flow、ABI 与 candidate scope（S）

画出：

```text
commit: write_log -> write_head(nonzero) -> install_trans(0) -> write_head(zero)
boot:   forkret -> fsinit -> initlog -> recover_from_log -> ireclaim -> kexec
```

| logical ID / point | candidate source edge | synchronous completion observable | normal unarmed effect | path scope |
|---|---|---|---|---|
| 10 / `LOG_DATA_COMPLETE` | | | | |
| 20 / `COMMIT_HEADER_COMPLETE` | | | | |
| 30 / `HOME_INSTALL_COMPLETE` | | | | |
| 40 / `HEADER_CLEAR_COMPLETE` | | | | |

说明 marker 完成、freeze、exact-PID kill 的顺序；列出 candidate/fixture allowlist 与 reverse result。

## 3. Crash image 与 first recovery matrix（R）

所有 home/log pattern 都记录 epoch、block index 和完整 block relation，不只记录首字节。

| point | crash header/targets | crash log payload | crash home 1990/1991 | marker -> kill | first `seen/installed` | recovered header/home | offline result |
|---|---|---|---|---|---|---|---|
| 10 | | | | | | | |
| 20 | | | | | | | |
| 30 | | | | | | | |
| 40 | | | | | | | |

逐点附 raw `RECOVERY CRASH`、host reopen parse、first boot `RECOVERY REPLAY` 和 checker JSON/digest。
解释 point 10 只能 before，points 20/30/40 只能 after；标出 byte-mixed 与 block-mixed 检查。

## 4. Second boot idempotence（R）

| point | first-stop SHA-256 | second `seen/installed` | second header/home | second-stop SHA-256 | equal |
|---|---|---|---|---|---|
| 10 | | | | | |
| 20 | | | | | |
| 30 | | | | | |
| 40 | | | | | |

任何 second replay、header 非零、home 改变或 digest 不等都必须保留为失败，不能用重跑覆盖。

## 5. QEMU、host 与 synthetic evidence 分离

| profile | 输入/trigger | raw observable | 支持的结论 | 明确不能推出 |
|---|---|---|---|---|
| QEMU logical order | | | | host/power-loss durability |
| host persistence | | | | cache flush/sector atomicity |
| synthetic tears | | | | failure frequency/complete tear space |

记录每个 QEMU PID 如何解析和 wait，以及 image 何时由 host 重新打开。不要把三行合成一个
“disk durable”结论。

## 6. Synthetic tears 与 rejection（B）

| id | source point / copy digest | exact operation | observed state/diagnostic | expected | accepted/rejected as intended |
|---|---|---|---|---|---|
| `header-zero` | | | | reject postcommit before | |
| `payload-half` | | | | reject mixed | |
| `home-half` | | | | repair to after | |
| `header-restore` | | | | redo after + second-boot idempotence | |
| `invalid-count` | | | | `E_LOG_COUNT`, no boot | |
| `invalid-target` | | | | `E_LOG_TARGET`, no boot | |

另列 offline oracle self-test 对 geometry、block duplicate、bitmap missing/leak、directory、nlink、orphan
mutation 的精确 diagnostic；这些 negative cases 不是 kernel runtime evidence。

## 7. Offline consistency 与 orphan reclaim

| checkpoint | input SHA-256 stable | header/log pending | inode/block/bitmap | directory/reachability/nlink | orphan | clean |
|---|---|---|---|---|---|---|
| pristine | | | | | | |
| point 10 first stop | | | | | | |
| point 20 first stop | | | | | | |
| point 30 first stop | | | | | | |
| point 40 first stop | | | | | | |

用 `sys_unlink -> commit -> crash -> recover_from_log -> ireclaim -> iput/itrunc/iupdate` 说明 redo 与
orphan cleanup 的责任边界。明确 `fsck.py` 是只读 publication acceptance oracle，不是 learner answer、
repair tool 或 production fsck。

## 8. Regression、资源与 cleanup

| tier | command/configuration | exit status | exact success condition | transcript/report digest |
|---|---|---|---|---|
| self-test | `run-project.py --self-test` | | good/bad oracle relations | |
| static | `run-project.py --static-only` | | pinned resources/source/scope | |
| focused | candidate + recovery cases | | four matrix + tears + offline | |
| quick | `CPUS=2` | | declared regression markers | |
| full | `CPUS=1` | | complete usertests regression | |

    qemu processes / pid handles after:
    hooks armed/fired after:
    private images removed:
    temporary export/build removed:
    candidate/fixture reverse result:
    source snapshot before/after:
    shared worktree/index before/after:
    shared fs.img before/after SHA-256:

记录失败重跑与 watchdog；timeout 不记为通过。

## 9. Evidence classification、limits 与签名

| dimension | 本报告支持 | 本报告不支持 |
|---|---|---|
| S | | runtime 全交错/形式化证明 |
| F | | 未执行路径的普遍性 |
| B | | 未建模 tear 与真实故障概率 |
| C=N/A | crash 固定 `CPUS=1`；quick 仅 regression | 并发 recovery/fairness |
| R | | physical power-loss durability |

- learner capability/entry contract（匿名）：
- non-author walkthrough record/path：
- author review：
- open corrections and disposition：
- learner signature：
- reviewer signature：

在 independent walkthrough、diff review、validator/generated-navigation 与全部机器 oracle 完成前，不得
用本模板的存在宣称 `verified`。
