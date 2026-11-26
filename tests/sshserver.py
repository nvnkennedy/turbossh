"""A loopback SSH server for the test suite (no network, no real sshd).

It is a Paramiko server with scripted behaviour for the cases the engine has to
get right: commands that hang, flood, exit without closing their output, a
``sudo`` that prompts (or doesn't), password / public-key auth with a log of
every attempt, ``direct-tcpip`` forwarding so it can act as its own jump host,
and an SFTP subsystem rooted in a temp folder.
"""

from __future__ import annotations

import os
import re
import shlex
import socket
import threading
import time

import paramiko

HOST_KEY = paramiko.RSAKey.generate(2048)
_SUDO_RE = re.compile(r"^sudo -S -p '([^']+)' (.*)$")


class _Handle(paramiko.SFTPHandle):
    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def chattr(self, attr):
        log = getattr(self, "chmod_log", None)          # set by StubSFTP.open
        if log is not None and attr._flags & attr.FLAG_PERMISSIONS:
            log.append((self.filename, attr.st_mode & 0o7777))
        return paramiko.SFTP_OK


class StubSFTP(paramiko.SFTPServerInterface):
    """SFTP over a local folder (``server.sftp_root``)."""

    def __init__(self, server, *args, **kwargs):
        super().__init__(server, *args, **kwargs)
        self._srv = server

    def _plain(self, path):
        """The tidied server path, rooted at one "/". Paramiko's default asks
        os.path.isabs, which on Windows from Python 3.13 says "/x" is not
        absolute, and then gives "//x" (the files were the same, but a path
        compared as text was not)."""
        return "/" + super().canonicalize(path).lstrip("/")

    def _real(self, path):
        return self._srv.sftp_root + self._plain(path)

    def canonicalize(self, path):
        """realpath. OpenSSH's sftp-server resolves symlinks here; some servers
        only tidy the string (``server.resolve_links = False`` imitates those)."""
        plain = self._plain(path)
        if not self._srv.resolve_links:
            return plain
        root = os.path.realpath(self._srv.sftp_root)
        real = os.path.realpath(self._srv.sftp_root + plain)
        if os.path.normcase(real).startswith(os.path.normcase(root)):
            return (real[len(root):].replace("\\", "/") or "/")
        return plain

    @staticmethod
    def _err(exc):
        return paramiko.SFTPServer.convert_errno(exc.errno)

    def list_folder(self, path):
        real = self._real(path)
        try:
            out = []
            names = os.listdir(real)
            order = self._srv.listing_order
            if order:
                names = sorted(names, reverse=(order == "reverse"))
            for name in names:
                attr = paramiko.SFTPAttributes.from_stat(
                    os.lstat(os.path.join(real, name)))
                attr.filename = name
                out.append(attr)
            return out
        except OSError as exc:
            return self._err(exc)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as exc:
            return self._err(exc)

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.lstat(self._real(path)))
        except OSError as exc:
            return self._err(exc)

    def open(self, path, flags, attr):
        real = self._real(path)
        try:
            fd = os.open(real, flags | getattr(os, "O_BINARY", 0), 0o666)
        except OSError as exc:
            return self._err(exc)
        if flags & os.O_WRONLY:
            mode = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            mode = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            mode = "rb"
        fh = os.fdopen(fd, mode)
        handle = _Handle(flags)
        handle.chmod_log = self._srv.chmod_calls
        handle.filename = real
        handle.readfile = fh
        handle.writefile = fh
        return handle

    def remove(self, path):
        time.sleep(self._srv.sftp_delay)     # a slow link's round trip
        try:
            os.remove(self._real(path))
        except OSError as exc:
            return self._err(exc)
        return paramiko.SFTP_OK

    def rename(self, oldpath, newpath):
        self._srv.rename_calls.append("rename")
        try:
            os.rename(self._real(oldpath), self._real(newpath))
        except OSError as exc:
            return self._err(exc)
        return paramiko.SFTP_OK

    def posix_rename(self, oldpath, newpath):
        self._srv.rename_calls.append("posix_rename")
        if not self._srv.posix_rename_supported:
            return paramiko.SFTP_OP_UNSUPPORTED
        try:
            os.replace(self._real(oldpath), self._real(newpath))
        except OSError as exc:
            return self._err(exc)
        return paramiko.SFTP_OK

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._real(path))
        except OSError as exc:
            return self._err(exc)
        return paramiko.SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(self._real(path))
        except OSError as exc:
            return self._err(exc)
        return paramiko.SFTP_OK

    def chattr(self, path, attr):
        if attr._flags & attr.FLAG_PERMISSIONS:
            self._srv.chmod_calls.append((path, attr.st_mode & 0o7777))
        if (self._srv.apply_modes and os.name != "nt"
                and attr._flags & attr.FLAG_PERMISSIONS):
            try:
                os.chmod(self._real(path), attr.st_mode & 0o7777)
            except OSError as exc:
                return self._err(exc)
        return paramiko.SFTP_OK


