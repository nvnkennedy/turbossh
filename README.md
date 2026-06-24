# TurboSSH

[![PyPI](https://img.shields.io/pypi/v/turbossh.svg)](https://pypi.org/project/turbossh/)
[![Python](https://img.shields.io/pypi/pyversions/turbossh.svg)](https://pypi.org/project/turbossh/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

SSH, serial, and SFTP for people who work on embedded and automotive boxes all
day. One toolkit, three ways to drive it: a **Python library** for your test
framework, a **command-line tool** for scripts, and a **desktop GUI** with a
real VT100 terminal. The GUI ships as a self-contained Windows `.exe` with PyQt5
baked in, so you don't have to fight a PyQt install to use it.

It came out of a specific headache: getting a shell on a QNX target that's only
reachable through a Windows RDP jump box, reading a debug board hanging off a
COM port on that same box, and pulling logs off the target — without juggling
three tools to do it.

```bash
pip install turbossh          # library + CLI + the GUI launcher
turbossh-gui                  # launch the GUI
```

Every feature below shows you all three ways to use it: **in a script**, **in
the GUI**, and **on the command line**.

## Contents

- [Install](#install)
- [The three interfaces, in 30 seconds](#the-three-interfaces-in-30-seconds)
- [Connecting](#connecting) · [Running commands](#running-commands) ·
  [Live logs](#live-logs) · [Files (SFTP / SCP)](#files-sftp--scp)
- [Serial ports](#serial-ports) · [Serial over RDP](#serial-over-rdp) ·
  [Scanning remote ports](#scanning-remote-ports) · [Camera](#camera)
- [Port forwarding](#port-forwarding) ·
  [Installing an SSH server](#installing-an-ssh-server) ·
  [Credentials](#credentials)
- [The GUI tour](#the-gui-tour) · [CLI reference](#cli-reference) ·
  [Results & errors](#results--errors)
- [Architecture](ARCHITECTURE.md) · [Changelog](CHANGELOG.md)

---

## Install

```bash
pip install turbossh
```

That gives you the `turbossh` library, the `turbossh` CLI, and `turbossh-gui`.
On Windows the GUI runs from a bundled executable, so PyQt5 doesn't need to
install. To run the GUI from source on a platform with PyQt5 wheels instead:

```bash
pip install "turbossh[gui]"
```

Python 3.8 or newer. Paramiko, scp, pyserial, keyring, pywinrm and pyte come
along automatically.

## The three interfaces, in 30 seconds

The library is one object, `SSHHandler`. By default it raises typed exceptions
(what you want in a test); pass `safe=True` and every call returns a result
object instead (what the GUI uses so a dropped socket logs a line instead of
crashing the window).

```python
from turbossh import SSHHandler, SSHConfig

with SSHHandler(SSHConfig(host="192.168.1.50", username="root", password="…")) as ssh:
    print(ssh.run("uname -a").text)
```

The CLI wraps the same calls behind `turbossh <command> --host … --user …`. The
GUI wraps them behind tabs and buttons. Nothing is GUI-only.

---

## Connecting

The starting point for everything. Direct, through a jump host, or to old gear
that needs deprecated crypto.

**In a script.** A connection is an `SSHConfig`. Nest one in `jump_host` to hop
through an RDP/Windows box first.

```python
from turbossh import SSHHandler, SSHConfig

# direct
cfg = SSHConfig(host="10.0.0.5", username="root", password="…")

# through a jump host: laptop -> RDP box -> target
cfg = SSHConfig(
    host="adelegg-mopf", username="root",
    jump_host=SSHConfig(host="10.232.9.120", username="EU\\nkennedy", password="…"),
)

# an old ECU that only speaks legacy ciphers/kex
cfg = SSHConfig(host="10.0.0.9", username="root", enable_legacy_algorithms=True)

# lab gear that gets re-imaged constantly: skip host-key checking
cfg = SSHConfig(host="10.0.0.9", username="root", host_key_policy="ignore")

with SSHHandler(cfg) as ssh:
    ...
```

**In the GUI.**
1. Click **New session** in the left sidebar (or right-click the sidebar →
   *New session…*).
2. Pick **SSH** as the connection type.
3. Fill in **Host**, **User**, **Password**. Tick **Connect through a jump
   host** if you need one (its fields pre-fill from Settings).
4. Tick **Enable legacy algorithms** for old ECUs, or leave **Ignore host key**
   on for lab boxes.
5. Click **OK** — it connects and opens a terminal tab.

The jump host you use most often goes in **Settings → Jump host** once, and
every session reuses it.

**On the command line.** The connection flags are shared by every command:

```bash
turbossh run --host 10.0.0.5 --user root --password uname -a
#   --domain CORP     for CORP\user logins
#   --key id_rsa      key-based auth
#   --use-stored      pull the password from the OS vault (see Credentials)
```

---

## Running commands

Get an exit code and output back as a structured object — no parsing exit codes
out of stdout.

**In a script.**

```python
res = ssh.run("systemctl is-active sshd")
res.ok            # True if exit code == 0
res.text          # stdout, stripped
res.stderr        # stderr
res.exit_code     # the actual number
res.duration      # seconds it took

ssh.run("reboot", check=True)        # raise SSHCommandError on a non-zero exit
ssh.sudo("mount -o remount,rw /")    # sudo, password fed on stdin
ssh.run_many(["sync", "reboot"])     # several commands over one channel
```

**In the GUI.** Open the session's **Terminal** tab and just type — it's a real
terminal, so arrow keys, Tab-completion and Ctrl-C all work. The **Quick**
buttons (top of the tab) fire common commands in one click; **Ctrl-C** sends an
interrupt; **Clear** wipes the screen.

**On the command line.**

```bash
turbossh run --host H --user U uname -a
turbossh run --host H --user U --json systemctl status sshd   # machine-readable
```

The CLI exits with the remote command's exit code, so it drops into shell
scripts cleanly.

---

## Live logs

Follow a command that never ends — `slog2info -w`, `journalctl -f`, `dmesg -w`,
`tail -f` — and react to it.

**In a script.** `iter_lines()` yields output as it arrives; `stream()` adds
regex matching and a tee-to-file.

```python
# print every line, stop the instant one matches
ssh.stream("slog2info -w", on_line=print, match=r"E/.*panic", stop_on_match=True)

# follow a log into a local file while watching for a string
result = ssh.stream("journalctl -f", save_to="boot.log", match="Started Target")
print(result["matched"], result["matches"])
```

**In the GUI.** Each SSH session has a **Logs** tab:
1. Pick a preset from the dropdown (`slog2info`, `journalctl -f`, …) or type
   your own command.
2. Add a **regex filter** if you only care about some lines.
3. Click **Start**. Use **Pause**, **Clear**, and **Save…** as needed.

The log runs on a pseudo-terminal with the login PATH, so QNX tools like
`slog2info` actually stream instead of buffering or coming back "not found".

**On the command line.**

```bash
turbossh stream --host H --user U slog2info -w
turbossh stream --host H --user U --match "panic" --save boot.log journalctl -f
```

Ctrl-C stops it.

---

## Files (SFTP / SCP)

A full remote filesystem, not just put and get.

**In a script.**

```python
ssh.push("dist/", "/opt/app", recursive=True)     # upload
ssh.pull("/var/log/messages", "logs/")            # download
ssh.listdir("/etc")
ssh.exists("/tmp/lock")
ssh.read_text("/proc/version")
ssh.write_text("/tmp/flag", "1")
ssh.makedirs("/opt/app/cache")
ssh.remove("/tmp/old")
for root, dirs, files in ssh.walk("/etc/network"):
    ...
```

SCP is there too — `ssh.scp_push(...)` / `ssh.scp_pull(...)` — for servers where
it's faster or SFTP is disabled.

**In the GUI.** Every SSH session has a **SFTP** tab: a two-pane browser. Double-
click folders to navigate, use the buttons to upload, download, make a
directory, rename, or delete. Transfers run on their own channel, so a big copy
never freezes the terminal.

**On the command line.**

```bash
turbossh push --host H --user U ./build /opt/app --recursive
turbossh pull --host H --user U /var/log ./logs --recursive
```

---

## Serial ports

A serial port on the machine you're sitting at.

**In a script.**

```python
from turbossh import SerialHandler, list_serial_ports

print(list_serial_ports())                        # what's plugged in

with SerialHandler("COM5", baudrate=115200) as ser:
    ser.write("version\n")
    ser.stream(on_line=print, match="login:")     # follow it
```

**In the GUI.** New session → **Serial** → pick the **Device** and **Baud** →
**OK**. The console is a native terminal: type straight into it.

**On the command line.**

```bash
turbossh list-serial                              # list local ports
turbossh serial-monitor --port COM5 --baud 115200 --match "login:"
```

---

## Serial over RDP

The automotive bread-and-butter: the debug board is plugged into the Windows
RDP box, not your laptop. TurboSSH runs the serial bridge *on that box* and pipes
it back over SSH, as a native terminal.

**In a script.** Read the remote port with `serial_stream` (auto-detects COM vs
`/dev`), write to it with `serial_write`.

```python
# stream a COM port on the RDP machine, save the console to a file
ssh.serial_stream("COM4", baudrate=115200, on_line=print, save_to="console.log")

# send a line to it
ssh.serial_write("COM4", "reboot\n", baudrate=115200)
```

For a fully interactive, character-by-character session there's `serial_bridge()`
(it returns a raw channel you read/write), with `serial_in_use()` and
`serial_release()` around it — that's what the GUI drives.

**In the GUI.**
1. New session → **Serial**.
2. Tick **Port is on the RDP machine (connect to it remotely)**. The section
   above relabels to the **RDP machine** — fill in its IP, your Windows login,
   and password.
3. Click **Scan remote** to list that machine's COM ports, and pick yours.
4. Set the **Baud** and click **OK**.
5. You get a native terminal — type into it directly, with Tab-completion and
   Ctrl-C. If the port's already in use it asks before taking it, and it
   releases the port cleanly when you close the tab or the app.

**On the command line.**

```bash
turbossh serial-ssh --host 10.232.9.120 --user "EU\\nkennedy" \
    --device COM4 --baud 115200 --save console.log
#   --send "reboot"      write a line first
#   --match "login:"     flag matching lines
```

---

## Scanning remote ports

Ask the box you're about to connect to which COM ports it actually has.

**In a script.**

```python
for p in ssh.remote_serial_ports():
    print(p["device"], "-", p["description"])
    # COM4 - Silicon Labs CP210x USB to UART Bridge (COM4)
```

**In the GUI.** It's the **Scan remote** button in the serial session dialog —
it fills the device dropdown for you.

**On the command line.**

```bash
turbossh scan-ports --host H --user U
```

---

## Camera

Watch any camera — **on your own machine** or **on a remote machine** — live in
TurboSSH. It's **opt-in**: turn it on in **Settings → Enable camera** and a
**📷 Camera** button appears in the ribbon (it stays hidden otherwise).

**In the GUI.** The Camera button opens a panel with the view front-and-centre:
1. Pick a **Source** — *Local (this PC)* (the default, no connecting) or any
   saved machine as a remote source.
2. Pick a **Camera** from the list (Refresh re-scans), then **Start**.
3. Use **Snapshot**, **Record**, **Pause**; saved files show an *open folder*
   link. Files save on your laptop.

Local needs nothing but ffmpeg. For a remote source, it connects over that
saved machine's own SSH connection on its own threads (so it never slows the
terminal/serial work), runs ffmpeg there, and streams MJPEG back; closing the
tab releases the remote camera. If a remote camera is busy it offers to take it.

ffmpeg isn't bundled in the pip wheel (it would blow past PyPI's size limit), so
the first time you use the camera it's fetched once from a public GitHub build,
cached, and (for a remote source) pushed to that machine. For a fully offline
setup, point **Settings → ffmpeg path** at a local `ffmpeg.exe`.

**In a script** (the remote path):

```python
for cam in ssh.list_cameras(ffmpeg=r"C:\ffmpeg\ffmpeg.exe"):
    print(cam)
chan = ssh.webcam_channel("Integrated Camera", ffmpeg=r"C:\ffmpeg\ffmpeg.exe",
                          width=640, height=480, fps=15)
# chan.recv() yields MJPEG (concatenated JPEG frames); chan.close() to stop
ssh.webcam_release()
```

(There's no CLI subcommand for the camera — it's a GUI/library feature.)

## Port forwarding

`ssh -L` and `ssh -R` tunnels through the same connection or jump host.

**In a script.**

```python
fwd = ssh.forward_local("10.0.0.9", 80, local_port=8080)   # -L
print(fwd.local_port)                                       # browse localhost:8080
# ... use it ...
fwd.close()

ssh.forward_remote(9000, "127.0.0.1", 3000)                 # -R
```

**On the command line.** Runs until you Ctrl-C:

```bash
turbossh forward --host H --user U --local-port 8080 --to-host 10.0.0.9 --to-port 80
```

(The GUI doesn't expose forwarding yet — use the library or CLI.)

---

## Installing an SSH server

The catch with the RDP-jump-box workflow is that the box often doesn't have SSH
yet, and corporate networks block the Windows-Update path that would install it.
So TurboSSH bundles the OpenSSH binaries (ARM64 / x64 / x86) and installs them
with zero downloads. It's a **one-time** install — `sshd` is registered as an
automatic service, so it's back on every reboot and listening at the login
screen. You don't re-run it.

**On the machine itself.**

```bash
turbossh-setup                         # self-elevates, installs + starts sshd,
                                       # opens the firewall, fixes host keys
turbossh-setup --install-pip --port 22 # also pip-install turbossh; custom port
```

Or in the GUI: the **SSH server** button → **This PC**. A window shows progress,
and the GUI tells you when `sshd` is listening.

**On a remote machine, from your laptop** (needs WinRM reachable on the target
and a local-admin account on it) — pushes the bundled OpenSSH over WinRM and
installs it there:

```bash
turbossh install-ssh-remote --host 10.232.9.120 --user "EU\\nkennedy"
```

Or in the GUI: the **SSH server** button → **A remote machine (WinRM)**.

**In a script** (the remote, WinRM path):

```python
from turbossh import enable_openssh_via_winrm_offline
enable_openssh_via_winrm_offline(
    "10.232.9.120", "EU\\nkennedy", "password",
    openssh_dir="…/turbossh/openssh", log=print)
```

---

## Credentials

Passwords belong in the OS vault, not in a script.

**In a script.**

```python
from turbossh import CredentialStore, SSHHandler, SSHConfig

CredentialStore("my_lab").set("EU\\nkennedy", "the-password")   # store once
pw = CredentialStore("my_lab").get("EU\\nkennedy")              # later
ssh = SSHHandler(SSHConfig(host="…", username="nkennedy", password=pw))
```

Passwords are wrapped in a `Secret` that masks itself in logs and reprs, so they
don't bleed into your test output.

**In the GUI.** Saved-session passwords go straight into the OS keyring, never
plaintext on disk. Show/hide them with the eye icon in the dialog.

**On the command line.**

```bash
turbossh store-credential --user nkennedy --domain EU --service my_lab
turbossh run --host H --user nkennedy --domain EU --use-stored --service my_lab uname -a
```

---

## The GUI tour

```bash
turbossh-gui
```

- **Sidebar** of saved sessions with a quick-connect filter. Right-click for
  new / open / edit / duplicate / delete.
- **Tabbed sessions**, each with **Terminal**, **SFTP**, and **Logs** tabs.
- **Real VT100 terminal** — htop, vim, less render correctly.
- **10,000 lines of scrollback** (configurable in Settings), and **Save all
  output**, which writes the *entire* session to disk, not just what's on
  screen — it scales to millions of lines.
- **Split view** tiles several sessions at once.
- **Menu bar** (File / Edit / View / Session / Help) and a ribbon with the same
  actions plus **SSH server**, **Check updates**, and **Settings**.
- **Check updates** pulls the latest from PyPI and reinstalls in place, then
  offers to refresh the bundled OpenSSH on the machines you use.
- Dark and light themes; a desktop and Start-menu shortcut on first run.

## CLI reference

Connection flags (`--host --user [--domain] [--key] [--password|--use-stored]`)
are shared.

```bash
turbossh run     --host H --user U uname -a
turbossh stream  --host H --user U --match "panic" slog2info -w
turbossh push    --host H --user U ./build /opt/app --recursive
turbossh pull    --host H --user U /var/log ./logs --recursive
turbossh info    --host H --user U --json

turbossh serial-ssh  --host H --user U --device COM4 --baud 115200 --save log.txt
turbossh scan-ports  --host H --user U
turbossh forward     --host H --user U --local-port 8080 --to-host 10.0.0.9 --to-port 80

turbossh list-serial
turbossh serial-monitor --port COM5 --baud 115200

turbossh install-ssh-remote --host H --user "DOMAIN\\admin"
turbossh store-credential --user U --domain CORP --service my_lab

turbossh-gui          # the GUI
turbossh-setup        # install OpenSSH Server on THIS machine (offline)
turbossh-shortcut     # (re)create the desktop / Start-menu shortcut
turbossh-docs         # open the documentation
```

## Results & errors

- `CommandResult` — `.exit_code`, `.stdout`, `.stderr`, `.text`, `.duration`, `.ok`
- `TransferResult` — `.size_bytes`, `.duration`, `.human_speed`, `.files`
- `OperationResult` — the safe-mode wrapper: `bool(res)`, `.value`, `.error`,
  `.unwrap()`

Raise mode (the default) gives you typed exceptions to catch precisely —
`SSHConnectionError`, `SSHAuthenticationError`, `SSHTimeoutError`,
`SSHCommandError`, `SSHTransferError`, `SerialError`, `WinRMError`,
`CredentialError`, all under `SSHError`. Safe mode (`SSHHandler(cfg, safe=True)`)
turns every call into an `OperationResult` instead.

## A real-world example

A hardware-in-the-loop check that reboots a target through the RDP box's serial
port and waits for it to come back:

```python
from turbossh import SSHHandler, SSHConfig

def test_target_boots_clean(rdp_box, target):
    jump = SSHConfig(host=rdp_box.ip, username=rdp_box.user, password=rdp_box.pw)
    cfg  = SSHConfig(host=target.ip, username="root", jump_host=jump)
    with SSHHandler(cfg, safe=True) as ssh:
        ssh.serial_write("COM4", "reboot\n")
        res = ssh.serial_stream("COM4", match=r"login:", stop_on_match=True,
                                timeout=120, save_to="boot.log")
        assert res["matched"], "target never reached the login prompt"
        assert ssh.run("slog2info | grep -c FATAL").text.strip() == "0"
```

## License

MIT — see [LICENSE](LICENSE).
