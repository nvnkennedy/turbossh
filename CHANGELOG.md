# Changelog

All notable changes to TurboSSH. Dates are ISO-8601.

## 2.1.0 — 2026-11-14

A reliability release. The whole project was reviewed for what can make the
window stop responding, crash, lose data, or lock a lab account. Then the fixes
themselves were reviewed again, so that what worked in 2.0.0 keeps working.

### Changed — please read when upgrading
- **FTPS checks the server's certificate and host name.**
  - A self-signed lab server: `FTPConfig(..., verify_tls=False)`.
  - A lab's own CA: `ca_file="lab-ca.pem"`.
  - A server reached by an IP address its certificate does not list:
    `check_hostname=False`. The certificate itself is still checked.

  The same options exist on `FTPHandler(config, ...)`. The error says which one
  you need.
- **Transfers replace a file instead of writing into it.** `push`, `pull`, the
  GUI's uploads and downloads and the remote editor write to a temporary
  `.<name>.<n>.tsp` next to the target, which already has the target's mode.
  They rename it over the target when complete, keeping its mode, owner and
  group, so a dropped link or a Cancel leaves the old file untouched. The file is
  written in place, as before, in these cases, and the log says so:
  - it has more than one hard link;
  - it is not a regular file (`/dev/null`, a FIFO, a device);
  - the folder does not allow new files;
  - the owner or mode cannot be kept;
  - it cannot be renamed over (a bind mount, a file held open on Windows);
  - the disk has no room for a second copy, and the new file fits where the old
    one is.

  A new file that does not fit fails the transfer, and the old file is kept
  whole. Two transfers to one file (two tabs, a pool) no longer clobber each
  other. A temporary file left by a failed attempt (a dropped link) is removed
  by the next transfer to that file from the same TurboSSH, at once. One left
  by another program or run is removed once it is 10 minutes old. A symlink
  stays a symlink. Tools that watch a folder now see a `.tsp` file and a
  rename.
- **Serial writes have a timeout.** `SerialHandler.write()` raises
  `SerialError` once no byte has gone out for `write_timeout` seconds (default
  5). The error says how many bytes were sent. A slow port does not trigger it
  while bytes keep moving. With flow control (`rtscts`, `xonxoff`, `dsrdtr`),
  the default is still to wait for as long as the device holds the line. A USB
  virtual COM port (CDC-ACM) can hold writes back without flow control while
  the device is busy. Pass `write_timeout=None` there to wait, as 2.0.0 did.
- **The remote camera and serial port are never taken from someone else
  without `force`.**
  - `webcam_release()` stops this handler's own camera streams.
    `webcam_release(force=True)` stops every TurboSSH camera stream on that
    machine. With nothing of its own to stop, it returns exit status 1 with
    that hint, instead of reporting success.
  - `serial_stream()` and `turbossh serial-ssh` do not take a port another
    program or a GUI session holds. `force=True` / `--force` first stops a
    stale TurboSSH reader of the port. After monitoring, the CLI no longer
    releases the port's tracked holder, because that could be the GUI's live
    session.
  - `serial_release()` stops only the bridge's own processes; on Linux / QNX,
    `fuser -k` runs only with `force`. With nothing of its own to stop, it
    returns exit status 1 and suggests `force=True`.
- **A remote shell that exits is not reconnected.** `exit` or a nologin account
  ends the session. A dropped link, a reboot or an sshd restart still
  reconnects, and a link that dies right after connecting is retried after 2,
  4, 8 … 30 s.
- **The Logs tab shows the latest 50,000 lines** (it held 500,000, about 315 MB
  per tab). Save still writes every stored line that passes the filter. Find
  also searches the older ones and offers **Filter for it**.
- **The terminal's disk capture keeps the latest 256 MB** in four parts. It used
  to stop at 256 MB, so Save all output missed everything after that.
- **WinRM bootstrap commands have deadlines.** These are:
  - probes and upload chunks: 2 minutes;
  - the offline install: 10 minutes;
  - the online install (`Add-WindowsCapability`): 45 minutes.

  A command that runs over its deadline is stopped with `WinRMTimeoutError`.
  To raise a deadline, use `command_timeout=`, `SSHConfig.winrm_command_timeout`
  or `install-ssh-remote --timeout`.
- **`install-ssh-remote` takes the password the way other commands do:**
  `--password` (prompt), `--password-stdin`, `--use-stored`, or
  `TURBOSSH_PASSWORD`. The environment variable is used only when there is no
  terminal to prompt at. Without a terminal and without a password, the
  command stops instead of waiting for input.
- **Jump passwords.** The jump password in Settings belongs to the jump host and
  user in Settings (compared without case). Sessions through that jump host use
  it, and follow it when it changes. A session through another jump host uses
  its own password, and no password means a key or agent login. The CLI follows
  the same rule, also with `--jump-host`. Settings saves the jump host, user and
  password together, and only when you change them. When the shared password
  changes, the log names sessions that keep the old one under another spelling
  of the jump host (a full DNS name, an IP address).
- **The hosting protocol checks arguments** (EMBEDDING.md):
  - flags must be real booleans; `null` means the default;
  - numbers may be JSON numbers or whole-number strings such as `"22"`;
  - `NaN`, `Infinity`, negative values and fractional ports are refused;
  - a `shutdown` `wait` is clamped to 0–60 s;
  - after `shutdown`, requests other than queries are refused instead of
    answered `ok` and dropped.
- **settings.json is shared, but the look is per process.** Each TurboSSH
  process (desktop app, embedded GUI) keeps its own theme and Camera choice.
  Saving writes only what that process changed, so it no longer undoes another
  process's changes.

### Added
- `SSHHandler.disconnect(sticky=True)`: nothing reconnects the handler until
  the next explicit `connect()`. The GUI uses it for closed tabs. Plain
  `disconnect()` still lets `auto_reconnect` connect again, as in 2.0.0.
- `SSHConfig.sftp_timeout` (60 s): an SFTP request the server never answers
  fails instead of hanging. Also `SSHConfig.winrm_command_timeout`.
- `FTPConfig` / `FTPHandler`: `verify_tls`, `ca_file`, `check_hostname`.
- `SerialHandler(..., write_timeout=..., dsrdtr=...)`.
- `SSHHandler.serial_stream(..., force=...)`, `turbossh serial-ssh --force`,
  `turbossh install-ssh-remote --timeout / --password-stdin / --use-stored`.
- `turbossh.WinRMTimeoutError`.
- Quitting asks about remote-file editors with unsaved changes, and so does
  `TurboSSHWidget.confirm_close()` for hosts. An editor whose session closed
  stays open offline with **Save a copy…**.
- The file browser carries on over a reconnected session, keeping its queue,
  history and editors. The interrupted transfer starts again unless you
  cancelled it.

### Fixed — the window stopped responding
- Typing, pasting and terminal resizes go out on a writer thread per session
  (SSH, serial, serial over SSH, local shell). A rekey, a stalled link, a low
  baud rate or a busy shell no longer freezes the window. Ctrl-C drops the
  queued paste and goes out first. A paste to a small-window server over a slow
  link is no longer cut off.
- Logs tab: re-filtering 200k lines froze the window for 6–12 s. The view is
  now built in timed steps, newest matches first, and Save runs on a worker
  thread.
