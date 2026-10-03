"""A shell process on this machine, and the line editing it needs on Windows.

On POSIX the shell runs on a real pty, so it is a full terminal. On Windows it
runs over plain pipes (no ConPTY: that needs Windows 10 1809+ and a lot of
Win32 plumbing), which means the shell has no terminal to echo keystrokes or
edit the line — :class:`LineEditor` does that locally, with history — and Ctrl+C
cannot be delivered as a signal, so "interrupt" stops whatever the shell started
and leaves the shell itself at its prompt.

No Qt in here: the widget is :mod:`turbossh.gui.local_terminal`.
"""

from __future__ import annotations

import codecs
import os
import subprocess

_WINDOWS = os.name == "nt"
_UTF8 = ("$e=New-Object System.Text.UTF8Encoding $false;"
         "[Console]::OutputEncoding=$e;[Console]::InputEncoding=$e")
# label -> argv. Both are told to talk UTF-8, and cmd is told not to echo the
# commands it reads (the line editor already showed them).
WINDOWS_SHELLS = {
    "PowerShell": ["powershell.exe", "-NoLogo", "-NoExit", "-Command", _UTF8],
    "Command Prompt": ["cmd.exe", "/Q", "/K", "chcp 65001>nul"],
}


def available_shells() -> list:
    """Labels of the shells this machine can open."""
    if _WINDOWS:
        return list(WINDOWS_SHELLS)
    return [os.path.basename(os.environ.get("SHELL") or "/bin/sh")]


