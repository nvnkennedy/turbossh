"""PyInstaller entry point. Imports the GUI as a package module (so relative
imports resolve) and launches it. Any *startup* crash (e.g. a missing import in
the frozen exe) is caught and shown in a native popup + written to a crash log,
so the exe never fails silently."""

import os
import sys
import traceback


def _fatal(message: str):
    # write a crash log next to the user's other turbossh state
    try:
        d = os.path.join(os.path.expanduser("~"), ".turbossh")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "crash.log"), "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
    except Exception:
        pass
    # native Windows popup — works even if PyQt5/Qt failed to load
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, message[:1800], "TurboSSH GUI failed to start", 0x10)
    except Exception:
        sys.stderr.write(message + "\n")


def run():
    try:
        from turbossh.gui.app import main
        return main()
    except Exception:
        _fatal(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
