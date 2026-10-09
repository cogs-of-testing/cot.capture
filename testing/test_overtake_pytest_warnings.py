"""The opt-in warnings binding, run in a fresh pytest process each time."""

from __future__ import annotations

import sys

import pytest

pytest_plugins = ["pytester"]


def run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    cmd = [sys.executable, "-m", "pytest", "-p", "cot.capture.overtake_pytest_warnings", *args]
    return pytester.run(*cmd)


def test_warnings_reach_the_summary_once(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import warnings

        def test_warns():
            warnings.warn("careful", UserWarning)
        """
    )
    result = run(pytester)
    result.stdout.fnmatch_lines(
        [
            "warnings: cot.capture",
            "*= warnings summary =*",
            "test_warnings_reach_the_summary_once.py::test_warns",
            "*UserWarning: careful",
            "*= 1 passed, 1 warning in *",
        ]
    )


def test_deprecation_warnings_show_by_default(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import warnings

        def test_warns():
            warnings.warn("old", DeprecationWarning)
        """
    )
    run(pytester).stdout.fnmatch_lines(["*DeprecationWarning: old", "*= 1 passed, 1 warning in *"])


def test_dash_w_error_fails_the_test(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import warnings

        def test_warns():
            warnings.warn("careful", UserWarning)
        """
    )
    result = run(pytester, "-W", "error::UserWarning")
    result.assert_outcomes(failed=1)


def test_ini_filters_apply_and_marks_win(pytester: pytest.Pytester) -> None:
    pytester.makeini(
        """
        [pytest]
        filterwarnings = error
        """
    )
    pytester.makepyfile(
        """
        import warnings
        import pytest

        def test_fails():
            warnings.warn("careful")

        @pytest.mark.filterwarnings("ignore:care")
        def test_ignored():
            warnings.warn("careful")
        """
    )
    run(pytester).assert_outcomes(failed=1, passed=1)


GARBAGE = """
    import gc

    gc.disable()


    class Noisy:
        def __init__(self):
            self.cycle = self

        def __del__(self):
            raise RuntimeError("from __del__")


    def test_makes_garbage():
        Noisy()


    def test_clean():
        pass
"""


def test_unraisable_exceptions_land_in_the_test_that_made_the_garbage(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(GARBAGE)
    result = run(pytester)
    result.stdout.fnmatch_lines(
        [
            "*= warnings summary =*",
            "*::test_makes_garbage",
            "*PytestUnraisableExceptionWarning: Exception ignored *Noisy.__del__*",
            "*RuntimeError: from __del__",
            "*= 2 passed, 1 warning in *",
        ]
    )
    assert "::test_clean" not in result.stdout.str()


def test_unraisable_exceptions_fail_under_dash_w_error(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(GARBAGE)
    result = run(pytester, "-W", "error::pytest.PytestUnraisableExceptionWarning")
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*ERROR at teardown of test_makes_garbage*"])


def test_thread_exceptions_become_warnings(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import threading

        def test_thread():
            def fail():
                raise KeyError("boom")
            t = threading.Thread(target=fail, name="worker")
            t.start()
            t.join()
        """
    )
    run(pytester).stdout.fnmatch_lines(
        [
            "*PytestUnhandledThreadExceptionWarning: Exception in thread worker",
            "*KeyError: 'boom'",
            "*= 1 passed, 1 warning in *",
        ]
    )


def test_collection_warnings_are_recorded(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import warnings

        warnings.warn("at import", UserWarning)

        def test_ok():
            pass
        """
    )
    run(pytester).stdout.fnmatch_lines(["*UserWarning: at import", "*= 1 passed, 1 warning in *"])


def test_configure_time_warnings_are_issued(pytester: pytest.Pytester) -> None:
    # pytest core drops them while a plugin named "warnings" is blocked
    pytester.makeconftest(
        """
        def pytest_configure(config):
            config.issue_config_time_warning(UserWarning("at configure"), stacklevel=2)
        """
    )
    pytester.makepyfile("def test_ok(): pass")
    run(pytester).stdout.fnmatch_lines(["*UserWarning: at configure", "*1 passed, 1 warning*"])
    result = run(pytester, "-W", "error::UserWarning")
    result.stderr.fnmatch_lines(["*UserWarning: at configure"])
    assert result.ret != 0


def test_filters_apply_between_recordings(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
        import warnings

        def pytest_sessionstart(session):
            warnings.warn("at sessionstart", UserWarning)
        """
    )
    pytester.makepyfile("def test_ok(): pass")
    result = run(pytester, "-W", "error::UserWarning")
    assert result.ret != 0
    assert "UserWarning: at sessionstart" in result.stdout.str() + result.stderr.str()