def _descendants(pid: int) -> list:
    """PIDs of every process started (directly or not) by *pid* — Windows. The
    shell's console host is left out: ending it would take the shell with it."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)   # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return []
    children: dict = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = kernel.Process32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.szExeFile.lower() != b"conhost.exe":
                children.setdefault(entry.th32ParentProcessID, []).append(
                    entry.th32ProcessID)
            more = kernel.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    found, queue = [], [pid]
    while queue:
        for child in children.get(queue.pop(), []):
            if child not in found and child != pid:
                found.append(child)
                queue.append(child)
    return found


def _terminate(pid: int) -> None:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x0001, False, pid)             # PROCESS_TERMINATE
    if handle:
        try:
            kernel.TerminateProcess(handle, 1)
        finally:
            kernel.CloseHandle(handle)


class LocalShell:
    """A shell process on this machine. ``read()`` blocks and returns bytes, or
    None once the shell has exited; ``uses_pty`` says whether keystrokes can be
    written to it raw (POSIX) or must go through a :class:`LineEditor`."""

    def __init__(self, shell: str | None = None, cwd: str | None = None):
        self.label = shell or available_shells()[0]
        self.cwd = cwd or os.path.expanduser("~")
        self.uses_pty = not _WINDOWS
        # PowerShell repeats each line it reads from a pipe after its prompt;
        # cmd (/Q) does not. The line editor must not show the command twice.
        self.echoes_input = _WINDOWS and self.label == "PowerShell"
        self._proc = None
        self._master = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if _WINDOWS:
            argv = WINDOWS_SHELLS.get(self.label) or WINDOWS_SHELLS["PowerShell"]
            self._proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0, cwd=self.cwd,
                # no window; and its own process group, so it is not hit by a
                # Ctrl+C aimed at the app that launched TurboSSH from a console
                creationflags=0x08000000 | 0x00000200)
            return
        import pty
        master, slave = pty.openpty()
        shell = os.environ.get("SHELL") or "/bin/sh"
        env = dict(os.environ, TERM="xterm-256color")
        try:
            self._proc = subprocess.Popen(
                [shell, "-i"], stdin=slave, stdout=slave, stderr=slave,
                cwd=self.cwd, env=env, start_new_session=True, close_fds=True)
        finally:
            os.close(slave)
        self._master = master

    def read(self, size: int = 65536):
        try:
            if self._master is not None:
                data = os.read(self._master, size)
            else:
                data = self._proc.stdout.read(size)
        except (OSError, ValueError, AttributeError):
            return None                  # the pty / pipe is gone: the shell ended
        return data or None

    def write(self, data: bytes) -> None:
        try:
            if self._master is not None:
                os.write(self._master, data)
            elif self._proc is not None and self._proc.stdin:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def interrupt(self) -> None:
        """Stop the running command. POSIX: Ctrl+C through the pty. Windows: end
        the processes the shell started (a pipe cannot carry a Ctrl+C, and
        Ctrl+Break is ignored by many programs); the shell stays at its prompt."""
        if self._master is not None:
            self.write(b"\x03")
        elif self.alive:
            for pid in _descendants(self._proc.pid):
                _terminate(pid)

    def resize(self, cols: int, rows: int) -> None:
        if self._master is None:
            return                       # pipes have no window size
        try:
            import fcntl
            import struct
            import termios
            fcntl.ioctl(self._master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", int(rows), int(cols), 0, 0))
        except Exception:
            pass

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if _WINDOWS:
                    for pid in _descendants(proc.pid):
                        _terminate(pid)
                    proc.kill()
                else:
                    import signal
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGHUP)
                    except OSError:
                        proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
        master, self._master = self._master, None
        if master is not None:
            try:
                os.close(master)
            except OSError:
                pass


class LineEditor:
    """Echo and line editing for a shell on pipes.

    Fed the bytes the terminal widget would send to a pty; calls ``echo(bytes)``
    for what should appear on screen, ``submit(text)`` with a finished line, and
    ``interrupt()`` for Ctrl+C. Keys: Backspace, Enter, Up / Down (history),
    Ctrl+U (clear the line), Ctrl+C. Pasted text may contain several lines.
    """

    def __init__(self, echo, submit, interrupt=None, history_limit: int = 500,
                 shell_echoes: bool = False):
        self._echo, self._submit, self._interrupt = echo, submit, interrupt
        # True when the shell prints each command itself once it reads it: the
        # locally echoed copy is then wiped on Enter instead of ending the line
        self._shell_echoes = shell_echoes
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.line = ""
        self.history: list = []
        self._pos = 0                    # index into history while browsing
        self._limit = history_limit
        self._pending = ""               # an escape sequence still arriving

    def feed(self, data: bytes) -> None:
        text = self._pending + self._decoder.decode(data)
        self._pending = ""
        i, typed = 0, []

        def flush():
            if typed:
                chunk = "".join(typed)
                typed.clear()
                self.line += chunk
                self._echo(chunk.encode("utf-8"))

        while i < len(text):
            ch = text[i]
            if ch == "\x1b":
                flush()
                consumed = self._escape(text[i:])
                if consumed == 0:        # incomplete: wait for the rest
                    self._pending = text[i:]
                    return
                i += consumed
                continue
            i += 1
            if ch in "\r\n":
                flush()
                self._enter()
            elif ch in "\x7f\x08":
                flush()
                if self.line:
                    self.line = self.line[:-1]
                    self._echo(b"\b \b")
            elif ch == "\x03":
                flush()
                self._echo(b"^C\r\n")
                self.line = ""
                if self._interrupt:
                    self._interrupt()
            elif ch == "\x15":
                flush()
                self._replace("")
            elif ch == "\t":
                typed.append("    ")     # no completion over a pipe: indent
            elif ch >= " ":
                typed.append(ch)
        flush()

    def _escape(self, text: str) -> int:
        """Handle the escape sequence at the start of *text*; return its length
        (0 if it is not complete yet)."""
        if len(text) < 2:
            return 0
        if text[1] not in "[O":
            return 2                     # ESC + one char (Alt+key): ignored
        j = 2
        while j < len(text) and not ("@" <= text[j] <= "~"):
            j += 1
        if j >= len(text):
            return 0
        if text[1] == "[" and text[2:j] in ("200", "201") and text[j] == "~":
            return j + 1                 # bracketed-paste markers: drop
        if text[j] == "A":
            self._history(-1)
        elif text[j] == "B":
            self._history(+1)
        return j + 1

    def _enter(self):
        line = self.line
        if self._shell_echoes:
            if line:
                self._echo(b"\b \b" * len(line))
        else:
            self._echo(b"\r\n")
        if line.strip() and (not self.history or self.history[-1] != line):
            self.history.append(line)
            del self.history[:-self._limit]
        self.line = ""
        self._pos = len(self.history)
        self._submit(line)

    def _history(self, step: int):
        if not self.history:
            return
        self._pos = max(0, min(len(self.history), self._pos + step))
        self._replace(self.history[self._pos] if self._pos < len(self.history) else "")

    def _replace(self, new: str):
        if self.line:
            self._echo(b"\b \b" * len(self.line))
        self.line = new
        if new:
            self._echo(new.encode("utf-8"))
