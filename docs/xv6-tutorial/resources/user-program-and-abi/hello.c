#include "kernel/types.h"
#include "user/user.h"

int
main(int argc, char **argv)
{
  (void)argc;
  (void)argv;
  printf("hello-from-xv6\n");
  return 0;
}
