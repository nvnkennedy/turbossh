# TurboSSH

[![PyPI](https://img.shields.io/pypi/v/turbossh.svg)](https://pypi.org/project/turbossh/)
[![Python](https://img.shields.io/pypi/pyversions/turbossh.svg)](https://pypi.org/project/turbossh/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

SSH, serial, and SFTP for people who work on embedded and automotive boxes all
day — a Paramiko-based toolkit you can drive three ways: as a **Python library**
in a test framework, as a **command-line tool** in a script, and as a **desktop
GUI** with a real VT100 terminal. The GUI ships as a self-contained Windows
`.exe` (PyQt5 baked in), so you don't have to fight a PyQt install to use it.

It grew out of a real problem: getting a shell on a QNX target that's only
reachable through a Windows RDP jump box, reading a debug board hanging off a
COM port on that same box, and pulling logs off the target — without keeping
three different tools open.

```bash
pip install turbossh          # library + CLI + the GUI launcher
turbossh-gui                  # launch the GUI
```

---

## What it does

- [Connect over SSH](#connecting) — direct, through a jump host, or to old
  ECUs that need legacy crypto.
- [Run commands and capture results](#running-commands) — exit code, stdout,
  stderr, timing, all on a typed result object.
- [Follow live logs](#live-logs) — `slog2info -w`, `journalctl -f`, `dmesg -w`
  and friends, with regex matching and tee-to-file.
- [Move files with SFTP/SCP](#files-sftp--scp) — upload, download, a full
  remote-filesystem API, and a graphical browser in the GUI.
- [Talk to serial ports](#serial--com-ports) — locally, or **a COM port on a
  remote RDP machine over SSH**, as a native terminal with Tab-completion.
- [Scan a remote machine's ports](#scanning-remote-ports) — list the COM ports
  on the box you're about to connect to.
- [Stand up an SSH server, offline](#turning-a-windows-box-into-an-ssh-server) —
  install OpenSSH on a locked-down Windows machine with no internet, locally or
  to a remote box over WinRM.
- [Forward ports](#port-forwarding) — `ssh -L` / `ssh -R` tunnels.
- [The GUI](#the-gui) — tabbed sessions, split view, SFTP browser, log viewer,
  10k-line scrollback, save-everything-to-disk.
- [The CLI](#cli-reference) — every one of the above as a `turbossh <command>`.

Full surface area is in [ARCHITECTURE.md](ARCHITECTURE.md); what changed and
when is in [CHANGELOG.md](CHANGELOG.md).

---

## Install

```bash
pip install turbossh
```

That gives you the library, the `turbossh` CLI, and `turbossh-gui`. On Windows
the GUI runs from a bundled executable, so PyQt5 doesn't need to install. If you
want to run the GUI from source on a platform with PyQt5 wheels:

```bash
pip install "turbossh[gui]"
```

Python 3.8+. The only required third-party pieces are Paramiko, scp, pyserial,
keyring, pywinrm, and pyte — all pulled in automatically.

---

## Quick start

The same object serves all three styles. By default it raises typed exceptions;
pass `safe=True` and it hands back result objects instead (handy in a GUI or a
long test run that shouldn't die on the first hiccup).

```python
from turbossh import SSHHandler, SSHConfig

with SSHHandler(SSHConfig(host="192.168.1.50", username="root", password="…")) as ssh:
    print(ssh.run("uname -a").text)          # one-shot command
    ssh.push("build/app", "/tmp/app")         # upload
    for line in ssh.iter_lines("slog2info -w"):   # follow logs
        print(line)
```

---

## Connecting

A connection is described by an `SSHConfig`. The common cases:

```python
from turbossh import SSHHandler, SSHConfig

# direct
SSHConfig(host="10.0.0.5", username="root", password="…")

# through a jump host (laptop -> RDP/Windows box -> target)
SSHConfig(
    host="adelegg-mopf", username="root",
    jump_host=SSHConfig(host="10.232.9.120", username="EU\\nkennedy", password="…"),
)

# an old ECU that only speaks deprecated ciphers/kex
SSHConfig(host="10.0.0.9", username="root", enable_legacy_algorithms=True)
```

Host-key policy defaults to strict; set `host_key_policy="ignore"` for lab gear
that gets re-imaged constantly. Passwords can come straight from the OS
credential vault instead of your source code — see
[confidential credentials](#confidential-credentials).

---

## Running commands

`run()` returns a `CommandResult` — no parsing exit codes out of stdout.

```python
res = ssh.run("systemctl is-active sshd")
res.ok            # exit code == 0
res.text          # stdout, stripped
res.stderr        # stderr
res.exit_code     # the actual code
res.duration      # seconds

ssh.run("reboot", check=True)        # raise SSHCommandError on non-zero
ssh.sudo("mount -o remount,rw /")    # sudo with the password fed on stdin
ssh.run_many(["sync", "reboot"])     # several commands, one channel
```

For something that won't terminate on its own, use the streaming API below
rather than `run()` (which waits for the command to finish).

---

## Live logs

`iter_lines()` yields output line by line as it arrives, so you can follow a
`-w`/`-f` command and react to it. `stream()` wraps it with regex matching and
a tee-to-file:

```python
# print everything, and stop the moment a line matches
ssh.stream("slog2info -w", on_line=print, match=r"E/.*panic", stop_on_match=True)

# tail a log into a local file while watching for a string
ssh.stream("journalctl -f", save_to="boot.log", match="Started Target")
```

In the GUI this is the **Logs** tab; on the command line it's
[`turbossh stream`](#cli-reference). Both run the command on a pseudo-terminal
so line-buffered tools actually stream instead of sitting in a 4 KB buffer.

---

## Files (SFTP / SCP)

A full remote-filesystem API, not just put/get:

```python
ssh.push("dist/", "/opt/app", recursive=True)
ssh.pull("/var/log/messages", "logs/")
ssh.listdir("/etc")
ssh.exists("/tmp/lock")
ssh.read_text("/proc/version")
ssh.write_text("/tmp/flag", "1")
ssh.makedirs("/opt/app/cache")
for root, dirs, files in ssh.walk("/etc/network"):
    ...
```

SCP is there too (`scp_push` / `scp_pull`) for servers where it's faster or
where SFTP is disabled. In the GUI, every SSH session has a **SFTP** tab with a
browsable, drag-free upload/download panel that runs on its own channel so big
transfers never freeze the terminal.

---

## Serial / COM ports

Two situations, one API.

**A port on this machine:**

```python
from turbossh import SerialHandler

with SerialHandler("COM5", baudrate=115200) as ser:
    ser.stream(on_line=print, match="login:")
```

**A port on a remote machine, reached over SSH** — this is the automotive
bread-and-butter: the debug board is plugged into the Windows RDP box, not your
laptop. TurboSSH runs the serial bridge *on that box* and pipes it back:

```python
# read a remote COM port (auto-detects COM vs /dev)
ssh.serial_stream("COM4", baudrate=115200, on_line=print, save_to="console.log")

# write a line to it
ssh.serial_write("COM4", "version\n", baudrate=115200)
```

In the GUI a serial-over-SSH session is a **native terminal**: you type directly
into it, character by character, with Tab-completion and Ctrl-C going through to
the device's own shell. It checks whether the port is already in use and asks
before taking it, and it releases the port cleanly when you close the tab, close
the app, or even if the app is killed.

Under the hood that's `serial_bridge()` (interactive), with `serial_in_use()`
and `serial_release()` for the open/close lifecycle — see
[ARCHITECTURE.md](ARCHITECTURE.md#serial-over-ssh) for why it needs a PTY and
raw console mode to work on Windows OpenSSH.

---

## Scanning remote ports

Before you connect, ask the remote box what it actually has:

```python
for p in ssh.remote_serial_ports():
    print(p["device"], "-", p["description"])
    # COM4 - Silicon Labs CP210x USB to UART Bridge (COM4)
```

On Windows it returns friendly names from the device manager; on Linux/QNX it
lists the `/dev` nodes. The GUI's serial dialog has a **Scan remote** button
that fills the dropdown from this.

---

## Turning a Windows box into an SSH server

The chicken-and-egg problem with the RDP-jump-box workflow is that the box often
*doesn't have SSH yet*, and corporate networks block the Windows Update / Add-
WindowsCapability path that would install it. So TurboSSH **bundles the OpenSSH
binaries** (ARM64 / x64 / x86) and installs them with zero downloads.

**On the machine itself:**

```bash
turbossh-setup            # self-elevates, installs + starts sshd, opens the
                          # firewall, fixes host keys. No internet needed.
```

Or from the GUI: the **SSH server** button → *This PC*.

**On a remote machine, from your laptop** (needs WinRM reachable and a local-
admin account on the target) — pushes the bundled OpenSSH over WinRM and installs
it remotely:

```bash
turbossh install-ssh-remote --host 10.232.9.120 --user "EU\\nkennedy"
```

Or from the GUI: the **SSH server** button → *A remote machine (WinRM)*.

This is a **one-time** install per machine. It registers `sshd` as an automatic
Windows service, so it comes back on every reboot and is listening at the login
screen — you don't re-run it after a restart.

---

## Port forwarding

```python
fwd = ssh.forward_local("10.0.0.9", 80, local_port=8080)   # ssh -L
print(fwd.local_port)                                       # browse localhost:8080
fwd.close()

ssh.forward_remote(9000, "127.0.0.1", 3000)                 # ssh -R
```

On the command line: `turbossh forward --host … --local-port 8080 --to-host
10.0.0.9 --to-port 80`.

---

## Confidential credentials

Passwords belong in the OS vault, not in a script. Store one once:

```bash
turbossh store-credential --user nkennedy --domain EU --service my_lab
```

```python
from turbossh import CredentialStore
pw = CredentialStore("my_lab").get("EU\\nkennedy")
ssh = SSHHandler(SSHConfig(host="…", username="nkennedy", password=pw))
```

Passwords are wrapped in a `Secret` that masks itself in logs and reprs, so they
don't leak into your test output. The GUI keeps every saved session's password
in the keyring, never in plaintext on disk.

---

## The GUI

```bash
turbossh-gui
```

- **Tabbed sessions** with a saved-session sidebar and a quick-connect filter.
  Right-click for new / open / edit / duplicate / delete.
- **A real VT100 terminal** (pyte) — htop, vim, less and friends render
  correctly, with colour and a block cursor.
- **10,000 lines of scrollback** you can wheel through, and **Save all output**
  that writes the *entire* session to disk, not just what's on screen (the full
  log is teed to a file as it runs, so it scales to millions of lines).
- **A SFTP browser** under every SSH session.
- **A Logs tab** for `slog2info` / `journalctl` / `dmesg` with a regex filter,
  pause, clear, and save.
- **Serial consoles**, local or [over RDP](#serial--com-ports), as native
  terminals.
- **Split view** to tile several sessions at once.
- **Check for updates** that pulls the latest from PyPI and reinstalls in place,
  and offers to refresh the bundled OpenSSH on the machines you use.
- A menu bar, dark/light themes, configurable terminal font and scrollback, and
  a desktop + Start-menu shortcut created on first run.

---

## CLI reference

Everything the GUI and library do is also a subcommand. Connection flags
(`--host --user [--domain] [--key] [--password|--use-stored]`) are shared.

```bash
turbossh run     --host H --user U uname -a
turbossh stream  --host H --user U --match "panic" slog2info -w
turbossh push    --host H --user U ./build /opt/app --recursive
turbossh pull    --host H --user U /var/log ./logs --recursive
turbossh info    --host H --user U --json

turbossh serial-ssh  --host H --user U --device COM4 --baud 115200 --save console.log
turbossh scan-ports  --host H --user U
turbossh forward     --host H --user U --local-port 8080 --to-host 10.0.0.9 --to-port 80

turbossh list-serial                         # local ports
turbossh serial-monitor --port COM5 --baud 115200   # local serial

turbossh install-ssh-remote --host H --user "DOMAIN\\admin"   # over WinRM, offline
turbossh store-credential --user U --domain CORP --service my_lab

turbossh-gui          # the GUI
turbossh-setup        # install OpenSSH Server on THIS machine (offline)
turbossh-shortcut     # (re)create the desktop / Start-menu shortcut
turbossh-docs         # open the documentation
```

---

## Result objects & errors

- `CommandResult` — `.exit_code`, `.stdout`, `.stderr`, `.text`, `.duration`, `.ok`
- `TransferResult` — `.size_bytes`, `.duration`, `.human_speed`, `.files`
- `OperationResult` — the safe-mode wrapper: `bool(res)`, `.value`, `.error`,
  `.unwrap()`

**Raise mode** (default) gives you typed exceptions you can catch precisely:
`SSHConnectionError`, `SSHAuthenticationError`, `SSHTimeoutError`,
`SSHCommandError`, `SSHTransferError`, `SerialError`, `WinRMError`,
`CredentialError` — all subclasses of `SSHError`.

**Safe mode** (`SSHHandler(cfg, safe=True)`) turns every call into an
`OperationResult` instead, which is what the GUI uses so a dropped connection
logs a line instead of crashing the window.

---

## Using it in a test framework

The library is the interface here — there's nothing GUI-only. A typical
hardware-in-the-loop check:

```python
from turbossh import SSHHandler, SSHConfig

def test_target_boots_clean(rdp_box, target):
    jump = SSHConfig(host=rdp_box.ip, username=rdp_box.user, password=rdp_box.pw)
    cfg = SSHConfig(host=target.ip, username="root", jump_host=jump, safe=True)
    with SSHHandler(cfg, safe=True) as ssh:
        ssh.serial_write("COM4", "reboot\n")
        result = ssh.serial_stream("COM4", match=r"login:", stop_on_match=True,
                                   timeout=120, save_to="boot.log")
        assert result["matched"], "target never reached the login prompt"
        assert not ssh.run("slog2info | grep -c FATAL").text.strip() != "0"
```

---

## License

MIT — see [LICENSE](LICENSE).
