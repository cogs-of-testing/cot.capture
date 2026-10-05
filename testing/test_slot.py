"""Slot-level capture: the proxy in sys.stdout / sys.stderr."""

from __future__ import annotations

import io
import sys

import pytest

from cot.capture import StreamProxy, capture


def test_captures_print_and_restores_slots() -> None:
    before_out, before_err = sys.stdout, sys.stderr
    with capture() as scope:
        assert isinstance(sys.stdout, StreamProxy)
        print("hello")
        print("oops", file=sys.stderr)
    assert (scope.out, scope.err) == ("hello\n", "oops\n")
    assert (sys.stdout, sys.stderr) == (before_out, before_err)


def test_scopes_nest() -> None:
    with capture(name="outer") as outer:
        print("a")
        with capture(name="inner") as inner:
            print("b")
        print("c")
    assert outer.out == "a\nc\n"
    assert inner.out == "b\n"


def test_none_leaves_a_stream_alone() -> None:
    before = sys.stderr
    with capture(stderr=None):
        assert sys.stderr is before


def test_encoding_comes_from_the_replaced_stream() -> None:
    replaced = sys.stdout
    with capture():
        assert sys.stdout.encoding == (getattr(replaced, "encoding", None) or "utf-8")


def test_proxy_is_not_a_tty() -> None:
    with capture():
        assert sys.stdout.isatty() is False


def test_fileno_raises_at_slot_level() -> None:
    """D4: no descriptor is created behind the user's back."""
    with capture():
        with pytest.raises(io.UnsupportedOperation, match="slot-level"):
            sys.stdout.fileno()


def test_code_under_test_cannot_close_the_proxy() -> None:
    with capture() as scope:
        sys.stdout.close()
        print("still open")
    assert scope.out == "still open\n"
