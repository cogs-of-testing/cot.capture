"""Terminal streams: never captured, never closed (O9, D7).

Each scenario runs in a fresh interpreter, since a terminal stream is
created once per interpreter.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from cot.capture import TerminalStream, capture, terminal


def _run(script: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # type: ignore[call-overload,no-any-return]
        [sys.executable, "-c", textwrap.dedent(script)],
        text=True,
        check=True,
        **kwargs,
    )


def test_writes_reach_the_terminal_through_fd_capture(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with out.open("w") as f:
        _run(
            """
            from cot.capture import capture, terminal
            with capture(stdout="fd", stderr=None) as scope:
                terminal("stdout").write("live\\n")
                print("captured")
            print(repr(scope.out))
            """,
            stdout=f,
        )
    assert out.read_text() == "live\n'captured\\n'\n"


def test_obtained_before_capture_stays_valid(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with out.open("w") as f:
        _run(
            """
            from cot.capture import capture, terminal
            term = terminal("stdout")
            with capture(stdout="fd", stderr=None):
                with capture(stdout="slot", stderr=None):
                    term.write("a\\n")
                term.write("b\\n")
            term.write("c\\n")
            """,
            stdout=f,
        )
    assert out.read_text() == "a\nb\nc\n"


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pty")
def test_sees_the_real_terminal(tmp_path: Path) -> None:
    result = tmp_path / "result"
    controller, terminal_fd = os.openpty()
    try:
        _run(
            f"""
            import sys
            from cot.capture import capture, terminal
            with capture(stdout="fd", stderr=None):
                seen = (terminal("stdout").isatty(), sys.stdout.isatty())
            open({str(result)!r}, "w").write(repr(seen))
            """,
            stdout=terminal_fd,
        )
    finally:
        os.close(terminal_fd)
        os.close(controller)
    assert result.read_text() == "(True, False)"


def test_never_fails_without_a_target() -> None:
    _run(
        """
        import os, sys
        os.close(1)
        from cot.capture import terminal
        term = terminal("stdout")
        term.write("nowhere\\n")
        term.flush()
        term.write("still nowhere\\n")
        """
    )


def test_same_object_and_close_does_nothing() -> None:
    with capture():
        first = terminal("stderr")
    first.close()
    assert terminal("stderr") is first
    assert isinstance(first, TerminalStream)
    assert not first.closed
