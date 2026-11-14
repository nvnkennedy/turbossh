"""A program in a Windows pseudo console (ConPTY, Windows 10 1809 and later).

The program gets a real console: the shell edits its own line — Tab completion,
history, PSReadLine's colours — puts its cursor where it wants, and takes
Ctrl+C as Ctrl+C. Both directions are VT text over two pipes, as with a pty
elsewhere, so the terminal widget shows it like any other session.

No Qt in here. Three threads are involved, and each handle has one owner:

* the output pipe is read by a thread of its own, all the time, into a buffer
  :meth:`PseudoConsole.read` takes from. Closing a pseudo console waits for its
  last output to be read (before Windows 11 24H2, for ever if nobody reads),
  so it must never depend on the caller still reading. That thread closes the
  pipe once the console is gone;
* a second thread waits for the program to end, then closes the console, so
  the reader sees the end. The process handle stays open until the program
  has ended and :meth:`PseudoConsole.close` was called, so its PID is not
  reused meanwhile;
* :meth:`write` and :meth:`close` share a lock for the input pipe.
"""

from __future__ import annotations

import ctypes
import subprocess
import threading
from collections import deque
from ctypes import wintypes

_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_STARTF_USESTDHANDLES = 0x00000100
_INFINITE = 0xFFFFFFFF
_STILL_ACTIVE = 259
# output not taken by read() is kept up to this much; beyond it the oldest goes
# (the terminal's own reader drops a backlog the same way)
_BUFFER_CAP = 8 * 1024 * 1024


class _Coord(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


_k32 = None


def _kernel32():
    """kernel32 with the signatures used here; raises OSError where Windows has
    no pseudo console (before Windows 10 1809) — the caller falls back then."""
    global _k32
    if _k32 is not None:
        return _k32
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    if not hasattr(k, "CreatePseudoConsole"):
        raise OSError("this Windows has no pseudo console (ConPTY)")
    k.CreatePseudoConsole.argtypes = [_Coord, wintypes.HANDLE, wintypes.HANDLE,
                                      wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    k.CreatePseudoConsole.restype = ctypes.c_long            # HRESULT
    k.ResizePseudoConsole.argtypes = [wintypes.HANDLE, _Coord]
    k.ResizePseudoConsole.restype = ctypes.c_long
    k.ClosePseudoConsole.argtypes = [wintypes.HANDLE]
    k.ClosePseudoConsole.restype = None
    k.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE),
                             ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p,
                             wintypes.DWORD]
    k.CreatePipe.restype = wintypes.BOOL
    k.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    k.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    k.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    k.UpdateProcThreadAttribute.restype = wintypes.BOOL
    k.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    k.DeleteProcThreadAttributeList.restype = None
    k.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(_StartupInfoEx), ctypes.POINTER(_ProcessInformation)]
    k.CreateProcessW.restype = wintypes.BOOL
    k.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k.ReadFile.restype = wintypes.BOOL
    k.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k.WriteFile.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetExitCodeProcess.restype = wintypes.BOOL
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.TerminateProcess.restype = wintypes.BOOL
    k.GetConsoleWindow.argtypes = []
    k.GetConsoleWindow.restype = wintypes.HWND
    k.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
    k.SetConsoleCtrlHandler.restype = wintypes.BOOL
    _k32 = k
    return k


def _let_ctrl_c_through(k) -> None:
    """Whether a process ignores Ctrl+C is inherited by every process it starts
    — the shell, and what the shell runs — and a process started in a new
    process group ignores it: the updater relaunches TurboSSH that way, and a
    host application may start it so. Ctrl+C in the console then stopped
    nothing. With no console of its own Ctrl+C never reaches this process, so
    the setting only matters for what it starts: it is cleared. (A process with
    a console keeps whatever its console's owner chose.)"""
    try:
        if not k.GetConsoleWindow():
            k.SetConsoleCtrlHandler(None, False)
    except Exception:
        pass