class Server(paramiko.ServerInterface):
    """Scripted server behaviour + a record of what clients did."""

    def __init__(self, sftp_root):
        self.sftp_root = sftp_root
        self.auth_log = []               # every auth attempt, in order
        self.accepted_key = None         # a paramiko PKey to accept, if any
        self.sudo_mode = "prompt"        # "prompt" | "nopasswd"
        self.sudo_password = "good"
        self.posix_rename_supported = True
        self.resolve_links = True
        # The order folder listings come in: None is the file system's (sorted
        # on Windows and macOS, hash order on Linux's ext4, so it differs from
        # runner to runner); "sorted" / "reverse" pin it, so a test can show
        # that a result does not depend on it.
        self.listing_order = None
        # chmod requests are accepted and ignored unless this is set (POSIX
        # only: on Windows a mode without owner write makes the file read-only,
        # which other tests' later writes would trip over)
        self.apply_modes = False
        self.chmod_calls = []            # (path, mode) of every chmod asked for
        self.rename_calls = []
        self.tcpip_dest = {}             # chanid -> (host, port)
        self.execs = []
        # what the login takes: get_allowed_auths, and SSH's "none" method (a
        # QNX / RichOS head unit logs in without a password or key)
        self.allowed_auths = "password,publickey"
        self.none_ok = False
        # a QNX head unit: an exec session cannot start programs ("sh: uname:
        # cannot execute"), an interactive login shell can (see _qnx_shell)
        self.qnx = False
        self.shell_lines = []            # what the QNX shell was sent, line by line
        # called (on the server's thread) when a password is refused: a test
        # closes the connection there, as a server at MaxAuthTries does
        self.on_refusal = None
        self.sftp_delay = 0.0            # s an SFTP remove takes (a slow link)

    # ---- auth ----
    def get_allowed_auths(self, username):
        return self.allowed_auths

    def check_auth_none(self, username):
        self.auth_log.append("<none>")
        return paramiko.AUTH_SUCCESSFUL if self.none_ok else paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        self.auth_log.append(password)
        if username == "blank":
            ok = password == ""
        else:
            ok = password == "good"
        if not ok and self.on_refusal is not None:
            self.on_refusal()
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        self.auth_log.append("<publickey>")
        if self.accepted_key is not None and key == self.accepted_key:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    # ---- channels ----
    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        """An interactive "shell" that echoes back whatever is typed."""
        if self.qnx:
            threading.Thread(target=self._qnx_shell, args=(channel,),
                             daemon=True).start()
            return True

        def echo():
            try:
                channel.sendall(b"$ ")
                while True:
                    data = channel.recv(4096)
                    if not data:
                        break
                    channel.sendall(data)
            except Exception:
                pass
        threading.Thread(target=echo, daemon=True).start()
        return True

    def check_channel_window_change_request(self, *args):
        return True

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        self.tcpip_dest[chanid] = destination
        return paramiko.OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel, command):
        cmd = command.decode()
        self.execs.append(cmd)
        threading.Thread(target=self._run, args=(channel, cmd), daemon=True).start()
        return True

    # ---- scripted commands ----
    def _finish(self, channel, status):
        channel.send_exit_status(status)
        channel.close()

    def _qnx_shell(self, channel):
        """A login shell as QNX's: a banner and a "# " prompt, echo of what is
        typed (with a pty) until ``stty -echo``; runs echo (with $? and the ''
        of the markers), eval of a quoted script, export, uname, true / false,
        hang, exit, and quit-now (the shell dies at once). A quoted string
        open at the end of a line goes on to the next one."""
        state = {"echo": True, "status": 0, "prompt": "# "}

        def out(text):
            channel.sendall((text + "\r\n").encode())

        def parts_of(line):
            parts, cur, quoted = [], "", False
            for ch in line:
                if ch == "'":
                    quoted = not quoted
                if ch == ";" and not quoted:
                    parts.append(cur)
                    cur = ""
                else:
                    cur += ch
            parts.append(cur)
            return parts

        def run(part):
            part = part.strip()
            if part.startswith("(") and part.endswith(")"):   # a subshell
                part = part[1:-1].strip()
            if not part:
                return None
            if part == "stty -echo":
                state["echo"] = False
            elif part.startswith("PS1="):
                state["prompt"] = ""
            elif part.startswith("PS2="):
                pass
            elif part.startswith("export "):
                state["status"] = 0
            elif part == "exit":
                return "exit"
            elif part == "quit-now":
                return "quit"
            elif part.startswith("eval "):
                for line in shlex.split(part[5:])[0].split("\n"):
                    for piece in parts_of(line):
                        stop = run(piece)
                        if stop:
                            return stop
            elif part.startswith("echo "):
                out(part[5:].replace("''", "").replace("$?", str(state["status"])))
                state["status"] = 0
            elif part == "uname":
                out("QNX")
                state["status"] = 0
            elif part == "true":
                state["status"] = 0
            elif part == "false":
                state["status"] = 1
            elif part == "hang":
                time.sleep(120)
            else:
                out(f"sh: {part}: not found")
                state["status"] = 127
            return None
        try:
            channel.sendall(b"QNX Neutrino 7.1\r\nWelcome\r\n# ")
            buf, pending = b"", None
            while True:
                data = channel.recv(4096)
                if not data:
                    return
                if state["echo"]:
                    channel.sendall(data.replace(b"\n", b"\r\n"))
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode().rstrip("\r")
                    self.shell_lines.append(text)
                    pending = text if pending is None else pending + "\n" + text
                    if pending.count("'") % 2:     # a quoted string goes on
                        continue
                    logical, pending = pending, None
                    for part in parts_of(logical):
                        stop = run(part)
                        if stop == "exit":
                            channel.send_exit_status(state["status"])
                            channel.close()
                            return
                        if stop == "quit":
                            channel.close()
                            return
                    if state["prompt"]:
                        channel.sendall(state["prompt"].encode())
        except Exception:
            pass

    def _run(self, channel, cmd):
        time.sleep(0.2)                  # let the exec-request reply go out first
        try:
            if self.qnx:
                channel.sendall_stderr(f"sh: {cmd.split()[0]}: cannot execute\n".encode())
                self._finish(channel, 126)
                return
            sudo = _SUDO_RE.match(cmd)
            if sudo:
                self._sudo(channel, sudo.group(1), sudo.group(2))
            elif cmd == "hang":
                time.sleep(120)
            elif cmd == "partial-hang":
                channel.sendall(b"before\n")
                time.sleep(120)
            elif cmd == "big":
                channel.sendall(b"x" * (6 * 1024 * 1024))
                self._finish(channel, 0)
            elif cmd == "bigerr":
                channel.sendall_stderr(b"e" * (3 * 1024 * 1024))
                channel.sendall(b"o" * (3 * 1024 * 1024))
                self._finish(channel, 0)
            elif cmd == "bg":
                # exits, but a background child keeps the output stream open:
                # an exit status with no EOF / close after it
                channel.sendall(b"done\n")
                channel.send_exit_status(0)
                time.sleep(120)
            elif cmd.startswith("exit "):
                channel.sendall_stderr(b"failing\n")
                self._finish(channel, int(cmd.split()[1]))
            elif cmd == "uname -s":
                channel.sendall(b"Linux\n")
                self._finish(channel, 0)
            else:
                channel.sendall(b"ok\n")
                self._finish(channel, 0)
        except Exception:
            pass

    @staticmethod
    def _read_line(channel, timeout):
        """One line from the client's stdin; b"" on EOF, None if nothing came."""
        channel.settimeout(timeout)
        buf = b""
        while not buf.endswith(b"\n"):
            try:
                piece = channel.recv(1)
            except socket.timeout:
                return buf or None
            if not piece:
                return buf
            buf += piece
        return buf

    def _sudo(self, channel, marker, inner):
        if self.sudo_mode == "nopasswd":
            # sudo does not ask: anything the client still writes goes to the
            # COMMAND's stdin — report it so a test can prove nothing was sent
            leaked = self._read_line(channel, 0.6)
            if leaked:
                channel.sendall(b"STDIN:" + leaked)
            channel.sendall(b"root\n")
            self._finish(channel, 0)
            return
        channel.sendall_stderr(marker.encode())
        line = self._read_line(channel, 5.0)
        if line == (self.sudo_password + "\n").encode():
            channel.sendall(b"root\n")
            self._finish(channel, 0)
            return
        channel.sendall_stderr(b"Sorry, try again.\n" + marker.encode())
        if self._read_line(channel, 5.0) in (b"", None):
            channel.sendall_stderr(b"sudo: no password was provided\n")
        self._finish(channel, 1)


