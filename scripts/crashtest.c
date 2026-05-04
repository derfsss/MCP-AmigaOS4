/* Deliberate-crash test fixture for the MCPd debug.exception hook.
 *
 * Cross-compile via the same Docker image used for MCPd:
 *   ppc-amigaos-gcc -o crashtest crashtest.c
 *
 * Running it dereferences NULL, raising ACPU_AddressErr / DSI on
 * PPC. The MCPd crash hook should fire and emit a JSON-RPC
 * `events.notify` for topic `debug.exception` with the captured
 * register state.
 */

#include <stdio.h>

int main(void) {
    volatile int *p = (int *)0x4;  /* unmapped low address */
    printf("crashtest: about to fault\n");
    *p = 0xDEADBEEF;
    printf("crashtest: not reached\n");
    return 0;
}
