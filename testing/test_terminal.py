"""Terminal streams: never captured, owned and closed by the caller (O9, D7).

Scenarios that need a real terminal or a closed descriptor run in a fresh
interpreter.
"""

from __future__ import annotations

import gc
import io
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from cot.capture import TerminalClosedWarning, TerminalStream, capture, terminal


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
            with terminal("stdout") as term:
                with capture(stdout="fd", stderr=None) as scope:
                    term.write("live\\n")
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
            with terminal("stdout") as term:
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
            with terminal("stdout") as term, capture(stdout="fd", stderr=None):
                seen = (term.isatty(), sys.stdout.isatty())
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
        with terminal("stdout") as term:
            term.write("nowhere\\n")
            term.flush()
            term.write("still nowhere\\n")
        """
    )


def _open_fds() -> int:
    return len(os.listdir(f"/proc/{os.getpid()}/fd"))


def test_each_call_owns_its_descriptor() -> None:
    with terminal("stderr", owner="a") as a, terminal("stderr", owner="b") as b:
        assert a is not b
        assert a.fileno() != b.fileno()
        fd = a.fileno()
    assert a.closed and b.closed
    with pytest.raises(OSError):
        os.fstat(fd)


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_repeated_sessions_leak_no_descriptors() -> None:
    def session() -> None:
        with terminal("stdout", owner="session") as term:
            with capture(stdout="fd", stderr="fd"):
                term.write("")

    session()
    before = _open_fds()
    for _ in range(50):
        session()
    assert _open_fds() == before


def test_use_after_close_warns_and_goes_to_the_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = io.StringIO()
    monkeypatch.setattr(sys, "__stdout__", original)
    term = terminal("stdout", owner="gone")
    term.close()
    with pytest.warns(TerminalClosedWarning, match="owned by 'gone'") as record:
        term.write("late\n")
    assert record[0].filename == __file__
    assert original.getvalue() == "late\n"


def test_unclosed_stream_warns_and_releases_on_collection() -> None:
    term = terminal("stdout", owner="forgotten")
    fd = term.fileno()
    with pytest.warns(ResourceWarning, match="unclosed terminal stdout owned by 'forgotten'"):
        del term
        gc.collect()
    with pytest.raises(OSError):
        os.fstat(fd)


def test_close_records_where() -> None:
    with capture():
        term = terminal("stderr", owner="o")
    term.close()
    assert isinstance(term, TerminalStream)
    assert term.closed_at is not None
    assert term.closed_at.filename == __file__


def test_pdb_uses_terminal_streams_inside_fd_capture(tmp_path: Path) -> None:
    """D9: the debugger gets the terminal through terminal streams, not suspend."""
    out = tmp_path / "out"
    with out.open("w") as f:
        _run(
            """
            import pdb, sys
            from cot.capture import capture, terminal
            with capture(stdout="fd", stderr="fd", stdin=True) as scope:
                print("before")
                with terminal("stdin") as tin, terminal("stdout") as tout:
                    debugger = pdb.Pdb(stdin=tin, stdout=tout)
                    debugger.prompt = "(dbg) "
                    secret = 41 + 1
                    debugger.set_trace()
                    pass
                print("after")
            print(repr(scope.out))
            print("rest:", sys.stdin.read().strip())
            """,
            stdout=f,
            input="p secret\nc\nleft for the process\n",
        )
    text = out.read_text()
    assert "(dbg) 42\n" in text
    assert text.endswith("'before\\nafter\\n'\nrest: left for the process\n")


def test_stdin_reads_lines_and_is_not_writable(tmp_path: Path) -> None:
    result = _run(
        """
        from cot.capture import terminal
        with terminal("stdin") as tin:
            assert tin.readable() and not tin.writable()
            print(repr(tin.readline()), repr(tin.readline(2)), repr(tin.read()))
            try:
                tin.write("x")
            except OSError as exc:
                print(type(exc).__name__)
        """,
        input="one\ntwo\nthree\n",
        capture_output=True,
    )
    assert result.stdout == "'one\\n' 'tw' 'o\\nthree\\n'\nUnsupportedOperation\n"


def test_stdin_after_close_warns_and_reads_the_original(tmp_path: Path) -> None:
    result = _run(
        """
        import warnings
        from cot.capture import terminal
        tin = terminal("stdin", owner="dbg")
        tin.close()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            line = tin.readline()
        print(repr(line), w[0].category.__name__, "'dbg'" in str(w[0].message))
        """,
        input="from original\n",
        capture_output=True,
    )
    assert result.stdout == "'from original\\n' TerminalClosedWarning True\n"
