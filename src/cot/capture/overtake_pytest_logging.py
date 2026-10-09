"""Opt-in: capture logging with cot.capture instead of pytest.

Enable it per project with::

    [pytest]
    addopts = -p cot.capture.overtake_pytest_logging

It takes over from pytest's ``logging`` plugin, which it blocks, and keeps
what that plugin offers (design docs/design/logging.md, "The pytest
binding"):

- every test runs inside one log scope, split at the end of setup, call and
  teardown (L7); each phase's records become its "Captured log" report
  section, and ``caplog`` sees them with its usual API;
- the ``log_*`` ini keys, the ``--log-*`` options and ``--log-disable`` are
  declared through cot.config.ingest, which adopts the ones pytest already
  declared;
- live logging (``log_cli``) and ``log_file`` are the binding's own handlers
  on the scopes (L8), so they also see non-propagating loggers.

What differs from pytest:

- records are kept, not text (LD2): the report section and ``caplog.text``
  are formatted when they are read, and ``caplog.handler`` keeps no stream;
- logger levels are only lowered for the scope and put back afterwards; a
  level changed under a scope is reported with ``LevelChangedWarning`` (L4);
- ``--log-disable`` is undone when pytest unconfigures.

This module imports pytest; the rest of cot.capture never does.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator, Iterator
from contextlib import ExitStack, contextmanager
from typing import Annotated, Literal

import pytest
from _pytest.logging import (
    ColoredLevelFormatter,
    DatetimeFormatter,
    PercentStyleMultiline,
    _LiveLoggingStreamHandler,
    _remove_ansi_escape_sequences,
)

from cot.config import ConfigPart, from_parent, help, named, no_cli
from cot.config.pytest_binding import add_config, get_config, manager_for

from ._logs import DiscardPolicy, LogScope

DEFAULT_LOG_FORMAT = "%(levelname)-8s %(name)s:%(filename)s:%(lineno)d %(message)s"
DEFAULT_LOG_DATE_FORMAT = "%H:%M:%S"

When = Literal["setup", "call", "teardown"]

# The session scope only feeds the live and file handlers; it keeps nothing.
_KEEP_NOTHING = logging.CRITICAL + 1


class LogOutputConfig(ConfigPart):
    """Settings shared by every log output; nested outputs fall back to them."""

    level: Annotated[str | None, from_parent, help("level of messages to catch")] = None
    format: Annotated[str, from_parent, help("log format")] = DEFAULT_LOG_FORMAT
    date_format: Annotated[str, from_parent, help("log date format")] = (
        DEFAULT_LOG_DATE_FORMAT
    )


class LogCliConfig(LogOutputConfig):
    """Live logging to the terminal."""

    enabled: Annotated[bool, named("log_cli"), no_cli, help("enable live logs")] = False


class LogFileConfig(LogOutputConfig):
    """Logging to a file."""

    path: Annotated[str | None, named("log_file"), help("path to log file")] = None
    mode: Annotated[str, named("log_file_mode"), help("log file open mode")] = "w"


class LoggingConfig(LogOutputConfig, ConfigPart, prefix="pytest", name_prefix="log"):
    """pytest's logging options, by their pytest names."""

    auto_indent: Annotated[str | None, help("auto-indent multiline messages")] = None
    logger_disable: Annotated[
        list[str], named("log_disable"), help("disable a logger by name")
    ] = []

    cli: LogCliConfig
    file: LogFileConfig


def pytest_addoption(parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager) -> None:
    # Called as this plugin is registered from -p. pytest's logging plugin was
    # imported before, so its options exist: they are adopted, not redeclared.
    pluginmanager.set_blocked("logging")
    add_config(parser, LoggingConfig, adopt=True)


# after terminalreporter and capturemanager, as pytest's plugin does
@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(LoggingBinding(config), "cot-capture-logging")


def pytest_report_header() -> str:
    return "logging: cot.capture"


