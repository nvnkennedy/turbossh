# Architecture

This is a map of how TurboSSH is put together — what lives where, and why a few
of the trickier parts work the way they do. If you're extending it or debugging
something, start here.

## The one-object, three-consumers idea

Everything funnels through a single `SSHHandler`. The same object is meant to be
used three ways, and the only thing that really changes between them is how
errors come back:

| Consumer            | How it's used                                  |
|---------------------|------------------------------------------------|
| Test framework      | default raise-on-error; catch typed exceptions |
| CLI script          | `turbossh.cli` parses args and calls the same methods |
| PyQt5 GUI           | `safe=True`, so calls return `OperationResult` instead of raising |

`safe=True` is the important toggle. In a GUI you can't let a dropped socket
throw out of a slot and kill the event loop, so safe mode wraps every public
call in an `OperationResult` (`bool(res)`, `.value`, `.error`). The test
framework wants the opposite — fail loud, fail typed — so raising is the default.

## Package layout

```
turbossh/
├── core.py            SSHHandler: commands, shells, SFTP, serial-over-SSH, forwards
├── config.py          SSHConfig / FTPConfig dataclasses
├── credentials.py     Secret (self-masking), CredentialStore (OS keyring)
├── results.py         CommandResult, TransferResult, OperationResult, strip_ansi
├── exceptions.py      SSHError hierarchy
├── serial_handler.py  local pyserial wrapper (SerialHandler)
├── tunnel.py          LocalForward / RemoteForward, generate_keypair
├── pool.py            SSHPool — run across many hosts
├── ftp.py             plain-FTP fallback (FTPHandler)
├── winrm_bootstrap.py offline OpenSSH install over WinRM
├── cli.py             the `turbossh` command-line tool
├── setup_openssh_server.ps1   the offline OpenSSH installer (self-elevating)
├── openssh/           the bundled OpenSSH ZIPs (ARM64 / Win64 / Win32)
├── bin/               the prebuilt GUI .exe (shipped in the wheel)
├── assets/            icons
└── gui/               the PyQt5 application
    ├── app.py             QApplication setup, theme, crash hook, shortcuts
    ├── main_window.py     ribbon, menu bar, sidebar, tabs, log dock
    ├── session_widgets.py SshSessionWidget / SerialSessionWidget
    ├── session_dialog.py  new/edit session dialog
    ├── settings*.py       persisted settings + dialog
    ├── vt100.py           the VT100 terminal widget (pyte-backed)
    ├── terminal.py        ReaderThread (buffered pull model)
    ├── sftp_browser.py    the file panel
    ├── logs_tab.py        the live-log viewer
    ├── theme.py           dark/light stylesheet
    └── updater.py         in-app "check for updates"
```

## Core: SSHHandler

Built on Paramiko. Beyond the obvious `run` / `push` / `pull`, the parts worth
knowing:

- **Jump hosts** chain by nesting `SSHConfig.jump_host`; the handler opens the
  jump connection, then a channel through it to the target.
- **Streaming** (`iter_lines` / `stream`) opens its own exec channel and yields
  output line by line. It can merge stderr (so tools that log there, like
  `slog2info`, actually show up) and is given a PTY when run from the GUI's
  Logs tab so line-buffered programs flush instead of sitting in a pipe buffer.
- **Legacy algorithms** re-enable the ciphers/kex Paramiko drops by default, for
  old embedded SSH servers.
- **Safe mode** is implemented by a single `_guard` wrapper around every public
  method.

## GUI: the buffered pull model

Continuous output (a chatty `journalctl -f`, a flood of serial data) will freeze
a naive GUI that emits a signal per line. So the terminal never does that.
Instead:

1. A `ReaderThread` reads the channel/port on a background thread into a capped
   `bytearray`.
2. The terminal widget drains that buffer on a `QTimer` (~30 fps), feeding a
   bounded amount per tick into pyte.

The UI thread only ever does a fixed slice of work per frame, so throughput
can't lock it up. On Ctrl-C the reader buffer is flushed so a flood stops on
screen immediately instead of trickling out for seconds.

Scrollback is pyte's `HistoryScreen` (≈16 KB/line, so it's capped — 10k lines by
default, configurable). The *full* session is teed to a file on disk as it
streams, which is what **Save all output** reads — that part is unbounded.

## Serial over SSH

This is the subtle one. The board is on a Windows RDP machine; we want a native
terminal to it from the laptop, through SSH.

`serial_bridge()` opens a raw SSH channel and runs a PowerShell process on the
RDP box that bridges the COM port both ways: it polls `SerialPort.BytesToRead`
and writes to stdout, and reads stdin asynchronously and writes to the port. Two
details make it actually work:

- **It allocates a PTY.** Without one, Windows OpenSSH buffers stdin and your
  keystrokes never reach the port (output works, input doesn't). The SSH
  *terminal* worked all along precisely because it uses an interactive shell,
  i.e. a PTY — the serial bridge needed the same.
- **It sets the remote console to raw mode** (no line buffering, no echo) via a
  small `SetConsoleMode` P/Invoke, so input is character-by-character and the
  device's own echo is the only echo. That's what makes Tab-completion and
  Ctrl-C behave like a real terminal.

Port lifecycle is handled explicitly, because Windows OpenSSH likes to orphan
the child process on disconnect:

- Each bridge writes its PID to `%TEMP%\turbossh_ser_<port>.pid`.
- **Open** is self-healing: it kills the previously-tracked holder, and on a
  forced open clears any other TurboSSH serial process holding the port, then
  retries.
- **`serial_in_use()`** lets the GUI ask before taking a busy port.
- **`serial_release()`** kills the bridge by PID on close (session close, app
  close), and the PTY teardown covers an unexpected crash.

`/dev/*` ports use the equivalent shell construction (`stty raw` + paired `cat`,
`fuser -k` to free a stale holder).

## OpenSSH, bundled and offline

Corporate networks block the Windows-Update path that installs OpenSSH, so the
binaries ship inside the package (`turbossh/openssh/*.zip`, one per arch).

- `setup_openssh_server.ps1` installs from the matching ZIP, generates host
  keys, fixes their ACLs, registers `sshd` as an automatic service, and opens
  the firewall — self-elevating to admin and writing a result file the GUI
  polls. It repairs an existing-but-broken install on every run.
- `winrm_bootstrap.py` does the same to a *remote* machine: it picks the ZIP for
  the remote's CPU architecture, ships it over WinRM (admin-share copy when
  available, base64 chunks otherwise), and runs the install there.

## Build & release

The GUI is shipped as a one-file PyInstaller `.exe` bundled inside the wheel at
`turbossh/bin/`, so `pip install turbossh` gives a working GUI even where PyQt5
can't be installed.

```
python scripts/build_exe.py --onefile   # build dist/turbossh-gui.exe
# copy it to turbossh/bin/turbossh-gui.exe
python -m build --wheel                  # build the wheel (bundles the exe)
python -m twine upload dist/*.whl        # publish
```

`scripts/release.py` automates the version bump + build + check for the
ordinary case.
