# Changelog

All notable changes to TurboSSH. Dates are ISO-8601.

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
