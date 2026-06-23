"""
Run the same operation across many hosts in parallel — useful for test labs
and fleet operations where speed matters. Each host gets its own SSHHandler.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional, Sequence

from .config import SSHConfig
from .core import SSHHandler
from .results import OperationResult


class SSHPool:
    """
    >>> pool = SSHPool([cfg1, cfg2, cfg3], max_workers=8)
    >>> results = pool.run("uptime")        # {host: OperationResult}
    >>> pool.push("a.bin", "/tmp/a.bin")
    >>> pool.close()
    """

    def __init__(self, configs: Sequence[SSHConfig], *, max_workers: int = 10,
                 log_callback: Optional[Callable[[str], None]] = None):
        self.handlers = {
            cfg.host: SSHHandler(cfg, log_callback=log_callback, safe=True)
            for cfg in configs
        }
        self.max_workers = max_workers

    def _fanout(self, method: str, *args, **kwargs) -> dict:
        results: dict[str, OperationResult] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futures = {}
            for host, handler in self.handlers.items():
                # ensure each worker is connected first
                def _job(h=handler):
                    if not h.is_connected:
                        c = h.connect()
                        if isinstance(c, OperationResult) and not c.success:
                            return c
                    return getattr(h, method)(*args, **kwargs)
                futures[ex.submit(_job)] = host
            for fut in as_completed(futures):
                host = futures[fut]
                try:
                    results[host] = fut.result()
                except Exception as exc:  # pragma: no cover
                    results[host] = OperationResult(False, method, error=exc)
        return results

    def connect(self) -> dict:
        return self._fanout_connect()

    def _fanout_connect(self) -> dict:
        results = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futures = {ex.submit(h.connect): host
                       for host, h in self.handlers.items()}
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()
        return results

    def run(self, command: str, **kwargs) -> dict:
        return self._fanout("run", command, **kwargs)

    def push(self, local_path: str, remote_path: str, **kwargs) -> dict:
        return self._fanout("push", local_path, remote_path, **kwargs)

    def pull(self, remote_path: str, local_path_template: str, **kwargs) -> dict:
        """
        Pull from every host. ``local_path_template`` may contain ``{host}`` so
        downloads don't collide, e.g. ``"logs/{host}_syslog.txt"``.
        """
        results = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futures = {}
            for host, handler in self.handlers.items():
                local = local_path_template.format(host=host)

                def _job(h=handler, l=local):
                    if not h.is_connected:
                        c = h.connect()
                        if isinstance(c, OperationResult) and not c.success:
                            return c
                    return h.pull(remote_path, l, **kwargs)
                futures[ex.submit(_job)] = host
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()
        return results

    def close(self) -> None:
        for h in self.handlers.values():
            h.disconnect()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
