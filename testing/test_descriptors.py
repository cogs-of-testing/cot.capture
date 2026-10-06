"""Descriptor-level capture: borrowed fds, temporary file targets (O5, O6, D1, D3)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from cot.capture import BorrowError, borrow, capture, protect, unprotect


def test_captures_os_write_and_children() -> None:
    with capture(stdout="fd", stderr="fd") as scope:
        os.write(1, b"raw\n")
        subprocess.run(
            [sys.executable, "-c", "import sys; print('child'); print('cerr', file=sys.stderr)"],
            check=True,
        )
    assert scope.out == "raw\nchild\n"
    assert scope.err == "cerr\n"


def test_python_writes_go_through_the_descriptor_in_order() -> None:
    """D3: one byte stream in write order."""
    with capture(stdout="fd", stderr=None) as scope:
        print("one")
        os.write(1, b"two\n")
        print("three")
    assert scope.out == "one\ntwo\nthree\n"


def test_windows_line_endings_read_as_newlines() -> None:
    """D11: \\r\\n becomes \\n; a lone \\r stays."""
    with capture(stdout="fd", stderr=None) as scope:
        os.write(1, b"a\r\nb\rc\n")
    assert scope.out == "a\nb\rc\n"


def test_fileno_is_the_borrowed_descriptor() -> None:
    with capture(stdout="fd", stderr=None):
        assert sys.stdout.fileno() == 1


def test_descriptor_is_restored() -> None:
    before = os.fstat(1)
    with capture(stdout="fd", stderr=None):
        assert os.fstat(1).st_ino != before.st_ino
    after = os.fstat(1)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)


def test_nested_fd_scopes() -> None:
    with capture(stdout="fd", stderr=None) as outer:
        os.write(1, b"a\n")
        with capture(stdout="fd", stderr=None) as inner:
            os.write(1, b"b\n")
        os.write(1, b"c\n")
    assert (outer.out, inner.out) == ("a\nc\n", "b\n")


def test_out_of_order_give_back_names_both_borrowers() -> None:
    first = borrow(1, "first")
    second = borrow(1, "second")
    try:
        with pytest.raises(BorrowError, match="'first'.*'second'"):
            first.give_back()
    finally:
        second.give_back()
        first.give_back()


def test_protected_descriptor_cannot_be_borrowed() -> None:
    protect(1)
    try:
        with pytest.raises(BorrowError, match="protected"):
            borrow(1, "intruder")
    finally:
        unprotect(1)


@pytest.mark.skipif(sys.version_info < (3, 14), reason="concurrent.interpreters")
def test_subinterpreters_cannot_borrow() -> None:
    """O7: the borrow stack lives in the main interpreter."""
    from concurrent import interpreters  # type: ignore[attr-defined,unused-ignore]

    interp = interpreters.create()
    try:
        interp.exec(
            "from cot.capture import borrow, BorrowError\n"
            "try:\n"
            "    borrow(1, 'sub')\n"
            "except BorrowError as e:\n"
            "    assert 'main interpreter' in str(e)\n"
            "else:\n"
            "    raise AssertionError('borrowed from a subinterpreter')\n"
        )
    finally:
        interp.close()
