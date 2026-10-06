# Can cot.capture replace pytest's capture, logging and warnings plugins?

Research, 2026-10-05. **Short answer: yes in principle, for all three.**
pytest's report pipeline is decoupled from how output is captured, and every
plugin can be switched off. What stands in the way is a set of missing
primitives in cot.capture and a de-facto API (`capturemanager`) that core and
third-party plugins call into. Sections 4 and 5 list both.

Sources: pytest main at `f5240bef`; 32 popular plugins downloaded from PyPI
(list in [appendix A](#a-plugins-surveyed)); a spike plugin run against
pytest ([appendix B](#b-the-spike)). Section numbers like
[2.1](research.md#21-stale-references-to-sysstdoutsysstderr-the-largest-class)
point into [the capture research](research.md).

## 1. The seams that make replacement possible

1. **Results flow through report sections, not through the plugins.**
   Capture and logging hand their text to `item.add_report_section(when,
   key, content)` (`capture.py:874`, `logging.py:852`); collection output is
   appended to `CollectReport.sections`. Everything downstream reads only
   sections: `TestReport.capstdout/capstderr/caplog`, the terminal's
   "Captured ..." blocks and `--show-capture`, JUnit `system-out`, `-rP`,
   pytest-xdist (sections travel inside serialized reports), pytest-html.
   Third parties already use this seam for their own output
   (`pytest_structlog`: `item.add_report_section("call", "structlog", ...)`).
2. **Warnings flow through one historic hook.** `pytest_warning_recorded`
   (`hookspec.py:1131`) is the only output of the warnings plugin; the
   terminal summary, xdist, pytest-reportlog and
   pytest-github-actions-annotate-failures consume only that hook.
3. **All three can be disabled.** `-p no:capture`, `-p no:logging`,
   `-p no:warnings` all work: none is in `essential_plugins`
   (`config/__init__.py:337`), and every core consumer of `capturemanager`
   checks for `None` (`debugging.py`, `doctest.py`, `setuponly.py`,
   `subtests.py`, and `logging.py:713` "if capturemanager plugin is disabled,
   live logging still works").
4. **Options mostly live elsewhere.** `-W/--pythonwarnings`, `filterwarnings`
   and `--max-warnings` are registered in `main.py`; `--show-capture` and
   `--disable-warnings` in `terminal.py`; `junit_logging` in `junitxml.py`.
   A replacement must re-register only `--capture/-s` and the logging
   options (`--log-*`, `log_cli*`, `log_file*`, `--log-disable`).
5. **A `-p` or entry-point plugin loads before conftests**, so a
   replacement can wrap `pytest_load_initial_conftests` and capture conftest
   imports (pytest's #93) the same way the built-in does.

## 2. Who reaches into the plugins

### 2.1 pytest core

| Consumer | What it uses | Why |
|---|---|---|
| `debugging.py` (pdb, `--pdb`, `breakpoint()`) | `capturemanager.suspend_global_capture(in_=True)`, `read_global_capture()`, `resume_global_capture()`, `is_capturing()` | give the debugger the real terminal and stdin; print what was captured so far |
| `doctest.py` (macOS only) | `suspend_global_capture(in_=True)`, `read_global_capture()` | #985 |
| `setuponly.py` (`--setup-show`) | `suspend_global_capture()` / `resume_global_capture()` | write fixture actions to the terminal mid-test |
| `subtests.py` | `global_and_fixture_disabled`, `_capture_fixture`, the `capture` option, and it **instantiates `_pytest.capture.CaptureFixture(FDCapture/SysCapture)` itself**; imports `catching_logs`, `LogCaptureHandler`, reads `logging-plugin`.`log_level` | per-subtest sections |
| `logging.py` live logging | `capturemanager.global_and_fixture_disabled()` around every record | write live logs to the terminal |
| `terminal.py` | `TerminalReporter(config, sys.stdout)` at configure time; `pytest_warning_recorded`; `rep.sections` | early-bound `sys.stdout` |
| `config/__init__.py` | `Config._catch_configured_warnings` wraps `pytest_configure` in a process-global `catch_warnings` **only if a plugin named `warnings` is registered** | `-W error` during configure |
| `pytester.py` | `_get_multicapture` | in-process runs |
| `unraisableexception.py`, `threadexception.py` | `warnings.warn(...)` | rely on the warnings plugin to record them |

### 2.2 Third-party plugins (32 surveyed)

Only five reach past the seams:

| Plugin | Reaches into | For |
|---|---|---|
| pytest-timeout | `capturemanager.suspend_global_capture(item)`, `read_global_capture()` | dump captured output on timeout |
| pytest-benchmark | `capturemanager.suspend_global_capture` / `resume_global_capture` (also the pre-4 names) | write progress to the terminal |
| pytest-print | `CaptureManager.global_and_fixture_disabled()` | print to the terminal from tests |
| pytest-html | `_pytest.logging._remove_ansi_escape_sequences` | a private helper, not capture |
| pytest-xdist, -reportlog, -github-actions-annotate-failures | `pytest_warning_recorded` only | already the seam |

None imports `CaptureFixture`, `LogCaptureHandler`, `catching_logs` or the
caplog stash keys. The private surface third parties depend on is small:
**suspend, resume, read-so-far, and "disabled" as a context manager.** Three
of those five uses exist only to reach the terminal, which a
[terminal stream](design/streams.md#terminal-streams) gives without
suspending anything. cot.capture has no suspend
([D9](design/streams.md#d9)), so all of them have to be expressed that way.

## 3. What the spike showed

A 30-line plugin ([appendix B](#b-the-spike)) run with `-p no:capture`
wraps setup/call/teardown in `cot.capture.capture(...)` and calls
`add_report_section`:

- **Works as-is:** "Captured stdout/stderr call" sections, with fd mode
  capturing Python, `os.write` and child-process output in order; `-rA`
  passes sections; `--pdb` post-mortem (runs after the phase ended);
  `--capture`-style slot mode.
- **A kept `sys.stdout` from one test now fails in the next**, with
  `ProxyExpiredError` naming the test that owned it. pytest today silently
  writes it into the next test's capture. Correct per the design, but a
  behaviour change; `write_back=True` makes it a warning plus output in the
  current test's capture instead.
- **`breakpoint()` inside a test breaks**: the stdin proxy refuses the
  read, and pdb's prompt goes into the capture. Nothing suspends the scope,
  by design ([D9](design/streams.md#d9)). A pdb built on terminal stdin
  and stdout works inside an fd scope (`testing/test_terminal.py`); wiring
  it into pytest's debugging plugin is 4.1.
- **fd mode is slower**: 3000 one-line tests, best of five runs:

  | Mode | Time |
  |---|---|
  | cot.capture fd, a temporary file per scope | 3.3 s |
  | cot.capture fd, [capture files](design/streams.md#d8) shared by a test's phases | 3.0 s |
  | built-in fd | 2.4 s |
  | cot.capture slot | 2.3 s |
  | built-in sys | 2.0 s |
  | no capture | 1.8 s |

  Sharing the files saves about 0.1 ms per test. The rest of the gap is
  per-scope set-up in Python (proxies, the descriptor-level text wrapper,
  annotations, the stdin proxy): about 0.2 ms per test for three scopes,
  measured outside pytest. File creation is no longer the main cost.

## 4. Gaps in cot.capture, per plugin

### 4.1 stdout/stderr (`capture`)

| Needed | Today | Gap |
|---|---|---|
| per-phase sections | `Scope` + `add_report_section` | none |
| collection and conftest-import capture | same | none (host wiring) |
| **reach the terminal mid-scope** (pdb, `--setup-show`, timeout, benchmark) | terminal streams for stdout, stderr and stdin; no suspend by design | host wiring: the breakpoint hook and `--pdb` build `pdb.Pdb(stdin=terminal("stdin"), stdout=terminal("stdout"))` and close both when the debugger returns; pytest's `pytestPDB._init_pdb` builds the debugger without streams (so it uses `sys.stdin`/`sys.stdout`) and its header `TerminalWriter` on `sys.stdout`, so this needs a pytest change or a replacement debugging plugin |
| KeyboardInterrupt, internal error | scopes exit on the exception | none; the host ends the scope instead of suspending it |
| **read so far** (pdb, timeout, `capsys.readouterr()`) | text only at scope end | snapshot-and-clear on a live scope |
| `capsys`/`capfd`/`*binary`/`capteesys` | no fixture; text only | bytes access; tee at slot level (write to target and a terminal stream); `disabled()` has to make `print()` reach the terminal without suspend, for example a nested scope whose slot target is a terminal stream (undecided) |
| one fixture per test, fixture over `-s` | scopes nest freely | none; nesting is strictly more general |
| speed in fd mode | capture files shared by a test's phases | per-scope set-up cost (section 3) |
| `capturemanager` duck API for pytest core and the five plugins | no | a compat object registered as `capturemanager`: `read_global_capture()`, `is_capturing()`, `is_globally_capturing()`, `_capture_fixture`; `suspend_global_capture(in_)`, `resume_global_capture()` and `global_and_fixture_disabled()` can only be no-ops, so callers that print after "suspending" stay captured unless they write through the terminal reporter |
| `TerminalReporter` bound to `sys.stdout` at configure | pytest suspends global capture before configure, so it binds the real stream | the host must keep no scope active at `pytest_configure`, or hand the reporter a terminal stream (needs a pytest change) |
| stale-reference behaviour | raise | the pytest binding should default to `write_back=True` |

### 4.2 Logging (`logging`)

cot.capture has no logging source yet (deferred in the design). Everything
the logging plugin does is replaceable in principle, because its only
coupling to capture is live logging, and a terminal stream removes that
coupling:

- report section `log` per phase, via the same seam;
- `caplog` with its public API (`records`, `record_tuples`, `messages`,
  `text`, `get_records(when)`, `clear`, `set_level`, `at_level`,
  `filtering`), which needs per-phase record storage and level handling
  without the leaks in [2.7](research.md#27-logging-capture);
- live logging to a terminal stream instead of suspending capture;
- `log_file` handler and all `--log-*` options re-registered;
- `subtests` reads `logging-plugin`.`log_level` and uses
  `_pytest.logging.catching_logs` directly; the module stays importable when
  the plugin is disabled, so subtests keeps working but with pytest's own
  handler, not ours.

### 4.3 Warnings (`warnings`, and `recwarn`)

cot.capture has no warnings source yet. Replacing the `warnings` plugin is
the smallest job of the three:

- record per phase and call `pytest_warning_recorded` with the same
  arguments (`warning_message`, `when`, `nodeid`, `location`), historic;
- apply `filterwarnings` ini, `-W` and `@pytest.mark.filterwarnings`, plus
  the default "show `DeprecationWarning`" rule (#2908);
- `-W error` during `pytest_configure` comes from core, and only when a
  plugin is registered under the name `warnings`. A replacement that takes
  the name inherits that process-global `catch_warnings`; one that does not
  loses it.
- `recwarn`, `pytest.warns` and `pytest.deprecated_call` are a separate
  plugin (`recwarn`) and public API built on nested `catch_warnings`. They
  can stay as they are, or be rebuilt on the same source to get
  thread-safety on 3.14 ([2.4](research.md#24-threads)).
- `unraisableexception` and `threadexception` only call `warnings.warn`,
  so they keep working with any recorder.

## 5. Risks

- **Two capture systems at once.** `subtests` builds pytest's own
  `FDCapture`/`SysCapture` when the `capture` option is `fd` or `sys`. If a
  replacement registers `--capture`, subtests stacks pytest's capture on top
  of cot.capture's. It nests correctly in practice (LIFO), but cot.capture's
  borrow stack does not see it. Either name the option differently or
  provide a subtests integration.
- **Name squatting.** Registering under `capturemanager`, `logging-plugin`
  or `warnings` turns on core behaviour keyed on those names (live logging
  lookup, `_catch_configured_warnings`). Each name is a decision, not a
  default.
- **`pytester` in-process runs** use `_get_multicapture` and nest a whole
  session; terminal streams already release their dups (D7), but borrows and
  proxies of the inner session must also be gone when it returns.
- **Behaviour changes users will see**: stale references warn or raise
  instead of landing in the wrong test; fixtures can nest; `fileno()` raises
  in slot mode as in pytest's `sys` mode.

## 6. Suggested order

1. Read-so-far, pdb wired to terminal streams, then a `capturemanager`
   compat object whose suspend is a no-op; that unblocks pdb, timeout, benchmark,
   print, `--setup-show`.
2. Cut per-scope set-up cost in fd mode, to match built-in speed.
3. `capsys`/`capfd` fixtures and tee on the same scope API.
4. Warnings recorder (smallest plugin, one hook).
5. Logging source, `caplog`, live logging on a terminal stream.

## Appendix

### A. Plugins surveyed

hypothesis, pytest-asyncio, pytest-bdd, pytest-benchmark, pytest-check,
pytest-clarity, pytest-cov, pytest-django, pytest-env,
pytest-github-actions-annotate-failures, pytest-html, pytest-icdiff,
pytest-instafail, pytest-mock, pytest-mpl, pytest-pretty, pytest-print,
pytest-qt, pytest-randomly, pytest-repeat, pytest-reportlog,
pytest-rerunfailures, pytest-socket, pytest-split, pytest-structlog,
pytest-sugar, pytest-timeout, pytest-trio, pytest-twisted, pytest-xdist,
pytest-xprocess, syrupy (latest PyPI releases on 2026-10-05). Searched,
outside their test suites, for `capturemanager`, `_pytest.capture`,
`_pytest.logging`, `_pytest.warnings`, `logging-plugin`, the
`CaptureManager` methods, `catching_logs`, `LogCaptureHandler`, the caplog
stash keys, `pytest_warning_recorded`, `capstdout`/`capstderr`/`caplog`,
`add_report_section`, `capsys`/`capfd`.

### B. The spike

Not part of the package and not executed by the test suite; recorded so the
results in section 3 can be reproduced
(`pytest -p no:capture -p cot_spike`, with `--cot-files=per-scope` for the unshared row).

```python
import pytest
from cot.capture import capture, capture_files


class CotCapture:
    def __init__(self, config: pytest.Config) -> None:
        self.level = "fd" if config.getoption("cot_capture") == "fd" else "slot"
        self.shared = config.getoption("cot_files") == "shared"
        self.files = None

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item):
        if not self.shared:
            return (yield)
        with capture_files(item.nodeid) as self.files:
            try:
                return (yield)
            finally:
                self.files = None

    def _wrap(self, item: pytest.Item, when: str):
        scope = capture(
            stdout=self.level,
            stderr=self.level,
            stdin=True,
            name=f"{item.nodeid}::{when}",
            files=self.files,
        )
        try:
            with scope:
                return (yield)
        finally:
            if scope.out:
                item.add_report_section(when, "stdout", scope.out)
            if scope.err:
                item.add_report_section(when, "stderr", scope.err)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_setup(self, item):
        return (yield from self._wrap(item, "setup"))

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item):
        return (yield from self._wrap(item, "call"))

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(self, item):
        return (yield from self._wrap(item, "teardown"))


def pytest_addoption(parser):
    parser.addoption("--cot-capture", default="fd", choices=["fd", "slot"])
    parser.addoption("--cot-files", default="shared", choices=["shared", "per-scope"])


def pytest_configure(config):
    config.pluginmanager.register(CotCapture(config), "cot-capture")
```
