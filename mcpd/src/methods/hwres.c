/* sys.hardware.* extras: I2C bus enumeration + CPU perf counters.
 *
 * These touch hardware resources (i2c.resource, performancemonitor.resource)
 * that aren't always present - probe before opening, return empty
 * arrays when missing rather than hard-failing the call. */

#include "../rpc.h"
#include "methods.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <proto/exec.h>

#include <interfaces/i2c.h>
#include <resources/performancemonitor.h>
#include <interfaces/performancemonitor.h>


/* ---- sys.hardware.i2c ----------------------------------------- */

static const struct {
    uint32_t addr;
    const char *hint;
} _i2c_hints[] = {
    { 0x50, "EEPROM/SPD-0" },
    { 0x51, "EEPROM/SPD-1" },
    { 0x52, "EEPROM/SPD-2" },
    { 0x53, "EEPROM/SPD-3" },
    { 0x68, "RTC/DS1338" },
    { 0x4c, "Temp/LM75" },
    { 0x4d, "Temp/LM75" },
    { 0x4e, "Temp/LM75" },
    { 0x2f, "PWMon/ADM1024" },
    { 0,    NULL },
};

static const char *_i2c_hint(uint32_t addr) {
    for (int i = 0; _i2c_hints[i].hint; i++) {
        if (_i2c_hints[i].addr == addr) return _i2c_hints[i].hint;
    }
    return NULL;
}


int sys_hardware_i2c(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)out_err;

    /* Optional `addr_low` / `addr_high` to narrow the probe range.
     * Default is 0x08 .. 0x77 (the standard 7-bit range, skipping
     * reserved 0..7 + 120..127). */
    long long lo_ll = p_int(params, "addr_low", 0x08);
    long long hi_ll = p_int(params, "addr_high", 0x77);
    if (lo_ll < 0)   lo_ll = 0;
    if (hi_ll > 127) hi_ll = 127;
    uint32_t lo = (uint32_t)lo_ll, hi = (uint32_t)hi_ll;

    cJSON *r = cJSON_CreateObject();
    cJSON *buses = cJSON_AddArrayToObject(r, "buses");

    APTR i2c_res = IExec->OpenResource("i2c.resource");
    if (!i2c_res) {
        cJSON_AddBoolToObject(r, "available", 0);
        *out_result = r;
        return 0;
    }
    cJSON_AddBoolToObject(r, "available", 1);

    struct I2CResourceIFace *I2CRes =
        (struct I2CResourceIFace *)IExec->GetInterface(
            (struct Library *)i2c_res, "main", 1, NULL);
    if (!I2CRes) {
        cJSON_AddStringToObject(r, "error",
                                "GetInterface(i2c.resource, 'main')");
        *out_result = r;
        return 0;
    }

    /* Walk numbered buses 0..7 (most boards expose 1-2). */
    for (uint32_t bn = 0; bn < 8; bn++) {
        struct I2CIFace *bus = I2CRes->GetBusByNumber(bn);
        if (!bus) continue;

        cJSON *bj = cJSON_CreateObject();
        cJSON_AddNumberToObject(bj, "bus_number", (double)bn);
        STRPTR bname = bus->GetName();
        if (bname) cJSON_AddStringToObject(bj, "name", bname);

        cJSON *devs = cJSON_AddArrayToObject(bj, "devices");
        bus->Lock();
        for (uint32_t a = lo; a <= hi; a++) {
            if (bus->Probe(a)) {
                cJSON *d = cJSON_CreateObject();
                cJSON_AddNumberToObject(d, "addr", (double)a);
                /* GetDeviceId returns vendor/device on 0x50-style
                 * eeproms; safe to call. */
                uint32_t id = bus->GetDeviceId(a);
                if (id != 0 && id != 0xffffffff) {
                    cJSON_AddNumberToObject(d, "device_id", (double)id);
                }
                const char *h = _i2c_hint(a);
                if (h) cJSON_AddStringToObject(d, "hint", h);
                cJSON_AddItemToArray(devs, d);
            }
        }
        bus->Unlock();
        bus->Release();
        cJSON_AddItemToArray(buses, bj);
    }

    IExec->DropInterface((struct Interface *)I2CRes);
    *out_result = r;
    return 0;
}


