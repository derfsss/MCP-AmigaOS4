# Running QEMU guests (`config.json`)

`qemu.start` boots a QEMU guest from a per-machine JSON file, named by
`qemu_config` in your target's `config.toml` block. This page covers
setting up an emulated AmigaOS 4.1 machine for amiga-fleet-mcp, running
it from day to day, what the file contains, and how the server turns
it into a QEMU command line.

## Start with Kyvos

**Set up your emulated AmigaOS 4 machine with
[Kyvos](https://ko-fi.com/s/6476fdadd2) first.** Kyvos is a QEMU front
end for AmigaOS 4. It downloads the files a machine needs, creates the
disk image, takes you through installing AmigaOS 4.1 Final Edition,
and writes a `config.json` for every machine in the machine's folder.
That `config.json` is exactly the file amiga-fleet-mcp reads, so with
Kyvos you never have to write one yourself.

You can write a config by hand (see
[Writing a config by hand](#writing-a-config-by-hand)), but then you
also have to find and put together the boot loader, Kickstart and disk
image yourself. Do that only if you can't run Kyvos, or want a setup
Kyvos doesn't offer.

### First-time setup, step by step

1. **Install Kyvos and create a machine** for the board you want
   (Pegasos 2, AmigaOne XE or Sam460ex). Install AmigaOS 4.1 FE on it
   from your install CD, plus the updates you use.
2. **Check the guest in Kyvos.** It should boot to Workbench, and
   networking should be up. QEMU's user-mode network answers DHCP, so
   Roadshow's default setup for the emulated rtl8139 card is enough. A
   web browser in the guest loading a website is a good test.
3. **Close the machine in Kyvos.** The server and Kyvos must never run
   the same machine at the same time: both would write to one disk
   image, which corrupts it.
4. **Find two paths:**
   - the machine's `config.json`, in the folder Kyvos created for it;
   - the `qemu-system-ppc` Kyvos runs, stored as `qemuppc` in Kyvos's
     `prefs.json` (`C:\Users\<you>\.kyvos\prefs.json` on Windows,
     `~/.kyvos/prefs.json` elsewhere).
5. **Add the target to `config.toml`**, or run `amiga-fleet-mcp --init`,
   which asks for the same things:

   ```toml
   [paths]
   qemu_binary = "<the qemuppc path from prefs.json>"

   [targets.qemu-pegasos2]
   type        = "qemu"
   machine     = "pegasos2"            # or amigaone / sam460ex
   qemu_config = "<the machine's config.json>"

     [targets.qemu-pegasos2.channels.mcpd]
     endpoint = "127.0.0.1:4422"

     [targets.qemu-pegasos2.channels.qmp]
     endpoint = "127.0.0.1:14422"
   ```

   Don't add port forwards in Kyvos: the server adds the ones it needs
   when it starts the machine (see [What the server adds](#what-the-server-adds)).
6. **Start the machine from the server:**

   ```sh
   python -m amiga_fleet_mcp.cli qemu_start --target qemu-pegasos2
   ```

   or ask your MCP client to run `qemu.start`. The guest's window opens
   as it would from Kyvos. On Windows, allow QEMU through the firewall
   if asked.
7. **Install MCPd in the guest, once.** The server can't talk to the
   guest until the daemon is running in it. The easiest way is the
   release archive: in the guest, download `MCPd-<version>.lha` from
   the [latest release](https://github.com/derfsss/MCP-AmigaOS4/releases/latest),
   then from a Shell:

   ```
   LhA x MCPd-<version>.lha
   Execute MCPd/MCPd-Install
   ```

   This installs MCPd and starts it at every boot. Other ways to get
   the file into the guest, and the host-side installer, are in
   [INSTALL.md](INSTALL.md#installing-mcpd-on-a-target).
8. **Check it:**

   ```sh
   python -m amiga_fleet_mcp.cli qemu_status --target qemu-pegasos2
   amiga-fleet-mcp --doctor
   ```

   `mcpd_reachable=True` means the guest booted, MCPd is running and
   the port forwards work. `--doctor` checks the rest of the setup and
   prints a fix for anything wrong.

From then on, `qemu.start` boots the guest, MCPd starts on its own,
and the other tools (`fs.*`, `exec.*`, `sys.*`, `wb.screenshot`, ...)
work against the target.

## Running guests day to day

- **Stop with `qemu.stop`, not by closing the window.** AmigaOS writes
  its disk cache back lazily, so killing QEMU can lose files written
  in the last few seconds, including ones you just uploaded.
  `qemu.stop` flushes the guest's disks first (`fs.sync`), then shuts
  QEMU down. Don't reset the guest from QEMU, or reboot it from inside,
  either: stop it and start it again.
- **Give the guest a moment after it answers.** MCPd answers before the
  rest of the system has finished starting, and booting under
  emulation is slow. Allow a few seconds after `mcpd_reachable` turns
  true before heavy work.
- **Stop guests you aren't using.** An idle emulated PowerPC still
  keeps a host CPU core busy.
- **One running guest per disk image.** To run two guests at once,
  give each its own machine (copy it in Kyvos) and its own host ports
  in `config.toml`. Two targets with the same ports can't both start.
- **A guest started elsewhere still works.** If you started the
  machine from Kyvos or by hand with the forwards in
  [Starting QEMU yourself](#starting-qemu-yourself), the tools work
  against it and `qemu.stop` can stop it through QMP. The server
  refuses to start a second copy.
- **Headless.** `headless = true` on the target runs the guest with no
  window, for unattended runs. `qemu.screenshot` and `wb.screenshot`
  still work.
- **Snapshots.** `qemu.savevm` / `qemu.loadvm` save and restore the
  whole running machine. They need a qcow2 disk image, which is what
  Kyvos creates.
- **Serial log.** Each start writes the guest's serial output to
  `archive_root/serial-logs/<target>/`. Old logs are pruned by
  `serial_log_keep` and `serial_log_max_total_mb` in `config.toml`.
- **Changing the machine in Kyvos is fine.** The server reads
  `config.json` every time it starts the guest, so settings changed in
  Kyvos apply at the next `qemu.start`. Hand edits to a Kyvos file may
  be overwritten the next time Kyvos saves the machine.
- **Keep the ports private.** QEMU's port forwards listen on every
  network interface of the host, not only `127.0.0.1`, and MCPd has no
  password: anyone who can reach port 4422 on your PC has full control
  of the guest. Make sure your firewall blocks those ports from other
  machines. See [SECURITY.md](SECURITY.md).

## Writing a config by hand

A ready-to-edit template is in
[`host/qemu-config.example.json`](host/qemu-config.example.json).
Kyvos users can skip this section.

1. Copy `host/qemu-config.example.json` into the folder that holds the
   machine's disk image, for example `E:/QEMU/Machines/peg2/config.json`.
2. Replace every `<MACHINE_DIR>` with that folder's absolute path.
   Forward slashes work on Windows too and need no escaping.
3. Make sure the files it names exist: `bboot`, `kickstart.zip` and
   `hd0.qcow2` (see [Files the machine needs](#files-the-machine-needs)).
4. Attach the install CD (see `cd0` in the [Key reference](#key-reference)),
   start QEMU and install AmigaOS 4.1 FE onto `hd0.qcow2`.
5. Carry on from step 5 of
   [First-time setup](#first-time-setup-step-by-step): add the target
   to `config.toml` with `qemu_binary` set to your `qemu-system-ppc`,
   start the guest from the server, install MCPd in it, and check it.

## File structure

The file is a JSON object. The server reads one key from it, `args`:

```json
{
  "args": {
    "<any key>": "<one or more QEMU options, as you would type them>",
    "...": "..."
  }
}
```

Everything outside `args` is ignored. A Kyvos file also has a `params`
object, which holds the state of the Kyvos GUI (the checkboxes and
fields it builds `args` from). The server never reads `params`, and
Kyvos can rewrite it, so don't rely on it, and don't bother adding one
to a file you write by hand.

### How `args` becomes a command line

The server builds `qemu-system-ppc <values of args...>`:

- **Values are used in file order.** Key names are labels for your own
  benefit, with three exceptions listed below. Order matters where QEMU
  needs it: a controller has to come before the disks that plug into
  it.
- **An empty value is skipped.** `"hd1": ""` is a convenient way to
  keep a slot in the file but switch it off.
- **Each value is split on spaces** into separate arguments, like a
  shell would, so one value can hold several options
  (`"-drive ... -device ..."`).
- **Quotes group text that contains spaces**, and are then removed.
  Both `"` and `'` work, and a quoted part can sit in the middle of an
  option: `file="C:/My Machines/hd0.qcow2",format=qcow2` becomes one
  argument. Inside JSON a `"` must be written `\"`.
- **No other escaping is done.** A backslash is passed through as is.
  JSON itself needs a backslash written `\\`, so a Windows path is
  either `"C:\\QEMU\\hd0.qcow2"` or, more simply, `"C:/QEMU/hd0.qcow2"`.
- **Use absolute paths.** QEMU is started with its working directory
  set to the folder of `qemu_binary`, not the folder of `config.json`,
  so a relative path resolves against the QEMU install.

### Keys the server treats specially

| Key | What happens |
|---|---|
| `network` | The server appends its port forwards to this value (see below). It must contain a `-netdev user,...` option, and that option must be the **last** thing in the value, because the forwards are added to the end of the string. Without a `network` key nothing is forwarded and the server can't reach MCPd. |
| `display` | Replaced by `-display none` when the target has `headless = true` in `config.toml`. Keep the display option in this key so headless mode can find it. |
| `pidfile` | Always dropped. Kyvos uses it to track its own processes; the server tracks the process itself. |

### What the server adds

These come from the target's channels in `config.toml`, so leave them
**out** of `config.json`:

| Added | From | Notes |
|---|---|---|
| `hostfwd=tcp::<port>-:4322` on the `-netdev user` | `channels.mcpd.endpoint` | MCPd always listens on 4322 inside the guest. |
| `hostfwd=udp::<port>-:4323` on the `-netdev user` | `channels.mcpd.discovery_port`, or the mcpd port if unset | LAN discovery for `fleet.discover`. |
| `-qmp tcp:<host>:<port>,server=on,wait=off` | `channels.qmp.endpoint` | Used by `qemu.stop`, snapshots and screenshots. |
| `-gdb tcp::<port>` | `channels.gdb.endpoint` | Only when a `gdb` channel is enabled. |

Each guest that runs at the same time needs its own host ports. Two
guests that forward the same host port can't both start: the second
QEMU exits.

Your own extra forwards on the `-netdev user` (for a web server in the
guest, say) are kept. Don't add your own forward for guest port 4322 or
4323 on a host port the server also uses.

### Serial output

Keep `"serial": "-serial stdio"`. The server sends QEMU's standard
output to a log under `archive_root/serial-logs/<target>/`, which is
what the `amiga://<target>/qemu/serial_log` resource and the crash
investigation prompts read. `"serial_output": "-append \"serial debuglevel=1\""`
passes the kernel options that make AmigaOS print its debug output to
that port (pegasos2 and amigaone; sam460ex takes kernel options from
its U-Boot environment instead).

## Key reference

These are the key names Kyvos uses. You can use any names, but these
make a file easy to compare with a Kyvos one.

| Key | Purpose | Typical value |
|---|---|---|
| `name` | Window title / process name | `-name my-peg2` |
| `machine` | Board model | `-M pegasos2`, `-M amigaone`, `-M sam460ex` |
| `cpu` | CPU override (rarely needed) | `""` |
| `memory` | Guest RAM | `-m 2048M` |
| `accel` | Accelerator | `-accel tcg` |
| `clock` | Real-time clock | `-rtc base=localtime` |
| `bios` | Firmware ROM, if the board takes one | `""` |
| `loader` | Boot loader and Kickstart (pegasos2, amigaone) | see [Machine variants](#machine-variants) |
| `serial_output` | Kernel command line | `-append "serial debuglevel=1"` |
| `vga` | Turns QEMU's default VGA card off | `-vga none` |
| `gfx` | Graphics card | `-device sm501` |
| `sound` | Sound card | `-device es1370,addr=0x08` |
| `tablet` | Absolute USB pointer | `-device usb-tablet`, or `""` |
| `hd0`, `hd1`, `hd2` | Hard disks | `-drive if=none,id=hd0,file=...,format=qcow2 -device ide-hd,unit=0,drive=hd0,bus=ide.0` |
| `cd0` | CD drive, empty or with an ISO | `-drive if=none,id=cd -device ide-cd,unit=1,drive=cd,bus=ide.1` |
| `usb`, `usbpath` | USB storage passthrough | `""` |
| `serial` | Serial port 1 | `-serial stdio` |
| `display` | Display back end | `-display sdl,gl=off,show-cursor=off,full-screen=off` |
| `network` | Network card plus user-mode network | `-device rtl8139,addr=0x09,netdev=nic -netdev user,id=nic,hostname=my-peg2` |
| `customargs` | Anything else (virtio, 9p shares, extra disks) | free-form |
| `pidfile` | Ignored by the server | |

`addr=0x..` pins a device to a PCI slot. It isn't required, but a fixed
slot keeps device order stable between QEMU versions, and AmigaOS
finds its devices at the same place every boot.

To insert an install ISO, give the CD drive a file:

```json
"cd0": "-drive if=none,id=cd,file=\"E:/ISOs/AmigaOS-4.1FE-Pegasos2.iso\",format=raw,media=cdrom -device ide-cd,unit=1,drive=cd,bus=ide.1"
```

`-display sdl` is what the Windows QEMU builds offer. On Linux,
`-display gtk` is usually available; on macOS, `-display cocoa`.

## Machine variants

The template is for pegasos2. For the other boards, change these keys
and keep the rest.

### Pegasos2 (`-M pegasos2`)

```json
"machine": "-M pegasos2",
"loader":  "-kernel \"<MACHINE_DIR>/bboot\" -initrd \"<MACHINE_DIR>/kickstart.zip\"",
"sound":   "-device es1370,addr=0x08",
"hd0":     "-drive if=none,id=hd0,file=\"<MACHINE_DIR>/hd0.qcow2\",format=qcow2 -device ide-hd,unit=0,drive=hd0,bus=ide.0",
"cd0":     "-drive if=none,id=cd -device ide-cd,unit=1,drive=cd,bus=ide.1",
"network": "-device rtl8139,addr=0x09,netdev=nic -netdev user,id=nic,hostname=my-peg2"
```

The disks use the board's built-in IDE (`ide.0`, `ide.1`). `bboot`
loads `kickstart.zip` passed as the initrd.

### AmigaOne (`-M amigaone`)

```json
"machine": "-M amigaone",
"loader":  "-kernel \"<MACHINE_DIR>/bboot\" -device loader,addr=0x600000,file=\"<MACHINE_DIR>/kickstart.zip\"",
"sound":   "-device es1370,addr=0x09",
"hd0":     "-drive if=none,id=hd0,file=\"<MACHINE_DIR>/hd0.qcow2\",format=qcow2 -device ide-hd,unit=0,drive=hd0,bus=ide.0",
"cd0":     "-drive if=none,id=cd -device ide-cd,unit=1,drive=cd,bus=ide.1",
"network": "-device rtl8139,addr=0x0a,netdev=nic -netdev user,id=nic,hostname=my-a1"
```

On amigaone the Kickstart is placed in guest memory at `0x600000` with
a `loader` device instead of `-initrd`, and the sound and network cards
sit one slot later than on pegasos2.

### Sam460ex (`-M sam460ex`)

```json
"machine":       "-M sam460ex",
"loader":        "",
"serial_output": "",
"vga":           "",
"gfx":           "",
"sound":         "-device es1370",
"cd0":           "-device sii3112 -drive if=none,id=cd -device ide-cd,unit=0,drive=cd,bus=ide.1",
"hd0":           "-drive if=none,id=hd0,file=\"<MACHINE_DIR>/hd0.qcow2\",format=qcow2 -device ide-hd,unit=0,drive=hd0,bus=ide.0",
"network":       "-device rtl8139,netdev=nic -netdev user,id=nic,hostname=my-sam"
```

The Sam460ex boots from its own U-Boot firmware, which reads the
Kickstart from the installed disk, so there is no `bboot` or
`kickstart.zip`, and it uses its on-board graphics. It has no IDE
controller of its own: `-device sii3112` adds a SATA card that provides
`ide.0` and `ide.1`, so `cd0` (which creates it) **must come before**
`hd0` in the file.

## Files the machine needs

| File | Needed by | Where it comes from |
|---|---|---|
| `qemu-system-ppc` | all | QEMU 8.0 or later. Set it as `[paths] qemu_binary`. With Kyvos, use the one its `prefs.json` names as `qemuppc`. |
| `bboot` | pegasos2, amigaone | A small boot loader that loads the Kickstart modules for QEMU's pegasos2 and amigaone boards. Kyvos installs it at `~/.kyvos/bboot` (`C:\Users\<you>\.kyvos\bboot` on Windows); you can point `-kernel` at that copy. |
| `kickstart.zip` | pegasos2, amigaone | A zip holding a `Kickstart/` folder: the Kickstart modules from an installed system's `SYS:Kickstart/`, with its `Kicklayout`. Kyvos builds it when it creates a machine. Rebuild it after an update that changes the Kickstart. |
| `hd0.qcow2` | all | The system disk. Create an empty one with `qemu-img create -f qcow2 hd0.qcow2 8G` and install AmigaOS 4.1 FE onto it from the install CD, or use one Kyvos made. qcow2 is needed for `qemu.savevm` / `qemu.loadvm`. |
| install ISO | first install only | Your AmigaOS 4.1 Final Edition CD for that board. |

The guest also needs networking configured (the rtl8139 card, with
DHCP from QEMU's user-mode network) and MCPd installed: see step 7 of
[First-time setup](#first-time-setup-step-by-step).

## Kyvos machines: things to check

Usually nothing to do: point `qemu_config` at the machine's
`config.json` and the server uses its `args` as they are. Check three
things:

- **Remove old port forwards.** Older machines may carry
  `hostfwd=tcp::4321-:4321` (a SerialShell forward) or forwards you
  added in Kyvos for MCPd. The server adds the ones it needs; a stale
  one on a port another guest uses stops QEMU starting.
- **Look at `args`, not `params`.** If they disagree, `args` is what
  runs. `params.customargs` in particular can point at a different
  machine's disk after a machine was copied.
- **Keep `-netdev user` last in `network`.** Kyvos writes it that way;
  keep it that way if you edit by hand.

Kyvos and the server can share one file, but don't run the same
machine from both at once: both would write to the disk image.

## Starting QEMU yourself

The server doesn't need `config.json` to use a guest that's already
running. It only needs MCPd reachable, and QMP for the `qemu.*` tools.
Add these to whatever command line you use (Kyvos users can put them in
the machine's custom arguments and network settings):

```
-netdev user,id=nic,hostfwd=tcp::4422-:4322,hostfwd=udp::4422-:4323
-qmp tcp:127.0.0.1:14422,server=on,wait=off
```

and match them in `config.toml` (`channels.mcpd.endpoint =
"127.0.0.1:4422"`, `channels.qmp.endpoint = "127.0.0.1:14422"`).
`qemu.stop` works on a guest it didn't start, through QMP.

## Checking a file

To see the exact command line a file produces without starting QEMU:

```sh
python -c "from pathlib import Path; from amiga_fleet_mcp.config import load_config; from amiga_fleet_mcp.qemu.cmdline import build_cmdline; c=load_config(None); t=c.targets['qemu-pegasos2']; print(' '.join(build_cmdline(c.paths.qemu_binary, t.qemu_config, t)[0]))"
```

Paste the output into a shell to run QEMU directly (put quotes back
around any path that contains spaces, since the printout drops them); any error QEMU
prints there is the same one `qemu.start` would hit. The serial log
from a failed `qemu.start` also holds QEMU's own error message.

| Symptom | Likely cause |
|---|---|
| `target.qemu_config is required for qemu.start` | No `qemu_config` in the target block. |
| `paths.qemu_binary not set in config` | No `[paths] qemu_binary`. |
| `kyvos config not found` | The `qemu_config` path is wrong. |
| QEMU exits at once, "could not set up host forwarding rule" | Another guest or program holds that host port. Give each target its own ports. |
| QEMU exits with a bus-not-found error for `ide.0` (sam460ex) | `hd0` comes before the `-device sii3112` that creates the bus. |
| Guest boots but `mcpd_reachable` stays false | MCPd not installed or not started in the guest, guest networking not up, or no `network` key / `-netdev user` not last. |
| Black window, nothing on serial | Wrong `loader` for the board, or a `kickstart.zip` that doesn't match the installed system. |
