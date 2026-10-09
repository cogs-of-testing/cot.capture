"""Opt-in: record warnings with cot.capture instead of pytest.

Enable it per project with::

    [pytest]
    addopts = -p cot.capture.overtake_pytest_warnings

It takes over from pytest's ``warnings``, ``unraisableexception`` and
``threadexception`` plugins, which it unregisters, and keeps what they report
(design docs/design/warnings.md, "The pytest binding"):

- every test runs inside one recording, split at the end of setup, call and
  teardown; conftest loading, collection, session finish and the terminal
  summary get recordings of their own;
- the filters are pytest's: the "show DeprecationWarning" default when no
  ``-W`` was given, ini ``filterwarnings``, ``-W``, then
  ``@pytest.mark.filterwarnings``, the later winning;
- each recorded warning reaches ``pytest_warning_recorded`` as before, so the
  warnings summary, pytest-xdist and pytest-reportlog keep working;
- unraisable and thread exceptions become
  ``PytestUnraisableExceptionWarning`` and
  ``PytestUnhandledThreadExceptionWarning`` in the phase that saw them, so
  ``-W error`` fails that phase.

What differs from pytest: the garbage collector runs at the end of every
test, so a finalizer's error is reported in the test that made the garbage
rather than in a later one (WD3; grouping it is issue #11).

``recwarn`` and ``pytest.warns`` are left as they are. The ``warnings``
plugin is unregistered rather than blocked, because pytest core drops
``Config.issue_config_time_warning`` while that name is blocked.

This module imports pytest; the rest of cot.capture never does.
"""

from __future__ import annotations

import sys
import warnings
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Literal

import pytest
from _pytest.config import parse_warning_filter

from ._records import (
    GCPolicy,
    Recording,
    ThreadExceptionRecord,
    UnraisableRecord,
    WarningFilter,
    WarningRecord,
    record,
)

if sys.version_info < (3, 11):
    from exceptiongroup import ExceptionGroup

_REPLACED = ("warnings", "unraisableexception", "threadexception")

# PyPy can resurrect objects in __del__ and needs several passes (pytest #14441)
GC = GCPolicy(
    passes=5 if sys.implementation.name == "pypy" else 1, on_take=False, on_end=False
)

When = Literal["config", "collect", "runtest"]


def pytest_addoption(parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager) -> None:
    # Called as this plugin is registered from -p, before any hook of the
    # plugins it replaces has run.
    for name in _REPLACED:
        if name == "warnings":
            # Unregistered, not blocked: pytest core drops configure-time
            # warnings (Config.issue_config_time_warning) while a plugin
            # named "warnings" is blocked.
            plugin = pluginmanager.get_plugin(name)
            if plugin is not None:
                pluginmanager.unregister(plugin)
        else:
            pluginmanager.set_blocked(name)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> Generator[None]:
    with _recording(early_config, early_config.hook, "config", None, "loading conftests"):
        return (yield)


def pytest_configure(config: pytest.Config) -> None:
    # As pytest's plugin: the filters apply from configure to unconfigure, so
    # -W error also covers what no recording below wraps (sessionstart, ...).
    filters_only = record(
        warnings=False,
        unraisable=False,
        thread_exceptions=False,
        filters=_filters(config, None),
        name="pytest configure",
        installed="pytest_configure",
        closed="pytest_unconfigure",
    )
    filters_only.__enter__()
    config.add_cleanup(lambda: filters_only.__exit__(None, None, None))
    config.addinivalue_line(
        "markers",
        "filterwarnings(warning): add a warning filter to the given test. "
        "see https://docs.pytest.org/en/stable/how-to/capture-warnings.html"
        "#pytest-mark-filterwarnings ",
    )


def pytest_report_header() -> str:
    return "warnings: cot.capture"


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection(session: pytest.Session) -> Generator[None, object, object]:
    config = session.config
    with _recording(config, config.hook, "collect", None, "collection"):
        return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> Generator[None]:
    config = terminalreporter.config
    with _recording(config, config.hook, "config", None, "terminal summary"):
        return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_sessionfinish(session: pytest.Session) -> Generator[None]:
    config = session.config
    with _recording(config, config.hook, "config", None, "session finish"):
        return (yield)