/* ---- sys.hardware.perfcounters -------------------------------- */

/* Mapping from PMCI_* item id to a friendly key. */
static const char *_pmci_name(uint32_t i) {
    switch (i) {
    case PMCI_Hold:           return "hold";
    case PMCI_CPUCycles:      return "cpu_cycles";
    case PMCI_Instr:          return "instructions";
    case PMCI_FPUInstr:       return "fpu_instructions";
    case PMCI_Transition:     return "rtc_transitions";
    case PMCI_InstrDisp:      return "instr_dispatched";
    case PMCI_EIEIO:          return "eieio";
    case PMCI_SYNC:           return "sync";
    case PMCI_L1DCacheHits:   return "l1d_hits";
    case PMCI_L1ICacheHits:   return "l1i_hits";
    case PMCI_L2DCacheHits:   return "l2d_hits";
    case PMCI_L2ICacheHits:   return "l2i_hits";
    case PMCI_L1DCacheMiss:   return "l1d_miss";
    case PMCI_L1ICacheMiss:   return "l1i_miss";
    case PMCI_L2DCacheMiss:   return "l2d_miss";
    case PMCI_L2ICacheMiss:   return "l2i_miss";
    case PMCI_L2Hits:         return "l2_hits";
    case PMCI_L1LoadThresh:   return "l1_load_thresh";
    case PMCI_ValidEA:        return "valid_ea";
    case PMCI_UnresolvedBra:  return "unresolved_bra";
    case PMCI_InstrBreak:     return "instr_break";
    case PMCI_DataBreak:      return "data_break";
    default:                  return "unknown";
    }
}


int sys_hardware_perfcounters(cJSON *params,
                              cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;
    cJSON *r = cJSON_CreateObject();

    APTR pmres = IExec->OpenResource("performancemonitor.resource");
    if (!pmres) {
        cJSON_AddBoolToObject(r, "available", 0);
        *out_result = r;
        return 0;
    }
    cJSON_AddBoolToObject(r, "available", 1);

    struct PerformanceMonitorIFace *PM =
        (struct PerformanceMonitorIFace *)IExec->GetInterface(
            (struct Library *)pmres, "main", 1, NULL);
    if (!PM) {
        cJSON_AddStringToObject(r, "error", "GetInterface failed");
        *out_result = r;
        return 0;
    }

    uint32_t ncounters = PM->Query(PMQI_NumCounters);
    cJSON_AddNumberToObject(r, "counter_count", (double)ncounters);
    cJSON_AddBoolToObject(r, "instr_breakpoint",
                          PM->Query(PMQI_IBreakPoint) ? 1 : 0);
    cJSON_AddBoolToObject(r, "breakpoint_mask",
                          PM->Query(PMQI_BreakPointMask) ? 1 : 0);

    /* For each physical counter, ask which PMCI item it's tracking
     * (CounterMatch) and read the live value (CounterGet). */
    cJSON *counters = cJSON_AddArrayToObject(r, "counters");
    for (uint32_t i = 0; i < ncounters && i < 16; i++) {
        cJSON *c = cJSON_CreateObject();
        cJSON_AddNumberToObject(c, "index", (double)i);
        uint32_t item = PM->CounterMatch(i);
        cJSON_AddNumberToObject(c, "item_id", (double)item);
        cJSON_AddStringToObject(c, "item", _pmci_name(item));
        uint32_t v = PM->CounterGet(i);
        cJSON_AddNumberToObject(c, "value", (double)v);
        cJSON_AddItemToArray(counters, c);
    }

    IExec->DropInterface((struct Interface *)PM);
    *out_result = r;
    return 0;
}