- Transfers panel: cancelling 5,000 queued items took 3.4 s (now 5 ms). A tick
  with 500 rows of history took 320 ms (now 7 ms).
- Folder listings are sorted and formatted on the worker, and link targets are
  looked up in one batch. The local pane's start folder is checked on a worker,
  and deleting runs on one.
- Every network wait has a deadline:
  - SFTP clients give up after 30 s of silence;
  - channel requests (PTY, exec, shell, subsystem) time out;
  - opening a tunnel channel times out after 20 s;
  - WinRM commands stop at their deadlines.

  Cancel frees a transfer stuck on a dead link, but waits while a slow link
  still moves data.
- `connect()` on a handler that looks connected checks the link first. After a
  target reboot, the dead connection is replaced instead of failing on the next
  command, and the old client is closed. A live link that answers slowly is
  kept. A changed config (host, port, login, authentication, jump host) gets a
  new link, as in 2.0.0.
- `connect()` and `disconnect()` no longer hang on a stalled link while another
  thread uploads: the transport is closed first.
- A cancelled download stops its read-ahead (Paramiko 3.3 and later). It used to
  keep sending reads on the closed file.
- Set up SSH server polls for its result once, not once per click.

### Fixed — crashes
- A Logs tab running a one-shot command (`journalctl`, `dmesg`, `slog2info`)
  could abort the application when the command ended.
- Closing the app during a slow update check could abort it.
- A failed change to a saved session is reported in a message box instead of
  crashing.
- The CLI no longer fails printing a character the console's code page lacks
  (cp1252, redirected output). paramiko errors, end of input and subprocess
  errors end in an `ERROR` line instead of a traceback.

### Fixed — lost or damaged data
- Transfers: see above. A recursive download also maps names that are invalid
  on Windows (reserved names, trailing dots, case-only clashes), never writes
  outside the destination, and skips devices and FIFOs. Uploads ask about a
  case-only clash on a Windows remote.
- Remote editor:
  - It opens only regular files, and reads at most its size limit.
  - Saving checks that the file did not change on the remote since it was
    opened.
  - Text the editor would alter opens read-only: non-breaking spaces, lone CRs,
    U+2028/9, mixed line ends, damaged UTF-8.
- The terminal reads a channel's last output before it reports the end: logout
  and reboot messages, and the serial bridge's BUSY line.
- Save all output includes every capture part. If one is missing, it says that
  earlier output is not included.
- known_hosts is read line by line, so `@cert-authority` and `@revoked` lines no
  longer break every connect. New keys are appended under a lock, never
  rewritten.
- A settings.json or sessions.json that another program (antivirus) holds is
  retried. It is no longer set aside as corrupt or written over. The desktop
  app reads sessions.json again once the file is free.
- Serial over SSH (Windows): a retry on a busy port erased the record of the
  real holder, which then kept the port until a reboot. A force open could
  also stop its own sshd wrapper under a PowerShell default shell.
- Serial over SSH (Linux / QNX): the bridge's terminal is raw but keeps the
  newline translations, so output does not staircase and Enter sends a
  newline.
- `serial_write` over SSH reports a failed open or write instead of returning
  quietly, and sends long data as several commands in order. If a later part
  fails, the error says how many bytes had already been written.
- FTP downloads go to a temporary file and replace the target only when
  complete. A file another program holds open, or one in a folder that does not
  allow new files, is written in place.
- The SSH-server setup script keeps `PATH` as `REG_EXPAND_SZ` without expanding
  it.

### Fixed — security
- The SSH-server setup result is written only into an Administrators-only
  folder, never through a link. The POSIX serial bridge keeps its PID files in a
  private 0700 folder and refuses symlinks.
- PowerShell literals double the typographic quotes (U+2018–U+201B) too. A name
  containing one could end the string early.
- Camera names in remote command lines drop `"`, `%` and `|`, and the ffmpeg
  path drops `"` and `|`; `%VAR%` in it still works. ffmpeg is never taken from
  the current directory.
- Drops from another session's remote pane are refused, so a file cannot land on
  the wrong machine.
- The Camera's Remote source filled in the jump host's password when you picked
  the jump host, and kept it when you then picked another machine, so Scan sent
  it there. It is now filled in only for the jump host's own account, and
  cleared when you pick another machine.
- A temporary copy gets no setuid, setgid or sticky bit until its data is
  written.

## 2.0.0 — 2026-11-10

Two things: the Camera is now there only when you want it, and the whole GUI
can live inside another application — written in anything — and be driven from
it. Plus fixes found while reviewing the code around both.

### Changed — please read when upgrading
- **License: GNU GPL v3 (GPL-3.0-only), was MIT.** From 2.0.0 on, TurboSSH —
  library, CLI, GUI and the bundled exe — is distributed under the GNU General
  Public License version 3; the exe carries the license text. Releases up to
  1.7.0 were published under MIT and stay under it.
- **The Camera is off until you ask for it.** There is no Camera button, menu
  entry or Home card by default. Tick **View → Camera** (or Settings → Camera):
  a Camera tab opens, and the button, menu entry and Home card stay until you
  untick it. Unticking closes the Camera tabs, which stops streams and
  recordings and releases the device, and asks first if one is streaming. The
  choice is remembered (`camera_enabled` in settings.json).
- **Opening a Camera tab downloads nothing.** On a machine without ffmpeg it used
  to start the ~160 MB download straight away; now the tab says what Scan
  cameras will do, and the download starts when you click it.

### Added
- **TurboSSH inside any application.** `turbossh-gui --embed <window handle>
  --control stdio` (the bundled exe takes the same options) runs the whole GUI
  inside a window of another program — C# WinForms / WPF, C++, Electron, Java,
  Tk, wxPython, PySide6 … — fills it, follows its size, and takes the keyboard
  when clicked. The host drives it with JSON lines on stdin / stdout: open SSH,
  serial and local sessions, type into them, read the screen, run commands over
  their connection, theme, Camera, geometry, shutdown; and gets events when a
  session opens, connects, drops, fails or closes. It matches the host window's
  DPI awareness so it fits exactly whether the host is DPI-aware or not, takes
  the keyboard when clicked (and gives it back), never makes the host's UI
  thread wait, never stalls if the host reads slowly, and closes its sessions
  cleanly when the host sends `shutdown`, closes the pipe, or destroys the window.
  - **`turbossh.embed_client.TurboSSHProcess`** — a Qt-free Python client.
  - Complete host examples: **examples/embed_host_tk.py** (Tk) and
    **examples/embed_host_winforms.cs** (C# WinForms, builds with the C#
    compiler in Windows). Tested on Windows with Tk, PyQt5 and WinForms hosts,
    from Python and from the exe.
  - `turbossh gui` takes the same options (`--embed`, `--control`, `--hide`,
    `--theme`, `--camera`).
