"""Camera panel plumbing and the ffmpeg download / upload — no camera, no
internet: a local HTTP server stands in for the release download."""

from __future__ import annotations

import hashlib
import http.server
import io
import os
import socket
import sys
import threading
import time
import zipfile

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtTest import QTest  # noqa: E402

from turbossh.gui import ffmpeg_tools  # noqa: E402

FAKE_EXE = b"MZ" + os.urandom(40_000)


def wait_until(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(20)
    return predicate()


def _zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe", FAKE_EXE)
        z.writestr("ffmpeg-master-latest-win64-gpl/LICENSE.txt", "x")
    return buf.getvalue()


@pytest.fixture()
def release(tmp_path, monkeypatch):
    """A fake release server + an isolated cache folder. ``release.mode`` picks
    what it serves: "ok", "truncated" or "notzip"."""
    payload = _zip_bytes()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            mode = server.mode
            body = b"<html>blocked by proxy</html>" if mode == "notzip" else payload
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[:len(body) // 2] if mode == "truncated" else body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.mode = "ok"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cache = tmp_path / "cache" / "ffmpeg"
    monkeypatch.setattr(ffmpeg_tools, "_FFMPEG_URL",
                        f"http://127.0.0.1:{server.server_address[1]}/ffmpeg.zip")
    monkeypatch.setattr(ffmpeg_tools, "_cache_dir", lambda: str(cache))
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda name: None)
    monkeypatch.delenv(ffmpeg_tools.PIN_ENV, raising=False)
    server.cache = cache
    yield server
    server.shutdown()
    server.server_close()


def leftovers(cache):
    return sorted(p.name for p in cache.iterdir()) if cache.exists() else []


def test_download_is_atomic_and_recorded(release):
    exe = ffmpeg_tools.ensure_local_ffmpeg()
    assert open(exe, "rb").read() == FAKE_EXE
    assert leftovers(release.cache) == ["ffmpeg.exe", "ffmpeg.exe.sha256"]
    digest, size = open(exe + ".sha256").read().split()
    assert digest == hashlib.sha256(FAKE_EXE).hexdigest() and int(size) == len(FAKE_EXE)
    assert ffmpeg_tools.cached_ffmpeg() == exe


def test_truncated_download_is_rejected_and_leaves_nothing(release):
    release.mode = "truncated"
    with pytest.raises(RuntimeError, match="download"):
        ffmpeg_tools.ensure_local_ffmpeg()
    assert leftovers(release.cache) == []
    assert ffmpeg_tools.cached_ffmpeg() is None


def test_a_page_that_is_not_a_zip_is_rejected(release):
    release.mode = "notzip"
    with pytest.raises(RuntimeError, match="zip"):
        ffmpeg_tools.ensure_local_ffmpeg()
    assert leftovers(release.cache) == []


def test_sha256_pin_mismatch_is_rejected(release, monkeypatch):
    monkeypatch.setenv(ffmpeg_tools.PIN_ENV, "0" * 64)
    with pytest.raises(RuntimeError, match="pinned"):
        ffmpeg_tools.ensure_local_ffmpeg()
    assert leftovers(release.cache) == []
    monkeypatch.setenv(ffmpeg_tools.PIN_ENV, hashlib.sha256(FAKE_EXE).hexdigest())
    assert os.path.exists(ffmpeg_tools.ensure_local_ffmpeg())


def test_a_cache_that_no_longer_matches_its_record_is_not_reused(release):
    exe = ffmpeg_tools.ensure_local_ffmpeg()
    with open(exe, "ab") as fh:              # e.g. a half-finished overwrite
        fh.write(b"junk")
    assert ffmpeg_tools.cached_ffmpeg() is None


def test_cancel_stops_a_download(release):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RuntimeError, match="cancelled"):
        ffmpeg_tools.ensure_local_ffmpeg(cancel=cancel)
    assert leftovers(release.cache) == []


def test_remote_ffmpeg_upload_is_verified_by_size(ssh, ssh_server, tmp_path):
    local = tmp_path / "ffmpeg.exe"
    local.write_bytes(FAKE_EXE)
    said = []
    remote = ffmpeg_tools.ensure_remote_ffmpeg(ssh, str(local), said.append,
                                               remote_dir="/cam")
    assert remote == "/cam/ffmpeg.exe"
    assert (ssh_server.root / "cam" / "ffmpeg.exe").read_bytes() == FAKE_EXE
    assert not (ssh_server.root / "cam" / "ffmpeg.exe.part").exists()
    assert len(said) == 1

    ffmpeg_tools.ensure_remote_ffmpeg(ssh, str(local), said.append, remote_dir="/cam")
    assert len(said) == 1, "a complete remote copy was uploaded again"

    # an interrupted upload: same name, wrong size -> replaced
    (ssh_server.root / "cam" / "ffmpeg.exe").write_bytes(FAKE_EXE[:1000])
    ffmpeg_tools.ensure_remote_ffmpeg(ssh, str(local), said.append, remote_dir="/cam")
    assert len(said) == 2
    assert (ssh_server.root / "cam" / "ffmpeg.exe").read_bytes() == FAKE_EXE


