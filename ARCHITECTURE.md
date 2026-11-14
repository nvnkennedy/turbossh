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
├── update.py          self-update logic (no Qt): versions, owning interpreter
├── paths.py           the per-user state folder (TURBOSSH_HOME / ~/.turbossh)
├── _jsonstore.py      atomic JSON writes; corrupt files copied aside
├── _logging.py        the package logger (one console handler, opt-out)
├── cli.py             the `turbossh` command-line tool
├── setup_openssh_server.ps1   the offline OpenSSH installer (self-elevating)
├── openssh/           the bundled OpenSSH ZIPs (ARM64 / Win64 / Win32)
├── bin/               the prebuilt GUI .exe (shipped in the wheel)
├── assets/            icons
└── gui/               the PyQt5 application
    ├── app.py             QApplication setup, theme, crash hook, shortcuts
    ├── main_window.py     ribbon, menu bar, sidebar, tabs, log dock
    ├── embed.py           TurboSSHWidget / EmbeddedSession: the public API for
    │                      hosting the whole GUI inside another PyQt5 application
    ├── control.py         --embed / --control: the GUI as a guest of ANY
    │                      application (native reparenting + JSON-lines channel)
    ├── options.py         the GUI's command-line options (no Qt)
    ├── session_widgets.py SshSessionWidget / SerialSessionWidget
    ├── session_dialog.py  new/edit session dialog
    ├── settings*.py       persisted settings + dialog
    ├── vt100.py           the terminal widget: painting, keys, selection, paste
    ├── termscreen.py      its screen model (no Qt): pyte + alternate screen,
    │                      scrollback, query replies, fast text path
    ├── terminal.py        ReaderThread (buffered pull model)
    ├── qtutil.py          worker-thread / teardown helpers (park_thread, run_job)
    ├── file_browser.py    dual-pane file browser: local pane + remote pane
    ├── sftp_browser.py    the remote pane and its transfer queue
    ├── transfer_log.py    transfer history model (no Qt): progress, speed, ETA
    ├── transfer_panel.py  the Transfers table (filter, retry, report)
    ├── remote_edit.py     edit a remote text file in place
    ├── local_terminal.py  a local shell in a tab
    ├── localshell.py      its process + line editor (no Qt)
    ├── toast.py           corner notifications
    ├── logs_tab.py        the live-log viewer (level colours, find)
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
- **Connection lifetime.** With `auto_reconnect`, a `run` / `push` after the
  link dropped, or after `disconnect()`, connects again. The GUI disposes of a
  handler with `disconnect(sticky=True)`, so a worker still running cannot bring
  a closed tab's connection back. `connect()` on a handler that looks connected
  checks the link first by opening a session channel, which every SSH server
  must answer, yes or no. Any packet that arrives meanwhile also counts. After a
  target reboot the old TCP link only looks alive. With no sign of life within
  `max(connect_timeout, 10 s)`, the old client is closed and a new link is
  opened. A changed config (host, port, login, authentication, jump host) also
  gets a new link. Closing always stops the transport first and only then
  forgets the SFTP client, so a link stalled under another thread's upload
  cannot block `connect()` or `disconnect()`.
- **Timeouts everywhere a peer can stall:** channel requests (PTY, exec, shell,
  subsystem), SFTP requests (`SSHConfig.sftp_timeout`, 60 s), and the WinRM
  bootstrap's remote commands. A timeout is raised, never taken for "file not
  there".
- **known_hosts** is read line by line (`@cert-authority` and `@revoked` lines
  are skipped, not fatal). New keys are appended under a lock, never rewritten.

## GUI: the buffered pull model

Continuous output (a chatty `journalctl -f`, a flood of serial data) will freeze
a naive GUI that emits a signal per line. So the terminal never does that.
Instead:

1. A `ReaderThread` reads the channel/port on a background thread into a capped
   `bytearray`.
2. The terminal widget drains that buffer on a `QTimer` (~30 fps), feeding a
   bounded amount per tick into pyte.

The UI thread only ever does a fixed slice of work per frame, so throughput
can't lock it up: one shared timer and one time budget serve every terminal, in
2 KB slices. On Ctrl-C the reader buffer is flushed so a flood stops on screen
immediately instead of trickling out for seconds.

