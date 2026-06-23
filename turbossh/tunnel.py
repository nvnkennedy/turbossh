"""SSH port forwarding (tunnels) over an existing connection's transport.

* LocalForward  — like `ssh -L`  (expose a remote service on a local port)
* RemoteForward — like `ssh -R`  (expose a local service on a remote port)

Each returns a stoppable handle with .close() and context-manager support.
Forwarding runs on daemon threads so it never blocks the caller.
"""

from __future__ import annotations

import select
import socket
import socketserver
import threading


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
    except (OSError, EOFError):
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass


class _LocalHandler(socketserver.BaseRequestHandler):
    def handle(self):
        srv = self.server
        try:
            chan = srv.ssh_transport.open_channel(
                "direct-tcpip", (srv.remote_host, srv.remote_port),
                self.request.getpeername())
        except Exception:
            return
        if chan is not None:
            _pump(self.request, chan)


class _ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class LocalForward:
    """`ssh -L [local_host:]local_port -> remote_host:remote_port`."""

    def __init__(self, transport, remote_host, remote_port,
                 local_port=0, local_host="127.0.0.1"):
        self.server = _ForwardServer((local_host, local_port), _LocalHandler)
        self.server.ssh_transport = transport
        self.server.remote_host = remote_host
        self.server.remote_port = remote_port
        self.local_host, self.local_port = self.server.server_address[:2]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass

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
        self.remote_port = transport.request_port_forward("", remote_port)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while self._alive:
            chan = self.transport.accept(1.0)
            if chan is None:
                continue
            threading.Thread(target=self._serve, args=(chan,), daemon=True).start()

    def _serve(self, chan):
        sock = socket.socket()
        try:
            sock.connect((self.local_host, self.local_port))
        except Exception:
            chan.close()
            return
        _pump(sock, chan)

    def close(self):
        self._alive = False
        try:
            self.transport.cancel_port_forward("", self.remote_port)
        except Exception:
            pass

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
