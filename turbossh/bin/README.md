# turbossh/bin

The prebuilt GUI executable (`turbossh-gui.exe`) lives here in a published
release, but it is **not committed to the source repository** — it's a ~75 MB
build artifact.

- In a `pip install turbossh`, the exe is already inside the wheel.
- To build it from a source checkout:

  ```bash
  python scripts/build_exe.py --onefile
  cp dist/turbossh-gui.exe turbossh/bin/turbossh-gui.exe
  python -m build --wheel
  ```
