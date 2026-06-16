/* wb.screenshot - capture a screen to a PNG file on the target.
 *
 * Why this exists: qemu.screenshot (host-side, QMP screendump) only
 * works for QEMU guests. This method runs inside MCPd on the Amiga
 * itself, so it captures the framebuffer of a REAL X5000 / A1222 as
 * well as a QEMU guest.
 *
 * Pipeline:
 *   1. Find the target Screen (frontmost, or by index) under
 *      LockIBase, read its geometry.
 *   2. IGraphics->ReadPixelArray() the RastPort into a chunky
 *      24-bit RGB buffer (PIXF_R8G8B8). graphics.library converts
 *      from whatever the screen depth / RTG format happens to be.
 *   3. PNG-encode that buffer ourselves: filtered scanlines deflated
 *      via z.library (the same shared zlib MCPd already opens for
 *      fs.c), framed into IHDR / IDAT / IEND chunks with CRC32.
 *
 * AmigaOS 4.1 ships no PNG *writer* datatype (picture.datatype's
 * DTM_WRITE only emits ILBM; warppng.datatype is decode-only), so we
 * encode the PNG directly rather than going through datatypes.library.
 *
 * IGraphics is auto-opened by -lauto. intuition.library is opened
 * per-call (same idiom as wb.c). z.library (IZ) is opened once in
 * main.c and shared via extern.
 */

#include "../rpc.h"
#include "methods.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <proto/exec.h>
#include <proto/dos.h>
#include <proto/graphics.h>
#include <proto/intuition.h>
#include <proto/z.h>
#include <intuition/intuition.h>
#include <intuition/intuitionbase.h>
#include <graphics/gfx.h>
#include <exec/memory.h>
#include <libraries/z.h>

/* Shared zlib interface, opened in main.c. NULL if z.library v53+
 * was unavailable at startup; PNG encoding then returns NOTCAPABLE. */
extern struct ZIFace *IZ;

/* Safety ceiling on the raw (filtered) image buffer. 64 MiB covers a
 * 4096x4096 truecolor screen; anything larger is refused rather than
 * triggering an arbitrary multi-MiB allocation spike. */
#define SHOT_RAW_MAX (64u * 1024u * 1024u)

#define SHOT_DEFAULT_PATH "T:mcpd-shot.png"


static void be32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)(v);
}


/* Write one PNG chunk: length(BE) + type + data + CRC(BE). The CRC
 * covers type+data (not the length), per the PNG spec. z.library's
 * CRC32 is the same CRC-32 PNG uses (inversion handled internally).
 * Returns 0 on success, -1 on a short write. */
static int png_chunk(BPTR fh, const char *type,
                     const uint8_t *data, uint32_t len) {
    uint8_t hdr[4];
    be32(hdr, len);
    if (IDOS->Write(fh, hdr, 4) != 4) return -1;
    if (IDOS->Write(fh, (APTR)type, 4) != 4) return -1;
    if (len && IDOS->Write(fh, (APTR)data, (int32)len) != (int32)len) return -1;

    uint32_t crc = IZ->CRC32(0, NULL, 0);
    crc = IZ->CRC32(crc, (const uint8 *)type, 4);
    if (len) crc = IZ->CRC32(crc, data, len);
    uint8_t crcbe[4];
    be32(crcbe, crc);
    if (IDOS->Write(fh, crcbe, 4) != 4) return -1;
    return 0;
}


/* ---- wb.screenshot ----------------------------------------------- */

