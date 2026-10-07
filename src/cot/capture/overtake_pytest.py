"""Opt-in: capture test output with cot.capture instead of pytest.

Enable it per project with::

    [pytest]
    addopts = -p cot.capture.overtake_pytest

pytest's capture plugin stays registered, so ``capsys``, ``capfd`` and the
``capturemanager`` API that pytest and other plugins call keep working, but
its global capture is switched off. Instead, with ``--capture=fd`` (the
default) or ``--capture=sys``, conftest loading, the collection of each file
and each test run inside a cot.capture scope, at descriptor or slot level.
A test's scope is split at the end of setup, call and teardown, and what
each phase captured becomes the usual "Captured stdout/stderr" report
sections. ``--capture=no`` and ``-s`` capture nothing; ``tee-sys`` is
left to pytest.

What differs from pytest (docs/research-pytest-replacement.md):

- a ``sys.stdout`` kept from an earlier test warns with
  ``ProxyExpiredWarning`` naming that test, and writes to the current
  ``sys.stdout``, instead of silently landing in the current test's capture;
- nothing is suspended (design D9): pdb gets terminal streams, and so does
  pytest's terminal writer under fd capture, so live logging and
  ``--setup-show`` reach the terminal while a scope is active;
- ``capsys.disabled()`` does not reach the terminal, its output is captured.

This module imports pytest; the rest of cot.capture never does.
"""

from __future__ import annotations

import sys
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from ._scope import Level, Scope, capture
from ._terminal import TerminalStream, terminal

_LEVELS: dict[str, Level] = {"fd": "fd", "sys": "slot"}


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config) -> Generator[None]:
    # Runs outside pytest's own wrapper, which builds its capture manager
    # from the namespace: "no" keeps that manager registered but idle.
    ns = early_config.known_args_namespace
    level = _LEVELS.get(ns.capture)
    if level is not None:
        ns.capture = "no"
    plugin = _Capture(level)
    early_config.pluginmanager.register(plugin, "cot-capture")
    early_config.add_cleanup(plugin.close)
    if level is None:
        return (yield)
    scope = plugin.scope("conftest")
    try:
        with scope:
            return (yield)
    except BaseException:
        # pytest shows what conftests printed when loading them failed
        sys.stdout.write(scope.out)
        sys.stderr.write(scope.err)
        raise


class _Capture:
    """The per-session state: the level, the running test's scope, pdb's streams."""

    def __init__(self, level: Level | None) -> None:
        self.level = level
        self._test: Scope | None = None
        self._pdb_streams: list[TerminalStream] = []
        self._writer: Any = None
        self._writer_file: Any = None

    def scope(self, name: str) -> Scope:
        return capture(
            stdout=self.level,
            stderr=self.level,
            stdin=True,
            name=name,
            # a reference kept past its test warns and reaches the current
            # sys.stdout instead of raising in an unrelated test
            write_back=True,
        )

    def close(self) -> None:
        self._close_pdb_streams()
        if self._writer is not None:
            stream = self._writer._file
            self._writer._file = self._writer_file
            self._writer = None
            stream.close()

    # -- reporting ----------------------------------------------------------

    def pytest_report_header(self) -> str:
        if self.level is None:
            return "capture: none"
        return f"capture: cot.capture ({self.level} level)"

    @pytest.hookimpl(trylast=True)
    def pytest_configure(self, config: pytest.Config) -> None:
        if self.level != "fd":
            return
        # Under fd capture the terminal writer's stream writes to fd 1, which
        # a scope borrows; a terminal stream keeps reaching the terminal.
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is None:
            return
        writer = config.get_terminal_writer()
        self._writer, self._writer_file = writer, writer._file
        writer._file = terminal("stdout", owner="pytest's terminal writer")

    # -- collection and tests -----------------------------------------------

    @pytest.hookimpl(wrapper=True)
    def pytest_make_collect_report(
        self, collector: pytest.Collector
    ) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
        if self.level is None or not isinstance(collector, pytest.File):
            return (yield)
        scope = self.scope(f"{collector.nodeid}::collect")
        with scope:
            report = yield
        if scope.out:
            report.sections.append(("Captured stdout", scope.out))
        if scope.err:
            report.sections.append(("Captured stderr", scope.err))
        return report

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item) -> Generator[None, object, object]:
        if self.level is None:
            return (yield)
        # One scope for the whole test, split into phases with take(): capsys
        # and capfd save sys.stdout in setup and put it back in teardown, so
        # it must be the same proxy throughout.
        scope = self._test = self.scope(item.nodeid)
        try:
            with scope:
                return (yield)
        finally:
            self._test = None
            # printed after teardown, while the test's reports were logged
            sys.stdout.write(scope.out)
            sys.stderr.write(scope.err)

    # The phase wrappers are tryfirst so they run outside pytest's: closing
    # capsys or capfd at the end of a phase writes what they still hold to
    # sys.stdout, and that belongs to this phase.

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item: pytest.Item) -> Generator[None, None, None]:
        with self._phase(item, "setup"):
            return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, None, None]:
        with self._phase(item, "call"):
            return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item: pytest.Item) -> Generator[None, None, None]:
        with self._phase(item, "teardown"):
            return (yield)

    @contextmanager
    def _phase(self, item: pytest.Item, when: str) -> Iterator[None]:
        scope = self._test
        if scope is None:
            yield
            return
        try:
            yield
        finally:
            self._close_pdb_streams()
            out, err = scope.take()
            if out:
                item.add_report_section(when, "stdout", out)
            if err:
                item.add_report_section(when, "stderr", err)

    # -- the debugger ---------------------------------------------------------

    def pytest_enter_pdb(self, pdb: Any) -> None:
        if self.level is None:
            return
        # The scope stays in place (D9): the debugger talks to the terminal.
        stdin = terminal("stdin", owner="pdb")
        stdout = terminal("stdout", owner="pdb")
        self._pdb_streams += [stdin, stdout]
        pdb.stdin, pdb.stdout = stdin, stdout
        pdb.use_rawinput = False

    def pytest_leave_pdb(self) -> None:
        self._close_pdb_streams()

    def _close_pdb_streams(self) -> None:
        while self._pdb_streams:
            self._pdb_streams.pop().close()