class CaplogHandler(logging.Handler):
    """Keeps the records ``caplog`` sees; formats only when read (LD2)."""

    def __init__(self, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def reset(self) -> None:
        self.records = []

    def clear(self) -> None:
        self.records.clear()

    def text(self) -> str:
        return "".join(self.format(r) + "\n" for r in self.records)


class _NullLive(logging.NullHandler):
    def reset(self) -> None:
        pass

    def set_when(self, when: str | None) -> None:
        pass


class _FileHandler(logging.FileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        pass  # surfaces in the report section, as in pytest


class _Test:
    """The scope of one test, its caplog handler and its records per phase."""

    def __init__(self, scope: LogScope, caplog: CaplogHandler) -> None:
        self.scope = scope
        self.caplog = caplog
        self.records: dict[str, list[logging.LogRecord]] = {}


_TEST = pytest.StashKey[_Test]()


class LoggingBinding:
    """The per-run part: formatters, the live and file handlers, the scopes.

    ``formatter`` and ``log_level`` carry the names pytest's ``LoggingPlugin``
    uses, for the replacement object cot.pytest registers as
    ``logging-plugin``.
    """

    def __init__(self, config: pytest.Config) -> None:
        self._config = config
        self.options = options = get_config(config, LoggingConfig)
        self.formatter = self._formatter(options.format, options.date_format)
        self.log_level = _parse_level(options.level, "log_level")

        self.log_file_level = _parse_level(options.file.level, "log_file_level")
        self.log_file_handler: logging.Handler | None = None
        if options.file.path:
            path = os.path.abspath(options.file.path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.log_file_handler = _FileHandler(
                path, mode=options.file.mode or "w", encoding="UTF-8"
            )
            self.log_file_handler.setFormatter(
                DatetimeFormatter(options.file.format, options.file.date_format)
            )
            if self.log_file_level is not None:
                self.log_file_handler.setLevel(self.log_file_level)

        self.log_cli_level = _parse_level(options.cli.level, "log_cli_level")
        self.log_cli_handler: _LiveLoggingStreamHandler | _NullLive = _NullLive()
        if self._live_enabled():
            reporter = config.pluginmanager.get_plugin("terminalreporter")
            assert reporter is not None  # checked by _live_enabled()
            capman = config.pluginmanager.get_plugin("capturemanager")
            self.log_cli_handler = _LiveLoggingStreamHandler(reporter, capman)
            self.log_cli_handler.setFormatter(
                self._formatter(options.cli.format, options.cli.date_format)
            )
            if self.log_cli_level is not None:
                self.log_cli_handler.setLevel(self.log_cli_level)

        self._disabled: list[logging.Logger] = []
        for name in options.logger_disable:
            logger = logging.getLogger(name)
            if not logger.disabled:
                logger.disabled = True
                self._disabled.append(logger)

        self._session = ExitStack()

    def _live_enabled(self) -> bool:
        if self._config.pluginmanager.get_plugin("terminalreporter") is None:
            return False  # disabled, e.g. on a pytest-xdist worker
        if self.options.cli.enabled:
            return True
        # as in pytest, a log_cli_level from the command line switches it on
        layers = manager_for(self._config).layers(LoggingConfig, "cli.level")
        return any(layer.origin.kind == "cli" for layer in layers)

    def _formatter(self, fmt: str, datefmt: str) -> logging.Formatter:
        color = getattr(self._config.option, "color", "no")
        formatter: logging.Formatter
        if color != "no" and ColoredLevelFormatter.LEVELNAME_FMT_REGEX.search(fmt):
            formatter = ColoredLevelFormatter(
                self._config.get_terminal_writer(), fmt, datefmt
            )
        else:
            formatter = DatetimeFormatter(fmt, datefmt)
        formatter._style = PercentStyleMultiline(
            formatter._style._fmt, auto_indent=self.options.auto_indent
        )
        return formatter

    def _outputs(self) -> list[logging.Handler]:
        outputs: list[logging.Handler] = []
        if not isinstance(self.log_cli_handler, _NullLive):
            outputs.append(self.log_cli_handler)
        if self.log_file_handler is not None:
            outputs.append(self.log_file_handler)
        return outputs

    def _levels(self, *, test: bool) -> dict[str, int]:
        """The root level for a scope: lowered to what is asked, never raised (LD1)."""
        asked = []
        if not isinstance(self.log_cli_handler, _NullLive) and self.log_cli_level is not None:
            asked.append(self.log_cli_level)
        if self.log_file_handler is not None and self.log_file_level is not None:
            asked.append(self.log_file_level)
        if test and self.log_level is not None:
            asked.append(self.log_level)
        current = logging.getLogger().level
        if not asked or min(asked) >= current:
            return {}
        return {"": min(asked)}

    # -- session ---------------------------------------------------------

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionstart(self) -> Generator[None]:
        self.log_cli_handler.set_when("sessionstart")
        self._session.enter_context(
            LogScope(
                levels=self._levels(test=False),
                threshold=_KEEP_NOTHING,
                handlers=self._outputs(),
                name="pytest session",
                installed="pytest_sessionstart",
                closed="pytest_sessionfinish",
            )
        )
        return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection(self) -> Generator[None]:
        self.log_cli_handler.set_when("collection")
        return (yield)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtestloop(self, session: pytest.Session) -> Generator[None, object, object]:
        if (
            not session.config.option.collectonly
            and not isinstance(self.log_cli_handler, _NullLive)
            and self._config.get_verbosity() < 1
        ):
            # the verbose flag avoids messy progress output, as in pytest
            self._config.option.verbose = 1
        return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self) -> Generator[None]:
        self.log_cli_handler.set_when("sessionfinish")
        try:
            return (yield)
        finally:
            self._session.close()

    def pytest_unconfigure(self) -> None:
        self._session.close()
        if self.log_file_handler is not None:
            self.log_file_handler.close()
        for logger in self._disabled:
            logger.disabled = False

    # -- tests -----------------------------------------------------------

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item) -> Generator[None, object, object]:
        caplog = CaplogHandler()
        caplog.setFormatter(self.formatter)
        if self.log_level is not None:
            caplog.setLevel(self.log_level)
        scope = LogScope(
            levels=self._levels(test=True),
            threshold=self.log_level or logging.NOTSET,
            handlers=[*self._outputs(), caplog],
            name=item.nodeid,
            installed=f"start of {item.nodeid}",
            closed=f"end of {item.nodeid}",
        )
        item.stash[_TEST] = _Test(scope, caplog)
        try:
            with scope:
                return (yield)
        finally:
            del item.stash[_TEST]

    def pytest_runtest_logstart(self) -> None:
        self.log_cli_handler.reset()
        self.log_cli_handler.set_when("start")

    def pytest_runtest_logreport(self) -> None:
        self.log_cli_handler.set_when("logreport")

    def pytest_runtest_logfinish(self) -> None:
        self.log_cli_handler.set_when("finish")

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_setup(self, item: pytest.Item) -> Generator[None]:
        with self._phase(item, "setup"):
            return (yield)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None]:
        with self._phase(item, "call"):
            return (yield)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(self, item: pytest.Item) -> Generator[None]:
        with self._phase(item, "teardown"):
            return (yield)

    @contextmanager
    def _phase(self, item: pytest.Item, when: When) -> Iterator[None]:
        self.log_cli_handler.set_when(when)
        test = item.stash.get(_TEST, None)
        if test is None:  # a phase run outside pytest_runtest_protocol
            yield
            return
        test.scope.take()  # what came between phases has no section
        test.caplog.reset()
        test.records[when] = test.caplog.records
        failed = True
        try:
            yield
            failed = False
        finally:
            records = test.scope.take(when=when, failed=failed, nodeid=item.nodeid)
            text = "".join(self.formatter.format(r) + "\n" for r in records)
            item.add_report_section(when, "log", text.strip())


