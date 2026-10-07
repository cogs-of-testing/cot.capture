"""The opt-in pytest binding, run in a fresh pytest process each time."""

from __future__ import annotations

import sys

import pytest

pytest_plugins = ["pytester"]


def run(pytester: pytest.Pytester, *args: str, stdin: bytes | None = None) -> pytest.RunResult:
    cmd = [sys.executable, "-m", "pytest", "-p", "cot.capture.overtake_pytest", *args]
    if stdin is None:
        return pytester.run(*cmd)
    return pytester.run(*cmd, stdin=stdin)


PHASES = """
    import os
    import pytest

    @pytest.fixture
    def noisy():
        print("setup out")
        yield
        os.write(2, b"teardown fd err\\n")

    def test_fails(noisy):
        print("call out")
        os.write(1, b"call fd out\\n")
        assert False
"""


def test_phases_become_report_sections(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(PHASES)
    result = run(pytester)
    result.stdout.fnmatch_lines(
        [
            "capture: cot.capture (fd level)",
            "*- Captured stdout setup -*",
            "setup out",
            "*- Captured stdout call -*",
            "call out",
            "call fd out",
            "*- Captured stderr teardown -*",
            "teardown fd err",
        ]
    )
    assert result.ret == pytest.ExitCode.TESTS_FAILED


def test_sys_level_leaves_descriptors_alone(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(PHASES)
    result = run(pytester, "--capture=sys")
    result.stdout.fnmatch_lines(["capture: cot.capture (slot level)"])
    out = result.stdout.str()
    # os.write reaches the terminal at once, print waits for the report
    assert out.index("call fd out") < out.index("Captured stdout call")
    result.stdout.fnmatch_lines(["*- Captured stdout call -*", "call out"])


def test_s_captures_nothing(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(PHASES)
    result = run(pytester, "-s")
    result.stdout.fnmatch_lines(["capture: none"])
    result.stdout.no_fnmatch_line("*Captured stdout*")
    result.stdout.fnmatch_lines(["*call out"])


def test_passing_tests_show_nothing(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("def test_ok():\n    print('hidden')\n")
    result = run(pytester)
    result.stdout.no_fnmatch_line("*hidden*")
    assert result.ret == 0


def test_rA_shows_sections_of_passing_tests(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("def test_ok():\n    print('shown')\n")
    result = run(pytester, "-rA")
    result.stdout.fnmatch_lines(["*- Captured stdout call -*", "shown"])


def test_collection_output_goes_to_the_collect_report(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("print('at import')\nraise ImportError('boom')\n")
    result = run(pytester)
    result.stdout.fnmatch_lines(["*- Captured stdout -*", "at import"])
    assert result.ret == pytest.ExitCode.INTERRUPTED


def test_capsys_and_capfd_still_work(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import os

        def test_capsys(capsys):
            print("one")
            assert capsys.readouterr().out == "one\\n"
            print("after")
            assert False

        def test_capfd(capfd):
            os.write(1, b"two\\n")
            assert capfd.readouterr().out == "two\\n"
        """
    )
    result = run(pytester)
    result.assert_outcomes(passed=1, failed=1)
    # output after the fixture's capture ended returns to the phase's scope
    result.stdout.fnmatch_lines(["*- Captured stdout call -*", "after"])


def test_pytests_capture_manager_is_idle(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        def test_manager(request):
            capman = request.config.pluginmanager.get_plugin("capturemanager")
            assert capman is not None
            assert not capman.is_globally_capturing()
        """
    )
    assert run(pytester).ret == 0


def test_kept_stdout_warns_in_the_next_test(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import sys

        kept = []

        def test_keep():
            kept.append(sys.stdout)

        def test_use():
            kept[0].write("late\\n")
        """
    )
    result = run(pytester, "-rA")
    result.stdout.fnmatch_lines(["*ProxyExpiredWarning*test_keep*", "*- Captured stdout call -*", "late"])
    assert result.ret == 0


def test_stdin_is_refused(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("def test_read():\n    input()\n")
    result = run(pytester)
    result.stdout.fnmatch_lines(["*StdinRefusedError*"])


def test_breakpoint_talks_to_the_terminal(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        def test_debug():
            print("captured before")
            value = 21 * 2
            breakpoint()
            print("captured after")
            assert False
        """
    )
    result = run(pytester, stdin=b"p value\nc\n")
    out = result.stdout.str()
    # pdb's prompt and answer reach the terminal, not the capture
    assert "(Pdb) 42" in out.replace("\n", " ") or "42" in out.split("Captured stdout call")[0]
    result.stdout.fnmatch_lines(["*- Captured stdout call -*", "captured before", "captured after"])


def test_live_logging_reaches_the_terminal(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import logging

        def test_log():
            logging.getLogger().warning("live record")
        """
    )
    result = run(pytester, "-o", "log_cli=true")
    result.stdout.fnmatch_lines(["*WARNING*live record*"])
    result.stdout.no_fnmatch_line("*Captured*")
    assert result.ret == 0
