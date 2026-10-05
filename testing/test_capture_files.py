"""Capture files shared by the scopes of one set, such as a test's phases (D8)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from cot.capture import BorrowError, capture, capture_files


def test_each_scope_gets_its_own_slice() -> None:
    with capture_files() as files:
        results = []
        for phase in ("setup", "call", "teardown"):
            with capture(stdout="fd", stderr="fd", files=files, name=phase) as scope:
                print(f"{phase} out")
                os.write(2, f"{phase} err\n".encode())
            results.append((scope.out, scope.err))
    assert results == [
        ("setup out\n", "setup err\n"),
        ("call out\n", "call err\n"),
        ("teardown out\n", "teardown err\n"),
    ]


def test_the_same_file_backs_every_scope() -> None:
    with capture_files() as files:
        seen = set()
        for _ in range(3):
            with capture(stdout="fd", stderr=None, files=files):
                st = os.fstat(1)
                seen.add((st.st_dev, st.st_ino))
        assert len(seen) == 1


def test_children_append_to_the_shared_file() -> None:
    with capture_files() as files:
        with capture(stdout="fd", stderr=None, files=files):
            print("first")
        with capture(stdout="fd", stderr=None, files=files) as second:
            subprocess.run([sys.executable, "-c", "print('child')"], check=True)
    assert second.out == "child\n"


def test_nested_scope_cannot_share_a_file_in_use() -> None:
    before = sys.stdout
    with capture_files() as files:
        with capture(stdout="fd", stderr=None, files=files, name="outer") as outer:
            with pytest.raises(BorrowError, match="in use by 'outer'"):
                with capture(stdout="fd", stderr="fd", files=files, name="inner"):
                    pass  # pragma: no cover
            print("still outer")
    assert outer.out == "still outer\n"
    assert sys.stdout is before


def test_refused_scope_leaves_nothing_installed() -> None:
    """stdout borrows first and succeeds, stderr is refused: stdout is undone."""
    fd1 = os.fstat(1).st_ino
    with capture_files() as files:
        with capture(stdout=None, stderr="fd", files=files):
            stdout = sys.stdout
            with pytest.raises(BorrowError):
                with capture(stdout="fd", stderr="fd", files=files):
                    pass  # pragma: no cover
            assert sys.stdout is stdout
            assert os.fstat(1).st_ino == fd1
            assert files.stdout.holder is None


def test_closing_in_use_is_refused_then_allowed() -> None:
    files = capture_files("test_x")
    with capture(stdout="fd", stderr=None, files=files, name="call"):
        with pytest.raises(BorrowError, match="'call' still uses it"):
            files.close()
    files.close()
    assert files.stdout.closed and files.stderr.closed
    with pytest.raises(BorrowError, match="closed"):
        with capture(stdout="fd", stderr=None, files=files):
            pass  # pragma: no cover


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_repeated_sets_leak_no_descriptors() -> None:
    def count() -> int:
        return len(os.listdir("/proc/self/fd"))

    before = count()
    for _ in range(50):
        with capture_files() as files:
            for _phase in range(3):
                with capture(stdout="fd", stderr="fd", files=files):
                    print("x")
    assert count() == before
