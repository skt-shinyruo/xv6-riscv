#include <stdio.h>

enum node_flag {
  NODE_ACTIVE = 1u << 0,
  NODE_PINNED = 1u << 1,
};

struct node {
  int value;
  unsigned flags;
  struct node *next;
};

static int
has_flag(const struct node *node, unsigned flag)
{
  return (node->flags & flag) != 0;
}

static int
sum(const struct node *head)
{
  int total = 0;

  for (const struct node *node = head; node != NULL; node = node->next)
    total += node->value;
  return total;
}

static int
count_with_flag(const struct node *head, unsigned flag)
{
  int count = 0;

  for (const struct node *node = head; node != NULL; node = node->next)
    if (has_flag(node, flag))
      count++;
  return count;
}

int
main(void)
{
  struct node tail = {7, NODE_ACTIVE | NODE_PINNED, NULL};
  struct node head = {5, NODE_ACTIVE, &tail};

  printf("count=2 sum=%d active=%d pinned=%d\n",
         sum(&head),
         count_with_flag(&head, NODE_ACTIVE),
         count_with_flag(&head, NODE_PINNED));
  return 0;
}
