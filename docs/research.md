# Output capture in pytest: caveats, problems and solutions

Initial research for `cot.capture`. It records how pytest captures output
today, where that breaks, and what has been proposed or done about it, so the
design of this library can start from the known failure modes instead of
rediscovering them.

## Sources and how to read the citations

- pytest source at `pytest-dev/pytest@f5240bef` (main, 2026-10-04):
  `src/_pytest/capture.py`, `src/_pytest/logging.py`,
  `src/_pytest/faulthandler.py`, `testing/test_capture.py`,
  `doc/en/how-to/capture-stdout-stderr.rst`.
- pytest's changelog (`doc/en/changelog.rst` plus unreleased `changelog/*.rst`)
  for fixed issues.
- The open issues under the `plugin: capture` and `plugin: logging` labels on
  2026-10-05.

`#N` means `https://github.com/pytest-dev/pytest/issues/N`. Each open issue is
marked **(open)**; issues whose discussion was read are summarised, the ones
marked *(title only)* were not read beyond their title, so the description
there is the title's claim, not a verified analysis. Statements marked
*inferred* follow from reading the code, not from an issue.

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

### 2.4 Threads and subprocesses

- *Inferred:* both modes are process-global. Output from a background thread
  lands in whatever test phase is being captured when it writes; there is no
  per-thread or per-test attribution.
- **#13693 (open)** live logging from background threads is racy and can make
  `capfd`/`capsys` lose messages *(title only)*.
- Subprocess output is captured only in fd mode and only if the child
  inherits fd 1/2 (see #4428, #10693).
- pytest-xdist workers replace `sys.stdout`/`sys.stderr` with non-file
  objects (#2666, faulthandler comment), and `-s` does not stream worker
  output to the controller's terminal.

### 2.5 Logging capture

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

### 2.6 faulthandler

- It must use a real fd, so it dups stderr before capture can interfere. Edge
  cases fixed: already-enabled faulthandler (`-X dev`, `PYTHONFAULTHANDLER`)
  (#8258, #6575), `sys.stderr` closed at teardown (#11439, #11572), no
  traceback at interpreter shutdown (#8260). `faulthandler_exit_on_timeout`
  added for deadlocks (#13678). pytest's own faulthandler tests are skipped
  on some CI for truncated output (#7022).

### 2.7 Reporting and debugging

- What is shown: `--show-capture` (#1478; fixed with `--tb=line` in #13865;
  **#11037 (open)** show stdout/stderr but not logs), teardown output with
  `-rP` (#2780), **#13959 (open)** `-rA` without output for passing tests,
  JUnit `system-out`/`system-err` and `junit_logging` (#3156, #1334).
- `-s` disables progress output (#3203); `console_output_style =
  progress-even-when-capture-no` restores it (#10755).
- pdb: captured output printed before entering pdb (#1223, #3052, #3204),
  capture resumed after `continue` (#2619), fixture-only capture with
  `set_trace` (#4951), pdb++ recursion (#4347).

### 2.8 Performance and control

- **#2205 (open)** optimise capture setup/teardown; **#3329 (open)** reduce
  memory of the capture plugin. Unreleased in main: fd capture skips reading
  the buffer for tests with no output (`changelog/14687`).
- **#14444 (open)** forcing `--capture=sys` without the CLI flag seems
  impossible; **#9775 (open)** disable stdout capture from outside a test
  *(title only)*. `-p no:capture` removes the plugin (#4957).

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

## 4. Implications for cot.capture (proposals, not decisions)

1. **Never close what you hand out.** Install stable proxy objects on
   `sys.stdout`/`sys.stderr` whose target is switched, not replaced, and which
   fall back to the original after capture ends. That removes most of 2.1,
   which is the most reported failure (#5502, #5282, #5997, #12876).
2. **Capture as composable values, not a singleton.** A capture should be an
   object that can be nested and stacked, so "one fixture per test" and
   "fixture breaks global capture" (#4428) cannot happen by construction.
3. **Record an ordered event stream.** Store `(stream, chunk, time, thread)`
   once and derive per-stream text from it; this answers #5449 and gives
   thread attribution (2.4) without a separate mechanism.
4. **Make the fd/sys distinction explicit per stream and per capture,** with
   a real fd always available to consumers that need one (subprocess,
   faulthandler), so 2.2's `fileno()` failures go away.
5. **Treat logging as a source into the same event stream,** attached via a
   handler that does not depend on which loggers exist at entry or mutate the
   root level silently (2.5).
6. **Bound memory** (spill to file, or cap) from the start (#3329, #9215,
   #8307).
7. **Keep host policy out of the core.** Reporting sections, `-s`, `-r`,
   `--show-capture` and pdb suspension belong in a pytest binding, not in the
   capture primitives, matching the core/host split used in
   `cot.config.ingest`.