- **`TurboSSHWidget`** — the entire GUI as a `QWidget` for your own PyQt5
  application: `from turbossh.gui import TurboSSHWidget`. It keeps to itself:
  its stylesheet, icons and shortcuts stay inside the widget, it never quits the
  application, it offers neither Exit nor self-update, and it shuts its
  sessions down when the application quits or the widget is deleted.
  - Open sessions from code — `connect_ssh()`, `open_serial()`,
    `open_serial_over_ssh()`, `open_saved()`, `open_local_terminal()`,
    `open_camera()` — and get an `EmbeddedSession` back: `connected`,
    `disconnected`, `failed` and `closed` signals, `send()` / `send_line()`,
    `screen_text()`, `activate()`, `close()`, and the live `handler` to run
    commands over the same connection. Connections opened from code are not
    saved anywhere, and check host keys as the library does unless you pass
    `ignore_host_key=True` (for reimaged lab devices).
  - Choose what shows around the tabs (menu bar, ribbon, sidebar, log dock,
    status bar, Home), whether the Camera is the user's choice or yours, and the
    theme — or `theme="inherit"` to take the host's look.
  - Put TurboSSH's commands on your toolbar (`action("session")` …) and its menus
    in your menu bar (`menus()`); follow `session_opened`, `session_closed`,
    `current_session_changed`, `log_message`, `theme_changed`.
  - **EMBEDDING.md** is the reference (a test checks every public name is in it),
    and **examples/embed_demo.py** a complete host application.

### Fixed
- **A crash when closing a tab.** The connection is closed on a background
  thread after the tab is gone, and it logged into the deleted tab: an access
  violation, not an error message. The same could happen when "Save all output"
  or a Camera recording finished after its tab was closed. A toast whose window
  had gone raised an error when it timed out.
- The Camera's Remote source with a blank password sent an empty password and
  turned key lookup off, so key logins could not work (sessions were fixed in
  1.7.0; the Camera was missed). A blank field now means "use my keys / agent".
- A remote camera scan that failed after connecting (for example while pushing
  ffmpeg) left that SSH connection open.
- A Camera tab in the background kept rescaling every frame 33 times a second.
- A Camera tab no longer forces the window to ~1300 px wide: the picture options
  have a row of their own, and a long status message is clipped (the tooltip has
  it all) instead of widening the tab.
- In split view, Save output / Copy / Paste did nothing unless a pane had the
  focus: they picked the Home tile, which has no terminal.
- Each Settings dialog stayed in memory after it closed, and so did a background
  worker that finished at the moment its tab closed.
- The status bar said "Connecting to …" with no target for a local serial port.
- The Home tab's Documentation card read "Guides  reference": the "&" was taken
  for a keyboard accelerator.
- `examples/pyqt5_demo.py` imported a module removed in 1.3, so it failed at
  start. It now uses the library directly.
- Examples in the READMEs, the homepage and a GUI placeholder use neutral
  host names, logins and addresses.

## 1.7.0 — 2026-10-04

A review-driven hardening pass across the library, CLI and GUI, plus the
features TurboADB already had: a dual-pane file browser with transfer history,
a local terminal, toasts, log colouring and search, more themes, and a complete
CLI reference.

### Added — GUI
- **Dual-pane file browser.** This PC on the left, the remote host on the right:
  arrow buttons, drag-and-drop between the panes and in from Explorer, column
  sorting (folders stay first), rename / delete / new folder on both sides
  (local deletes go to the recycle bin), quick-jump buttons.
- **Transfers panel** with the history of every upload and download: progress,
  live speed, time left, status filter, cancel, retry failed, and a copyable
  report.
- **Edit remote files in place** — double-click a remote text file; line endings,
  encoding and permissions are kept. Binary and very large files are refused.
- **Local terminal** tab: PowerShell or Command Prompt on Windows (with line
  editing, history, and Ctrl-C that stops the running command), your shell on a
  real pty elsewhere. Ctrl+Shift+L.
- **Toasts** for connected / disconnected / failed / transfer finished, and a
  count of open sessions in the status bar.
- **Logs tab:** lines coloured by level, and find-in-log (Ctrl+F, F3 / Shift+F3,
  match case).
- **Seven more themes:** midnight, paper, nord, nord-light, solarized-dark,
  solarized-light, dracula. The toggle switches between each dark theme and its
  light partner.

### Added — CLI
- `--session NAME` (`-s`) on every connecting command: use a session saved in the
  GUI — host, user, key, jump host and stored passwords.
- Connection flags `--jump-host` / `--jump-user` / `--jump-port`,
  `--host-key-policy`, `--legacy`, `--passwordless`.
- New commands: `sudo`, `ls`, `copy-id`, `keygen`, `diagnose`, `sessions`,
  `check-update`, and `gui` / `docs` / `shortcut` / `version` (the separate entry
  points still work).
- **`CLI.md`** — the full reference. A test compares it with the parser, and
  another runs every command, so the two cannot drift apart.

### Fixed — library
- **Command timeouts are enforced, and large output no longer deadlocks.**
  `run()` waited for the exit status before reading, which never times out and
  hangs once output exceeds the SSH channel window (~2 MB). Output is now drained
  while waiting; on timeout `SSHTimeoutError.result` holds what was printed.
- **`sudo()` only sends the password when sudo asks for it.** It was typed into a
  PTY up front, so on root / NOPASSWD accounts it was echoed into the result and
  fed to the command's stdin. A wrong password now fails promptly. `get_pty=True`
  is available for `requiretty` hosts; `timeout` defaults to `command_timeout`.
- **Jump hosts:** one jump connection is reused per connect (a failed attempt no
  longer leaves one open), each auth attempt gets its own channel, and a wrong
  target password is reported as an authentication error instead of a "banner"
  error after three retries. The failure hint no longer probes ports from the
  local machine for a jump-only target.
- Two `forward_remote()` tunnels on one connection no longer steal each other's
  connections; a local forward on a busy port fails on Windows instead of
  silently sharing it.
- A handshake cut off by the peer (`EOFError`) is a normal, retried connection
  error. Concurrent callers reconnect once, not once each.
- `sftp()` reopens a dead SFTP channel; `rename()` falls back to plain rename on
  servers without the OpenSSH extension; recursive `pull()` / `walk()` follow
  symlinked folders (once) and skip dangling links; a failed or aborted file in a
  recursive pull is removed. `push()` / `pull()` accept `sftp=` for worker threads.
- Taking a busy remote COM port with `force=True` now only stops TurboSSH's own
  serial readers, not every `powershell -EncodedCommand` on that machine. A
  remote port that cannot be opened is reported instead of streaming nothing.
- `enable_legacy_algorithms` applies to that connection only (Paramiko ≥ 3.2)
  and only offers algorithms the installed Paramiko implements.
- The library no longer attaches its own log handlers or changes shared logger
  levels; FTP and serial log under `turbossh.*` too.
- Serial errors are `SerialError` (an `SSHError`), not raw pyserial exceptions.
- `strip_ansi` removes charset designations (no more stray `(B`); 
  `CommandResult.started_at` is the start time.
- WinRM: the firewall rule follows a changed SSH port, `sshd_config` gets exactly
  one `Port` line, and the fallback upload reuses one shell.

### Fixed — GUI
- **The terminal gets its keys.** Ctrl+W no longer closes the tab, Ctrl+Q no
  longer quits, Ctrl+S / Ctrl+T no longer open dialogs, Ctrl+N works again (it was
  registered twice). F5–F12, Insert, Shift+Tab, Alt+key, modified arrows and
  application-cursor mode are encoded as xterm does.
- **Key-based login works from the GUI** — a blank password field now means "use
  my keys / agent", as the dialog always said — and sessions have a key-file field.
- **Full-screen programs** (vim, less, htop) restore the screen on exit
  (alternate screen), and cursor-position queries are answered.
