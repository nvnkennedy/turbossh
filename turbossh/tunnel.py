"""SSH port forwarding (tunnels) over an existing connection's transport.

* LocalForward  — like `ssh -L`  (expose a remote service on a local port)
* RemoteForward — like `ssh -R`  (expose a local service on a remote port)

Each returns a stoppable handle with .close() and context-manager support.
Forwarding runs on daemon threads so it never blocks the caller.
"""

from __future__ import annotations

import os
import select
import socket
import socketserver
import threading

from . import _logging

_log = _logging.get_logger("tunnel")

# How long a new forwarded connection waits for the SSH server to open its
# channel. paramiko's default is an hour, so a server that never answered held
# the client's connection, and a thread, open for that long.
CHANNEL_OPEN_TIMEOUT = 20.0


def _pump(a, b):
    """Bidirectionally pump bytes between two socket-like objects until EOF."""
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 1.0)
            if a in r:
                data = a.recv(4096)
                if not data:
                    break
                b.sendall(data)
            if b in r:
                data = b.recv(4096)
                if not data:
                    break
                a.sendall(data)
    except (OSError, EOFError, ValueError):
        pass                    # ValueError: select() on an end closed meanwhile
    finally:
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass


def _hang_up(end) -> None:
    """End one side of a relayed connection so its pump wakes up and exits:
    shut a socket down (closing it under select() can leave the pump waiting),
    close a channel. The pump then closes both sides."""
    try:
        if isinstance(end, socket.socket):
            end.shutdown(socket.SHUT_RDWR)
        else:
            end.close()
    except Exception:
        pass


class _Live:
    """The connections a forward is relaying, so close() can end them: stopping
    the listener alone left every established connection pumping."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ends = {}
        self.closed = False

    def add(self, *ends) -> bool:
        """Track *ends*; False when the forward is already closed."""
        with self._lock:
            if self.closed:
                return False
            for end in ends:
                self._ends[id(end)] = end
            return True

    def discard(self, *ends) -> None:
        with self._lock:
            for end in ends:
                self._ends.pop(id(end), None)

    def close_all(self) -> None:
        with self._lock:
            self.closed = True
            ends, self._ends = list(self._ends.values()), {}
        for end in ends:
            _hang_up(end)


class _LocalHandler(socketserver.BaseRequestHandler):
    def handle(self):
        srv = self.server
        dest = f"{srv.remote_host}:{srv.remote_port}"
        try:
            chan = srv.ssh_transport.open_channel(
                "direct-tcpip", (srv.remote_host, srv.remote_port),
                self.request.getpeername(), timeout=srv.open_timeout)
        except Exception as exc:
            # the client's connection is dropped either way; say why, or a
            # forward the server refuses looks like a dead service
            _log.warning("Forward to %s: the SSH server did not open the "
                         "connection (%s)", dest, str(exc) or type(exc).__name__)
            return
        if chan is None:
            return
        if not srv.live.add(self.request, chan):      # closed meanwhile
            chan.close()
            return
        try:
            _pump(self.request, chan)
        finally:
            srv.live.discard(self.request, chan)


class _ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    # SO_REUSEADDR means "rebind a port in TIME_WAIT" on POSIX, but on Windows it
    # lets a second socket bind a port that is actively in use — so a forward on
    # a busy port would "succeed" and then receive connections at random.
    allow_reuse_address = os.name != "nt"


class LocalForward:
    """`ssh -L [local_host:]local_port -> remote_host:remote_port`."""

    def __init__(self, transport, remote_host, remote_port,
                 local_port=0, local_host="127.0.0.1", *,
                 open_timeout: float = CHANNEL_OPEN_TIMEOUT):
        self.server = _ForwardServer((local_host, local_port), _LocalHandler)
        self.server.ssh_transport = transport
        self.server.remote_host = remote_host
        self.server.remote_port = remote_port
        self.server.open_timeout = open_timeout
        self.server.live = _Live()
        self.local_host, self.local_port = self.server.server_address[:2]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        """Stop listening and end every connection still being relayed."""
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        self.server.live.close_all()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self):
        return (f"<LocalForward {self.local_host}:{self.local_port} -> "
                f"{self.server.remote_host}:{self.server.remote_port}>")


class RemoteForward:
    """`ssh -R remote_port -> local_host:local_port`."""

    def __init__(self, transport, remote_port, local_host, local_port):
        self.transport = transport
        self.local_host, self.local_port = local_host, local_port
        self._alive = True
        self._live = _Live()
        # A per-forward handler, not transport.accept(): the accept queue is
        # shared by the whole transport, so with two remote forwards each one's
        # accept loop could take the other's connection and send it to the wrong
        # local port.
        self.remote_port = transport.request_port_forward(
            "", remote_port, handler=self._on_channel)

    def _on_channel(self, chan, origin, server):
        if not self._alive:
            chan.close()
            return
        threading.Thread(target=self._serve, args=(chan,), daemon=True).start()

    def _serve(self, chan):
        sock = socket.socket()
        try:
            sock.connect((self.local_host, self.local_port))
        except Exception as exc:
            _log.warning("Remote forward %s -> %s:%s: the local service did not "
                         "accept the connection (%s)", self.remote_port,
                         self.local_host, self.local_port, exc)
            sock.close()
            chan.close()
            return
        if not self._live.add(sock, chan):            # closed meanwhile
            sock.close()
            chan.close()
            return
        try:
            _pump(sock, chan)
        finally:
            self._live.discard(sock, chan)

    def close(self):
        """Stop the remote listener and end every connection still relayed."""
        self._alive = False
        try:
            self.transport.cancel_port_forward("", self.remote_port)
        except Exception:
            pass
        self._live.close_all()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self):
        return (f"<RemoteForward remote:{self.remote_port} -> "
                f"{self.local_host}:{self.local_port}>")


def generate_keypair(path: str, bits: int = 3072, passphrase: str = None,
                     comment: str = "turbossh") -> str:
    """
    Generate an RSA key pair (like ssh-keygen). Writes the private key to *path*
    and the public key to *path*.pub. Returns the public-key line.
    """
    import os
    import paramiko
    path = os.path.expanduser(path)
    key = paramiko.RSAKey.generate(bits)
    key.write_private_key_file(path, password=passphrase)
    pub = f"{key.get_name()} {key.get_base64()} {comment}"
    with open(path + ".pub", "w", encoding="utf-8") as fh:
        fh.write(pub + "\n")
    return pub
