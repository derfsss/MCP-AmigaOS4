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

/* Default UDP port we listen on. */
#define MCPD_DISCOVERY_PORT 4323

/* Spawn the discovery responder task. Returns 0 on success.
 * Best-effort: discovery failures don't take MCPd down. */
int discovery_start(int methods_advertised);

#endif /* MCPD_DISCOVERY_H */