# ------------------------------------------------------------------ readers
class FakeChannel:
    def __init__(self, script):
        self.script = list(script)
        self.closed = False
        self.eof_received = False

    def recv(self, n):
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def test_channel_reader_treats_a_timeout_as_idle_not_as_the_end():
    from turbossh.gui.camera_widget import _channel_reader
    chan = FakeChannel([b"abc", socket.timeout(), b"def", OSError("reset")])
    read = _channel_reader(chan, b"PRE")
    assert read(10) == b"PRE"                # the verification bytes come first
    assert read(10) == b"abc"
    assert read(10) == b""                   # a pause, NOT the end of the stream
    assert read(10) == b"def"
    assert read(10) is None                  # a real error ends it
    chan.closed = True
    assert read(10) is None


def test_frame_reader_splits_frames_and_reports_the_end(qapp):
    from turbossh.gui.camera_widget import _FrameReader
    frame = b"\xff\xd8" + b"payload" + b"\xff\xd9"
    stream = b"junk" + frame + frame[:5]
    chunks = [stream, frame[5:], None]
    recorded = io.BytesIO()
    reader = _FrameReader(lambda n: chunks.pop(0))
    reader.set_recorder(recorded)
    reader.run()                             # inline: no thread needed
    assert recorded.getvalue() == frame * 2  # whole frames only, across chunks
    assert reader.ended is True

    stopped = _FrameReader(lambda n: b"")
    stopped.stop()
    stopped.run()
    assert stopped.ended is False            # stop() is not "the stream ended"


def test_process_reader_ends_when_the_process_exits():
    import subprocess
    from turbossh.gui.camera_widget import _process_reader
    proc = subprocess.Popen([sys.executable, "-c", "print('frame-bytes')"],
                            stdout=subprocess.PIPE, bufsize=0)
    read = _process_reader(proc)
    data, deadline = b"", time.monotonic() + 15
    while time.monotonic() < deadline:
        chunk = read(4096)
        if chunk is None:
            break
        data += chunk
    else:
        pytest.fail("the reader never reported the end of the stream")
    assert b"frame-bytes" in data
    proc.stdout.close()


# ------------------------------------------------------------------ panel
@pytest.fixture()
def panel(qapp, monkeypatch):
    from turbossh.gui import camera_widget
    monkeypatch.setattr(ffmpeg_tools, "ensure_local_ffmpeg",
                        lambda log=None, cancel=None: sys.executable)
    monkeypatch.setattr(ffmpeg_tools, "list_local_cameras", lambda ff: ["Cam A"])
    widget = camera_widget.CameraPanel()
    widget.show()
    assert wait_until(lambda: widget.camera.count() == 1)
    yield widget
    widget.close_session()
    widget.deleteLater()
    QTest.qWait(50)


def test_panel_lists_cameras_and_closes_cleanly_twice(panel):
    assert panel.camera.itemText(0) == "Cam A"
    panel.close_session()
    panel.close_session()


def test_switching_source_during_a_scan_does_not_crash_or_mix_results(panel):
    for index in (1, 0, 1, 0):               # remote, local, remote, local
        panel.source.setCurrentIndex(index)
    assert wait_until(lambda: panel.camera.count() == 1)
    assert panel.source.currentData() == "local"
    assert panel.camera.itemText(0) == "Cam A"


def test_local_camera_that_exits_is_reported_not_left_on_viewing(panel, monkeypatch):
    script = "import sys; sys.stderr.write('Could not run graph: camera busy'); sys.exit(1)"
    monkeypatch.setattr(ffmpeg_tools, "local_capture_args",
                        lambda *a, **k: [sys.executable, "-c", script])
    panel._start_local("Cam A", 640, 480, 15)
    assert wait_until(lambda: panel.status.text() == "The camera stream ended")
    assert "camera busy" in panel.view.text()
    assert panel.reader is None and panel.start_btn.text() == "▶ Start"
