/* discovery.h - LAN-local UDP discovery responder for MCPd.
 *
 * Spawned as a peer task at MCPd startup via CreateNewProcTags.
 * Listens on UDP 4323, replies to JSON probes from the host's
 * fleet.discover tool with a JSON announcement carrying server
 * version, advertised method count, hostname, and TCP port.
 *
 */
#ifndef MCPD_DISCOVERY_H
#define MCPD_DISCOVERY_H

#include <stdint.h>

/* Default UDP port we listen on. */
#define MCPD_DISCOVERY_PORT 4323

/* Spawn the discovery responder task. Returns 0 on success.
 * Best-effort: discovery failures don't take MCPd down.
 *
 * `tcp_port` is the port the RPC listener actually bound, which is
 * what the announcement reports. It is a parameter rather than a
 * constant because --port exists: a daemon that announces 4322 while
 * listening elsewhere sends clients to an endpoint that either
 * refuses the connection or, if something else is on 4322 at that
 * address, belongs to a different machine entirely. */
int discovery_start(int methods_advertised, uint16_t tcp_port);

#endif /* MCPD_DISCOVERY_H */
