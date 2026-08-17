# 通信与设备 I/O 唯一出口报告包

## Provenance

- 教程提交：
- pinned baseline（manifest authority）：
- fixture `communication.patch` SHA-256：
- runner `run-lab.py` SHA-256：
- 本报告 SHA-256（生成后回填）：
- 环境、QEMU/toolchain、CPUS：
- non-author reviewer / walkthrough：

## Ownership 与 pipeline

画出 `p->ofile[fd] -> struct file -> pipe/device -> process/channel` 的所有权图；逐 child
记录 `fork/dup/close/exec/read/write/exit/wait`，说明每次 ref、endpoint、slot 和 parent
关系何时改变。

## 原始账本与边界 marker

逐字粘贴两次 `ioflow` transcript，不删字段。至少包含：

| 阶段 | 原始字段 | 触发/前置 | 结果与允许副作用 | after 是否回 BASE |
|---|---|---|---|---|
| BASE | `fd/files/refs/pipes/procs/free` | 初始 console descriptors | 建立账本 | |
| PIPELINE | child pid、fd 1/0、bytes、EOF | fork/dup/close topology | 两 child status=0 | |
| EMPTY/FULL | child、state、channel、occupancy、wake | snapshot 先见 `SLEEPING` | wake 后 read/write | |
| EOF/BROKEN | occupancy、read/write result | 相反 endpoint 已关闭 | 0 / -1 | |
| KILL_READ/KILL_WRITE | child、state、channel、result/committed | snapshot 后 kill | wait 同 pid/status=-1 | |
| FD_ROLLBACK | filled、pipe | `NOFILE` 槽耗尽 | pipe=-1，账本不变 | |
| DEVICE | `read` | 普通 inode read | 16 字节；console/VirtIO 仅有静态 anchor，runtime completion 留下一单元 | |
| PASS | cleanup | 全部 child/endpoint 已回收 | cleanup=1 | |

## 验收与限制

- S：source anchors、fixture scope、patch apply/build。
- F/B/C：host 逐字段复算 raw marker；列出 focused/related、CPUS=2 quick、CPUS=1 full
  命令、退出码和 transcript digest。
- R：若无持久化测试写 `N/A`，并说明不推出公平性、任意 interleaving、DMA ordering 或
  crash recovery。

## Cleanup ledger

记录临时导出、私有 `fs.img`、QEMU/process group 的创建与逆向结果；给出 `make clean`、
patch reverse、共享工作树和 `fs.img` before/after 指纹，以及任何残留的处理结论。
