"""Transports.

- mcpd.py           - JSON-RPC client over framed TCP (the only
                      in-Amiga transport from phase 4 onwards).
- qmp.py            - QMP wrapper around qemu-runner (phase 2).
- gdb.py            - GDB Remote Serial Protocol client for QEMU's
                      `-gdb` stub.
- discovery.py      - UDP LAN discovery of MCPd instances.
- p18.py            - host-side MCU debug shell over the FTDI USB-TTL
                      cable (X5000 P18 / A1222 P15), used by power.*.
- serial_capture.py - host-side capture of a target's serial ports
                      (rear DB9 U-Boot / kernel debug, MCU UART).
"""