- **Speed:** terminal parsing is ~16× faster and bounded per frame, painting
  draws runs instead of cells, and skipped output is announced.
- Mouse selection, double-click word select, Ctrl+C copies a selection, Ctrl+wheel
  zoom, Shift+PageUp/Down. Paste sends CR, honours bracketed paste, asks before
  running several lines, and is no longer cut off at 32 KB. "Clear" keeps the
  scrollback.
- Closed tabs are deleted (each kept its scrollback and timers alive); reconnects
  and camera scans close the connection they replace.
- Nothing network-bound runs on the GUI thread any more: OS detection, opening and
  releasing the serial bridge, SFTP operations, shortcut creation, recording
  finalisation. Quitting waits briefly for remote ports / cameras to be released.
- Local serial echo no longer lags by up to a second; an unplugged port is reported.
- Connection news goes to the status bar, not one modal box per tab.
- Split view: Copy / Paste / Save / SFTP / Logs / close act on the focused pane.
- **SFTP browser:** folders and multiple files, progress and cancel, drag-and-drop
  upload, size / date / permission columns, symlinked folders, recursive delete,
  overwrite prompts, and no more acting on the wrong folder after a failed listing.
- **Camera:** the remote view no longer stops at the first 100 ms pause on
  Python ≥ 3.10; a local camera that fails is reported; switching source during a
  scan no longer crashes; a failed recording is not reported as saved.
- **ffmpeg** is downloaded and uploaded atomically, size-checked, hash-recorded,
  and can be pinned (`ffmpeg_sha256` setting / `TURBOSSH_FFMPEG_SHA256`).
- Sessions and settings are written atomically; an unreadable file is copied
  aside and reported instead of being overwritten. Renaming a session moves it
  (and its stored passwords); a duplicate name asks first.
- The updater waits for the app to exit, upgrades the interpreter that owns the
  install to the exact version, and verifies the result.
- One non-modal error popup however many errors; worker-thread errors are logged.

### Changed
- `allow_empty_password` now only adds the blank-password attempt when **no**
  password was supplied. A rejected password is one failed login, not two.
- `winrm_use_ssl` now validates the server certificate; set
  `winrm_verify_ssl=False` (or `verify_ssl=False`) for a self-signed listener.
- CLI: `-V/--version`; `--password-stdin` and `TURBOSSH_PASSWORD`; `--json` for
  `list-serial`, `scan-ports` and `camera-list`; Ctrl+C exits 130; expected errors
  print a message instead of a traceback; `store-credential --host` stores per
  host (the old user-only entry is still read). Importing `turbossh.cli` (and so
  `turbossh-gui`) no longer loads Paramiko.
- Window shortcuts that must work while a terminal has focus are Ctrl+Shift+T
  (new session) and Ctrl+Shift+W (close tab).
- Desktop / Start-Menu shortcuts are created on first run only and found even
  when Desktop is redirected to OneDrive.
- The elevated OpenSSH setup is staged in a fresh folder per run.
- `TURBOSSH_HOME` relocates the state folder (default `~/.turbossh`).
- Terminal captures are capped at 256 MB per session.

### Added
- A pytest suite with a loopback SSH/SFTP server and headless Qt; CI runs it on
  Linux and Windows, plus GUI and Python 3.8 jobs.
- `scripts/release.py` checks the changelog, requires the bundled exe in the
  wheel, and restores the version if a step fails.

## 1.6.0 — 2026-07-06

### Changed
- **Releases are published manually again.** Removed the tag-triggered PyPI
  publish workflow (and its GitHub↔PyPI Trusted Publishing link) — pushing a
  tag no longer uploads anything. Use `python scripts/release.py X.Y.Z` to
  build and publish. The CI workflow (lint + tests + build check on every
  push) is unchanged.
- Removed the Dependabot configuration — no more automated dependency-bump
  pull requests.
- No functional changes to the library, CLI, or GUI since 1.5.2.

## 1.5.2 — 2026-07-05

### Fixed
- **Much faster startup.** `import turbossh` no longer loads paramiko, pywinrm and
  keyring eagerly — the heavy modules are imported lazily on first use (PEP 562),
  paramiko only when you actually open a connection. The GUI window now builds and
  paints *before* any of that loads (one-time housekeeping runs after the window
  is on screen), cutting well over a second off cold start. `from turbossh import
  SSHHandler` etc. still work exactly as before.
- **No more surprise OpenSSH popup after "Check for updates".** The update check
  now only reports the PyPI version status; it no longer also probes your saved
  RDP/jump machines and offers to (re)install OpenSSH on them. That install is
  available on demand from the "Set up SSH server" button.

## 1.5.1 — 2026-07-05

### Fixed
- **The GUI is snappy again.** The app read and re-parsed its settings file from
  disk on *every* access, and `theme.emoji_icon()` reads a setting for each icon —
  so opening a menu, rebuilding the ribbon, switching theme, or creating a session
  tab fired a burst of disk reads and made the whole UI (and everything around an
  SSH/serial action) feel sluggish. Settings are now cached in memory and
  refreshed on save, cutting a single settings read from ~180 µs to ~150 ns with
  no change in behaviour.

## 1.5.0 — 2026-07-05

A maintenance and project-health release. No change to SSH / serial / SFTP /
camera behaviour — this hardens the tooling around the code. (Supersedes the
short-lived 1.3.3, published the same day with the same content; use 1.5.0.)

### Added
- **Continuous integration** (`.github/workflows/ci.yml`): lint + offline tests
  on Python 3.9–3.13, plus a build and `twine check` on every push/PR.
- **Tag-triggered release workflow** using PyPI **Trusted Publishing** (OIDC, no
  token) that rebuilds the bundled Windows GUI exe so the published wheel matches
  the source. Requires a one-time PyPI/GitHub setup (see `release.yml`).
- **`py.typed`** marker — the package now ships its type hints to downstream users.
- `SECURITY.md`, `CONTRIBUTING.md`, Dependabot config, and `ruff`/`mypy` settings.

### Fixed
- `strip_ansi` now removes **OSC sequences** (e.g. `ESC]0;title BEL` window-title
  escapes) instead of leaving the `0;title` text behind in saved logs.

### Changed
- **Single source of version truth**: `pyproject.toml` reads `__version__` from
  `turbossh/__init__.py` dynamically, so the two can't desync.
- Removed unused imports across the package and the dead legacy `TerminalView`
  widget (the live terminal is the pyte-backed `Vt100Terminal`).

## 1.3.2 — 2026-07-05

A correctness, security and cleanup release addressing a full code audit. The
bundled Windows GUI executable is rebuilt from this source, so the frozen app
carries the same fixes as the pip/source install.

