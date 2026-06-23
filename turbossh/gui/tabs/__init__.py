"""Feature tabs for the GUI: one self-contained widget per operation."""

from .command_tab import CommandTab
from .files_tab import FilesTab
from .serial_tab import SerialTab
from .stream_tab import StreamTab

__all__ = ["CommandTab", "FilesTab", "SerialTab", "StreamTab"]
