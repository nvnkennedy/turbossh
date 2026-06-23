"""
Modular PyQt5 GUI for turbossh.

Structure
---------
    theme.py             - colors + Qt stylesheet (automotive dark theme)
    worker.py            - Worker(QThread): owns the SSHHandler, runs jobs
    log_panel.py         - LogPanel: colored, capped log view + save/clear
    connection_panel.py  - ConnectionPanel: target + jump-host fields, connect
    tabs/                - one widget per feature (command, files, serial, stream)
    main_window.py       - MainWindow: assembles everything
    app.py               - main() entry point

Launch with `turbossh-gui` or `python -m turbossh.gui`.
"""

from .app import main

__all__ = ["main"]
