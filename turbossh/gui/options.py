"""The GUI's command-line options (no Qt in here).

Shared by ``turbossh-gui`` / ``python -m turbossh.gui`` / the bundled exe, by
``turbossh gui`` (cli.py), and by :mod:`turbossh.embed_client`, which builds the
command line for an application that embeds TurboSSH. One definition, so the
three can never disagree.

Without options it is the desktop app. ``--embed`` and/or ``--control`` make it
a guest of another application (any UI toolkit); EMBEDDING.md has the details.
"""

from __future__ import annotations

import argparse
from typing import List, Optional, Sequence

# what --hide accepts: the parts around the tabs (TurboSSHWidget's options)
PARTS = ("menu_bar", "ribbon", "sidebar", "log_dock", "status_bar", "home")
CONTROLS = ("stdio",)


def window_handle(text: str) -> int:
    """A native window handle: decimal, or hexadecimal with 0x."""
    try:
        value = int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"not a window handle: {text!r} (decimal, or hex like 0x1A2B)") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("a window handle is a positive number")
    return value


def part_list(text: str) -> List[str]:
    parts = [p.strip().replace("-", "_") for p in text.split(",") if p.strip()]
    unknown = [p for p in parts if p not in PARTS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown part(s) {', '.join(unknown)}; choose from {', '.join(PARTS)}")
    return parts


def add_gui_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group(
        "hosting", "Run inside another application (any UI toolkit). The other "
                   "options need --embed or --control.")
    group.add_argument("--embed", metavar="WINDOW", type=window_handle,
                       help="native handle of the window to live in (HWND on "
                            "Windows, X11 window id on Linux); TurboSSH fills it")
    group.add_argument("--control", choices=CONTROLS,
                       help="take commands and send events as JSON lines on "
                            "stdin / stdout")
    group.add_argument("--hide", metavar="PARTS", type=part_list, default=[],
                       help="comma-separated parts not to show: " + ", ".join(PARTS))
    group.add_argument("--theme", metavar="NAME",
                       help="a theme name, or 'inherit' (no TurboSSH styling)")
    group.add_argument("--camera", choices=("on", "off"),
                       help="decide the Camera instead of the user's View > Camera "
                            "choice")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="turbossh-gui", description="The TurboSSH desktop app.")
    add_gui_options(parser)
    return parser


def parse(argv: Optional[Sequence[str]]):
    """Parse the GUI's options. Unknown arguments are left for Qt (-platform …)."""
    parser = build_parser()
    args, rest = parser.parse_known_args(list(argv or ()))
    validate(parser, args)
    args.qt_args = rest
    return args


def validate(parser: argparse.ArgumentParser, args) -> None:
    if not is_hosted(args) and (args.hide or args.theme or args.camera):
        parser.error("--hide, --theme and --camera need --embed or --control")


def is_hosted(args) -> bool:
    return bool(getattr(args, "embed", None) or getattr(args, "control", None))


def to_argv(args) -> List[str]:
    """The options back as a command line (``turbossh gui …`` -> the GUI)."""
    out: List[str] = []
    if getattr(args, "embed", None):
        out += ["--embed", str(args.embed)]
    if getattr(args, "control", None):
        out += ["--control", args.control]
    if getattr(args, "hide", None):
        out += ["--hide", ",".join(args.hide)]
    if getattr(args, "theme", None):
        out += ["--theme", args.theme]
    if getattr(args, "camera", None):
        out += ["--camera", args.camera]
    return out
