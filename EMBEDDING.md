# Embedding TurboSSH in your own application

The whole TurboSSH GUI — sessions sidebar, tabbed terminals, SFTP browser, Logs
tab, split view, Camera, settings — can live inside another application and be
driven from it. It looks and behaves exactly like the TurboSSH desktop app,
because it *is* the desktop app, running as a guest.

There are two ways in:

| Your application is… | Use | How it runs |
|---|---|---|
| anything: C# WinForms / WPF, C++ Win32 / MFC / Qt, Electron, Java, Delphi, LabVIEW, Python with Tk / wxPython / PySide6 / PyQt6 … | [the TurboSSH process](#part-1--any-application-the-turbossh-process) | TurboSSH runs as its own process inside one of your windows, and you drive it over JSON lines on its stdin / stdout |
| a PyQt5 application | [`TurboSSHWidget`](#part-2--pyqt5-applications-turbosshwidget) | a `QWidget` in your own process |

Both give you the same commands (open sessions, type into them, read the screen,
run commands over their connection, theme, Camera …) and the same events (opened,
connected, dropped, failed, closed).

**Licensing.** TurboSSH is licensed under the GNU GPL v3 (see LICENSE). In the
first way TurboSSH stays a separate program that your application starts and
talks to over a pipe; the second loads it into your program's own process
(which PyQt5, being GPL v3 itself, already does). Which obligations follow for an
application you distribute depends on that difference; check the GPL's terms
for your case.

# Part 1 — Any application: the TurboSSH process

```text
 your application (any toolkit)                 TurboSSH (its own process)
┌──────────────────────────────────┐
│ your toolbar, your pages …       │   starts   turbossh-gui.exe
│ ┌──────────────────────────────┐ │ ─────────► --embed <window handle>
│ │ a panel / frame of yours     │ │            --control stdio
│ │  ╔════════════════════════╗  │ │
│ │  ║ TurboSSH fills it, and ║  │ │  stdin:  {"id":1,"cmd":"connect_ssh",…}
│ │  ║ follows its size       ║  │ │  stdout: {"id":1,"ok":true,…}
│ │  ╚════════════════════════╝  │ │          {"event":"session_connected",…}
│ └──────────────────────────────┘ │
└──────────────────────────────────┘
```

1. Give TurboSSH a window of its own in your UI: an empty panel, frame or child
   window. Get its native handle (table below).
2. Start `turbossh-gui.exe --embed <handle> --control stdio` (or
   `python -m turbossh.gui …` where PyQt5 is installed) with stdin and stdout
   redirected. TurboSSH appears inside that window, fills it, and follows it when
   it is resized, hidden or shown.
3. Read its stdout line by line: each line is a JSON object. Wait for
   `{"event": "ready"}`, then send requests on its stdin.
4. To finish, send `shutdown` (or just close stdin, or destroy the window: it
   notices either and closes cleanly).

Tested on Windows 11 at 125 % display scaling, each host with both
`python -m turbossh.gui` and the bundled `turbossh-gui.exe`:

| Host | Checked |
|---|---|
| Python + Tk (DPI-aware and DPI-unaware) | child of the frame and fills it exactly, follows resizes, `set_geometry` / `fill_parent`, real mouse clicks and keystrokes reach its terminal, Tk's own entry gets the keyboard back, closing the Tk window ends TurboSSH cleanly |
| a PyQt5 application, as a separate process (what a PySide6 / PyQt6 / Qt C++ application does) | the same, plus hiding and showing with the host's tab |
| C# WinForms, DPI-unaware (`examples/embed_host_winforms.cs`) | child of the panel and fills it exactly, follows a resize, commands sent over the channel run in the embedded terminal, closing the form ends TurboSSH |

| Platform | Status |
|---|---|
| Windows 10 / 11 | supported and tested |
| Linux (X11) | not tested. Qt supports foreign windows there, but TurboSSH does not follow the parent's size by itself, so send `set_geometry` when it changes |
| macOS | no: macOS does not let one process put a window inside another's. Use `--control stdio` without `--embed` (TurboSSH in a window of its own, driven by you) |

## Getting the window handle

| Toolkit | The handle to pass |
|---|---|
| Win32 / MFC / C++ | the `HWND` of a child window you create for TurboSSH |
| C# WinForms | `panel.Handle` of a `Panel` (Dock = Fill) — see `examples/embed_host_winforms.cs` |
| C# WPF | the child `HWND` your `HwndHost.BuildWindowCore` creates |
| Python Tk | `frame.winfo_id()` after `root.update()` — see `examples/embed_host_tk.py` |
| wxPython | `panel.GetHandle()` |
| PySide6 / PyQt6 / Qt C++ | `int(widget.winId())` of a `QWidget` with `Qt.WA_NativeWindow` set |
| Java (Swing / AWT) | `Native.getComponentID(canvas)` (JNA) of a heavyweight `Canvas` |
| Electron | `win.getNativeWindowHandle()` (read as a pointer-sized integer), then `set_geometry` with the placeholder element's rectangle × `screen.getPrimaryDisplay().scaleFactor` |
| Delphi / Lazarus | `Panel.Handle` |

Give TurboSSH a window of its own. It fills the window's whole client area unless
you send `set_geometry`.

## Starting it

```text
turbossh-gui.exe --embed <handle> --control stdio [--hide PARTS] [--theme NAME] [--camera on|off]
python -m turbossh.gui --embed <handle> --control stdio …        (with PyQt5 installed)
```

`--hide` takes a comma-separated list of `menu_bar`, `ribbon`, `sidebar`,
`log_dock`, `status_bar`, `home`. `--theme` takes a theme name or `inherit`.
`--camera on|off` decides the Camera for the user. A wrong option ends it at once
with exit code 2 and the reason on stderr, before anything is sent on stdout.
The exe ships in the wheel at `turbossh/bin/turbossh-gui.exe` (Windows); it needs
no Python.

**The keyboard.** A click inside TurboSSH gives it the keyboard (it joins its
input to your window's thread, as Windows does for a parent and child window), and
everything typed then goes to it, Ctrl+C and Tab included. A click on one of
your own controls takes the keyboard back: Win32, WinForms, WPF and Qt controls do
that by themselves. Tk does not (it only takes the keyboard when its window is
activated), so a Tk host adds
`root.bind_all("<Button-1>", lambda e: e.widget.focus_force(), add="+")`, as
`examples/embed_host_tk.py` does. The `focus` request hands the keyboard to
TurboSSH from code.

Never put passwords on the command line (other processes can read it): send them
in `connect_ssh` over stdin. TurboSSH takes on your window's DPI awareness by
itself, so both sides measure in the same pixels and it fills the window exactly
whether your application is DPI-aware or not.

## The protocol

UTF-8, one JSON object per line, both ways. Protocol version: `1` (in `ready`).

A request is `{"id": <number>, "cmd": "<command>", "args": {…}}`; `args` may be
left out when there are none. Every request gets exactly one response with the
same id:

```json
{"id": 7, "ok": true, "result": {"session": "s1"}}
{"id": 8, "ok": false, "error": "no open session 's9'", "type": "ProtocolError"}
```

Everything else on stdout is an event: `{"event": "<name>", …}`.

Arguments are checked by type: a flag is JSON `true` / `false` (not `"false"`, not
`0`), and a number is finite and within its range (no `Infinity`, `NaN` or
`1e999`). Anything else is refused with a `ProtocolError`.

TurboSSH answers every request immediately; what the request does to the window
happens right after the answer has gone. The one exception is a command whose
result only exists later — `run` and `confirm_close` — which is answered when it
is ready (other requests are answered in the meantime). Opening a session answers
with its id at once; watch the events for how it goes. Because of this you may
wait for an answer on your UI thread. On Windows, wait in a way that still
delivers *sent* window messages, as .NET's `WaitOne` on an STA thread does and as
`turbossh.embed_client` does (`MsgWaitForMultipleObjects` / `PeekMessage` with
`PM_QS_SENDMESSAGE` in C/C++): the embedded window shares your input queue, so
focus changes in it can send messages to your thread.

Session ids are strings (`"s1"`, `"s2"` …), also for tabs the user opens
themselves. The id an open request answers with is always the one its
`session_opened` carries, even when several opens are under way at once (one
waiting on a question to the user, say); an id that ends up with no session gets
`session_failed` and `session_closed`.

Read stdout continuously (a background reader, as in the examples), not only
while you wait for an answer. If you fall far behind, TurboSSH drops `log`
events rather than wait for you; answers and the other events are never dropped.

### Commands

| Command | `args` | Result |
|---|---|---|
| `ping` | — | `{"version", "protocol", "pid"}` |
| `connect_ssh` | `host`, and any of `username`, `password`, `port`, `domain`, `key_file`, `name`, `jump_host`, `jump_username`, `jump_password`, `jump_port`, `jump_domain`, `legacy_algorithms`, `ignore_host_key`, `quick_commands` | `{"session"}` — an SSH tab; nothing is saved. Host keys are checked unless `ignore_host_key` is true |
| `open_serial` | `device`, and any of `baudrate`, `name`, `quick_commands` | `{"session"}` — a serial port on that machine |
| `open_serial_over_ssh` | `device`, `host`, and any of `username`, `password`, `port`, `domain`, `key_file`, `baudrate`, `name`, `ignore_host_key`, `quick_commands` | `{"session"}` |
| `open_saved` | `name` | `{"session"}` — a session saved in TurboSSH, with its stored passwords |
| `open_local_terminal` | — | `{"session"}` |
| `open_camera` | — | `{"session"}` (turns the Camera on first) |
| `open_home` | — | `{}` |
| `new_session_dialog` | — | `{}` — TurboSSH's own dialog |
| `show_settings` | — | `{}` — TurboSSH's own dialog |
| `sessions` | — | `[{"session", "name", "kind", "state"}, …]` |
| `current_session` | — | `{"session"}` (or `null`) |
| `saved_session_names` | — | `["…", …]` |
| `send` | `session`, and `text` *or* `data_base64` | `{}` — type into the terminal (raw bytes base64-encoded, e.g. `"Aw=="` for Ctrl-C). An SSH / serial session must be connected |
| `send_line` | `session`, `text` | `{}` — type a line and press Enter |
| `screen_text` | `session` | `{"text"}` — what the terminal shows now |
| `run` | `session`, `command`, optional `timeout` (s, default 60, at most 86400) | `{"exit_code", "stdout", "stderr", "duration"}` — runs on the session's SSH connection in a channel of its own (the terminal is not touched); answered when it ends |
| `activate` | `session` | `{}` — bring the tab forward and give it the keyboard |
| `close_session` | `session` | `{}` |
| `focus` | — | `{}` — give TurboSSH the keyboard |
| `set_geometry` | `x`, `y`, `width`, `height` | `{}` — sit at this rectangle of your window (physical pixels, client coordinates) instead of filling it. Needs `--embed` |
| `fill_parent` | — | `{}` — fill your window again |
| `set_visible` | `visible` | `{}` |
| `window` | — | `{"window", "embedded_in", "geometry", "device_pixel_ratio", "follows_parent", "parent_client_size"}` — for chasing a layout problem |
| `camera_enabled` | — | `true` / `false` |
| `set_camera_enabled` | `enabled` | `{}` |
| `current_theme` | — | the theme name, or `"inherit"` |
| `set_theme` | `name` | `{}` |
| `set_chrome` | any of `menu_bar`, `ribbon`, `sidebar`, `log_dock`, `status_bar` (true / false) | `{}` |
| `action_names` | — | `["session", "local", …]` (see [Action names](#joining-the-hosts-menus-and-toolbars)) |
| `trigger_action` | `name` | `{}` — as if the ribbon button was clicked |
| `confirm_close` | — | `true` when nothing is open (no session, no remote-file editor with unsaved changes) or the user agreed to close; answered when they have |
| `shutdown` | optional `wait` (s, default 6, 0 to 60) | `{}` — closes every session (waiting up to `wait` for remote ports and cameras to be released, and no longer) and ends the process |

Once TurboSSH has shut down (by `shutdown`, or any of the ways under
[Ending it](#ending-it)), it answers only `ping`, `shutdown` and the queries
(`sessions`, `current_session`, `saved_session_names`, `camera_enabled`,
`current_theme`, `action_names`, `window`); every other request gets a
`ProtocolError` until the process has ended.

### Events

| Event | Fields | When |
|---|---|---|
| `ready` | `protocol`, `version`, `pid`, `window` (TurboSSH's own handle), `embedded_in` | it is up and listening |
| `session_opened` | `session`, `name`, `kind` (`ssh`, `serial`, `local`, `camera`), `state` | a tab opened, from a request or by the user |
| `session_connected` | `session` | the link is up (again after an automatic reconnect) |
| `session_disconnected` | `session`, `message` | the link dropped; an SSH tab retries by itself for 30 s |
| `session_failed` | `session`, `message` | it could not connect, or could not be opened at all |
| `session_closed` | `session` | the tab is gone, by any route |
| `current_session_changed` | `session` (or `null`) | another tab came to the front: the user switched tabs, or a new one opened (then right after its `session_opened`) |
| `log` | `text` | every line of TurboSSH's log |
| `theme_changed` | `theme` | |
| `camera_enabled_changed` | `enabled` | |
| `error` | `text` | an unexpected error inside TurboSSH (it keeps running) |
| `parent_closed` | — | your window went away; TurboSSH is closing |
| `exited` | — | the last line before the process ends |

### Ending it

Any of these closes every session cleanly (remote COM ports and cameras are
released) and ends the process with exit code 0, sending `exited` last:

- the `shutdown` request (wait for the process to exit afterwards);
- closing TurboSSH's stdin, or your process ending;
- destroying the window TurboSSH lives in;
- closing TurboSSH's own window: a `WM_CLOSE` sent to it, or, without `--embed`,
  the user closing it (who is asked first when sessions are open).

Nothing else ends it. In particular, without `--embed`, closing one of
TurboSSH's dialogs while its window is hidden (`set_visible`) does not.

## From Python: `turbossh.embed_client`

A ready-made client for any Python program; it needs no Qt (TurboSSH runs as
`python -m turbossh.gui` when this Python has PyQt5, otherwise as the bundled
exe). [`examples/embed_host_tk.py`](examples/embed_host_tk.py) is a complete Tk
application.

```python
from turbossh.embed_client import TurboSSHProcess

gui = TurboSSHProcess.launch(parent=frame.winfo_id(), hide=["log_dock"])
session = gui.connect_ssh("10.0.0.5", username="root", password="…")
gui.wait_for("session_connected", session)
gui.send_line(session, "uname -a")
print(gui.run(session, "uptime")["stdout"])
gui.close()
```

| | |
|---|---|
| `launch(parent=None, *, hide=(), theme=None, camera=None, command=None, env=None, cwd=None, timeout=60)` | Class method, `TurboSSHProcess.launch(…)`: start TurboSSH (in `parent`, or as its own window) and wait for `ready`. `command` overrides how it is started, e.g. `["C:/tools/turbossh-gui.exe"]`. |
| `call(cmd, *, timeout=30, **args)` | Any command of the table above; returns its result or raises `TurboSSHError` (with `.type`). |
| `call_async(cmd, callback=None, **args)` | The same without waiting; `callback(result_or_error)` runs on the callback thread (also, with an error, if TurboSSH exits first). |
| `connect_ssh`, `open_serial`, `open_serial_over_ssh`, `open_saved`, `open_local_terminal`, `open_camera` | Return the new session's id. |
| `sessions`, `current_session`, `saved_session_names`, `send`, `send_line`, `screen_text`, `run`, `activate`, `close_session` | As the commands (`send` takes `str` or `bytes`). |
| `focus`, `set_geometry`, `fill_parent`, `set_visible`, `set_theme`, `set_camera_enabled`, `set_chrome`, `trigger_action`, `show_settings`, `new_session_dialog` | As the commands. |
| `on_event(callback)` | `callback(event)` for every event, on the callback thread. |
| `poll_events()` | The events since the last poll — call it from a UI timer (Tk `after`, a `QTimer` …). |
| `wait_for(event, session=None, *, timeout=30)` | The first such event, including one that already arrived. |
| `window`, `version`, `ready`, `is_running`, `stderr_tail()` | TurboSSH's own handle, version, `ready` event, whether it runs, its last stderr lines. |
| `close(wait=6, timeout=20)` | `shutdown`, wait for the process, return its exit code. Also a context manager. |
| `kill()` | End it at once (sessions are not released). |

Waiting calls keep delivering the window messages the embedded window sends to
your thread, so they are safe on your UI thread.

**The callback thread.** One background thread only reads TurboSSH's output and
hands each answer to the call waiting for it; another runs your `on_event` and
`call_async` callbacks, one at a time and in the order they arrived. So a
callback may call TurboSSH itself (`call`, `send_line`, `wait_for` …): the answer
is read meanwhile. A slow callback holds up the callbacks after it, never the
answers. Marshal to your UI thread from a callback as usual (Tk `after`,
`QMetaObject.invokeMethod` …), or use `poll_events()` instead.

## From C# (.NET)

[`examples/embed_host_winforms.cs`](examples/embed_host_winforms.cs) is a
complete WinForms application; its `TurboSSH` class (about 100 lines, no NuGet
packages) is all a .NET host needs. It builds with the C# compiler in every
Windows install:

```text
%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe /target:winexe
    /r:System.Web.Extensions.dll embed_host_winforms.cs
```

```csharp
var turbo = new TurboSSH(@"C:\tools\turbossh-gui.exe", "", panel.Handle);
turbo.Ready.WaitOne();
turbo.EventReceived += e => BeginInvoke((MethodInvoker)(() => status.Text = (string)e["event"]));
var s = (Dictionary<string, object>)turbo.Call("connect_ssh",
    TurboSSH.Args("host", "10.0.0.5", "username", "root", "password", password));
```

For WPF, create a child window in an `HwndHost` and pass its handle.

## From Electron / Node.js

```js
const { spawn } = require("child_process");
const readline = require("readline");

const handle = win.getNativeWindowHandle();          // a Buffer holding the HWND
const hwnd = handle.length === 8 ? handle.readBigUInt64LE() : handle.readUInt32LE();
const turbo = spawn("C:/tools/turbossh-gui.exe",
                    ["--embed", hwnd.toString(), "--control", "stdio"]);
let nextId = 0;
const waiting = new Map();
readline.createInterface({ input: turbo.stdout }).on("line", (line) => {
  const msg = JSON.parse(line);
  if (msg.event) return onEvent(msg);                // ready, session_connected, …
  waiting.get(msg.id)?.(msg);
  waiting.delete(msg.id);
});
function call(cmd, args = {}) {
  return new Promise((resolve, reject) => {
    const id = ++nextId;
    waiting.set(id, (m) => (m.ok ? resolve(m.result) : reject(new Error(m.error))));
    turbo.stdin.write(JSON.stringify({ id, cmd, args }) + "\n");
  });
}
// Electron draws its page in the same window: put TurboSSH over a placeholder
// element and keep it there (physical pixels).
function place(rect) {
  const k = screen.getPrimaryDisplay().scaleFactor;
  return call("set_geometry", { x: Math.round(rect.x * k), y: Math.round(rect.y * k),
                                width: Math.round(rect.width * k),
                                height: Math.round(rect.height * k) });
}
```

# Part 2 — PyQt5 applications: `TurboSSHWidget`

The whole GUI is also a normal `QWidget` that a PyQt5 application can put on a
tab, in a splitter or in a dock, in its own process.

```python
from turbossh.gui import TurboSSHWidget

turbo = TurboSSHWidget(parent=self, log_dock=False)
tabs.addTab(turbo, "SSH")

session = turbo.connect_ssh("10.0.0.5", username="root", password="…")
session.connected.connect(lambda: session.send_line("uname -a"))
```

[`examples/embed_demo.py`](examples/embed_demo.py) is a complete host
application: TurboSSH on a tab, its commands on the host's toolbar, its menus in
the host's menu bar, a connection started from a host form, and a clean exit.

Requirements: `pip install "turbossh[gui]"` (PyQt5 5.15). The host must be a
PyQt5 application, since the widget runs in the host's own process and event loop.

## What embedding changes

It is the same window as the desktop app, running as a child widget. Everything
that belongs to the application stays with the host:

| | Desktop app | Embedded |
|---|---|---|
| Stylesheet | applied to the whole application | applied to the TurboSSH widget only, or not at all (`theme="inherit"`) |
| Window title, window / taskbar icon | set | untouched |
| Keyboard shortcuts | work anywhere in the window | work only while the focus is inside TurboSSH, so the host's own Ctrl+N, Ctrl+S, F1 … keep working |
| Exit, Check for updates | offered | not offered: quitting and updating are the host's business |
| Closing | asks, then quits the application | never quits; see [Lifetime](#lifetime) |
| Uncaught errors in TurboSSH | popup + log | log, and the host's `sys.excepthook` if it has one |
| Docks | can float | stay inside the widget |
| First-run tasks (desktop shortcut, docs page) | run once | never run |

The terminals still claim the keys a shell needs (Ctrl+C, Ctrl+W, arrows …) while
they have the focus; everything with Ctrl+Shift goes to the application, as in
the desktop app.

## Creating the widget

```python
TurboSSHWidget(parent=None, *, menu_bar=True, ribbon=True, sidebar=True,
               log_dock=True, status_bar=True, home=True, camera=None, theme=None)
```

| Option | Meaning |
|---|---|
| `menu_bar`, `ribbon`, `sidebar`, `log_dock`, `status_bar` | Show that part around the tabs. Change later with `set_chrome()`. |
| `home` | Open the Home tab at start. |
| `camera` | `None`: the user's own choice (View → Camera, remembered). `True` / `False`: the host decides; the user's switch is hidden and their saved choice is left alone. |
| `theme` | `None`: the user's saved TurboSSH theme. A name from `turbossh.gui.theme.THEME_NAMES` (`dark`, `light`, `midnight`, `paper`, `nord`, `nord-light`, `solarized-dark`, `solarized-light`, `dracula`): that theme, for this run only, and the theme switches are hidden. `"inherit"`: no TurboSSH stylesheet at all, so it takes the host's look; icon and log colours follow the host's palette. |

An unknown theme raises `ValueError` straight away.

## `TurboSSHWidget`

### Signals

| Signal | Arguments | When |
|---|---|---|
| `session_opened` | `EmbeddedSession` | a tab was opened, from code or by the user |
| `session_closed` | `EmbeddedSession` | a tab went away, by any route |
| `current_session_changed` | `EmbeddedSession` or `None` | another tab came to the front: the user switched tabs (`None`: the Home tab), or a new one opened (then right after its `session_opened`) |
| `log_message` | `str` | every line written to TurboSSH's log, also with the log dock hidden |
| `theme_changed` | `str` | the theme changed |
| `camera_enabled_changed` | `bool` | the Camera was shown or hidden |

### Opening things

Each returns an [`EmbeddedSession`](#embeddedsession) and shows the new tab.

| Method | |
|---|---|
| `connect_ssh(host, *, username=None, password=None, port=22, domain=None, key_file=None, name=None, jump_host=None, jump_username=None, jump_password=None, jump_port=22, jump_domain=None, legacy_algorithms=False, ignore_host_key=False, quick_commands=())` | An SSH tab (terminal, SFTP and Logs). Nothing is saved: not the session, not the password. With no password it logs in with `key_file`, the SSH agent or the default keys. The host key is checked as the library does: known hosts are verified and a changed key is refused; `ignore_host_key=True` accepts any key, for lab devices that are reimaged and get a new one. `quick_commands` become the one-click buttons above the terminal. |
| `open_serial(device, *, baudrate=115200, name=None, quick_commands=())` | A serial port on this machine (`"COM5"`, `"/dev/ttyUSB0"`). Returns `None` when that port is already open in another tab and the user chose to leave it there. |
| `open_serial_over_ssh(device, host, *, username=None, password=None, port=22, domain=None, key_file=None, baudrate=115200, name=None, ignore_host_key=False, quick_commands=())` | A serial port on another machine, over SSH (see "Serial over RDP" in the README). |
| `open_saved(name)` | A session the user saved in TurboSSH, with its stored passwords. `KeyError` if there is none by that name. |
| `open_local_terminal()` | A shell on this machine. |
| `open_camera()` | A Camera tab. Turns the Camera on first if it is off. |
| `open_home()` | Shows (or focuses) the Home tab. Returns nothing. |
| `new_session_dialog()` | TurboSSH's own New session dialog. |
| `show_settings()` | TurboSSH's own Settings dialog. Options the host decided (`theme=`, `camera=`) are read-only there. |

### What is open

| | |
|---|---|
| `sessions()` | Every open tab, in the order they were opened. |
| `current_session()` | The tab the user is working in, or `None`. In split view, the pane with the keyboard focus. |
| `saved_session_names()` | Names of the sessions saved in TurboSSH, re-read from disk when the file has changed (so sessions saved meanwhile in the desktop app count too; `open_saved` does the same). Cheap to poll: the sidebar and Home lists are rebuilt only on a change, and keep the user's selection. |

### Camera and look

| | |
|---|---|
| `camera_enabled()` | Whether the Camera is shown. |
| `set_camera_enabled(enabled)` | Show or hide the Camera button, menu entries and Home card. Hiding it closes any Camera tabs, which stops streams and recordings and releases the device. Saved as the user's choice, unless the host passed `camera=`. |
| `current_theme()` | The theme in force, or `"inherit"`. |
| `set_theme(name)` | Switch theme at once (a theme name, or `"inherit"`). Saved as the user's choice unless the host passed `theme=`, in which case it lasts for this run. |
| `set_chrome(*, menu_bar=None, ribbon=None, sidebar=None, log_dock=None, status_bar=None)` | Show or hide the parts around the tabs; `None` leaves one as it is. |

### Joining the host's menus and toolbars

| | |
|---|---|
| `action_names()` | The names `action()` accepts. |
| `action(name)` | A TurboSSH command as a `QAction`, to put on the host's own toolbar or menu. It is the ribbon's own action, so it follows TurboSSH's state (the Camera action hides with the Camera). `KeyError` for an unknown name. |
| `menus()` | TurboSSH's menus (File, Edit, View, Session, Help) as `QMenu`s: `host.menuBar().addMenu(menu)`. Pair it with `menu_bar=False`. Their shortcuts still only work while the focus is inside TurboSSH. |
| `main_window` | The `MainWindow` inside, for anything this API does not cover. Its methods are internal and can change in any release. |

Action names:

| Name | Command |
|---|---|
| `session` | New session dialog |
| `local` | Local terminal |
| `camera` | Camera tab (hidden while the Camera is off) |
| `sftp` | Switch the current session to its SFTP tab |
| `logs` | Switch the current session to its Logs tab |
| `split` | Split view on / off |
| `log_dock` | Show / hide the log dock |
| `ssh_server` | Set up an OpenSSH server (this PC or a remote one) |
| `settings` | Settings dialog |
| `help` | Documentation |

```python
toolbar = self.addToolBar("Bench")
for name in ("session", "local", "split"):
    toolbar.addAction(turbo.action(name))

turbo = TurboSSHWidget(self, menu_bar=False)
for menu in turbo.menus():
    self.menuBar().addMenu(menu)
```

### Lifetime

| | |
|---|---|
| `shutdown(wait=0.0)` | Close every tab and connection and stop all background work. The application keeps running. Safe to call twice; afterwards every `open_*` method raises `RuntimeError`. `wait` is how many seconds to block while remote COM ports and cameras are released: pass some when the process is about to end. |
| `confirm_close()` | Ask the user, as the desktop app does: "N open sessions will be disconnected", and about remote-file editors with unsaved changes. `True` when nothing is open or they agreed. |
| `is_shut_down` | `True` after `shutdown()`. |

You do not have to call `shutdown()`. It happens by itself when:

- the application quits (`QApplication.aboutToQuit`; it then waits up to 6 s for
  remote ports and cameras to be released, unless your last `shutdown(wait=…)`
  already waited),
- the widget is deleted: `deleteLater()`, a deleted parent, or the last Python
  reference to its parent dropped,
- the widget is shown as a window of its own and the user closes it.

A widget without a parent stays alive until one of those happens, even if your
code drops every reference to it: its sessions run threads, and Qt aborts the
process if a running thread is destroyed.

A typical host window:

```python
def closeEvent(self, event):
    if not self.turbo.confirm_close():
        event.ignore()
        return
    self.turbo.shutdown(wait=6.0)
    event.accept()
```

## `EmbeddedSession`

One open tab. It stays valid after the tab closes (its `state` is then
`"closed"`), so you can keep it around.

### Attributes

| | |
|---|---|
| `name` | The tab's name. |
| `kind` | `"ssh"`, `"serial"`, `"local"` or `"camera"`. |
| `state` | `"connecting"`, `"connected"`, `"disconnected"`, `"failed"` or `"closed"` for SSH and serial; `"open"` or `"closed"` for a local terminal and the Camera. |
| `widget` | The tab's page (a `QWidget`). |
| `terminal` | The terminal widget, or `None` for the Camera. |
| `handler` | The live connection: an `SSHHandler` (SSH, serial over SSH) or a `SerialHandler` (local serial), or `None` until it has connected. Use it to run commands or move files over the same connection: `session.handler.run("uptime")`. It is in safe mode (calls return an `OperationResult` instead of raising) and its calls block, so make them from a worker thread. |

### Signals

| Signal | Arguments | When |
|---|---|---|
| `connected` | | The link is up. Again after an automatic reconnect. |
| `disconnected` | `str` | The link dropped. An SSH tab then retries for 30 s by itself. |
| `failed` | `str` | It could not connect (wrong password, host unreachable, port busy …). |
| `closed` | | The tab is gone, by any route. |

### Methods

| | |
|---|---|
| `send(data)` | Type into the terminal: a `str`, or `bytes` such as `b"\x03"` (Ctrl-C). An SSH or serial session only takes input while `state` is `"connected"`, so send from its `connected` signal; before that, while it reconnects, and once closed this raises `RuntimeError` instead of losing the input. `TypeError` for the Camera. |
| `send_line(text)` | Type a line and press Enter. |
| `screen_text()` | What the terminal shows right now, as plain text (no scrollback). |
| `activate()` | Bring the tab to the front and give it the keyboard. |
| `close()` | Close the tab and its connection. |

## Good to know

- **GUI thread only.** Call everything here from the thread that runs the event
  loop, like any other widget. `handler` calls are the exception: they block, so
  make them from a worker thread.
- **Saved data.** The widget reads and writes the same saved sessions and
  settings as the desktop app (`~/.turbossh`). To keep a host's TurboSSH data
  apart, set the `TURBOSSH_HOME` environment variable before `turbossh.gui` is
  imported. Passwords stay in the OS credential vault either way.
- **One theme per process.** `theme=` applies to every TurboSSH widget in the
  process; the last one created wins, and it stays until the last widget with a
  `theme=` is shut down.
- **Dialogs.** TurboSSH's own dialogs (new session, settings, file editor) are
  parented to the widget, so they carry its theme and centre on the host window.
- **Errors.** PyQt5 ends the whole process when an exception escapes a slot,
  unless `sys.excepthook` has been replaced. If the host has not replaced it, the
  widget installs a hook that prints the traceback and sends it to `log_message`
  instead. A host with its own hook keeps it.
