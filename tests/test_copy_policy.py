"""A copy on the remote host is written by the upload policy, but never in a
way that could empty its own source (no PyQt5 needed: runs on every CI job)."""

from __future__ import annotations

import io

import pytest

from turbossh import _transfer


class _Linked:
    """A listing entry of a file with two hard links."""
    st_mode = 0o100644
    st_size = 3
    longname = "-rw-r--r--   2 u u 3 Nov 24 10:00 twin"


class _NoClient:
    """Nothing may be written: the refusal comes before any request."""

    def __getattr__(self, name):
        raise AssertionError(f"the server was asked to {name}")


def test_a_hard_linked_target_is_refused_for_a_copy():
    with pytest.raises(_transfer.NotInPlace, match="hard links"):
        _transfer.put_stream(_NoClient(), io.BytesIO(b"NEW"), 3, "/srv/twin",
                             existing=_Linked(), hard_links_in_place=False)


def test_writing_in_place_can_be_refused():
    with pytest.raises(_transfer.NotInPlace, match="in place"):
        _transfer.put_stream(_NoClient(), io.BytesIO(b"NEW"), 3, "/srv/twin",
                             existing=_Linked(), in_place=False)


def test_a_refusal_is_an_ioerror_callers_already_handle():
    assert issubclass(_transfer.NotInPlace, IOError)
