# 用 C 表达内存、指针、位标志和链式结构

## 问题场景与本单元成果

xv6 使用指针连接页表、进程、文件和缓存对象，也使用位标志在一个整数中表达多个独立状态。若只把指针理解为“特殊变量”，或者只把掩码当成魔法常量，后续很难判断对象由谁拥有、状态如何组合以及失败时应该恢复什么。

本单元要求你用地址图和位标志真值表解释教程链表程序，并完成一个有边界的结构修改。出口产物是一张内存关系图、一张 set/test/clear 真值表，以及空指针、数组越界和悬空指针三个失败场景的定位说明。

## 前置单元与暂存黑盒

硬前置：[从命令行构建并运行程序](command-line-build.md)。

暂时不解释虚拟地址、页表、分配器和 C 编译器的具体布局策略。当前只使用一个足够稳定的模型：对象占据一段内存，指针保存对象起始地址，对象的生命周期决定这个地址何时还能安全解引用。

## 最小模型和关键不变量

在 `pointer-list.c` 中：

```c
struct node {
  int value;
  unsigned flags;
  struct node *next;
};
```

`struct node` 把一个整数、一组位标志和一个指向同类对象的地址组合起来。`NULL` 表示当前没有下一个节点。表达式 `&tail` 取得 `tail` 的地址；`node->next` 等价于先解引用 `node`，再访问其中的 `next` 字段。

数组把固定数量的同类对象连续放置；长度为 2 的 `struct node nodes[2]` 只有 `nodes[0]` 和 `nodes[1]`。下标不会自动证明自己在边界内，访问 `nodes[2]` 已经越界，即使这次运行没有崩溃。

资源程序定义 `NODE_ACTIVE = 1u << 0` 和 `NODE_PINNED = 1u << 1`。左移为每个状态选择不同 bit；`flags | NODE_PINNED` 设置 bit，`flags & NODE_PINNED` 测试 bit，`flags & ~NODE_PINNED` 清除 bit。只有先用 `&` 取出目标 bit，再与 `0` 比较，才能回答该状态是否存在。

程序中的关系是：

```text
head { value=5, flags=ACTIVE, next=&tail }
  -> tail { value=7, flags=ACTIVE|PINNED, next=NULL }
```

关键不变量：

- 每次执行 `node->value` 前，`node != NULL`。
- `next` 要么是 `NULL`，要么指向仍在生命周期内的 `struct node`。
- 遍历每一步都向链尾前进；若形成环，当前 `sum()` 不会结束。
- 不同状态使用不重叠的单 bit 掩码；设置或清除一个 flag 不能改变其他 flag。
- 数组边界、结构体边界和对象生命周期不能由“程序这次没崩溃”来证明。

## 源码追踪计划

1. `docs/xv6-tutorial/resources/foundation/pointer-list.c:main`：对象创建和连接。
2. `docs/xv6-tutorial/resources/foundation/pointer-list.c:sum`：遍历状态机。
3. `docs/xv6-tutorial/resources/foundation/pointer-list.c:has_flag`：用掩码测试状态。
4. `kernel/types.h:uint64`：xv6 使用的无符号 64 位地址整数类型。
5. `user/ulib.c:memmove`：根据源/目标地址关系选择复制方向的真实例子。

在 `memmove` 中只回答两个问题：为什么参数类型是指针；当源和目标区域重叠时，复制方向为什么重要。不要在本单元展开用户运行库的其余函数。

## 观察任务

复制通用[源码追踪工作表](../templates/trace-worksheet.md)，为 `sum(&head)` 写三行状态：进入函数、从 `head` 前进到 `tail`、从 `tail` 前进到 `NULL`。每行至少记录当前 `node` 指向谁、读取的值、flags 和下一个地址。另写出 `ACTIVE`、`PINNED`、`ACTIVE|PINNED` 分别与 `NODE_PINNED` 执行 `&` 后的结果。

再画一个长度为 2 的 `struct node nodes[2]`：标出数组整体边界、`nodes[0]`、`nodes[1]` 以及紧邻边界外但不可访问的 `nodes[2]` 位置。图只表达相对布局，不猜测具体宿主地址。

