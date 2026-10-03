"""
PyQt5 GUI for turbossh — a tabbed, multi-session terminal.

Structure
---------
    app.py             - main() entry point (QApplication, crash hook, window)
    main_window.py     - MainWindow: ribbon, session sidebar, tabbed sessions
    session_widgets.py - SSH / serial session tabs (connect on a worker thread)
    vt100.py           - Vt100Terminal: real pyte-backed terminal widget
    sftp_browser.py    - remote file browser (SFTP)
    camera_widget.py   - Local/Remote webcam panel (ffmpeg over SSH)
    logs_tab.py        - live log follower (journalctl / slog2info / …)
    session_dialog.py  - create / edit a saved session
    settings_dialog.py - preferences (theme, fonts, jump host, machines, …)
    theme.py           - colors + Qt stylesheet (dark / light)
    log_panel.py       - colored, capped log dock
    termscreen.py      - the terminal's screen model (no Qt): alternate
                         screen, scrollback, fast text path
    qtutil.py          - worker-thread / teardown helpers
    updater.py         - "Check updates" dialog (logic in turbossh.update)

Launch with `turbossh-gui` or `python -m turbossh.gui`.
"""



def main(*args, **kwargs):
    """Launch the GUI. Imported lazily: importing this package for one of its
    Qt-free modules (sessions, settings, termscreen) must not pull in PyQt5 and
    the whole main window."""
    from .app import main as _main
    return _main(*args, **kwargs)


__all__ = ["main"]