class _Run:
    """A recording, and the warnings already taken out of it."""

    def __init__(self, rec: Recording) -> None:
        self.rec = rec
        self.warnings: list[WarningRecord] = []

    def report_exceptions(self) -> None:
        """Turn unraisable and thread exceptions into pytest's warnings (W5).

        The warnings issued here are recorded in turn, or raise where a
        filter says ``error``.
        """
        errors: list[Warning] = []
        for r in self.rec.take():
            if isinstance(r, WarningRecord):
                self.warnings.append(r)
                continue
            try:
                warnings.warn(_as_warning(r))
            except (
                pytest.PytestUnraisableExceptionWarning,
                pytest.PytestUnhandledThreadExceptionWarning,
            ) as e:
                # -W error: the traceback is shown as the cause, not in the text
                e.args = (_summary(r),)
                e.__cause__ = r.exc_value
                errors.append(e)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("multiple unraisable and thread exception warnings", errors)


_running: dict[str, _Run] = {}


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
    with _recording(item.config, item.ihook, "runtest", item, "the test") as run:
        _running[item.nodeid] = run
        try:
            return (yield)
        finally:
            del _running[item.nodeid]


# The phase wrappers run inside pytest's own phase hooks (trylast), as
# pytest's unraisableexception plugin does, so an error they raise fails the
# phase.


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None]:
    with _phase(item, collect_garbage=False):
        return (yield)


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None]:
    with _phase(item, collect_garbage=False):
        return (yield)


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None]:
    # the test's last phase: its garbage is collected here (WD3)
    with _phase(item, collect_garbage=True):
        return (yield)


@contextmanager
def _phase(item: pytest.Item, *, collect_garbage: bool) -> Iterator[None]:
    try:
        yield
    finally:
        run = _running.get(item.nodeid)
        if run is not None:
            if collect_garbage:
                GC.collect()
            run.report_exceptions()


def _summary(r: UnraisableRecord | ThreadExceptionRecord) -> str:
    if isinstance(r, UnraisableRecord):
        return f"{r.err_msg}: {r.object_repr}"
    return f"Exception in thread {r.thread}"


def _as_warning(r: UnraisableRecord | ThreadExceptionRecord) -> Warning:
    text = f"{_summary(r)}\n\n{r.traceback}"
    if isinstance(r, UnraisableRecord):
        return pytest.PytestUnraisableExceptionWarning(text)
    assert isinstance(r, ThreadExceptionRecord)
    return pytest.PytestUnhandledThreadExceptionWarning(text)


@contextmanager
def _recording(
    config: pytest.Config, ihook: Any, when: When, item: pytest.Item | None, label: str
) -> Iterator[_Run]:
    run = _Run(
        record(
            filters=_filters(config, item),
            name=label if item is None else item.nodeid,
            installed=f"before {label}",
            closed=f"after {label}",
        )
    )
    try:
        with run.rec:
            try:
                yield run
            finally:
                run.report_exceptions()
    finally:
        nodeid = "" if item is None else item.nodeid
        late = [r for r in run.rec.records if isinstance(r, WarningRecord)]
        for r in run.warnings + late:
            ihook.pytest_warning_recorded.call_historic(
                kwargs=dict(warning_message=r.message, nodeid=nodeid, when=when, location=None)
            )


def _filters(config: pytest.Config, item: pytest.Item | None) -> Sequence[WarningFilter]:
    """pytest's filters, in the order it applies them (``catch_warnings_for_item``)."""
    filters: list[WarningFilter] = []
    if not sys.warnoptions:
        # pytest #2908: show deprecation warnings unless -W was given
        filters.append(("always", "", DeprecationWarning, "", 0))
        filters.append(("always", "", PendingDeprecationWarning, "", 0))
    for arg in config.getini("filterwarnings"):
        try:
            filters.append(parse_warning_filter(arg, escape=False))
        except ImportError as e:
            warnings.warn(
                f"Failed to import filter module '{e.name}': {arg}", pytest.PytestConfigWarning
            )
    for arg in config.known_args_namespace.pythonwarnings or []:
        filters.append(parse_warning_filter(arg, escape=True))
    if item is not None:
        for mark in item.iter_markers(name="filterwarnings"):
            for arg in mark.args:
                filters.append(parse_warning_filter(arg, escape=False))
    return filters
