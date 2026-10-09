"""The opt-in logging binding, run in a fresh pytest process each time."""

from __future__ import annotations

import sys

import pytest

pytest_plugins = ["pytester"]


def run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    cmd = [sys.executable, "-m", "pytest", "-p", "cot.capture.overtake_pytest_logging", *args]
    return pytester.run(*cmd)


def test_failing_test_shows_its_log_per_phase(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import logging
        import pytest

        log = logging.getLogger("app")

        @pytest.fixture
        def thing():
            log.warning("in setup")
            yield
            log.warning("in teardown")

        def test_fails(thing):
            log.warning("in call")
            log.info("too low")
            assert False
        """
    )
    result = run(pytester)
    result.stdout.fnmatch_lines(
        [
            "logging: cot.capture",
            "*- Captured log setup -*",
            "WARNING  app:test_failing_test_shows_its_log_per_phase.py:8 in setup",
            "*- Captured log call -*",
            "WARNING  app:*in call",
            "*- Captured log teardown -*",
            "WARNING  app:*in teardown",
        ]
    )
    result.stdout.no_fnmatch_line("INFO *too low")
    result.stdout.no_fnmatch_line("*live log*")


def test_log_level_from_ini_and_option(pytester: pytest.Pytester) -> None:
    pytester.makeini("[pytest]\nlog_level = INFO\nlog_format = %(levelname)s|%(message)s\n")
    pytester.makepyfile(
        """
        import logging

        def test_fails():
            logging.getLogger("app").info("info")
            logging.getLogger("app").debug("debug")
            assert False
        """
    )
    result = run(pytester)
    result.stdout.fnmatch_lines(["INFO|info"])
    result.stdout.no_fnmatch_line("DEBUG|debug")

    result = run(pytester, "--log-level=DEBUG")
    result.stdout.fnmatch_lines(["INFO|info", "DEBUG|debug"])


def test_options_are_declared_when_pytests_plugin_is_excluded(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        """
        import logging

        def test_fails():
            logging.getLogger("app").debug("debug")
            assert False
        """
    )
    result = run(pytester, "-p", "no:logging", "--log-level=DEBUG")
    result.stdout.fnmatch_lines(["DEBUG    app:*debug"])


def test_non_propagating_logger_is_captured_once(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import logging

        log = logging.getLogger("quiet")
        log.propagate = False

        def test_fails():
            log.warning("before")
            log.propagate = True
            log.warning("after")
            assert False
        """
    )
    result = run(pytester)
    result.stdout.fnmatch_lines(["*- Captured log call -*", "*quiet*before", "*quiet*after"])
    lines = result.stdout.lines
    assert len([line for line in lines if line.startswith("WARNING  quiet:")]) == 2


def test_caplog(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import logging

        log = logging.getLogger("app")

        def test_api(caplog):
            log.warning("one %s", 1)
            assert caplog.messages == ["one 1"]
            assert caplog.record_tuples == [("app", logging.WARNING, "one 1")]
            assert "one 1" in caplog.text
            assert caplog.get_records("call") == caplog.records
            caplog.clear()
            assert caplog.records == []

            log.debug("hidden")
            with caplog.at_level(logging.DEBUG, logger="app"):
                log.debug("shown")
            log.debug("hidden again")
            assert caplog.messages == ["shown"]
            assert log.level == logging.NOTSET

            caplog.set_level(logging.INFO)
            log.info("info")
            assert caplog.messages == ["shown", "info"]

            class NoInfo(logging.Filter):
                def filter(self, record):
                    return record.levelno != logging.INFO

            with caplog.filtering(NoInfo()):
                log.info("filtered")
            assert "filtered" not in caplog.messages

        def test_restored():
            assert logging.getLogger().level == logging.WARNING

        def test_setup_records(caplog_setup):
            pass
        """,
        conftest="""
        import logging
        import pytest

        @pytest.fixture
        def caplog_setup(caplog):
            logging.getLogger("app").warning("from setup")
            yield
            assert [r.getMessage() for r in caplog.get_records("setup")] == ["from setup"]
            assert caplog.get_records("call") == []
        """,
    )
    result = run(pytester, "-p", "no:randomly")
    result.assert_outcomes(passed=3)


def test_live_logging(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import logging

        def test_logs():
            logging.getLogger("app").info("live!")
        """
    )
    result = run(pytester, "--log-cli-level=INFO")
    result.stdout.fnmatch_lines(["*- live log call -*", "INFO     app:*live!", "PASSED*"])

    pytester.makeini("[pytest]\nlog_cli = true\n")
    result = run(pytester)
    result.stdout.no_fnmatch_line("*live!*")  # INFO is below the root's WARNING
    result.stdout.fnmatch_lines(["*::test_logs PASSED*"])


def test_log_file(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import logging

        log = logging.getLogger("quiet")
        log.propagate = False

        def test_logs():
            logging.getLogger("app").info("to file")
            log.warning("non-propagating to file")
        """
    )
    result = run(pytester, "--log-file=logs/run.log", "--log-file-level=INFO")
    result.assert_outcomes(passed=1)
    text = (pytester.path / "logs" / "run.log").read_text()
    assert "to file" in text
    assert "non-propagating to file" in text


def test_log_disable(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import logging

        def test_fails():
            logging.getLogger("noisy").warning("noise")
            logging.getLogger("app").warning("signal")
            assert False
        """
    )
    result = run(pytester, "--log-disable=noisy")
    result.stdout.fnmatch_lines(["*signal"])
    result.stdout.no_fnmatch_line("WARNING  noisy:*")


def test_unknown_level_is_a_usage_error(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("def test_x(): pass")
    result = run(pytester, "--log-level=LOUD")
    result.stderr.fnmatch_lines(["*'LOUD' is not recognized as a logging level name*"])
    assert result.ret == pytest.ExitCode.USAGE_ERROR
