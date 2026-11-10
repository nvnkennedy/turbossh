"""
The whole TurboSSH GUI inside a Tkinter application — no Qt in this program.

    pip install "turbossh[gui]"        # or just turbossh, on Windows (bundled exe)
    python examples/embed_host_tk.py

TurboSSH runs as a process of its own and lives in the frame below the toolbar
(``TurboSSHProcess.launch(parent=frame.winfo_id())``). The same few lines work
for wxPython (``panel.GetHandle()``), PySide6 / PyQt6 (``int(widget.winId())``)
or any other toolkit that can give you a native window handle. EMBEDDING.md has
the protocol for hosts in other languages.
"""

import tkinter as tk

from turbossh.embed_client import TurboSSHError, TurboSSHProcess


class HostApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("My test bench (Tk)")
        root.geometry("1200x780")

        bar = tk.Frame(root, padx=6, pady=4)
        bar.pack(fill="x")
        self.host = tk.Entry(bar, width=18)
        self.host.insert(0, "10.0.0.5")
        self.user = tk.Entry(bar, width=10)
        self.user.insert(0, "root")
        self.password = tk.Entry(bar, width=12, show="•")
        for label, field in (("Host", self.host), ("User", self.user),
                             ("Password", self.password)):
            tk.Label(bar, text=label).pack(side="left")
            field.pack(side="left", padx=(2, 8))
        tk.Button(bar, text="Connect", command=self.connect).pack(side="left")
        tk.Button(bar, text="Local terminal",
                  command=lambda: self.gui.open_local_terminal()).pack(side="left", padx=4)
        self.status = tk.Label(bar, text="starting TurboSSH…", fg="#555")
        self.status.pack(side="right")

        # Tk takes the Windows keyboard focus only when its window is activated;
        # after TurboSSH had the keyboard, a click on a Tk widget must ask for it
        # back (Win32, WinForms, WPF and Qt controls do this by themselves).
        root.bind_all("<Button-1>", lambda e: e.widget.focus_force(), add="+")

        self.frame = tk.Frame(root, bg="#202020")      # TurboSSH fills this frame
        self.frame.pack(fill="both", expand=True)
        root.update()                                  # the frame needs its handle now

        self.gui = TurboSSHProcess.launch(parent=self.frame.winfo_id(),
                                          hide=["log_dock"])
        self.status.config(text=f"TurboSSH {self.gui.version} ready")
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.pump()

    def pump(self):
        """Events arrive on a background thread; Tk wants them on its own."""
        for event in self.gui.poll_events():
            if event["event"] in ("session_connected", "session_failed",
                                  "session_closed", "session_opened"):
                self.status.config(text=f"{event['event']} {event.get('session', '')} "
                                        f"{event.get('message', '')}".strip())
            if event["event"] == "exited":
                self.root.destroy()
                return
        self.root.after(50, self.pump)

    def connect(self):
        try:
            session = self.gui.connect_ssh(self.host.get().strip(),
                                           username=self.user.get().strip() or None,
                                           password=self.password.get() or None)
        except TurboSSHError as exc:
            self.status.config(text=str(exc))
            return
        self.status.config(text=f"connecting {session}…")

    def quit(self):
        self.gui.close()            # closes every session, releases remote ports
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    HostApp(root)
    root.mainloop()