The other direction works the same way. Typing, pasting and PTY resizes are
queued to a **writer thread** per session (SSH, serial, serial over SSH, local
shell), so a rekey, a stalled link, a low baud rate or a busy shell never blocks
the GUI thread. A full SSH window means "try again", not "give up". Ctrl-C drops
the queued paste and goes out first. When a reader reaches end-of-file, it
first hands over the output it already has (logout and reboot messages, the
serial bridge's BUSY line), and only then reports the end.

Scrollback lines are compact tuples of shared cells (about 1.8 KB a line).
The default is 10k lines, and it is configurable. The *full* session is
captured to disk as it streams, which is what **Save all output** reads. The
capture keeps the latest 256 MB in four rotating parts. A shell that exits
(`exit`, a nologin account) is not reconnected; a link that dies is.

The **Logs tab** keeps every line its worker receives, but the view shows only
the latest 50k lines. It is built in timed steps, newest matches first. Save
writes every stored line that passes the filter, on a worker thread.

## GUI: one window, two hosts

`MainWindow` is both the desktop app and, with `WindowOptions(embedded=True)`, a
child widget inside someone else's application. `embed.TurboSSHWidget` wraps it
in that mode and is the only supported way in (EMBEDDING.md is its reference).
What changes when it is hosted is everything that is application-wide in Qt:

- **Stylesheet.** `_apply_stylesheet` sets it on the window itself, not on the
  `QApplication`; dialogs and menus inherit it because they are parented to the
  window (`qtutil.owner_window` finds it, since `widget.window()` would be the
  host's). `theme.set_active` holds a host-chosen theme that is never saved.
- **Shortcuts.** Window-context shortcuts would take keys from the whole host
  window, and a key both define fires neither. Hosted, they are
  `WidgetWithChildrenShortcut`, and menu actions are also added to the window,
  because that context only matches widgets that can hold the focus.
- **Closing.** Nothing hosted quits the application or offers Exit or the
  self-update (which closes and relaunches the process).
- **Lifetime.** A child widget gets no `closeEvent` when the host closes, and
  its sessions own running QThreads, which Qt aborts the process for if one is
  destroyed. So `embed._LIVE` holds every widget until it is shut down, and the
  shutdown is triggered by `aboutToQuit`, by the widget's `destroyed` signal
  (deleteLater, a deleted parent), or by an explicit `shutdown()`.
- **Signals from plain threads.** Teardown threads outlive their tab: the
  handler logs while it disconnects, a save or a recording finishes later.
  Emitting into a widget Qt has deleted is an access violation, not an
  exception, so those threads emit through a `qtutil.SignalGate` that the widget
  closes in `close_session` / `cleanup`.

For applications that are not PyQt5 (another toolkit, another language, or a
PySide6 / PyQt6 program that cannot load PyQt5 next to its own Qt), `control.py`
runs the same `TurboSSHWidget` in a process of its own:

- **`--embed HANDLE`**: Qt's `QWindow.fromWinId` wraps the host's native window
  and becomes the parent of TurboSSH's window (Qt turns it into a `WS_CHILD`). A
  50 ms timer keeps it the size of the parent's client area and notices when
  the parent is destroyed. Its UI thread joins the host window's thread input
  (`AttachThreadInput`) — the keyboard goes to the focus window of the
  foreground thread's input state, and `SetFocus` fails across separate ones —
  and a click inside calls `SetFocus` (a child window does not get the keyboard
  by itself). Before Qt starts, it takes on the parent
  window's DPI awareness (`-platform windows:dpiawareness=N`): across DPI
  contexts Windows rescales child coordinates, and an unaware host saw TurboSSH
  25 % too large at 125 % scaling.
- **`--control stdio`**: a reader thread turns stdin lines into a Qt signal; the
  GUI thread dispatches `cmd_*` methods and writes one JSON line per answer or
  event. Standard output is then pointed at standard error so a stray `print`
  cannot corrupt the channel.
- **No cross-process deadlock.** The two processes share an input queue (Windows
  attaches them for a cross-process parent / child), so a focus change in one
  can send a message the other's UI thread must answer. So every request is
  answered before any UI work happens, nothing waits on the host, the window
  has `WS_EX_NOPARENTNOTIFY`, and the Python client waits with
  `PeekMessage(PM_QS_SENDMESSAGE)` so a host blocking in a call still answers.
- `turbossh/embed_client.py` is the Qt-free client: it starts the process
  (`python -m turbossh.gui`, or the bundled exe) and speaks the protocol.

The Camera is optional in both hosts: off by default (`camera_enabled`), and
while off there is no Camera action, menu entry or Home card, and
`camera_widget` (which pulls in the SSH stack) is never imported. Opening a
Camera tab does not fetch ffmpeg either; only Scan cameras does.

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

- Each bridge writes its PID to `%TEMP%\turbossh_ser_<port>.pid`, but only once
  `Open()` has succeeded, and removes the file only while it still names that
  bridge. A failed retry on a busy port therefore never erases the record of the
  real holder.
- **Open never takes a port someone holds.** A busy port reports `BUSY`.
  "Busy" means access denied, or an `IOException` that says "in use" or names a
  sharing violation (some USB-serial drivers). The GUI then asks. A **forced**
  open stops only TurboSSH's own serial PowerShell processes for that port, then
  retries. They are recognised by a port tag in the `-EncodedCommand` command
  line, and the force never matches this process or its parents: under a
  PowerShell default shell, the sshd wrapper carries the same text.
- **A forced open is two commands:** first the forced stop (the same as
  `serial_release(force=True)`), then the plain open, which retries while the
  stopped process lets go of the port. Every Windows command stays under 8,000
  characters, because Windows OpenSSH's default shell runs it through
  `cmd /c`, which refuses lines over 8,191. Each script carries only the
  pieces it uses, and `serial_write` sends long data as several commands in
  order. A test checks the length of every command builder.
- **`serial_in_use()`** lets the GUI ask before taking a busy port.
- **`serial_release()`** stops the bridge's own processes on close (session
  close, app close). The PTY teardown covers an unexpected crash. With nothing
  of its own to stop, it returns exit status 1 and suggests `force=True`.
- **`serial_stream()`** (scripts, `turbossh serial-ssh`) stops its own reader
  when it ends. It touches a port's tracked holder only with `force=True`,
  because that holder may be the GUI's live session on the port.

`/dev/*` ports use the equivalent shell construction, a paired `cat`. The device
is configured with `stty -F`, or through stdin where stty has no `-F` (QNX). The
SSH PTY is set raw (`-echo -isig`) but keeps the newline translations
(`icrnl opost onlcr`), so a device that prints bare `\n` does not staircase.
The bridge records its two PIDs in a private folder
(`${TMPDIR:-/tmp}/turbossh-<uid>/`, 0700, never a symlink). `serial_release`
stops only those. `fuser -k` frees the device only with force.

## Transfers: one policy

`turbossh/_transfer.py` (no Qt) decides how a file is written, for
`SSHHandler.push` / `pull`, the file browser's uploads and downloads, and the
remote editor's saves:

- **A new file** is written under a temporary name in the same folder and
  renamed into place. No partial file ever has the real name, and it costs no
  more round trips than writing it directly.
- **An existing regular file with one link** is replaced the same way. The new
  copy gets the old one's mode and, if they differ, its owner and group
  (`chown`). A root push used to leave a service's 0600 config root-owned.
- **Written in place, as 2.0 did,** where replacing would change what the file
  is or cannot be done. That covers:
  - more than one hard link;
  - not a regular file (`/dev/null`, a FIFO, a block device);
  - a folder the temporary file may not be created in;
  - an owner or a mode the copy may not be given;
  - a file that cannot be renamed over (a bind mount, a file another program
    holds open on Windows);
  - no room for a second copy (the server's "Failure" while writing it). This
    applies only when the new data fits where the old file is. Otherwise the
    transfer fails and the old file is kept whole.

  The log says so.
- **Symlinks** are resolved by the server (`realpath`), so the file they point
  at is replaced and the link stays a link. A link the server cannot resolve is
  written through.
- **Temporary names are per attempt:** `.<name>.<n>.tsp`, with the first free
  `n` from 0 to 7, else a random one. They are created exclusively, so two
  transfers to one file (two tabs, a pool) never write into or remove each
  other's copy. The temporary copy has the target's mode before any data goes
  in. The setuid, setgid and sticky bits are set only after the data. A
  leftover this process could not remove after a broken transfer is
  remembered, per server, and the next transfer to that file removes it at
  once. Any other leftover is removed once it has been unchanged for 10
  minutes, measured by the server's clock.
- Paramiko drops the replies to pipelined writes. `_transfer` reads them itself,
  so a refused write is caught without an extra round trip.
- **Downloads** go to a temporary local file and `os.replace` over the target. A
  target another program holds without delete sharing (Windows) is written in
  place instead. Read-ahead is capped at 64 requests, so a multi-GB file over a
  slow VPN does not run into the SFTP timeout. On Paramiko 3.3 and later,
  TurboSSH runs its own read loop, so a cancelled download also stops its
  read-ahead thread.

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