def available() -> bool:
    """Whether this Windows can run a program in a pseudo console."""
    try:
        _kernel32()
        return True
    except (OSError, AttributeError):
        return False


def _error(what: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{what} failed: {ctypes.FormatError(code).strip()}")


class PseudoConsole:
    """*argv* started in a pseudo console of *cols* x *rows*. Quacks enough like
    a :class:`subprocess.Popen` (``pid``, ``poll()``, ``wait()``, ``kill()``) for
    the code that manages processes.

    ``read()`` blocks until there is output and returns it, or None once the
    console is gone (the program ended, or :meth:`close`). ``write()`` sends
    keystrokes as a terminal would (Enter is ``\\r``, Ctrl+C is ``\\x03``)."""

    def __init__(self, argv, *, cwd=None, cols: int = 120, rows: int = 34):
        k = _kernel32()
        self._k = k
        self.returncode = None
        self._console_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._proc_lock = threading.Lock()
        self._out_cond = threading.Condition()
        self._out: deque = deque()
        self._out_bytes = 0
        self._out_done = False
        self._exited = threading.Event()
        self._closing = False            # close() was called (see _release_process)
        self._hpc = None
        self._in = None
        self._hproc = None

        in_read, in_write = wintypes.HANDLE(), wintypes.HANDLE()
        out_read, out_write = wintypes.HANDLE(), wintypes.HANDLE()
        if not k.CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write), None, 0):
            raise _error("CreatePipe")
        if not k.CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write), None, 0):
            err = _error("CreatePipe")
            k.CloseHandle(in_read); k.CloseHandle(in_write)
            raise err
        hpc = wintypes.HANDLE()
        hr = k.CreatePseudoConsole(_Coord(_clamp(cols), _clamp(rows)), in_read,
                                   out_write, 0, ctypes.byref(hpc))
        # the console has its own copies of its ends of the pipes now
        k.CloseHandle(in_read)
        k.CloseHandle(out_write)
        if hr != 0:
            k.CloseHandle(in_write); k.CloseHandle(out_read)
            raise OSError(f"CreatePseudoConsole failed (0x{hr & 0xFFFFFFFF:08X})")
        self._hpc, self._in = hpc, in_write
        try:
            self._spawn(argv, cwd)
        except BaseException:
            # the output end first: with nobody reading it, closing the console
            # would wait for its first output to be read
            k.CloseHandle(out_read)
            self._close_console()
            with self._write_lock:
                k.CloseHandle(self._in)
                self._in = None
            raise
        threading.Thread(target=self._pump, args=(out_read,), name="conpty-output",
                         daemon=True).start()
        threading.Thread(target=self._watch, name="conpty-exit", daemon=True).start()

    def _spawn(self, argv, cwd):
        k = self._k
        _let_ctrl_c_through(k)
        size = ctypes.c_size_t()
        k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attrs = ctypes.create_string_buffer(size.value)
        if not k.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)):
            raise _error("InitializeProcThreadAttributeList")
        try:
            if not k.UpdateProcThreadAttribute(
                    attrs, 0, _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, self._hpc,
                    ctypes.sizeof(wintypes.HANDLE), None, None):
                raise _error("UpdateProcThreadAttribute")
            si = _StartupInfoEx()
            si.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
            # no standard handles of ours: the program takes the console's
            # (without this, one started from a console would write there)
            si.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
            pi = _ProcessInformation()
            cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(argv)))
            if not k.CreateProcessW(None, cmdline, None, None, False,
                                    _EXTENDED_STARTUPINFO_PRESENT, None, cwd,
                                    ctypes.byref(si), ctypes.byref(pi)):
                raise _error(f"Starting {argv[0]}")
        finally:
            k.DeleteProcThreadAttributeList(attrs)
        k.CloseHandle(pi.hThread)
        self._hproc = pi.hProcess
        self.pid = pi.dwProcessId

    # ---- the output thread ----
    def _pump(self, handle):
        k = self._k
        buf = ctypes.create_string_buffer(65536)
        got = wintypes.DWORD()
        try:
            while True:
                if not k.ReadFile(handle, buf, len(buf), ctypes.byref(got), None) \
                        or got.value == 0:
                    break                # the console is gone: broken pipe
                data = buf.raw[:got.value]
                with self._out_cond:
                    self._out.append(data)
                    self._out_bytes += len(data)
                    while self._out_bytes > _BUFFER_CAP and len(self._out) > 1:
                        self._out_bytes -= len(self._out.popleft())
                    self._out_cond.notify_all()
        finally:
            k.CloseHandle(handle)
            with self._out_cond:
                self._out_done = True
                self._out_cond.notify_all()

    def read(self, size: int = 65536):
        """Output as it comes (blocks until there is some); None at the end."""
        with self._out_cond:
            while not self._out and not self._out_done:
                self._out_cond.wait()
            if not self._out:
                return None
            parts, total = [], 0
            while self._out and total < size:
                chunk = self._out.popleft()
                if total + len(chunk) > size:
                    keep = size - total
                    self._out.appendleft(chunk[keep:])
                    chunk = chunk[:keep]
                parts.append(chunk)
                total += len(chunk)
            self._out_bytes -= total
            return b"".join(parts)

    # ---- the exit thread ----
    def _watch(self):
        k = self._k
        k.WaitForSingleObject(self._hproc, _INFINITE)
        with self._proc_lock:
            code = wintypes.DWORD()
            if k.GetExitCodeProcess(self._hproc, ctypes.byref(code)):
                self.returncode = code.value
            else:
                self.returncode = -1
            self._exited.set()
            if self._closing:
                self._release_process()
        # the program is gone: so is the console, and the reader sees the end
        self._close_console()

    def _release_process(self):
        """(under _proc_lock) Close the process handle. It is kept until the
        program has ended AND close() was called: while it is open Windows
        cannot give the program's PID to another process, so the caller can
        still look for what the program started, by that PID, without hitting
        a stranger's process tree."""
        if self._hproc is not None:
            self._k.CloseHandle(self._hproc)
            self._hproc = None

    # ---- process ----
    def poll(self):
        return self.returncode if self._exited.is_set() else None

    def wait(self, timeout=None):
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired(f"pid {self.pid}", timeout)
        return self.returncode

    def kill(self) -> None:
        with self._proc_lock:
            if self._hproc is not None and not self._exited.is_set():
                self._k.TerminateProcess(self._hproc, 1)

    # ---- input ----
    def write(self, data: bytes) -> None:
        """Blocks until the console has taken *data*; raises OSError once it is
        closed."""
        k = self._k
        view = memoryview(bytes(data))
        with self._write_lock:
            while view:
                if self._in is None:
                    raise OSError("the console is closed")
                done = wintypes.DWORD()
                chunk = bytes(view[:65536])
                if not k.WriteFile(self._in, chunk, len(chunk), ctypes.byref(done), None):
                    raise _error("Writing to the console")
                view = view[done.value:]

    def resize(self, cols: int, rows: int) -> None:
        with self._console_lock:
            if self._hpc is not None:
                self._k.ResizePseudoConsole(self._hpc, _Coord(_clamp(cols), _clamp(rows)))

    # ---- the end ----
    def _close_console(self):
        with self._console_lock:
            hpc, self._hpc = self._hpc, None
        if hpc is not None:
            # ends the console host; the output thread keeps reading meanwhile,
            # so this does not wait on it
            self._k.ClosePseudoConsole(hpc)

    def close(self) -> None:
        """End the program (if it still runs) and the console. Safe to call
        twice; another thread blocked in read() gets None."""
        self.kill()
        self._close_console()
        with self._write_lock:
            handle, self._in = self._in, None
            if handle is not None:
                self._k.CloseHandle(handle)
        with self._proc_lock:
            self._closing = True
            if self._exited.is_set():    # else the exit thread does it, once it has
                self._release_process()  # read the exit code


def _clamp(n) -> int:
    return max(1, min(int(n), 32767))
