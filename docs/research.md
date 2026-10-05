# Capture in pytest: caveats, problems and solutions

Initial research for `cot.capture`. It records how pytest captures output and
warnings today, where that breaks, and what has been proposed or done about
it, so the design of this library can start from the known failure modes
instead of rediscovering them.

**Goal of cot.capture** (from the project owner): a library of building
blocks to configure capture at different levels, able to deal with threads,
subinterpreters and similar, with extra concessions for execnet and
`cot.runsomewhere`. Section 4 maps the findings onto that goal.

## Sources and how to read the citations

- pytest source at `pytest-dev/pytest@f5240bef` (main, 2026-10-04):
  `src/_pytest/capture.py`, `logging.py`, `faulthandler.py`, `warnings.py`,
  `recwarn.py`, `threadexception.py`, `config/__init__.py`,
  `testing/test_capture.py`, `doc/en/how-to/capture-stdout-stderr.rst`.
- pytest's changelog (`doc/en/changelog.rst` plus unreleased `changelog/*.rst`)
  for fixed issues.
- The open issues under the `plugin: capture`, `plugin: logging` and
  `plugin: warnings` labels on 2026-10-05.
- execnet 2.1.2 and pytest-xdist 3.8.0 sdists (`gateway_base.py`,
  `xdist/remote.py`, `xdist/workermanage.py`).
- `cot.runsomewhere` design (`docs/design/gateways-and-channels.md#output`,
  `bootstrap.md#the-protocol-stream`).
- Probes run on CPython 3.14.0rc2, default and free-threaded (`3.14t`)
  builds; the scripts are in the appendix.

`#N` means `https://github.com/pytest-dev/pytest/issues/N`. Each open issue is
marked **(open)**; issues whose discussion was read are summarised, the ones
marked *(title only)* were not read beyond their title, so the description
there is the title's claim, not a verified analysis. Statements marked
*inferred* follow from reading the code, not from an issue or a probe;
*probed* means a probe in the appendix showed it.

## 1. How pytest captures today

**Three streams, one manager.** `CaptureManager` owns one global `MultiCapture`
(in, out, err) selected by `--capture={fd,sys,no,tee-sys}` (`-s` is
`--capture=no`). It starts in `pytest_load_initial_conftests`, so conftest
imports are already captured, and it is wrapped around collection
(`pytest_make_collect_report`) and each of setup, call and teardown
(`item_capture`). The captured text is attached to the report as
`Captured stdout/stderr <phase>` sections.