int wb_screenshot(cJSON *params, cJSON **out_result, cJSON **out_err) {
    if (!IZ) {
        *out_err = rpc_make_error(MCPD_ERR_NOTCAPABLE,
            "z.library v53+ required for PNG encoding", NULL);
        return 0;
    }

    int screen_index = (int)p_int(params, "screen_index", 0);
    if (screen_index < 0) screen_index = 0;

    const char *path = SHOT_DEFAULT_PATH;
    if (cJSON_IsObject(params)) {
        cJSON *pj = cJSON_GetObjectItemCaseSensitive(params, "path");
        if (cJSON_IsString(pj) && pj->valuestring && pj->valuestring[0])
            path = pj->valuestring;
    }

    /* Open intuition for the screen-list walk. */
    struct Library *ibase = IExec->OpenLibrary("intuition.library", 50);
    if (!ibase) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "OpenLibrary intuition.library v50 failed", NULL);
        return 0;
    }
    struct IntuitionIFace *ii = (struct IntuitionIFace *)
        IExec->GetInterface(ibase, "main", 1, NULL);
    if (!ii) {
        IExec->CloseLibrary(ibase);
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "GetInterface(intuition main) failed", NULL);
        return 0;
    }

    /* Phase 1: locate the screen and read its geometry under the lock,
     * but do NOT allocate or capture while holding LockIBase. */
    struct IntuitionBase *ib = (struct IntuitionBase *)ibase;
    struct Screen *target = NULL;
    int width = 0, height = 0, depth = 0;

    ULONG lock = ii->LockIBase(0);
    int idx = 0;
    for (struct Screen *s = ib->FirstScreen; s != NULL; s = s->NextScreen) {
        if (idx == screen_index) {
            target = s;
            width  = (int)s->Width;
            height = (int)s->Height;
            depth  = (int)IGraphics->GetBitMapAttr(s->RastPort.BitMap, BMA_DEPTH);
            break;
        }
        idx++;
    }
    ii->UnlockIBase(lock);

    if (!target || width <= 0 || height <= 0) {
        IExec->DropInterface((struct Interface *)ii);
        IExec->CloseLibrary(ibase);
        cJSON *data = cJSON_CreateObject();
        if (data) cJSON_AddNumberToObject(data, "screen_index",
                                          (double)screen_index);
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "no such screen", data);
        return 0;
    }

    /* PNG truecolor (color type 2): one filter byte then RGB triplets
     * per row. Lay the rows out so ReadPixelArray writes pixels right
     * after each row's filter byte; the filter bytes are set to 0
     * (None) explicitly below. */
    uint32_t rowstride = 1u + (uint32_t)width * 3u;
    uint64_t rawlen64  = (uint64_t)rowstride * (uint64_t)height;
    if (rawlen64 > SHOT_RAW_MAX) {
        IExec->DropInterface((struct Interface *)ii);
        IExec->CloseLibrary(ibase);
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "screen too large to capture", NULL);
        return 0;
    }
    uint32_t rawlen = (uint32_t)rawlen64;

    /* No AVT_Clear here: AVT_Clear is AVT_ClearWithValue (the tag data
     * is the FILL BYTE, not a boolean), so AVT_Clear,TRUE would fill
     * with 0x01 and corrupt the PNG filter bytes. ReadPixelArray fills
     * every pixel; the filter bytes are zeroed explicitly post-capture. */
    uint8_t *raw = (uint8_t *)IExec->AllocVecTags((uint32)rawlen,
        AVT_Type, MEMF_SHARED, TAG_END);
    if (!raw) {
        IExec->DropInterface((struct Interface *)ii);
        IExec->CloseLibrary(ibase);
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "out of memory for capture buffer", NULL);
        return 0;
    }

    /* Phase 2: re-lock, confirm the screen is still present at the same
     * pointer + geometry (it could have closed between phases), then
     * capture under the lock so the bitmap can't be freed mid-read. */
    int captured = 0;
    lock = ii->LockIBase(0);
    idx = 0;
    for (struct Screen *s = ib->FirstScreen; s != NULL; s = s->NextScreen) {
        if (idx == screen_index) {
            if (s == target && (int)s->Width == width &&
                (int)s->Height == height) {
                IGraphics->ReadPixelArray(&s->RastPort, 0, 0,
                    raw + 1, 0, 0, rowstride, PIXF_R8G8B8,
                    (uint32)width, (uint32)height);
                captured = 1;
            }
            break;
        }
        idx++;
    }
    ii->UnlockIBase(lock);

    IExec->DropInterface((struct Interface *)ii);
    IExec->CloseLibrary(ibase);

    if (!captured) {
        IExec->FreeVec(raw);
        *out_err = rpc_make_error(MCPD_ERR_BUSY,
            "screen changed during capture; retry", NULL);
        return 0;
    }

    /* Set every scanline's filter byte to 0 (None). ReadPixelArray
     * only wrote the pixel bytes (raw+1 onward at rowstride pitch),
     * leaving the leading byte of each row uninitialised. */
    for (uint32_t r = 0; r < (uint32_t)height; r++)
        raw[(size_t)r * rowstride] = 0;

    /* Phase 3a: deflate the filtered scanlines into a zlib stream
     * (exactly what a PNG IDAT chunk carries). */
    z_stream zs;
    memset(&zs, 0, sizeof(zs));
    if (IZ->DeflateInit(&zs, 6) != Z_OK) {
        IExec->FreeVec(raw);
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "DeflateInit failed", NULL);
        return 0;
    }
    uint32_t bound = IZ->DeflateBound(&zs, rawlen);
    uint8_t *comp = (uint8_t *)IExec->AllocVecTags((uint32)bound,
        AVT_Type, MEMF_SHARED, TAG_END);
    if (!comp) {
        IZ->DeflateEnd(&zs);
        IExec->FreeVec(raw);
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "out of memory for compressed buffer", NULL);
        return 0;
    }
    zs.next_in   = raw;
    zs.avail_in  = rawlen;
    zs.next_out  = comp;
    zs.avail_out = bound;
    int rc = IZ->Deflate(&zs, Z_FINISH);
    uint32_t complen = (uint32_t)zs.total_out;
    IZ->DeflateEnd(&zs);
    IExec->FreeVec(raw);

    if (rc != Z_STREAM_END) {
        IExec->FreeVec(comp);
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "zlib Deflate failed", NULL);
        return 0;
    }

    /* Phase 3b: write the PNG file. */
    BPTR fh = IDOS->Open(path, MODE_NEWFILE);
    if (!fh) {
        IExec->FreeVec(comp);
        *out_err = target_error("cannot open output path for writing", path);
        return 0;
    }

    static const uint8_t sig[8] =
        { 137, 80, 78, 71, 13, 10, 26, 10 };
    uint8_t ihdr[13];
    be32(ihdr + 0, (uint32_t)width);
    be32(ihdr + 4, (uint32_t)height);
    ihdr[8]  = 8;   /* bit depth */
    ihdr[9]  = 2;   /* color type: truecolor RGB */
    ihdr[10] = 0;   /* compression: deflate */
    ihdr[11] = 0;   /* filter method: adaptive (we use filter 0/None) */
    ihdr[12] = 0;   /* interlace: none */

    int werr = 0;
    if (IDOS->Write(fh, (APTR)sig, 8) != 8) werr = 1;
    if (!werr && png_chunk(fh, "IHDR", ihdr, sizeof(ihdr)) != 0) werr = 1;
    if (!werr && png_chunk(fh, "IDAT", comp, complen) != 0) werr = 1;
    if (!werr && png_chunk(fh, "IEND", NULL, 0) != 0) werr = 1;

    IDOS->Close(fh);
    IExec->FreeVec(comp);

    if (werr) {
        IDOS->Delete(path);
        *out_err = target_error("write error while saving PNG", path);
        return 0;
    }

    /* Total file size: 8-byte signature + each chunk's 12 bytes of
     * framing (length+type+CRC) plus its payload. */
    uint64_t bytes = 8
        + (12 + 13)
        + (12 + (uint64_t)complen)
        + (12 + 0);

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "path", path);
    cJSON_AddStringToObject(r, "format", "png");
    cJSON_AddNumberToObject(r, "width", (double)width);
    cJSON_AddNumberToObject(r, "height", (double)height);
    cJSON_AddNumberToObject(r, "depth", (double)depth);
    cJSON_AddNumberToObject(r, "screen_index", (double)screen_index);
    cJSON_AddNumberToObject(r, "bytes", (double)bytes);
    *out_result = r;
    return 0;
}