### Fixed
- **Remote OS detection no longer misreads a PowerShell-default Windows host as
  Linux.** The old `uname -s || ver` probe is a parse error under PowerShell
  (`||` isn't valid there); detection now probes `uname -s` positively and falls
  back to `cmd /c ver`, which reports Windows from cmd *and* PowerShell shells.
- **A custom SSH port now actually takes effect.** `turbossh-setup`, the WinRM
  bootstrap, and the offline remote install all patch `sshd_config`'s `Port`
  directive and restart sshd — previously only the firewall was opened, so sshd
  kept listening on 22 and setup reported "not listening".
- **The Settings dialog no longer resets settings it doesn't display.** Saving a
  partial config (e.g. only the fields a dialog exposes) now preserves untouched
  keys like the compact-ribbon toggle instead of reverting them to defaults.
- **Opening a session while the split (tiled) view is active now shows it** — it
  used to connect but appear nowhere because the tab bar was detached.
- **Closing a session tab mid-connect no longer risks crashing the app** — an
  in-flight connect thread is detached instead of destroyed while running.
- **CLI `serial-ssh` releases the remote COM port on exit** (Windows OpenSSH left
  it locked after the stream ended).
- **`SSHPool` keeps duplicate hosts distinct** instead of collapsing two configs
  for the same host into one handler.
- **`run_many(check=…)` is now honored** (it was silently ignored).
- **FTP** `pull` no longer leaves a partial file behind on failure, and `exists()`
  matches by basename so it works on servers whose `NLST` returns bare names.
- **The update check no longer mistakes a PyPI pre-release for a newer version.**
- **The SFTP browser lists directories off the UI thread**, so it no longer
  freezes on slow/high-latency links; pressing **Stop** on a remote camera no
  longer briefly freezes the GUI either.
- Jump-host handling in the session dialog is consistent: a **jump port** field
  was added, the jump is applied to SSH sessions only, and the "no jump host"
  warning no longer fires for serial sessions.
- The default build (`python scripts/build_exe.py`) works from a fresh clone —
  the referenced `scripts/turbossh-gui.spec` now exists and is tracked.

### Security
- **Passwords are no longer passed on a command line.** The RDP launcher stores
  its credential via the Win32 `CredWrite` API, and the SMB fast-path feeds
  `net use` its password over stdin — neither shows up in a process listing. The
  SMB helper also no longer crashes on a missing password.
- **An unknown `host_key_policy` is now rejected** instead of silently falling
  back to auto-accept.
- Serial device names are escaped before embedding in PowerShell, and camera
  names are sanitised before embedding in ffmpeg commands.
- The GUI's terminal captures (used for *Save all output*) are now documented as
  plaintext-on-disk, with their cleanup behaviour spelled out in the README.

### Changed
- **`setuptools>=77` is now required to build** (PEP 639 license metadata).
- Dependency-missing error messages point at the real packages (`pip install
  pyserial` / `pywinrm`) instead of non-existent `turbossh[serial]`/`[winrm]`
  extras. `default_baud` and the first-run Startup toggles now actually apply.
- Removed the unused legacy GUI (the old `worker`, `connection_panel`, and
  `tabs/` modules) and dead camera-mode branches in the session dialog.

## 1.2.31 — 2026-06-28

### Changed
- **The version now lives in the status bar** (bottom-right corner), not in the
  Home header or the window title — out of the way but always visible.

### Added
- **Timestamps in the log dock.** Every log line is prefixed with its date and
  time (`YYYY-MM-DD HH:MM:SS`); continuation lines of a multi-line message are
  aligned under it.

## 1.2.30 — 2026-06-28

### Fixed
- **No more empty rows above a freshly-connected terminal, and a sane view after
  un-splitting.** The resize reflow now behaves like a real terminal: a short
  session (e.g. just the welcome banner) stays anchored at the **top** with blank
  space below it — not pushed to the bottom with a gap above — while a full
  session keeps its prompt pinned to the **bottom**. The cursor line always stays
  visible, and scrollback is still preserved all the way back to the first line.
- **Home action cards no longer clip their text.** The compact 1.2.28/1.2.29
  layout made the cards too short, cutting off the second line (e.g. "Local or RDP
  webcam"); they're tall enough again.

### Changed
- The connect banner starts at the very top of the terminal (removed its leading
  blank line).

## 1.2.29 — 2026-06-28

### Added
- **A real, draggable scrollbar on every terminal.** You can now scroll a session
  all the way back — from the very first line to the live prompt — by dragging the
  bar (or with the wheel), for both SSH and serial. Previously only the wheel
  scrolled and there was no visible bar, so it wasn't obvious you could go back at
  all. Typing snaps you back to the live tail.

### Fixed
- **Quick-command buttons no longer clip their text.** The tighter 1.2.28 layout
  squashed them so labels were cut off (e.g. `slog2info` looked like `slog7info`);
  they render at full height again.

### Changed
- **The version is shown again** — on the Home screen header and in the window
  title bar.

## 1.2.28 — 2026-06-28

### Fixed
- **Splitting (or resizing) no longer eats a terminal's scrollback.** When a pane
  shrank, the lines scrolled off the top were being dropped instead of moved into
  history, so the previous output for that terminal was gone. Those lines are now
  saved to scrollback first — wheel up and the full history is still there, per
  terminal, for both SSH and serial.

### Changed
- **Tighter, MobaXterm-style layout — more room for the terminal.** The ribbon is
  a single thin row (icon + label side by side, smaller icons) instead of the tall
  stacked one; the Home screen header and action cards are smaller; and each
  session's status line and Quick-command bar are slimmer. The terminal now gets
  the bulk of the window.

## 1.2.27 — 2026-06-28

### Fixed
- **Terminals no longer go blank or lose the prompt after split + resize.** The
  1.2.26 debounce stopped the resize *storm*, but the terminal library still
  anchored a resize to the top and never moved the cursor, so after splitting and
  then maximising the prompt could end up off-screen, stacked, or dropped
  entirely. The terminal now re-anchors to the **bottom** like a real terminal —
  the cursor follows the content and scrollback is pulled in when the pane grows —
  so the prompt (and `#`) stay put and visible in every view. Both SSH and serial.
- **The connect banner is two clean lines now** (it was a box that didn't close
  on the right): `● name  user@host:port  via jump` on the first line, OS and time
  on the second.

### Added
- **Camera "in use" → one-click retry.** When a remote camera can't start because
  another app (or a stuck ffmpeg from a previous run) is holding it — including the
  "no video frames came through" case — TurboSSH now asks *"force it closed and try
  again?"* (Yes / No) instead of just reporting the error. The CLI gains a matching
  `turbossh camera-grab --force` that clears any stale ffmpeg on the remote first.

### Changed
- Removed the version label from the Home screen.
- README: documented the `camera-list` / `camera-grab` CLI commands (incl.
  `--force`) and the in-use retry behaviour.

## 1.2.26 — 2026-06-28

### Fixed
- **Terminals stay clean when you maximise, split, or resize.** Previously,
  changing a terminal's size (maximising the window, building/closing a split,
  or dragging a divider) fired a storm of resize events — each one reshaped the
  screen buffer and nudged the remote shell — which left blank gaps and a
  missing prompt/cursor until you pressed Enter. Resizes are now **debounced**
  into a single clean update once the size settles: the view stays pinned to the
  live prompt, and the shell is told its new size just once, so the prompt
  redraws on its own. Applies to both SSH and serial terminals.

### Changed
- **Split view starts balanced.** Panes now open at equal sizes (even columns
  and rows) instead of whatever the contents happened to request, so a 2-, 3- or
  4-way split looks tidy from the start and is still drag-resizable.

## 1.2.25 — 2026-06-27

### Fixed
- **Remote OpenSSH install (over WinRM) now succeeds reliably on a re-run.**
  Two bugs made the remote installer fail intermittently — specifically on any
  machine that had been attempted before:
  - It now **repairs host keys and their permissions on every run**, instead of
    only on a first install. A machine left half-installed by an interrupted
    attempt (sshd present but missing/badly-permissioned host keys) would refuse
    to start `sshd` and keep failing; it now heals itself on the next run, the
    same way the local installer always has.
  - Fixed a stray cleanup step that **reported FAILED on an already-installed
    machine even though sshd was running fine** (it referenced an unset variable
    and aborted before reading the service status).

### Changed
- Clearer error when the WinRM NTLM/CredSSP transport dependency is missing
  (points you at `pip install "turbossh[winrm]"`) instead of a confusing failure
  from inside pywinrm.
- The offline WinRM installer now masks the password in its progress log, matching
  the online path.

## 1.2.24 — 2026-06-25

### Added
- **MobaXterm-style keyword highlighting in the terminal.** Plain words like
  *error / failed / fatal / denied* (red), *warning / deprecated* (amber) and
  *success / passed / connected / ready* (green) are now tinted in the SSH and
  serial terminals, making problems and results jump out at a glance. It layers
  **on top of** the server's own ANSI colours — a word the server already
  coloured is left exactly as the server sent it, so nothing that worked before
  changes. Matching is whole-word only (e.g. *terror* / *erroneous* are not
  touched). Toggle it under **Settings → Appearance → Keyword colouring**.

## 1.2.23 — 2026-06-25

### Added
- **One-click theme toggle in the toolbar** — a 🌙 / ☀ button switches light ↔ dark
  instantly; the whole app (styles, log colours, icons) changes at once and the
  choice is saved immediately.
- **App / taskbar icon matches the theme** — the original dark tile in dark mode
  and a generated light-tile variant in light mode, switched the moment you change
  theme.

## 1.2.22 — 2026-06-25

### Fixed
- **Theme changes the instant you pick it — and sticks.** Choosing light/dark in
  Settings → Appearance now applies everywhere *and saves immediately*, so it no
  longer reverts when you close or Cancel the dialog. You don't need to press OK
  for the theme (the other settings still apply on OK as before).

## 1.2.21 — 2026-06-25

### Fixed
- **Dropdown arrows are a clear arrow now** — a real arrow image (generated per
  theme, in the text colour) instead of the faint dot/square the CSS triangle was
  rendering. Visible in both light and dark.
- **Camera control dropdowns no longer collapse.** 1.2.20's resize tweak squashed
  them to a sliver; they're readable again (and still shrink sensibly in a split).

### Changed
- **Settings is a categorised window** — a left sidebar (Appearance · Defaults ·
  Jump host · Saved machines · Camera · Startup) with one section per page, no
  scrolling.
- **Theme applies instantly.** Pick light/dark in Settings → Appearance and the
  whole app updates immediately (styles, log colours, ribbon icons, window/taskbar
  icon). Cancel reverts the preview.
- **SSH-server icon** is a clean custom rack icon (the globe was wrong); ribbon
  icons re-tint on theme change. Per-mode app-icon variants are supported
  (`assets/icon-light.*` / `icon-dark.*`) if added.

## 1.2.20 — 2026-06-25

### Fixed
- **The terminal is always dark** (black background) in both themes — a white
  terminal in light mode looked bad; this matches MobaXterm and most terminals.
- **Log text is readable in light mode** — the level colours (green/amber/red…)
  now use darker hues on the light theme, and recolour live when you switch theme.
- **Invisible ribbon icons fixed** — monochrome symbols (⚙ ⬆ ⏻ …) follow the theme
  colour so they show in light mode; the **SSH server** and **Log dock** icons use
  reliably-rendered emoji; **Split** now has a proper custom two-pane icon instead
  of a blank square.
- **Taskbar icon** is set robustly (multi-size `.ico`, re-asserted after the window
  appears), so it stays the TurboSSH icon even when launched from source.
- **Camera resizes in the split view** — its control row no longer forces a wide
  minimum, so the pane can be dragged small.

## 1.2.19 — 2026-06-25

### Fixed
- **Light mode is readable now.** The terminal follows the theme — dark text on a
  near-white background in light mode (ANSI colours darkened to stay legible) —
  instead of a hardcoded black box that looked broken on a light UI.
- **Camera is resizable in the split view** — its large minimum size was stopping
  the splitter from shrinking it.

### Added
- **Per-tab welcome banner** on a successful connection (MobaXterm-style): the
  session name, `user@host:port`, jump host, detected remote OS and the time — for
  both SSH and serial sessions.
- **Better tab controls** — the tab right-click menu is richer and labelled (close
  this / left / right / others / all, plus split), shows the tab's name, and
  there's now an always-visible "▾" tab-actions button in the tab-bar corner.

## 1.2.18 — 2026-06-25

### Added
- **Home / welcome screen** (MobaXterm-style) shown on startup: the logo +
  version, quick-action cards (New session, Camera, Set up SSH server, Settings,
  Check updates, Docs), and a double-click-to-connect list of your saved sessions.
  It stays in sync as you add/edit sessions, and you can reopen it any time from
  **File → Home**.

## 1.2.17 — 2026-06-25

### Fixed
- **Blank terminal / missing prompt** after toggling Split↔tabs (and some tab
  switches). The terminal now re-renders whenever it's shown again, so the prompt
  and existing output reappear instead of going blank.
- **Serial-over-SSH stray blank lines.** The remote PTY is now sized to match the
  terminal — a fixed oversized console was padding the output with blank rows.
- Removed a duplicate layout-add in the serial view.

### Added / Changed
- **Resizable split view** — panes are now drag-resizable nested splitters with
  clear borders and a per-pane ✕ (4 sessions → 2×2).
- **Tab bar** — shorter, and full session names show (the bar scrolls instead of
  truncating).
- **Serial port in use** now shows a clear dialog with **Retry** (another app such
  as MobaXterm holds the port; Windows won't let it be taken — close it there)
  instead of one cryptic "busy" line.
- **Customizable Quick commands** — set comma-separated quick buttons per session
  (e.g. `mount -uw /mnt, reset, ls -l`) when saving/editing it; they appear above
  the SSH **and** serial terminals.
- **Log levels + filter** — debug / info / success / warning / error are
  colour-coded, with a **Show:** filter (All / Info+ / Warnings+ / Errors only).

## 1.2.16 — 2026-06-25

### Added
- **Compact / Standard ribbon** toggle — Compact shows icons only (more room for
  the terminal); Standard shows icons + labels. Remembered across launches.
- **Session icons** — pick an icon (🖥 🐧 🪟 🔌 🚗 🤖 …) when creating/editing a
  session; it shows in the sidebar and on the tab.
- **Camera: right-click → Copy image to clipboard** (and Save snapshot) — grab the
  current frame to paste straight into chat/email.
- **Split view tiles up to a clean grid** — 4 sessions → 2×2, etc.

### Changed
- **Dropdowns look normal** — replaced the cramped little arrow with a proper
  chevron + separator on every combo box.
- **Crisper terminal font** — defaults to Cascadia Mono (MobaXterm-like), with
  antialiasing + a Consolas/Courier fallback; existing installs are upgraded once.
- **Saves go to your user folder, not System32** — the app now runs from your
  Documents/home, so snapshots / "save output" / recordings land somewhere
  writable even when launched from a shortcut.
- **Manage machines updates the dropdowns live** — host drop-downs re-read the
  saved machines each time they open, so newly added ones appear immediately.
- **Edit / Delete removed from the ribbon** — they're on the session right-click
  menu (and the Session menu), keeping the toolbar uncluttered.

### Fixed
- **OpenSSH setup no longer fails when it's already installed/running.** The
  offline install skipped re-copying the (locked) binaries when sshd is already
  present — it just ensures the service is enabled, started and firewalled.

## 1.2.15 — 2026-06-25

### Added
- **Camera in the CLI** (parity with the library + GUI): `turbossh camera-list`
  (this machine, or `--host` for a remote Windows box over SSH) and
  `turbossh camera-grab --camera NAME --out FILE` — a `.jpg` snapshot, or a short
  `.mp4` clip with `--seconds N` (local or `--host` remote).
- **SSH auto-reconnect**: if a session drops (e.g. the target reboots), TurboSSH
  retries connecting for ~30 s automatically; if it still can't, the terminal
  shows *"SSH connection closed — press Ctrl+R to reconnect"* and **Ctrl+R**
  reconnects on demand.
- **Split/tiled view: per-pane ✕** to close a single session; the rest reflow,
  and closing the last pane drops back to tabs.

### Changed
- **Faster GUI startup**: when PyQt5 is installed, the app (and its shortcuts) run
  from source instead of self-extracting the bundled ~75 MB onefile exe on every
  launch. The exe remains the automatic fallback where PyQt5 isn't available.
- Camera: the **"Refresh"** button is now **"Scan cameras"**.
- **Settings** no longer scrolls horizontally; the saved-machines table moved into
  its own **"Manage machines…"** dialog so the window stays compact.

### Fixed
- **Clean shutdown**: quitting now disconnects and stops **every** session —
  including ones in the tiled/split view, which previously leaked their SSH /
  serial / camera connections and reader threads. Camera helper threads are
  awaited on close too, so nothing outlives the window.

## 1.2.14 — 2026-06-25

### Changed
- **Sharper remote camera.** Instead of the webcam's low-res default mode (often
  640×480, which looks blurry enlarged), TurboSSH now enumerates the camera's
  capture modes and picks the best **MJPEG** mode at/below your chosen resolution
  (e.g. 720p or 1080p on a Logitech C615), then streams it through **unchanged**
  (`-c:v copy`) — native resolution, no re-encode, no quality loss, and light on
  CPU/bandwidth. Choose the resolution in the **Quality** dropdown. (30 fps is the
  camera's hardware maximum for these modes.)
- **Smoother, less blocky view** — frames are now scaled with smooth (bilinear)
  filtering instead of nearest-neighbour.

## 1.2.13 — 2026-06-25

### Fixed
- **Remote camera: self-selecting, *verified* video transport.** Rather than
  assuming how the MJPEG should travel back over SSH, TurboSSH now starts the
  stream and confirms real JPEG frames actually arrive — trying a binary-clean
  SSH tunnel first and automatically falling back to ffmpeg's stdout, keeping
  whichever genuinely delivers video. If neither does, you get a clear reason
  (camera in use / privacy) instead of a silent "no video". The frames read while
  verifying are replayed, so nothing is dropped. (The camera was confirmed working
  over SSH throughout — this was purely about reliably moving the bytes back.)

## 1.2.12 — 2026-06-25

### Fixed
- **Remote camera: capture in the camera's default mode and never upscale.**
  1.2.11's `-vcodec mjpeg` request was too fragile and failed with "Could not set
  video options" on some webcams. We now capture in the mode the camera opens in
  reliably over SSH, and only ever scale **down** to the chosen size (never up).
  Upscaling + full-framerate encoding was the real cause of the buffer overrun in
  1.2.10/1.2.11; at native size ffmpeg keeps up and frames flow.
- Clarified the no-video dialog: you do **not** normally need to be signed into the
  RDP desktop — the camera opens over SSH; that note was a misleading catch-all.

## 1.2.11 — 2026-06-25

### Fixed
- **Remote camera: capture in the webcam's native MJPEG mode** (`-vcodec mjpeg`)
  rather than raw YUY2. Raw is ~18 MB/s for a 640×480 cam and overran ffmpeg's
  capture buffer ("real-time buffer too full → frame dropped"), so no clean frame
  ever made it out. MJPEG from the camera is ~10× smaller and far lighter on CPU;
  TurboSSH falls back to raw for the rare camera with no MJPEG mode. Also a bigger
  capture buffer and a longer connect window for cameras that are slow to start.

This is the third part of the remote-camera fix: 1.2.9 made the transport
binary-clean (tunnel), 1.2.10 cleared stale ffmpeg holding the device, and 1.2.11
stops the capture buffer overrun so frames actually flow.

## 1.2.10 — 2026-06-25

### Fixed
- **Remote camera: a stale ffmpeg no longer blocks the camera.** Closing an SSH
  exec channel doesn't kill the (no-PTY) child on Windows, so a previous attempt
  could leave an ffmpeg holding the webcam — the next open then failed with
  "Could not run graph … device already in use / I/O error". Every remote start
  now first kills any leftover turbossh ffmpeg (matched by command line **or**
  executable path) and waits for the device to release; **Force** clears *all*
  ffmpeg. Opening the stream also runs off the UI thread now, so the app stays
  responsive while it connects.

This completes the remote-camera fix begun in 1.2.9 (binary-clean tunnel
transport): 1.2.9 stopped the video being corrupted in transit, 1.2.10 makes sure
the camera isn't still held by a leftover process.

## 1.2.9 — 2026-06-25

### Fixed
- **Remote (RDP) camera now actually shows video.** The MJPEG stream was being
  piped through the remote command's stdout, and Windows OpenSSH's shell corrupts
  binary stdout (the same mangling behind the old serial CLIXML noise) — so no
  JPEG frame survived intact, even though ffmpeg was capturing perfectly. ffmpeg
  now serves the video on a loopback TCP port on the RDP machine, and TurboSSH
  reads it back over a **binary-clean SSH tunnel** (direct-tcpip, the same
  mechanism the jump host uses). The video bytes never pass through the shell, so
  frames arrive intact. (Diagnostics confirmed the camera itself was fine — this
  was purely the transport.)

## 1.2.8 — 2026-06-25

### Added
- **Rotate the camera view** — 0° / 90° / 180° / 270°, for a camera mounted
  sideways or upside-down. Snapshots are saved rotated to match.

### Changed
- **Remote camera "no video" now tells you why.** Instead of a blind "force it
  open?", TurboSSH runs a short verbose ffmpeg capture on the RDP machine and
  shows the actual reason (camera in use, Windows camera-privacy block, or a
  device that won't open over an SSH session with no desktop logged in), then
  still offers a force-retry.
- **Refresh resets the button to Start** — it no longer stays on "Stop" after a
  refresh when nothing is streaming.
- The SSH "no banner" hint now also points out that **OpenSSH may simply not be
  installed**, and that TurboSSH can install it ("Set up SSH server" / `turbossh-setup`).

## 1.2.7 — 2026-06-25

### Changed
- **"Error reading SSH protocol banner" now explains itself.** When TurboSSH
  reaches a host:port but it never sends an SSH banner, the error now says so and
  why — that port isn't an SSH server (OpenSSH not running, wrong port, or a
  firewall/proxy intercepting), and gives the exact `ssh` command to confirm.
  The SSH connection logic itself is unchanged (identical to 1.0.0); this only
  makes the failure actionable instead of cryptic.

## 1.2.6 — 2026-06-25

### Fixed
- **Regression (SSH "Error reading SSH protocol banner").** The saved-machines
  host drop-down added in 1.2.4 silently pre-filled the New Session dialog's
  **Host** field with a previously-used host, so a new connection went to the
  wrong machine. Populating the drop-down (saved machines or past hosts) now never
  changes what you typed — the field starts empty for a new session and shows the
  loaded host when editing one.

## 1.2.5 — 2026-06-25

### Added
- **Camera View control** — *Fill* (no black bars, edges may be cropped),
  *Fit* (the whole frame with thin bars), or *Stretch* (fill exactly). Default is
  Fill, so the video fills the tab with no side bars; switch to Fit any time to
  see the entire frame.
- **One-time ffmpeg setup now shows a progress popup** (it's ~160 MB) instead of
  only a small status line, so it's clear something big is downloading.
- **Colour-coded camera status** — green while viewing, red while recording or on
  error, amber when something needs attention.

### Changed
- **Remote camera shows the real reason when there's no video.** Instead of a
  generic "force it open?" prompt, the panel now reads ffmpeg's error from the RDP
  machine and tells you what's wrong — camera in use, a Windows camera-privacy
  block ("Let desktop apps access your camera"), or a capture error.
- Letterbox bars (Fit mode) use a near-black that matches the dark panels.

## 1.2.4 — 2026-06-25

### Fixed
- **Camera view no longer crops the frame.** The live view now shows the *whole*
  frame (same as the snapshot), centred — nothing is cut off the bottom/sides.
- **Video recording works and plays anywhere.** Recordings are re-encoded to a
  standard H.264 MP4 with real-time timestamps, so the file is no longer corrupt
  and plays in any player. Every frame is captured on clean JPEG boundaries.
- **"Recording saved" is logged once**, on stop — not repeated on every later
  action.
- **Remote (RDP) camera now actually lists cameras.** ffmpeg was never being
  uploaded to the remote machine (a safe-mode result was always truthy, so the
  "already there?" check always passed). It's now pushed and verified. If a
  remote camera still isn't found, the exact ffmpeg device listing is shown so
  the cause is clear (privacy setting, none attached, in use).

### Added
- **Saved machines.** Settings → *Saved machines* lets you store the RDP/Windows
  boxes you use often (name, host, user, domain). They appear as **host
  drop-downs** in the SSH/Serial session dialog and the Camera panel's Remote
  source, and picking one auto-fills the user/domain.

## 1.2.3 — 2026-06-24

### Fixed
- Remote camera no longer errors with "NoneType … webcam_channel": switching the
  source to Remote now resets and asks you to Refresh (connect) first.
- The video **fills the widget** (cover fit) instead of letterboxing with black
  bars on the sides.
- After **Stop**, the view clears instead of freezing on the last frame.
- Skip repainting duplicate frames (lower CPU).

## 1.2.2 — 2026-06-24

### Fixed
- **Camera remote source is now the RDP/Windows machine**, not the list of SSH
  targets (those are Linux/QNX and can't run a Windows camera). Pick **Remote**
  and enter the machine's host/login — pre-filled from Settings → Jump host.
- **Smooth video.** Frames are decoded off the UI thread and scaled fast, with
  low-latency capture flags — no more lag. Added **resolution and FPS**
  selectors and a live fps readout, and the view now fills the tab.

## 1.2.1 — 2026-06-24

### Changed
- The **Camera** button is now always in the ribbon (and File menu) — no Settings
  toggle to find first.
- ffmpeg already installed on PATH is used as-is (no download); the one-time
  download now shows progress and the right size (~160 MB).

## 1.2.0 — 2026-06-24

### Changed
- **Reworked the camera into a Local/Remote panel.** It's now its own tab with
  a source selector — **Local (this PC)** by default, or any saved machine as a
  remote source — instead of a connect-a-session flow. Pick a source, pick a
  camera, Start. The live view is front-and-centre.

### Added
- **View any local camera** (no SSH needed), as well as remote ones.
- More reliable camera detection — handles both ffmpeg `-list_devices` output
  formats, so cameras that previously showed as "not available" are found.

## 1.1.0 — 2026-06-24

### Added
- **Remote webcam** — stream a camera on the RDP machine over its own SSH
  connection and dedicated threads (independent of the terminal/serial work).
  A **Camera** session type with snapshot, video record, pause, stop, and an
  open-folder link; files save locally. It checks whether the camera is in use
  and offers to take it. Opt-in via **Settings → Enable camera** (the Camera
  button/menu stay hidden until then).
- ffmpeg is fetched once from a GitHub release, cached, and pushed to the remote
  over SFTP — it is **not** bundled in the wheel (keeps it under PyPI's limit).
  A local ffmpeg path can be set for fully offline use.
- API: `list_cameras()`, `webcam_channel()`, `webcam_release()`.

## 1.0.1 — 2026-06-24

### Changed
- Documentation expanded with step-by-step usage for every feature across all
  three interfaces (library, CLI, GUI). No code changes.

## 1.0.0 — 2026-06-24

First stable release. The library, the CLI, and the GUI now cover the same
feature set, and the serial-over-RDP workflow is solid end to end.

### Added
- **Full CLI parity** with the library and GUI. New subcommands:
  - `serial-ssh` — monitor (and optionally write to) a serial port on a remote
    host over SSH.
  - `scan-ports` — list the serial ports on a remote host.
  - `install-ssh-remote` — install OpenSSH on a remote Windows box over WinRM,
    fully offline.
  - `forward` — local port forwarding (`ssh -L`).
- **Native serial-over-SSH terminal** — type directly into the terminal with
  Tab-completion and Ctrl-C, via a PTY + raw-console bridge.
- **Port lifecycle handling** — `serial_in_use()` asks before taking a busy
  port; `serial_release()` frees it cleanly on close; a self-healing open
  recovers an orphaned holder.
- **Offline OpenSSH install**, locally (`turbossh-setup`) and remotely over
  WinRM, from bundled binaries — no internet required.
- **GUI**: menu bar, 10k-line scrollback, save-entire-session-to-disk, scan
  remote ports, in-app update check, and an OpenSSH "set up server" flow.
- `ARCHITECTURE.md` and this changelog.

### Changed
- Logs run on a PTY with the login PATH, so `slog2info` and other QNX tools
  stream correctly instead of buffering or reporting "no such file".
- Removed the line-input box from the SSH and serial terminals — they're native
  now.
- Removed legacy/unused modules (`pyqt_worker.py`, `gui_app.py`) and stale
  PyInstaller specs.

### Fixed
- Serial output no longer shows PowerShell `#<CLIXML` noise.
- `FixHostFilePermissions.ps1` is invoked correctly, so a freshly installed
  `sshd` actually starts.
- The new/edit session dialog fits on screen and never truncates.
- Ctrl-C interrupts long-running commands immediately.

## Earlier (0.x)

The 0.x line was the iterative build-out: the Paramiko core and SFTP surface,
the CLI, the PyQt5 GUI with a VT100 terminal, the bundled offline OpenSSH
installer, serial support (local then over SSH), the WinRM bootstrap, and a long
tail of fixes to the serial-over-RDP path and the GUI. 1.0.0 consolidates all of
it.