**`fd` mode (default).** `FDCapture` opens an unnamed `TemporaryFile`, saves
the original with `os.dup(targetfd)`, and `os.dup2`s the tmpfile over fd 1/2.
It *also* runs a `SysCapture` on top that replaces `sys.stdout`/`sys.stderr`
with an `EncodedFile` writing into the same tmpfile. This is what catches C
extensions and child processes that inherit the fds. For fd 0, stdin is
pointed at `/dev/null` and `sys.stdin` becomes `DontReadFromInput`, which
raises on read. If the target fd is invalid it is backed by `/dev/null` first
(#7091).

**`sys` mode.** Only `sys.stdout`/`sys.stderr` are replaced, by `CaptureIO`
(an in-memory `TextIOWrapper` over `BytesIO`). No fd is touched; anything that
writes to fd 1/2 directly (C code, subprocesses, `os.write`) escapes.
`tee-sys` (#4597) is `sys` mode that also writes through to the original.

**Fixtures.** `capsys`, `capfd`, `capsysbinary`, `capfdbinary`, `capteesys`
(#12081) each build their own `MultiCapture`, registered on the manager. Only
one may be active per test: `set_fixture` raises
`cannot use capfd and capsys at the same time`. A fixture captures even under
`-s` (#13731; old changelog: "capsys/capfd also work when output capturing
('-s') is disabled"). The fixture's own capture is started inside the global
one and suspended/resumed with it.

**Suspend/resume is everywhere.** The terminal reporter, live logging, `pdb`
and `capsys.disabled()` all work by suspending the global capture (and the
fixture's), writing, and resuming. `global_and_fixture_disabled()` is the
helper. Much of the bug history below is about this choreography.

**Logging is a separate plugin.** `logging.py` installs handlers through
`catching_logs`: a `LogCaptureHandler` for the report section, one for
`caplog`, optionally a live-log handler (`log_cli`) and a file handler
(`log_file`). It attaches them to the **root logger** and, since #3697, to
every logger that already exists and has `propagate = False`. It also lowers
the root logger's level for the duration.

**faulthandler is a third, fd-level path.** At `pytest_configure` the plugin
`os.dup`s the current stderr fd and `faulthandler.enable(file=dup)`, so crash
and timeout dumps bypass capture. It restores a previously enabled
faulthandler at unconfigure.

**Warnings are a fourth path, with no stream at all.** `warnings.py` wraps
config, collection, each test's whole `runtest` protocol, the terminal
summary and session finish in `warnings.catch_warnings(record=True)`
(`Config._catch_configured_warnings`). Inside it pytest shows
`DeprecationWarning`/`PendingDeprecationWarning` unless `-W` was given
(#2908), applies `filterwarnings` from ini and `-W` (`-W` wins, #3946), then
the test's `@pytest.mark.filterwarnings` marks. Recorded warnings go out
through the historic hook `pytest_warning_recorded` (#4049) and end up in the
"warnings summary". `recwarn`, `pytest.warns` and `pytest.deprecated_call`
are nested `catch_warnings` subclasses (`WarningsRecorder`); since 8.0
`pytest.warns` re-emits what it did not match (#9288). Unhandled thread
exceptions and unraisable exceptions are collected from
`threading.excepthook`/`sys.unraisablehook` and turned into warnings
(#5299, #12958, #13016).

## 2. Caveats and known problems

### 2.1 Stale references to `sys.stdout`/`sys.stderr` (the largest class)

pytest swaps `sys.stdout`/`sys.stderr` per phase and **closes** the replaced
capture objects. Anything that cached a reference (a `logging.StreamHandler`
made at import time, `atexit` handlers, a library's console object, a saved
stream in the embedding application) ends up writing to a closed or
redirected object.

- **#5502 (open)** `ValueError: I/O operation on closed file` from logging set
  up at import time or in `atexit`/multiprocessing paths. Workarounds in the
  thread: a fixture that removes `StreamHandler`s, or `-s`. A proposal there:
  install a permanent wrapper on `sys.stdout`/`sys.stderr` that pytest never
  closes and that forwards to the original after the session. Maintainers'
  position is that such logging setups are broken; users answer that
  third-party libraries force it.
- **#5282 (open)** pytest 4.5 floods output with logging errors when logging
  from `atexit` *(title only)*.
- **#5997 (open)** output shows in "Captured stderr call" but not in
  `capsys`/`capfd`: a handler bound to the stream before the fixture swapped
  it writes to the global capture, not the fixture's. Workarounds:
  `--capture=tee-sys`, patching the handler's stream, configuring logging
  lazily.
- **#13720 (open)** `capfd` does not capture a `StreamHandler(sys.stdout)`;
  **#10486 (open)** the `capsys` variant. The maintainer answer is "use
  `caplog`"; a stdio stream handler under pytest is called always incorrect.
- **#12876 (open)** on Windows, a `sys.stdout` saved before `pytest.main()`
  fails with `WinError 6 The handle is invalid` under fd capture; proposed
  resolution is documentation only.
- **#5743 (open)** improve the error when tests use a closed stdout/stderr
  *(title only)*.
- **#3344 (closed)** Click's `CliRunner` + pdb: the same closed-file failure
  from two libraries both swapping streams.

### 2.2 Mode-specific breakage

- **No real fd in `sys`/`tee-sys` mode.** **#10693 (open)**:
  `subprocess.check_output(..., stderr=sys.stderr)` raises
  `UnsupportedOperation` because `CaptureIO` has no `fileno()`; answer was
  "expected, use fd mode". faulthandler has the same problem and falls back
  to `sys.__stderr__.fileno()` when `sys.stderr` is not fd-backed, e.g. under
  pytest-xdist or twisted.logger (#8249; comment in `faulthandler.py`).
- **Encoding and stream attributes.** Capture objects have to imitate real
  text streams: `encoding` (#2375), `errors` (#555), `name` (#2555),
  `mode` without `b` (#5257), `write()` return value (#6557), `writelines`
  (#6566), line endings preserved by `capfd` (#7517), behaving the same with
  and without capture (#4861), full file-like `sys.stdin` (#10150).
  **#4389 (open)** stdout capturing still breaks `sys.stdout.encoding`
  *(title only)*.
- **isatty and terminal features.** Captured streams are not ttys, so
  libraries drop colours. **#11270 (open)** log colours are lost under
  `tee-sys`; **#13322 (open)** `resume_global_capture()` changes the detected
  terminal width *(title only)*. pytest imports `colorama` early
  (`_colorama_workaround`) because colorama binds to the terminal at import.
- **Windows console.** `dup2` over a `_WindowsConsoleIO` closes the console
  handle; `_windowsconsoleio_workaround` reopens stdio on a new fd (#2467,
  pytest-dev/py#103). **#10843 (open)** `capfd` is flaky on Windows,
  sometimes returning an empty string *(title only)*.
- **stdin.** `DontReadFromInput` must still be iterable during collection
  (#3314); `readline` needs `_readline_workaround` on Windows; Python 3.13
  with libedit broke `input()` under suspended capture (#12888).

### 2.3 Fixture semantics

- **One capture fixture per test**, enforced by an error. Fixture capture also
  takes precedence over `-s` (#13731), which surprises users who expect `-s`
  to show everything (#2079, closed with a docs explanation).
- **#4428 (open)** a fixture that merely requests `capfd` breaks capture of
  subprocess output for the whole test: the fixture's capture is local to the
  test and the subprocess inherited the fds earlier. A commenter concludes "a
  completely different structure for capture is needed"; no PR.
- **#5449 (open)** stdout and stderr are captured into separate buffers, so
  their relative order is lost. Proposals: dup both onto one fd, or record a
  single ordered sequence of `(stream, chunk)`. Objections: buffering and
  locking make ordering unreliable, and changing fixture results would break
  many suites.
- Fixed edge cases that show how fragile the choreography is: nested
  `disabled()` (#7148), using `capsys` from other fixtures (#2709) and in
  teardown (#3033), resuming after `disabled()` (#1993), `capteesys` doubling
  output under `-s` (#13784), `capsysbinary` crash (#6871), readouterr result
  no longer a namedtuple (#7631).

### 2.4 Threads

- Every capture level pytest uses is **process-global**: fds, `sys.stdout`,
  the root logger and (by default) the warnings filter list. *Inferred:*
  output, log records and warnings from a background thread land in whatever
  test phase is being captured when they happen; nothing attributes them to
  the thread or the test that started it.
- **#13693 (open)** live logging from background threads is racy and can make
  `capfd`/`capsys` lose messages *(title only)*.
- **`warnings.catch_warnings` is not thread-safe on the default build.** It
  swaps the module-global filter list and `showwarning`. *Probed:* on 3.14
  (GIL), a `catch_warnings(record=True)` in the main thread records a warning
  raised in an unrelated thread. So does every pytest warning capture,
  `recwarn` and `pytest.warns`, and a thread's own `catch_warnings` can
  undo the test's filters.
- **Python 3.14 makes warnings context-aware, but only by flag.**
  `sys.flags.context_aware_warnings` makes `catch_warnings` use a context
  variable; `sys.flags.thread_inherit_context` makes new threads start with
  a copy of the starting thread's context. *Probed:* both default to **on**
  for the free-threaded build and **off** for the default build (`-X
  context_aware_warnings=1 -X thread_inherit_context=1` turn them on). With them
  on, a warning from a thread started *before* the `catch_warnings` block
  escapes it, while one from a thread started *inside* is recorded. So the
  same test suite records different warnings depending on the build.
- Python's logging has no such mechanism: handlers and levels stay global on
  every build.
- Unhandled exceptions in threads are reported through `threading.excepthook`,
  which is process-global; pytest's `threadexception` plugin attributes them
  to the test during which the hook fired (#5299, #13016).

### 2.5 Subinterpreters

*All probed on 3.14 with `concurrent.interpreters` (PEP 734), both builds.*

- Each interpreter has its own `sys`. In a new interpreter
  `sys.stdout is sys.__stdout__`, freshly made over fd 1. **Replacing the
  main interpreter's `sys.stdout` does not capture a subinterpreter's
  `print`.** sys-level capture (`capsys`, `--capture=sys`, `tee-sys`) misses
  it entirely.
- fds are shared by the whole process, so **fd-level capture does catch
  subinterpreter output**, but cannot tell it apart from the main
  interpreter's or another subinterpreter's.
- The warnings module is per interpreter: a new interpreter starts with the
  default filter list (5 entries in the probe, the
  stdlib defaults), not the test's `-W`,
  `filterwarnings` or `catch_warnings`. *Inferred:* the same holds for
  logging configuration, which is module state.
- *Inferred:* `faulthandler` and signal handlers are process-wide but only
  installable from the main interpreter, so crash dumps stay main-interpreter
  business.
- No pytest issue covers subinterpreters and capture yet (none found under
  the three labels).

### 2.6 Subprocesses, execnet and pytest-xdist

- Subprocess output is captured only in fd mode and only if the child
  inherits fd 1/2 (see #4428, #10693). A child's warnings and log records
  arrive, if at all, as text on stderr, not as structured records.
- **execnet's popen worker sends worker stdout to `/dev/null`.**
  `init_popen_io` (execnet `gateway_base.py`) moves the protocol to dups of
  fds 0 and 1, then points fd 0 and 1 at the null device and rebuilds
  `sys.stdin`/`sys.stdout` over them (fd 2 too on Windows). *Inferred:* this
  is why, under pytest-xdist, `-s` shows nothing a worker prints to stdout,
  while stderr still reaches the terminal on POSIX.
- pytest-xdist transports pytest's own capture: captured sections travel in
  the serialized report, and warnings go through `pytest_warning_recorded`
  as a dict (`serialize_warning_message` in `xdist/remote.py`): the class is
  sent by module and name and re-imported on the controller, args are sent
  only if execnet can serialize them, and every other detail is sent as its
  `repr`. A warning class that does not exist on the controller, or whose
  args do not serialize, degrades.
- Under xdist, `sys.stderr` may not be fd-backed; faulthandler falls back to
  `sys.__stderr__` (comment in `faulthandler.py`), and Windows capture had to
  tolerate execnet channel objects as `sys.stdout` (#2666).
- **cot.runsomewhere** already decides part of this: a worker whose protocol
  has its own stream leaves stdio to the code it runs, with output configured
  as `inherit`, `forward` (lines on a `gateway.output` channel) or `discard`;
  on stdio-only places the worker moves the protocol off fd 0/1 before
  running anything (`gateways-and-channels.md#output`,
  `bootstrap.md#the-protocol-stream`). Its `Thread` and `Subinterpreter`
  places share the caller's fds, so for them sections 2.4 and 2.5 apply
  rather than this one.

### 2.7 Logging capture

- **Which loggers are seen.** Handlers go on the root logger plus loggers that
  are non-propagating *at the moment of entry*; the source says it "will miss
  loggers that *become* non-propagating after the `__enter__`. Not worth the
  trouble for now." (`logging.py`, `catching_logs`). **#7335 (open)** "caplog
  don't capture all logs" *(title only)*.
- **Levels leak in both directions.** Fixed: `caplog.set_level` overrides
  `log_level` (#7133), does not change the report section (#7159), handler
  level restored (#7569, #7672), works through `logging.disable` (#8711),
  warning when a test modifies the root logger (#11011). Still open:
  **#7904** `LogCaptureHandler.reset()` does not reset the level,
  **#7656** `caplog.at_level` affecting the handler too, **#10266**
  per-module levels *(all title only)*.
- **Live logging interferes with stdout.** `log_cli` writes to the terminal by
  suspending capture. Fixed: stdout not captured with live logging (#3819).
  Open: **#10553** `--log-cli-level` manipulates `sys.stdout`, **#13612** it
  also raises verbosity *(title only)*.
- **Lifecycle gaps.** Logging during collection was duplicated to stderr
  (#6240) or hidden (#3964), both fixed. Open: **#12203** teardown logs are
  lost when a fixture raises; **#9393** logging init timing change makes
  `logging.basicConfig` in code under test fail *(title only)*.
  **#9236** `LogCaptureHandler` garbage-collected when a hook raised; the fix
  (try/finally, PR #11123) has landed though the issue is still listed open.
- **Memory and speed.** Every record is kept for `caplog` and the report:
  **#8307** cannot log without keeping all records, **#9215** limit memory
  for captured logs/stdout, **#11772** performance regression for
  logging-heavy suites since 6.0 *(title only)*.
- **Control.** **#9018** disable the logging plugin for one test,
  **#9239** control which channels are captured/displayed *(title only)*.

### 2.8 faulthandler

- It must use a real fd, so it dups stderr before capture can interfere. Edge
  cases fixed: already-enabled faulthandler (`-X dev`, `PYTHONFAULTHANDLER`)
  (#8258, #6575), `sys.stderr` closed at teardown (#11439, #11572), no
  traceback at interpreter shutdown (#8260). `faulthandler_exit_on_timeout`
  added for deadlocks (#13678). pytest's own faulthandler tests are skipped
  on some CI for truncated output (#7022).

### 2.9 Reporting and debugging

- What is shown: `--show-capture` (#1478; fixed with `--tb=line` in #13865;
  **#11037 (open)** show stdout/stderr but not logs), teardown output with
  `-rP` (#2780), **#13959 (open)** `-rA` without output for passing tests,
  JUnit `system-out`/`system-err` and `junit_logging` (#3156, #1334).
- `-s` disables progress output (#3203); `console_output_style =
  progress-even-when-capture-no` restores it (#10755).
- pdb: captured output printed before entering pdb (#1223, #3052, #3204),
  capture resumed after `continue` (#2619), fixture-only capture with
  `set_trace` (#4951), pdb++ recursion (#4347).

### 2.10 Performance and control

- **#2205 (open)** optimise capture setup/teardown; **#3329 (open)** reduce
  memory of the capture plugin. Unreleased in main: fd capture skips reading
  the buffer for tests with no output (`changelog/14687`).
- **#14444 (open)** forcing `--capture=sys` without the CLI flag seems
  impossible; **#9775 (open)** disable stdout capture from outside a test
  *(title only)*. `-p no:capture` removes the plugin (#4957).

### 2.11 Warnings capture

- **Filters set at the wrong time do not apply.** pytest re-enters
  `catch_warnings` per test, which resets filters to the configured ones.
  Open: **#13485** filters set by collected code during collection have no
  effect during the test run, **#13284** cannot configure the warnings module
  in `pytest_configure`, **#12249** handle warnings raised at import time
  early *(all title only)*. Historical: warnings during `pytest_configure` are
  explicitly not errors because that "completely breaks pytest" (#5115);
  filters are now applied as early and reverted as late as possible (#10404);
  plugin-import warnings are captured since `changelog/12697`; unraisable
  exceptions from finalizers are collected before the session's filters are
  torn down (#14263).
- **`-W error` versus everything else.** Open: **#8534** `pytest -Werror`
  fails with ignore filters in `pytest.ini`, **#10098** graceful handling of
  `-W error` for plugins and packages, **#13964** treat
  `PytestCollectionWarning` as an error in strict mode, **#7051**
  `--strict-markers` and warnings *(all title only)*.
- **Filter syntax is fragile.** The module field is not regex-escaped
  (#4255), the message field of the mark is (#3936); a filter naming an
  unimportable class used to fail the run (#13732), and the class is resolved
  by import, which depends on `sys.path` (**#13274 (open)**, title only).
  Open: **#10478** warn on impossible-to-match patterns, **#8096** better
  error for an invalid `-W` *(title only)*.
- **Recording semantics.** `pytest.warns(None)` meant "at least one warning"
  and was deprecated (#8645); `pytest.warns` used to swallow unmatched
  warnings (#9288) and skip control-flow exceptions (#11907); records are
  kept when the block raises (#9036). Open: **#4343** check the warning's
  `stacklevel`, **#3559** show tracebacks with warnings, **#2717** warnings
  in JUnit XML, **#4732** warnings capture interferes with doctest
  *(all title only)*.
- **Threads and interpreters**: see 2.4 and 2.5. pytest's per-test warning
  capture is a process-global `catch_warnings` on the default build.
- **Framework integration.** **#6838 (open)** framework integration for
  warning filter setup *(title only)*. pytest's `filterwarnings` is
  pytest-only syntax over the stdlib's `-W` syntax.

## 3. Solutions in use today

| Problem | What people do |
|---|---|
| Stale stream references | Configure handlers lazily; remove `StreamHandler`s in a fixture (#5502); `-s` |
| Log assertions | `caplog` instead of `capsys`/`capfd` on a stream handler (#13720) |
| C / subprocess output | fd mode or `capfd`; pass no explicit `sys.stderr` to `subprocess` (#10693) |
| Live output plus capture | `--capture=tee-sys`, `capteesys` |
| Interleaving of out/err | none in pytest (#5449); merge in the child (`stderr=STDOUT`) |
| C-level capture in-process | third-party `wurlitzer` (dup2 onto pipes with reader threads) |
| Crash dumps | built-in faulthandler on a dup'ed stderr fd, `faulthandler_timeout` |
| Warnings from threads | nothing in pytest; on 3.14, `-X context_aware_warnings=1 -X thread_inherit_context=1` |
| Warnings across processes | pytest-xdist serializes `WarningMessage` (class by name, details by `repr`) |
| Worker stdout under xdist | none; it goes to `/dev/null` (execnet), use stderr or logging |

## 4. Implications for cot.capture (proposals, not decisions)

### 4.1 Capture levels

The findings sort into levels that differ in what they see and what they can
attribute. pytest picks one combination per run; cot.capture should offer
each as its own building block.

| Level | Scope | Sees | Attributes to | Breaks on |
|---|---|---|---|---|
| fd | process | everything written to fd 1/2: C code, child processes, every thread and interpreter | nothing | Windows console (2.2), protocol fds of execnet/runsomewhere workers (2.6) |
| sys | one interpreter | Python writes through `sys.stdout`/`sys.stderr` | nothing, unless routed | stale references (2.1), missing `fileno()` (2.2), subinterpreters (2.5) |
| context | thread or task, via a `contextvars.ContextVar` behind the sys proxy | the same as sys | the scope that is current in that context | threads started before the scope (2.4) |
| logging | one interpreter | `LogRecord`s | the record's thread, logger | handler attachment and level leaks (2.7) |
| warnings | one interpreter, or one context on 3.14 with the flags on | `WarningMessage`s | per context only on 3.14 with flags | thread safety (2.4), filter timing (2.11) |
| out-of-band | process (`faulthandler`), interpreter (`threading.excepthook`, `sys.unraisablehook`) | crashes, thread and unraisable exceptions | thread for excepthook | needing a real fd (2.8) |
| remote | another process or host | events forwarded by a capture in the worker | the worker | protocol on stdio, serialization (2.6) |

### 4.2 Building blocks

1. **Events, not buffers.** Every source emits
   `(source, stream, payload, time, thread, interpreter, scope)` into one
   ordered sink, and per-stream text is a view of it. That answers #5449
   (interleaving), gives thread attribution (2.4), and makes forwarding a
   matter of shipping events (4.3). Sinks are bounded or spill to a file
   from the start (#3329, #9215, #8307).
2. **Stable proxies, never closed.** `sys.stdout`/`sys.stderr` get a proxy
   once per interpreter whose target is switched, not replaced, and which
   falls back to the original after capture. It has a real `fileno()` when
   the fd level is active underneath. That removes most of 2.1 (#5502,
   #5282, #5997, #12876) and 2.2's `fileno()` failures (#10693).
3. **Context routing.** The proxy, a logging handler and a `showwarning`
   replacement look up the current scope in one `ContextVar`. On 3.14 with
   `thread_inherit_context`, threads started inside a scope inherit it; on
   older Pythons or without the flag, a helper to start threads in a
   context (`contextvars.copy_context().run`) is the explicit route. Output
   from threads with no scope goes to a session-level fallback, labeled as
   such, never into whichever test happens to run.
4. **Composable scopes.** A scope is a context manager that selects levels
   and a sink; scopes nest and stack, so "one capture fixture per test" and
   "a fixture breaks global capture" (#4428) cannot happen by construction.
   Suspend and resume belong to the scope, not to a manager singleton.
5. **Warnings as a first-class source.** Record warnings through the routed
   `showwarning` instead of a global `catch_warnings` per scope, so filters
   and recording are separated. Use the 3.14 context-aware machinery when
   the flags are on and say plainly when they are not, rather than giving
   different results per build silently (2.4).
6. **Logging without global mutation.** One handler on the root logger for
   the session, routing by scope, rather than adding and removing handlers
   and lowering the root level per test (2.7). Loggers that become
   non-propagating later are the remaining hole; it needs an explicit
   decision.
7. **Per-interpreter installation.** sys, logging and warnings blocks are
   installed in each interpreter; the fd level and faulthandler only once,
   from the main one (2.5).
8. **Host policy stays out.** Reporting sections, `-s`, `-r`,
   `--show-capture`, pdb suspension and live logging belong in a pytest
   binding, matching the core/host split of `cot.config.ingest`.

### 4.3 Concessions for execnet and cot.runsomewhere

- **Protected fds.** A worker's protocol may live on dups of fd 0/1 (execnet)
  or move off them before user code runs (runsomewhere). The fd level must
  take the set of fds it must never `dup2` over or close, and must be
  installable after the worker has moved its protocol.
- **Wire-safe events.** Events forwarded over a channel must use only values
  the channel carries: runsomewhere allows `None`, `bool`, `int`, `float`,
  `complex`, `str`, `bytes`, tuples, lists, dicts and sets, and never sends
  pickled objects. Warning categories and logger names travel by qualified
  name, args as `repr` unless they are plain values, and the receiving side
  must keep an event whose class it cannot import as data instead of failing
  (xdist's re-import approach degrades, 2.6).
- **`forward` as a sink.** runsomewhere's `forward` output mode delivers
  lines on `gateway.output`. A cot.capture sink could carry the full event
  stream there (stream, thread, interpreter, warnings, log records), with
  lines as one view of it. `inherit` and `discard` map to "no capture" and
  a null sink.
- **Places that share the caller's process.** `Thread` and `Subinterpreter`
  places share fds with the caller, so the fd level cannot separate them;
  attribution must come from the context level (thread) or the
  per-interpreter install (subinterpreter), which runsomewhere can perform
  because it creates the interpreter.
- **execnet as it is.** For existing execnet/xdist workers, worker stdout
  goes to the null device. A worker-side cot.capture that forwards events
  over an execnet channel would restore what `-s` users expect; that needs
  only the protected-fds rule and wire-safe events above.

### 4.4 Decisions this leaves open

- Whether the fd level is on by default, given it cannot attribute and is
  the main source of Windows trouble.
- What happens to output from threads with no scope: session fallback,
  attribute to the starting scope, or drop with a count.
- Whether to depend on 3.14's context-aware warnings or reimplement routing
  for older Pythons.
- How far to go for loggers that turn non-propagating after the handler is
  attached.

## Appendix: probes

Run with `python3.14 probe.py`, `python3.14t probe.py` and
`python3.14 -X context_aware_warnings=1 -X thread_inherit_context=1 probe.py`
(CPython 3.14.0rc2).

```python
import os, sys, tempfile, threading, warnings
from concurrent import interpreters

print(sys.flags.context_aware_warnings, sys.flags.thread_inherit_context)

# a warning from a thread started before catch_warnings
started, warned = threading.Event(), threading.Event()
def other():
    started.wait(); warnings.warn("other"); warned.set()
t = threading.Thread(target=other); t.start()
with warnings.catch_warnings(record=True) as log:
    warnings.simplefilter("always")
    started.set(); warned.wait()
t.join()
print("pre-started thread recorded:", any("other" in str(w.message) for w in log))

# a warning from a thread started inside catch_warnings
with warnings.catch_warnings(record=True) as log:
    warnings.simplefilter("always")
    t = threading.Thread(target=lambda: warnings.warn("child")); t.start(); t.join()
print("child thread recorded:", any("child" in str(w.message) for w in log))

# subinterpreter: own sys and warnings, shared fds
interp = interpreters.create()
interp.exec("import sys, warnings; print(sys.stdout is sys.__stdout__, len(warnings.filters))")
tmp, saved = tempfile.TemporaryFile(), os.dup(1)
sys.stdout.flush(); os.dup2(tmp.fileno(), 1)
interp.exec("print('subinterp-out', flush=True)")
os.dup2(saved, 1); tmp.seek(0)
print("fd capture saw subinterpreter:", b"subinterp-out" in tmp.read())
interp.close()
```

Observed: default build `0 0`, pre-started thread recorded `True`, child
recorded `True`; free-threaded build and the default build with both `-X`
flags `1 1`, pre-started `False`, child `True`. Both builds: the
subinterpreter prints `True 5`, and fd capture sees its output. Swapping the
main interpreter's `sys.stdout` around `interp.exec` did not capture the
subinterpreter's `print` (separate probe, same builds).
