# ssh-handler — full capability & usage guide

`ssh-handler` is an extensive SSH / SFTP / SCP / FTP / **serial** automation
toolkit built on Paramiko, usable three ways from one install:

1. **Python API** — import and script it (test frameworks, automation).
2. **CLI** — `ssh-handler …` / `python -m ssh_handler …` (argument-driven).
3. **PyQt5 GUI** — `ssh-handler-gui` (every operation as a button; build a
   standalone `.exe` with the bundled automotive-SSH icon).

```bash
pip install ssh-handler          # everything except the GUI toolkit
pip install "ssh-handler[gui]"   # also installs PyQt5 for the GUI
```
Batteries included: `paramiko`, `scp`, `pyserial`, `keyring`, `pywinrm` are all
pulled in automatically. Only PyQt5 is optional.

---

## What it can do

| Capability | API | CLI |
|---|---|---|
| Run a command | `ssh.run("uname -a")` → `.text` / `.stdout` / `.exit_code` | `ssh-handler run …` |
| Many commands | `ssh.run_many([...])` | — |
| sudo | `ssh.sudo("cmd", password)` | — |
| Interactive shell | `ssh.open_shell()` (send / read_until) | — |
| **Continuous logs** | `ssh.iter_lines(cmd)` / `ssh.stream(cmd, match=, save_to=)` | `ssh-handler stream …` |
| Upload (SFTP) | `ssh.push(local, remote, recursive=)` | `ssh-handler push …` |
| Download (SFTP) | `ssh.pull(remote, local, recursive=)` | `ssh-handler pull …` |
| SCP transfers | `ssh.scp_push` / `ssh.scp_pull` | — |
| FTP / FTPS | `FTPHandler(...)` | — |
| Remote FS ops | `listdir/stat/mkdir/makedirs/rename/remove/chmod/read_text/write_text/walk` | — |
| **Serial (local)** | `SerialHandler("COM5").stream(...)` | `ssh-handler serial-monitor …` |
| **Serial (remote/RDP)** | `ssh.serial_stream("COM5"/"/dev/ttyUSB0", match=, save_to=)` | — |
| List serial ports | `list_serial_ports()` | `ssh-handler list-serial` |
| Jump host (RDP) | `SSHConfig(jump_host=…)` | `--`(set in API) |
| Parallel fleet | `SSHPool([...]).run("cmd")` | — |
| Credential vault | `CredentialStore`, `Secret`, `prompt_password` | `ssh-handler store-credential …` |
| Connectivity check | `ssh.diagnose()` | `ssh-handler info …` |
| Install OpenSSH (offline) | — | `ssh-handler-setup` |
| Open docs | — | `ssh-handler-docs` |
| GUI | — | `ssh-handler-gui` |

Every action returns a structured result (`CommandResult`, `TransferResult`,
`ShellResult`) or, in safe mode, an `OperationResult`. Passwords are wrapped in
`Secret` and never appear in logs.

---

## Local vs Remote (via RDP / jump host)

The **only difference is the config** — every method is identical. Add a
`jump_host` to route through your RDP machine.

### Connect — local (direct)
```python
from ssh_handler import SSHHandler, SSHConfig
cfg = SSHConfig(host="10.0.0.5", username="root", password="pw",
                host_key_policy="ignore")
with SSHHandler(cfg, quiet=True) as ssh:
    print(ssh.run("uname -a").text)
```

### Connect — via RDP (laptop → RDP machine → target)
```python
rdp = SSHConfig(host="10.232.9.22", domain="CORP", username="user", password="pw")
target = SSHConfig(host="10.120.1.91", username="root", password="pw",
                   jump_host=rdp, host_key_policy="ignore")
with SSHHandler(target, quiet=True) as ssh:
    print(ssh.run("uname -a").text)
```

### Files (SFTP) — local and via RDP (same calls)
```python
ssh.push("firmware.bin", "/tmp/firmware.bin")           # upload
ssh.pull("/var/log/messages", "messages.log")           # download
ssh.push("./build", "/tmp/build", recursive=True)       # folder up
ssh.pull("/etc/nginx", "./nginx", recursive=True)       # folder down
# with progress:
ssh.pull("/big.img", "big.img",
         callback=lambda done, total: print(f"\r{done}/{total}", end=""))
```
Via RDP: identical — the transfer rides the `jump_host` tunnel automatically.

### Continuous logs (slog2info / tail -f) with match + save
```python
ssh.stream("slog2info -w", on_line=print,
           match=r"error|fail", save_to="device.log", timeout=120)
```
Works local or via RDP. Output is auto-cleaned of ANSI escape codes.

### Serial
- **Port on the machine running the code (e.g. your laptop):**
  ```python
  from ssh_handler import SerialHandler
  with SerialHandler("COM5", baudrate=115200, quiet=True) as ser:
      ser.write_line("version")
      ser.stream(on_line=print, match=r"login:", save_to="com5.log")
  ```
- **Port on a remote machine (via RDP/SSH):** auto-detects COM (Windows) vs
  `/dev/tty*` (Linux):
  ```python
  with SSHHandler(cfg_of_machine_with_port, quiet=True) as ssh:
      ssh.serial_write("COM5", "version", baudrate=115200)
      ssh.serial_stream("COM5", baudrate=115200, on_line=print,
                        match=r"login:", save_to="com5.log")
  ```

---

## CLI quick reference

```bash
ssh-handler run    --host H --user U [--domain CORP] [--use-stored] uname -a
ssh-handler push   --host H --user U ./build /tmp/build --recursive
ssh-handler pull   --host H --user U /var/log ./logs --recursive
ssh-handler info   --host H --user U --json
ssh-handler stream --host H --user U --match "error|fail" --save run.log -- slog2info -w
ssh-handler list-serial
ssh-handler serial-monitor --port COM5 --baud 115200 --match "login:" --save c.log
ssh-handler store-credential --user U --domain CORP --service my_lab
ssh-handler-setup                 # install OpenSSH Server on THIS machine (offline)
ssh-handler-gui                   # launch the PyQt5 app
ssh-handler-docs                  # open the docs in a browser
```
Password options: `--password` (hidden prompt), `--use-stored` (OS vault),
`--key FILE`. Add `--json` for machine-readable output.

---

## GUI application

```bash
ssh-handler-gui
```
A window with a Connection panel (incl. a "Via jump host (RDP machine)" toggle),
and tabs for **Command**, **Files (SFTP)**, **Serial**, and **Log stream** — each
operation is a button, and all commands, results, and live logs appear in one log
pane (with Clear / Save log). Opens the docs on first launch.

### Build a standalone .exe (with the automotive-SSH icon)
```bash
pip install "ssh-handler[gui]" pyinstaller
python scripts/build_exe.py            # -> dist/ssh-handler-gui/ssh-handler-gui.exe
python scripts/build_exe.py --onefile  # -> dist/ssh-handler-gui.exe
```
The bundled `ssh_handler/assets/icon.ico` (a car over a `>_` prompt) is used as
the executable and window icon.

---

## Enabling SSH on a Windows / RDP machine (offline)

```bash
ssh-handler-setup            # self-elevates, installs OpenSSH Server from the
                             # bundled ZIP (ARM64/Win64/Win32), starts sshd,
                             # opens the firewall, generates + fixes host keys
```
No Windows Update / internet needed. After this, connect to that machine over SSH
(directly or as a `jump_host`).

---

## Releasing (maintainers)

```bash
python scripts/release.py patch            # bump -> test -> build -> upload
python scripts/release.py 1.6.1 --dry-run  # build + check only
```
Token is read from `TWINE_PASSWORD` (never hard-coded).