用宿主调试器观察资源程序：

```sh
c_memory_tmp=$(mktemp -d)
cc -std=c99 -Wall -Wextra -g \
  docs/xv6-tutorial/resources/foundation/pointer-list.c \
  -o "$c_memory_tmp/pointer-list"
gdb-multiarch -q -nx "$c_memory_tmp/pointer-list"
```

在 `main` 和 `sum` 设置断点，打印 `head`、`tail`、`node` 及其地址。若系统没有 `gdb-multiarch`，记录缺失工具并先补齐环境，不用猜测输出。

GDB 在函数入口停下时，源码当前行通常还没有执行。`break main` 命中后，用 `next` 前进到当前行已经越过 `head` 初始化、即将调用 `printf` 时，再打印 `head` 和 `tail`。`break sum` 命中后，用 `next` 前进到当前行是 `total += node->value` 时，再打印 `node` 和 `*node`；继续一次循环后重复打印。若变量显示为未初始化或“不在当前上下文”，先核对当前行，不要把该值写入状态表。

## 有界修改任务

新建独立临时目录并把两个 foundation 资源复制进去，只修改临时副本：

```sh
cp docs/xv6-tutorial/resources/foundation/pointer-list.c \
  docs/xv6-tutorial/resources/foundation/check-pointer-list.sh \
  "$c_memory_tmp/"
```

在 `main` 中增加一个值为 `11`、初始只有 `NODE_ACTIVE` 的中间节点，使链变成 `head -> middle -> tail -> NULL`，并把输出中的 count 改为 3。设置 `middle.flags |= NODE_PINNED` 后，临时检查脚本的精确预期是 `count=3 sum=23 active=3 pinned=2`；再用 `middle.flags &= ~NODE_PINNED` 清除标志后，精确预期是 `count=3 sum=23 active=3 pinned=1`。两次都必须让临时检查脚本退出 `0`。

完成后再画出以下三个反例，但不要依赖未定义行为的实际输出作为 oracle：

1. 调用 `has_flag(NULL, NODE_ACTIVE)`：第一次非法访问发生在 `node->flags`，因为解引用前置条件被破坏。
2. 对 `struct node nodes[2]` 访问 `nodes[2]`：有效下标只有 0 和 1，第一次数组访问已经越界。
3. 返回一个已经离开作用域的局部节点地址：函数返回后对象生命周期结束，随后解引用该地址属于悬空指针访问。

另画出 `tail.next = &head` 形成的环，说明它破坏“遍历最终到达 `NULL`”的不变量，但不要把 watchdog timeout 当作正确性 oracle。

## Oracle、证据、失败路径和局限

- `S`：内存图中的每条箭头都对应一个具体 `next` 字段，且生命周期说明完整。
- `S`：位标志真值表能分别说明 `<<`、`|`、`&`、`~` 的输入和结果，且操作一个 bit 不改变另一个 bit。
- `F`：三节点版本输出精确 count、sum、active 和 pinned，检查脚本退出 `0`。
- `B`：能定位空指针、数组越界和悬空指针的首个非法访问，并解释环破坏哪个终止不变量。
- 单次成功运行不能证明没有越界或未定义行为；后续可使用 sanitizer 补充证据，但 sanitizer 也不是证明。

## 退出产物与后续单元

提交三节点内存图、两元素数组边界图、位标志真值表、set/test/clear 两次观察、一次 `sum` 状态表和三个失败场景的首个非法访问位置。缺少任一箭头的生命周期或数组边界、改变目标 bit 时误改其他 bit、三节点精确输出不符，或不能定位空指针/越界/悬空访问，都表示本单元未通过。先用 `test -n "$c_memory_tmp"` 和 `test -d "$c_memory_tmp"` 限定目标，再用 `rm -r -- "$c_memory_tmp"` 删除临时目录；确认仓库检查脚本仍通过后，进入 [从 C 调用栈到基础 RISC-V](machine-and-riscv.md)。