def _parse_level(value: str | None, setting: str) -> int | None:
    if not value:
        return None
    name = value.upper()
    try:
        return int(getattr(logging, name, name))
    except ValueError:
        raise pytest.UsageError(
            f"'{name}' is not recognized as a logging level name for "
            f"'{setting}'. Please consider passing the logging level num instead."
        ) from None


class LogCaptureFixture:
    """``caplog``, with the API of pytest's ``LogCaptureFixture``.

    Levels set with :meth:`set_level` are put back at the end of the test,
    and those set with :meth:`at_level` at the end of the block (L4).
    """

    def __init__(self, item: pytest.Item) -> None:
        self._item = item
        self._initial_handler_level: int | None = None
        self._initial_logger_levels: dict[str | None, int] = {}
        self._initial_disabled_logging_level: int | None = None

    @property
    def _test(self) -> _Test:
        return self._item.stash[_TEST]

    @property
    def handler(self) -> CaplogHandler:
        return self._test.caplog

    def get_records(self, when: When) -> list[logging.LogRecord]:
        return self._test.records.get(when, [])

    @property
    def text(self) -> str:
        return _remove_ansi_escape_sequences(self.handler.text())

    @property
    def records(self) -> list[logging.LogRecord]:
        return self.handler.records

    @property
    def record_tuples(self) -> list[tuple[str, int, str]]:
        return [(r.name, r.levelno, r.getMessage()) for r in self.records]

    @property
    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]

    def clear(self) -> None:
        self.handler.clear()

    def set_level(self, level: int | str, logger: str | None = None) -> None:
        logger_obj = logging.getLogger(logger)
        self._initial_logger_levels.setdefault(logger, logger_obj.level)
        logger_obj.setLevel(level)
        if self._initial_handler_level is None:
            self._initial_handler_level = self.handler.level
        self.handler.setLevel(level)
        disabled = _force_enable_logging(level, logger_obj)
        if self._initial_disabled_logging_level is None:
            self._initial_disabled_logging_level = disabled

    @contextmanager
    def at_level(self, level: int | str, logger: str | None = None) -> Iterator[None]:
        logger_obj = logging.getLogger(logger)
        orig_level = logger_obj.level
        logger_obj.setLevel(level)
        handler_orig_level = self.handler.level
        self.handler.setLevel(level)
        disabled = _force_enable_logging(level, logger_obj)
        try:
            yield
        finally:
            logger_obj.setLevel(orig_level)
            self.handler.setLevel(handler_orig_level)
            logging.disable(disabled)

    @contextmanager
    def filtering(self, filter_: logging.Filter) -> Iterator[None]:
        self.handler.addFilter(filter_)
        try:
            yield
        finally:
            self.handler.removeFilter(filter_)

    def _finalize(self) -> None:
        if self._initial_handler_level is not None and _TEST in self._item.stash:
            self.handler.setLevel(self._initial_handler_level)
        for name, level in self._initial_logger_levels.items():
            logging.getLogger(name).setLevel(level)
        if self._initial_disabled_logging_level is not None:
            logging.disable(self._initial_disabled_logging_level)
            self._initial_disabled_logging_level = None


def _force_enable_logging(level: int | str, logger: logging.Logger) -> int:
    """Lift ``logging.disable`` for ``level``; return what it was."""
    original: int = logger.manager.disable
    numeric = logging.getLevelName(level) if isinstance(level, str) else level
    if not isinstance(numeric, int):
        logging.disable(logging.NOTSET)
    elif not logger.isEnabledFor(numeric):
        logging.disable(max(numeric - 10, logging.NOTSET))
    return original


@pytest.fixture
def caplog(request: pytest.FixtureRequest) -> Iterator[LogCaptureFixture]:
    """Access and control log capturing, as pytest's ``caplog``."""
    item = request.node
    assert isinstance(item, pytest.Item)
    result = LogCaptureFixture(item)
    yield result
    result._finalize()
