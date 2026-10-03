# TurboSSH command-line reference

Everything the `turbossh` command can do. `tests/test_cli_docs.py` compares this
file with the real parser, so every command and every option below exists, and
nothing that exists is missing here.

```text
turbossh <command> [options]        # or: python -m turbossh <command> [options]
turbossh --help                     # the command list
turbossh <command> --help           # one command's options
turbossh --version                  # or -V
```

Installed entry points: `turbossh` (this CLI), `turbossh-gui`, `turbossh-setup`,
`turbossh-docs`, `turbossh-shortcut`.

- [Conventions](#conventions): credentials, saved sessions, JSON, exit codes
- [Connection options](#connection-options)
- Remote commands: [run](#turbossh-run) · [sudo](#turbossh-sudo) ·
  [stream](#turbossh-stream) · [info](#turbossh-info) · [ls](#turbossh-ls) ·
  [push](#turbossh-push) · [pull](#turbossh-pull) · [forward](#turbossh-forward) ·
  [copy-id](#turbossh-copy-id)
- Serial: [list-serial](#turbossh-list-serial) ·
  [serial-monitor](#turbossh-serial-monitor) · [serial-ssh](#turbossh-serial-ssh) ·
  [scan-ports](#turbossh-scan-ports)
- Camera: [camera-list](#turbossh-camera-list) · [camera-grab](#turbossh-camera-grab)
- Setup: [setup-server](#turbossh-setup-server) ·
  [install-ssh-remote](#turbossh-install-ssh-remote) · [diagnose](#turbossh-diagnose)
- Keys and credentials: [store-credential](#turbossh-store-credential) ·
  [keygen](#turbossh-keygen) · [sessions](#turbossh-sessions)
- The app: [gui](#turbossh-gui) · [docs](#turbossh-docs) ·
  [shortcut](#turbossh-shortcut) · [check-update](#turbossh-check-update) ·
  [version](#turbossh-version)

## Conventions

### Credentials

A password is never taken from the command line (it would show up in the process
list and the shell history). It comes from, in this order:

1. `--use-stored`: the OS credential vault (see [store-credential](#turbossh-store-credential));
2. `--password`: a hidden prompt;
3. `--password-stdin`: one line read from standard input, for scripts;
4. the `TURBOSSH_PASSWORD` environment variable;
5. the password stored with a saved session, when `--session` is used.

With none of these, TurboSSH logs in with your SSH key (`--key`, the SSH agent,
or the default keys in `~/.ssh`) and, failing that, tries a blank password, which
is what many lab targets use. `--passwordless` restricts it to keys.

A jump host's password comes from `TURBOSSH_JUMP_PASSWORD` or the saved session.

```bash
echo "$PW" | turbossh run --host 10.0.0.5 --user root --password-stdin uname -a
TURBOSSH_PASSWORD=secret turbossh run --host 10.0.0.5 --user root uname -a
```

### Saved sessions

`--session NAME` (or `-s NAME`) uses a session saved in the GUI: its host, port,
user, key file, jump host, host-key setting and stored passwords. Any connection
flag given as well overrides that one value. [`turbossh sessions`](#turbossh-sessions)
lists the names.

```bash
turbossh run -s lab-qnx slog2info
turbossh pull -s lab-qnx /var/log/messages ./messages.txt
```

### JSON output

Commands that print data take `--json` and then print one JSON document on
standard output; progress and messages always go to standard error, so the
output can be piped.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | the operation failed (cannot connect, wrong password, transfer error, …) |
| 2 | bad usage or a missing prerequisite (no target, no ffmpeg, unknown session) |
| 130 | interrupted with Ctrl+C |
| other | `run` and `sudo` return the remote command's own exit code |

Expected failures print `ERROR: …` on standard error; they do not end in a
traceback.

### State folder

Saved sessions, settings and the cached ffmpeg live in `~/.turbossh`. Set
`TURBOSSH_HOME` to use another folder.

## Connection options

Every command that connects to a host accepts these:

| Option | Meaning |
|---|---|
| `-s NAME`, `--session NAME` | use a session saved in the GUI |
| `--host HOST` | host name or address (required unless `--session` is given) |
| `--port PORT` | SSH port, default 22 |
| `--user USER` | login user |
| `--domain DOMAIN` | Windows domain, for `DOMAIN\user` logins |
| `--key FILE` | private key file |
| `--password` | prompt for the password |
| `--password-stdin` | read the password from standard input |
| `--use-stored` | read the password from the OS credential vault |
| `--service NAME` | credential-vault namespace, default `turbossh` |
| `--passwordless` | keys / agent only; never send a password |
| `--jump-host HOST` | connect through this jump host (bastion) |
| `--jump-user USER` | user on the jump host |
| `--jump-port PORT` | SSH port of the jump host, default 22 |
| `--host-key-policy POLICY` | `auto` (default: accept new keys, reject changed ones), `reject` (known hosts only), `warn`, or `ignore` (lab devices that get re-imaged) |
| `--legacy` | also offer old key-exchange and cipher algorithms (old ECUs) |
| `--timeout SECONDS` | how long a remote command may run |
| `--no-fast-auth` | probe keys and the agent even when a password is given |
| `--json` | machine-readable output |

## Remote commands

### `turbossh run`

Run a command on the remote host. Its output is printed as it was produced
(stdout to stdout, stderr to stderr) and its exit code becomes the exit code.

```bash
turbossh run --host 10.0.0.5 --user root uname -a
turbossh run --host 10.0.0.5 --user root "ls -l /tmp | head"
turbossh run -s lab-qnx --timeout 20 --json pidin info
```

Arguments are joined with spaces, as `ssh` does. To keep a command's own quoting
or to use pipes and `&&`, pass it as one quoted argument. With `--json` the
result is `{command, exit_code, stdout, stderr, duration, host, started_at}`.
A command still running after `--timeout` seconds is stopped and reported as an
error.

Options: the [connection options](#connection-options).

### `turbossh sudo`

Run a command through `sudo`. The connection's password is sent to sudo only if
sudo asks for one, so it never appears in the output. The exit code is the
command's.

```bash
turbossh sudo --host 10.0.0.5 --user admin --password mount -o remount,rw /
```

| Option | Meaning |
|---|---|
| `--pty` | allocate a terminal, for hosts whose sudoers has `requiretty` |

Plus the [connection options](#connection-options).

### `turbossh stream`

Run a command that keeps printing (`tail -f`, `journalctl -f`, `slog2info -w`)
and show its lines live until it ends or you press Ctrl+C.

```bash
turbossh stream --host 10.0.0.5 --user root --match "error|fail" --save run.log slog2info -w
turbossh stream -s lab-qnx --match "login:" --stop-on-match "cat /dev/ser1"
```

| Option | Meaning |
|---|---|
| `--match REGEX` | count the lines that match (reported at the end) |
| `--save FILE` | also append every line to this local file |
| `--stop-on-match` | stop as soon as a line matches |

Plus the [connection options](#connection-options).

### `turbossh info`

Connect and report what kind of system the host is (`linux` or `windows`).

```bash
turbossh info --host 10.0.0.5 --user root --json
```

Options: the [connection options](#connection-options).

### `turbossh ls`

List a remote folder over SFTP: permissions, size, modification time, name,
folders first. The path defaults to the login folder.

```bash
turbossh ls --host 10.0.0.5 --user root /var/log
turbossh ls -s lab-qnx --json /tmp
```

Options: the [connection options](#connection-options).

### `turbossh push`

Upload a file, or a folder with `--recursive`, over SFTP. Missing remote folders
are created.

```bash
turbossh push --host 10.0.0.5 --user root app.bin /opt/app/app.bin
turbossh push -s lab-qnx --recursive ./build /tmp/build
```

| Option | Meaning |
|---|---|
| `--recursive` | upload a folder and everything in it |

Plus the [connection options](#connection-options).

### `turbossh pull`

Download a file, or a folder with `--recursive`, over SFTP. A download that
fails part-way does not leave a partial file behind.

```bash
turbossh pull --host 10.0.0.5 --user root /var/log/messages ./messages.txt
turbossh pull -s lab-qnx --recursive /var/log ./logs
```

| Option | Meaning |
|---|---|
| `--recursive` | download a folder and everything in it |

Plus the [connection options](#connection-options).

### `turbossh forward`

Open a local port that leads, through the SSH connection, to a host and port
reachable from the remote side (`ssh -L`). It runs until Ctrl+C or until the
connection drops.

```bash
turbossh forward --host gateway --user me --local-port 8080 --to-host 192.168.7.2 --to-port 80
```

| Option | Meaning |
|---|---|
| `--local-port PORT` | the port to open on this machine |
| `--to-host HOST` | the destination, as seen from the SSH server |
| `--to-port PORT` | the destination port |

Plus the [connection options](#connection-options).

### `turbossh copy-id`

Add a public key to the remote account's `~/.ssh/authorized_keys`, so later
logins need no password (`ssh-copy-id`). For Linux / QNX hosts.

```bash
turbossh copy-id --host 10.0.0.5 --user root --password
turbossh copy-id -s lab-qnx --public-key ~/.ssh/lab.pub
```

| Option | Meaning |
|---|---|
| `--public-key FILE` | the `.pub` file; default `~/.ssh/id_ed25519.pub`, then `~/.ssh/id_rsa.pub` |

Plus the [connection options](#connection-options).

## Serial

### `turbossh list-serial`

List the serial / COM ports on this machine.

```bash
turbossh list-serial
turbossh list-serial --json
```

| Option | Meaning |
|---|---|
| `--json` | machine-readable output |

### `turbossh serial-monitor`

Show what arrives on a local serial port, live, until Ctrl+C.

```bash
turbossh serial-monitor --port COM5 --baud 115200 --match "login:" --save console.log
```

| Option | Meaning |
|---|---|
| `--port PORT` | the port, e.g. `COM5` or `/dev/ttyUSB0` |
| `--baud RATE` | baud rate, default 115200 |
| `--match REGEX` | count the lines that match |
| `--save FILE` | also append every line to this local file |
| `--stop-on-match` | stop as soon as a line matches |
| `--timeout SECONDS` | stop after this long |

### `turbossh serial-ssh`

Monitor a serial port that is plugged into the *remote* machine, over SSH. The
port is released on that machine when the command ends.

```bash
turbossh serial-ssh --host rdp-box --user me --password --device COM4 --baud 115200
turbossh serial-ssh -s rdp-box --device COM4 --send "reset" --match "U-Boot" --stop-on-match
```

| Option | Meaning |
|---|---|
| `--device PORT` | the remote port, e.g. `COM4` or `/dev/ser1` |
| `--baud RATE` | baud rate, default 115200 |
| `--match REGEX` | count the lines that match |
| `--save FILE` | also append every line to this local file |
| `--stop-on-match` | stop as soon as a line matches |
| `--send TEXT` | write this line to the port before monitoring |

Plus the [connection options](#connection-options).

### `turbossh scan-ports`

List the serial ports on the remote machine (COM ports with their friendly names
on Windows, device nodes on Linux / QNX).

```bash
turbossh scan-ports --host rdp-box --user me --password
turbossh scan-ports -s rdp-box --json
```

Options: the [connection options](#connection-options).

## Camera

Both camera commands work on this machine when no target is given, and on a
remote Windows machine when `--host` or `--session` is. They need ffmpeg: on
this machine it is found on `PATH` or in the GUI's cache.

### `turbossh camera-list`

List the cameras.

```bash
turbossh camera-list
turbossh camera-list --host rdp-box --user me --password --json
```

| Option | Meaning |
|---|---|
| `--ffmpeg PATH` | the local ffmpeg to use |
| `--remote-ffmpeg PATH` | ffmpeg on the remote machine, default: the one on its `PATH` |

Plus the [connection options](#connection-options) (all optional here).

### `turbossh camera-grab`

Save a snapshot, or a short clip with `--seconds`.

```bash
turbossh camera-grab --camera "USB Camera" --out shot.jpg
turbossh camera-grab --host rdp-box --user me --password --camera "USB Camera" --seconds 5 --out clip.mp4
```

| Option | Meaning |
|---|---|
| `--camera NAME` | the camera, as printed by `camera-list` |
| `--out FILE` | output file: `.jpg`, or `.mp4` with `--seconds` |
| `--seconds N` | record this long; 0 (the default) takes one snapshot |
| `--width PIXELS` | maximum width of a clip, default 1280 |
| `--fps N` | frame rate of a clip, default 25 |
| `--force` | first stop a stale ffmpeg that is still holding the camera on the remote machine |
| `--ffmpeg PATH` | the local ffmpeg to use |
| `--remote-ffmpeg PATH` | ffmpeg on the remote machine |

Plus the [connection options](#connection-options) (all optional here).

## Setup

### `turbossh setup-server`

Install and start OpenSSH Server on *this* Windows machine from the copy bundled
with TurboSSH (no internet needed), and open the firewall. Windows asks for
Administrator rights.

```bash
turbossh setup-server
turbossh setup-server --port 2222
```

| Option | Meaning |
|---|---|
| `--port PORT` | the port sshd should listen on, default 22 |
| `--install-pip` | also install TurboSSH with pip on this machine |

### `turbossh install-ssh-remote`

Install OpenSSH Server on *another* Windows machine over WinRM, uploading the
bundled copy. The account must be a local administrator there, and WinRM must be
reachable. The password is asked for with a hidden prompt.

```bash
turbossh install-ssh-remote --host 10.0.0.9 --user admin --domain CORP
```

| Option | Meaning |
|---|---|
| `--host HOST` | the machine to install on |
| `--user USER` | a local-administrator account on it |
| `--domain DOMAIN` | Windows domain of that account |
| `--ssh-port PORT` | the port sshd should listen on, default 22 |
| `--winrm-port PORT` | WinRM port, default 5985 |

### `turbossh diagnose`

Check whether a host's SSH port can be reached, without logging in. Exit code 0
if it can, 1 if not; the message says whether the host looks up but has no SSH
server.

```bash
turbossh diagnose --host 10.0.0.5
turbossh diagnose --host 10.0.0.5 --port 2222 --json
```

| Option | Meaning |
|---|---|
| `--host HOST` | the host to probe |
| `--port PORT` | its SSH port, default 22 |
| `--json` | machine-readable output |

## Keys and credentials

### `turbossh store-credential`

Save a password in the OS credential vault (Windows Credential Manager, macOS
Keychain, Secret Service) for `--use-stored`. The password is asked for with a
hidden prompt.

```bash
turbossh store-credential --user root --host 10.0.0.5
turbossh run --host 10.0.0.5 --user root --use-stored uname -a
```

| Option | Meaning |
|---|---|
| `--user USER` | the account |
| `--domain DOMAIN` | Windows domain of the account |
| `--host HOST` | store it for this host only (recommended); without it, one entry is shared by every host for that user |
| `--service NAME` | vault namespace, default `turbossh` |

### `turbossh keygen`

Generate an RSA key pair: the private key at the given path and the public key
next to it as `<path>.pub`. The public key line is printed.

```bash
turbossh keygen ~/.ssh/lab
turbossh copy-id --host 10.0.0.5 --user root --password --public-key ~/.ssh/lab.pub
turbossh run --host 10.0.0.5 --user root --key ~/.ssh/lab uname -a
```

| Option | Meaning |
|---|---|
| `--bits N` | key size, default 3072 |
| `--comment TEXT` | the comment at the end of the public key, default `turbossh` |
| `--passphrase-stdin` | read a passphrase for the key from standard input |
| `--force` | overwrite an existing key file |

### `turbossh sessions`

List the sessions saved in the GUI. Their names work with `--session`.

```bash
turbossh sessions
turbossh sessions --json
```

| Option | Meaning |
|---|---|
| `--json` | machine-readable output |

## The app

### `turbossh gui`

Open the graphical app. Same as the `turbossh-gui` entry point.

### `turbossh docs`

Open the documentation in a browser. Same as `turbossh-docs`.

### `turbossh shortcut`

Create the Desktop and Start-Menu shortcuts (Windows). Same as
`turbossh-shortcut`.

### `turbossh check-update`

Compare the installed version with the latest release on PyPI. Exit code 1 if
PyPI could not be reached.

```bash
turbossh check-update
turbossh check-update --json
```

| Option | Meaning |
|---|---|
| `--json` | machine-readable output: `{installed, latest, update_available}` |

### `turbossh version`

Print the version. Same as `turbossh --version`.
