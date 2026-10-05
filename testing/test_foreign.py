"""Foreign replacement of a slot is tolerated and reported (O4, D5)."""

from __future__ import annotations

import contextlib
import io
import sys

import pytest

from cot.capture import SlotReplacedWarning, capture


def test_writes_to_a_foreign_object_are_not_captured() -> None:
    foreign = io.StringIO()
    with capture() as scope:
        with contextlib.redirect_stdout(foreign):
            print("to foreign")
        print("to scope")
    assert foreign.getvalue() == "to foreign\n"
    assert scope.out == "to scope\n"
    assert scope.diagnostics == []


def test_foreign_object_left_at_exit_is_reported_and_kept() -> None:
    before = sys.stdout
    foreign = io.StringIO()
    try:
        with pytest.warns(SlotReplacedWarning, match="left in place"):
            with capture(name="s") as scope:
                sys.stdout = foreign
        assert sys.stdout is foreign
        [diag] = scope.diagnostics
        assert diag.slot == "stdout"
        assert "owned by 's'" in diag.proxy
    finally:
        sys.stdout = before
