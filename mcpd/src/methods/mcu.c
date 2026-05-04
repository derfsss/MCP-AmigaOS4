/* sys.mcu_cmd - talk to the Cyrus MCU via UART1 (serial.device unit 1).
 *
 * The Cyrus board's MCU is a serial supervisor reachable via the SoC's
 * second UART at 38400 8N1, using a documented '#cmd\n / $reply\n'
 * ASCII protocol (TRM Cyrus 1.1.1 sec 8.1). MCU commands include:
 *   #t  -> 3 temperatures (PCB, CPU, PCIe-switch)
 *   #v  -> 13 voltage rails
 *   #f  -> fan PWM + RPM
 *   #s  -> power off
 *
 * The canonical sensor path on X5000: talks the documented MCU
 * protocol directly via AOS's serial.device. No closed-source
 * dependency.
 *
 * Default unit is 1 (UART1). The user can override via params if their
 * AOS install enumerates the MCU UART under a different unit number.
 */

#include "../rpc.h"
#include "methods.h"

#include <stdio.h>
#include <string.h>

#include <proto/exec.h>
#include <devices/serial.h>
#include <devices/timer.h>
#include <exec/io.h>
#include <exec/ports.h>


#define MCU_BAUD 38400
#define MCU_TIMEOUT_S 5
#define MCU_RX_MAX 256
#define MCU_TX_MAX 32