def _pump(chan, sock):
    """Relay a direct-tcpip channel to a real socket (the jump-host role)."""
    def one_way(src_recv, dst_send, closer):
        try:
            while True:
                data = src_recv(65536)
                if not data:
                    break
                dst_send(data)
        except Exception:
            pass
        finally:
            closer()

    def close_all():
        for obj in (chan, sock):
            try:
                obj.close()
            except Exception:
                pass
    threading.Thread(target=one_way, args=(chan.recv, sock.sendall, close_all),
                     daemon=True).start()
    threading.Thread(target=one_way, args=(sock.recv, chan.sendall, close_all),
                     daemon=True).start()


class _SilentSFTP(paramiko.SubsystemHandler):
    """An SFTP subsystem that is accepted but never answers (a target that
    took the login and then stopped answering)."""

    def start_subsystem(self, name, transport, channel):
        time.sleep(600)


class LoopbackSSH:
    """Listens on 127.0.0.1:<port>; ``.server`` is the scripted behaviour."""

    def __init__(self, sftp_root):
        self.server = Server(str(sftp_root).replace("\\", "/"))
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(50)
        self.port = self._sock.getsockname()[1]
        self.transports = []
        self.accepted = 0                # TCP connections taken
        self.sftp_silent = False         # SFTP accepted, never answering
        self.pre_banner = b""            # sent before the SSH greeting
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while True:
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        if self.pre_banner:
            client.sendall(self.pre_banner)
        transport = paramiko.Transport(client)
        transport.add_server_key(HOST_KEY)
        if self.sftp_silent:
            transport.set_subsystem_handler("sftp", _SilentSFTP)
        else:
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, StubSFTP)
        try:
            transport.start_server(server=self.server)
        except Exception:
            return
        self.transports.append(transport)
        while transport.is_active():
            chan = transport.accept(0.5)
            if chan is None:
                continue
            dest = self.server.tcpip_dest.pop(chan.get_id(), None)
            if dest is not None:
                try:
                    _pump(chan, socket.create_connection(dest, timeout=5))
                except OSError:
                    chan.close()

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass
        for transport in self.transports:
            try:
                transport.close()
            except Exception:
                pass
