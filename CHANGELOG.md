# Changelog

All notable changes to TurboSSH. Dates are ISO-8601.

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