int sys_mcu_cmd(cJSON *params, cJSON **out_result, cJSON **out_err) {
    cJSON *cmd_err = NULL;
    const char *cmd = p_str(params, "cmd", &cmd_err);
    if (!cmd) {
        *out_err = cmd_err;
        return 0;
    }
    size_t cmd_len = strlen(cmd);
    if (cmd_len == 0 || cmd_len > 16) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "cmd must be 1..16 chars (e.g. \"t\", \"v\", \"f\")", NULL);
        return 0;
    }

    /* Gate on '#s' (POWER OFF) -- requires confirm:true. Same pattern
     * as sys.cold_reboot. Defense in depth: catch even raw JSON-RPC
     * callers that bypass the typed host wrapper. */
    if (strcmp(cmd, "s") == 0) {
        cJSON *confirm = cJSON_GetObjectItemCaseSensitive(params, "confirm");
        if (!cJSON_IsBool(confirm) || !cJSON_IsTrue(confirm)) {
            *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                "sys.mcu_cmd cmd=\"s\" (board POWER OFF) requires confirm:true",
                NULL);
            return 0;
        }
    }

    long long unit = p_int(params, "unit", 1);
    if (unit < 0 || unit > 7) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "unit out of range [0..7]", NULL);
        return 0;
    }

    struct MsgPort     *ser_port = NULL;
    struct MsgPort     *tim_port = NULL;
    struct IOExtSer    *ser_io   = NULL;
    struct TimeRequest *tim_io   = NULL;
    int ser_open = 0, tim_open = 0;

    char rx[MCU_RX_MAX];
    int rx_len = 0;
    int timed_out = 0;
    int aborted = 0;

    ser_port = (struct MsgPort *)IExec->AllocSysObjectTags(ASOT_PORT, TAG_END);
    tim_port = (struct MsgPort *)IExec->AllocSysObjectTags(ASOT_PORT, TAG_END);
    if (!ser_port || !tim_port) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "AllocSysObject(MsgPort) failed", NULL);
        goto cleanup;
    }

    ser_io = (struct IOExtSer *)IExec->AllocSysObjectTags(ASOT_IOREQUEST,
        ASOIOR_Size,      sizeof(struct IOExtSer),
        ASOIOR_ReplyPort, ser_port,
        TAG_END);
    tim_io = (struct TimeRequest *)IExec->AllocSysObjectTags(ASOT_IOREQUEST,
        ASOIOR_Size,      sizeof(struct TimeRequest),
        ASOIOR_ReplyPort, tim_port,
        TAG_END);
    if (!ser_io || !tim_io) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "AllocSysObject(IORequest) failed", NULL);
        goto cleanup;
    }

    if (IExec->OpenDevice("serial.device", (uint32)unit,
                          (struct IORequest *)ser_io, 0) != 0) {
        cJSON *data = cJSON_CreateObject();
        if (data) cJSON_AddNumberToObject(data, "unit", (double)unit);
        *out_err = rpc_make_error(MCPD_ERR_NOTCAPABLE,
            "OpenDevice(serial.device, unit) failed - try different unit",
            data);
        goto cleanup;
    }
    ser_open = 1;

    if (IExec->OpenDevice("timer.device", UNIT_VBLANK,
                          (struct IORequest *)tim_io, 0) != 0) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "OpenDevice(timer.device) failed", NULL);
        goto cleanup;
    }
    tim_open = 1;

    /* Match the working reference (kas1e's x5k_temp_no_serdes_working.c):
     * configure params AFTER OpenDevice, AND clear SERF_PARTY_ON +
     * SET SERF_XDISABLED. Without SERF_XDISABLED, XON/XOFF flow
     * control is ENABLED -- our writes block waiting for an XON the
     * MCU never sends. */
    ser_io->IOSer.io_Command = SDCMD_SETPARAMS;
    ser_io->io_Baud     = MCU_BAUD;
    ser_io->io_ReadLen  = 8;
    ser_io->io_WriteLen = 8;
    ser_io->io_StopBits = 1;
    ser_io->io_SerFlags &= ~SERF_PARTY_ON;
    ser_io->io_SerFlags |=  SERF_XDISABLED;
    IExec->DoIO((struct IORequest *)ser_io);
    if (ser_io->IOSer.io_Error) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "SDCMD_SETPARAMS failed", NULL);
        goto cleanup;
    }

    /* Build TX buffer: "#<cmd>\n" */
    char tx[MCU_TX_MAX];
    int tx_len = snprintf(tx, sizeof(tx), "#%s\n", cmd);

    /* Send synchronously - typically <= 32 bytes; serial.device
     * buffers it. */
    ser_io->IOSer.io_Command = CMD_WRITE;
    ser_io->IOSer.io_Data    = tx;
    ser_io->IOSer.io_Length  = tx_len;
    IExec->DoIO((struct IORequest *)ser_io);
    if (ser_io->IOSer.io_Error) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "serial CMD_WRITE failed", NULL);
        goto cleanup;
    }

    /* Read reply with timeout. Pattern: SendIO async read, set up timer,
     * Wait on either signal, AbortIO whichever didn't fire. Repeat 1
     * byte at a time until '\n' or rx-buffer full. */
    tim_io->Request.io_Command = TR_ADDREQUEST;
    tim_io->Time.Seconds       = MCU_TIMEOUT_S;
    tim_io->Time.Microseconds  = 0;
    IExec->SendIO((struct IORequest *)tim_io);

    uint32 ser_sig = 1U << ser_port->mp_SigBit;
    uint32 tim_sig = 1U << tim_port->mp_SigBit;

    while (rx_len < MCU_RX_MAX - 1) {
        ser_io->IOSer.io_Command = CMD_READ;
        ser_io->IOSer.io_Data    = rx + rx_len;
        ser_io->IOSer.io_Length  = 1;
        IExec->SendIO((struct IORequest *)ser_io);

        uint32 got = IExec->Wait(ser_sig | tim_sig);

        if (got & tim_sig) {
            /* Timer fired first - abort the pending read. */
            IExec->AbortIO((struct IORequest *)ser_io);
            IExec->WaitIO((struct IORequest *)ser_io);
            timed_out = 1;
            /* Drain timer reply. */
            IExec->WaitIO((struct IORequest *)tim_io);
            break;
        }

        /* Serial completed first. Reap it. */
        IExec->WaitIO((struct IORequest *)ser_io);
        if (ser_io->IOSer.io_Actual == 0 || ser_io->IOSer.io_Error) {
            aborted = 1;
            break;
        }
        rx_len += (int)ser_io->IOSer.io_Actual;
        if (rx[rx_len - 1] == 0x0A) break;  /* '\n' terminator */
    }
    rx[rx_len] = 0;

    /* Cancel timer if still pending. */
    if (!timed_out) {
        IExec->AbortIO((struct IORequest *)tim_io);
        IExec->WaitIO((struct IORequest *)tim_io);
    }

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "cmd", cmd);
    cJSON_AddNumberToObject(r, "unit", (double)unit);
    cJSON_AddNumberToObject(r, "baud", (double)MCU_BAUD);
    cJSON_AddNumberToObject(r, "tx_len", (double)tx_len);
    cJSON_AddStringToObject(r, "reply", rx);
    cJSON_AddNumberToObject(r, "reply_len", (double)rx_len);
    cJSON_AddBoolToObject(r, "timed_out", timed_out);
    cJSON_AddBoolToObject(r, "aborted",  aborted);
    *out_result = r;

cleanup:
    if (ser_open) IExec->CloseDevice((struct IORequest *)ser_io);
    if (tim_open) IExec->CloseDevice((struct IORequest *)tim_io);
    if (ser_io)   IExec->FreeSysObject(ASOT_IOREQUEST, ser_io);
    if (tim_io)   IExec->FreeSysObject(ASOT_IOREQUEST, tim_io);
    if (ser_port) IExec->FreeSysObject(ASOT_PORT, ser_port);
    if (tim_port) IExec->FreeSysObject(ASOT_PORT, tim_port);
    return 0;
}
