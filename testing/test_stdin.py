"""The minimal stdin proxy (D6)."""

from __future__ import annotations

import sys

import pytest

from cot.capture import StdinRefusedError, capture


def test_reading_without_input_is_refused_naming_the_scope() -> None:
    with capture(stdin=True, name="quiet"):
        with pytest.raises(StdinRefusedError, match="owned by 'quiet'"):
            input()


def test_supplied_input_is_served() -> None:
    with capture(stdin="yes\nno\n"):
        assert input() == "yes"
        assert sys.stdin.readline() == "no\n"
        assert sys.stdin.read() == ""


def test_stdin_is_left_alone_by_default() -> None:
    before = sys.stdin
    with capture():
        assert sys.stdin is before
