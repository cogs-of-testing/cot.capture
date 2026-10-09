# Warnings, unraisable exceptions and thread exceptions

How a scope records warnings, exceptions that Python cannot raise
(`sys.unraisablehook`) and exceptions that end a thread
(`threading.excepthook`). Issue #6. This is a design draft. The pull request
that carries it grows into the implementation.
Section numbers in brackets point into [the research](../research.md). Rules
O1 to O9 and decisions D1 to D12 are in [streams.md](streams.md).

## What there is to capture

None of these three sources is a stream. Each one is a **slot** in the sense of
[streams.md](streams.md#the-three-things-people-call-stdout): a module attribute
that whoever wants the events assigns to.

| Slot | What reaches it | Scope | Who else assigns it |
|---|---|---|---|
| `warnings.showwarning` | every warning that the filters let through and do not turn into an error | one interpreter | `warnings.catch_warnings(record=True)`, which resets it to the default for its block and restores it afterwards; test runners; IDEs |
| `sys.unraisablehook` | exceptions raised in `__del__`, in finalizers, during garbage collection or at interpreter shutdown | one interpreter | pytest's `unraisableexception` plugin, trio, asyncio test helpers |
| `threading.excepthook` | an exception that ends a `threading.Thread` | one interpreter | pytest's `threadexception` plugin |

The warnings **filters** are not a slot. They decide what reaches
`showwarning`. On the default build they are the module global
`warnings.filters`. On 3.14 with `sys.flags.context_aware_warnings` (on by
default only in the free-threaded build), `catch_warnings` keeps them in a
context variable instead. The flag does not change `showwarning`, which stays
a module global
([2.4](../research.md#24-threads); read in `_py_warnings.py` of 3.14.6:
`_showwarnmsg` looks up the module's `showwarning`, and only the filters and
the record log are per context).

## The API

`record()` returns a `Recording`, a context manager like a capture
[scope](streams.md#scopes) but for these three slots. It is separate from
`capture()`, so a host can record without capturing streams, and the other
way round. `warnings=`, `unraisable=` and `thread_exceptions=` choose the
slots, `filters=` and `gc=` are W1 and WD3, and `records` and `take()` work
like a scope's `out` and `take()`. Below, "scope" means a recording.

## Rules

**W1. Recording is separate from filtering.** A scope records by putting a
recorder in `warnings.showwarning`. It does not use
`catch_warnings(record=True)`, which would also replace the filters and make
them the scope's business. Filters are a separate, optional part of the scope
(`filters=`, in the stdlib's `-W` syntax: `action:message:category:module:lineno`).
When the scope has filters, it applies them on top of the current ones for its
lifetime and restores them when it ends. Without filters, the scope records
whatever the filters in force let through (#13485, #13284: filters set by the
code under test are not reset by the scope).

**W2. The three slots follow the stream ownership rules.** A scope installs a
recorder in each slot it was asked for. When the scope ends, the slot gets back
what it held before (O1). A recorder is annotated like a proxy (O2). If the slot
does not hold the scope's recorder when the scope ends, the scope reports a
`ForeignReplacement` and leaves the slot alone (O4). A
`catch_warnings(record=True)` block inside a scope is not foreign: it
restores `showwarning` when it ends, so what it records is not seen by the
scope. That is the same behaviour pytest's `recwarn` has today.

**W3. A recorder called after its scope has ended passes the event on.** Code
that copied `warnings.showwarning` or `sys.unraisablehook` and calls it later
reaches a recorder whose scope is gone. It emits the same kind of warning as a
stale proxy (O3), naming the scope, and hands the event to whatever the slot
holds now, the way write-back does. It never falls back to the interpreter's
original hook directly ([D10](streams.md#d10)); if the slot holds the original
hook, the event goes there like to any other holder.

**W4. Events, not text.** A scope keeps what it recorded as records:

- `WarningRecord`: the `warnings.WarningMessage`, the thread name, and the
  time the scope recorded it;
- `UnraisableRecord`: the exception type and value, the `err_msg`, a `repr`
  of the object and the formatted traceback;
- `ThreadExceptionRecord`: the thread name, the exception type and value and
  the formatted traceback.

The `repr` and the traceback text are taken while the hook runs, because
the object may be finalized as soon as the hook returns (pytest does the
same in `unraisableexception.py`). How the records are shown is up to the
host.

**W5. The core never turns one kind into another.** Unraisable exceptions and
thread exceptions stay records of their own kind. pytest turns them into
`PytestUnraisableExceptionWarning` and `PytestUnhandledThreadExceptionWarning`
through `warnings.warn`. A pytest binding that wants pytest's behaviour
issues those warnings itself.

**W6. One current scope per interpreter, as for streams.** A warning raised in
any thread of the interpreter is recorded by the scope that is current when it
arrives (as for streams, see
[Scopes](streams.md#scopes)). The record carries the thread name, so mixing
is visible after the fact. Routing by thread or task belongs with
[R2](streams.md#r2).

**W7. Installed per interpreter.** All three slots exist once per
interpreter. A subinterpreter starts with the default filters and its own
hooks ([2.5](../research.md#25-subinterpreters)), so a scope in the main
interpreter does not see its warnings. Whoever creates the subinterpreter (for
example cot.runsomewhere) enters a scope inside it.

**W8. `take()` splits a scope.** As with [D12](streams.md#d12), a host that runs
setup, call and teardown takes the records at the end of each part instead of
running three scopes.

## Decisions

### WD1

**The recorder is a `showwarning` replacement, not `catch_warnings(record=True)`.**

That keeps the filters out of the recording (W1). It also records a warning
from a thread started before the scope on both builds, where
`catch_warnings(record=True)` misses it on a context-aware build (2.4). So
recording gives the same result on both builds. *Probed* on 3.14.6, default
build and with `-X context_aware_warnings=1 -X thread_inherit_context=1`: a
`showwarning` replacement recorded warnings from a thread started before it
and from one started after it.

*Cost:* `catch_warnings(record=True)` resets the module-global `showwarning`
for its block, so while **any** thread is inside such a block, the recorder
misses warnings from every thread. *Probed* on both builds: a main-thread
warning raised while another thread held a record block did not reach the
recorder. Code that sets `warnings.showwarning` without restoring it makes the
scope report a foreign replacement and stop recording.

### WD2

**Filters stay as global as the build makes them.** A scope's filters are
installed with `catch_warnings()` (without recording), so they are per context
on a context-aware build and process-wide otherwise.

*Cost:* on the default build, a scope's filters apply to every thread for the
scope's lifetime, and a thread's own `catch_warnings` can undo them, the same
as in pytest today (2.4). The core does not try to fix the stdlib.

### WD3

**Garbage collection follows a policy the host chooses.** Unraisable
exceptions from finalizers only appear when the garbage is collected, so when
collection runs decides which scope they land in. A scope takes a
`GCPolicy`: how many `gc.collect()` passes to run, and at which points (the
end of the scope, each `take()`). The core default is no collection.

pytest collects at session end only (1 pass on CPython, 5 on PyPy, #14441;
`unraisableexception.py`), and reads unraisable exceptions after each test
phase without collecting. A test's cycles are therefore reported in a later
test, or at session end.

*Cost:* without collection in the policy, a finalizer's exception is
attributed to whichever scope is current when the garbage collector happens
to run. With collection, every scope end pays for a full collection.

## Follow-up: a package for warning filtering

General warning filtering and interaction moves to a package of its own, as
a follow-up (Ronny, 2026-10-08). The scope's `filters=` (W1, WD2) and the
binding's handling of `-W` and `filterwarnings` stay here until then.
`recwarn`, `pytest.warns` and `pytest.deprecated_call` move with it. Until
then they stay on pytest's nested `catch_warnings`, and W2 describes how
they interact with a scope.

## The pytest binding

This is host policy, not core. It is split in two (Ronny, 2026-10-08):

- **cot.capture** ships the pytest parts of its own capture, in
  `cot.capture.overtake_pytest`: the scopes per test, the hooks and the
  fixtures below. They work on their own with `-p`.
- **cot.pytest** is the glue. It switches the parts on, and it owns
  everything that spans more than one cot package, such as the order in
  which parts are set up and the header line.

To replace pytest's `warnings`, `unraisableexception` and `threadexception`
plugins, the cot.capture part:

- runs one scope per test, split with `take()` at the end of setup, call
  and teardown (W8), plus scopes for configure, collection and session
  finish;
- applies `-W`, ini `filterwarnings` and `@pytest.mark.filterwarnings` as
  the scope's filters, in pytest's order (`-W` wins, #3946), plus the rule
  that shows `DeprecationWarning` and `PendingDeprecationWarning` when no
  `-W` was given (#2908);
- calls `pytest_warning_recorded(warning_message, when, nodeid, location)`
  for each warning record, so the terminal summary, pytest-xdist and
  pytest-reportlog keep working;
- issues `PytestUnraisableExceptionWarning` and
  `PytestUnhandledThreadExceptionWarning` from the other two record kinds
  (W5), so `-W error` turns them into failures as it does today;
- sets the scopes' `GCPolicy` (WD3). The starting default collects at the
  end of every test, so unraisable exceptions are attributed to the test
  that made the garbage (Ronny, 2026-10-09). Grouping collection by default,
  so projects without leaks don't pay for it on every test, is #11.

cot.pytest provides replacement objects wherever pytest or a plugin looks up
the old plugin by name. One example is `warnings`: pytest core wraps
`pytest_configure` in a process-global `catch_warnings` only if a plugin of
that name is registered
([replacement research 4.3](../research-pytest-replacement.md#43-warnings-warnings-and-recwarn)).

## Appendix: probe

Run with `python3.14 probe.py` and
`python3.14 -X context_aware_warnings=1 -X thread_inherit_context=1 probe.py`
(CPython 3.14.6). Both print
`[('pre', 'pre-started'), ('child', 'child')]`: the main-thread warning raised
during the other thread's record block is missing.

```python
import sys, threading, warnings
warnings.simplefilter("always")
got = []
def rec(message, category, filename, lineno, file=None, line=None):
    got.append((threading.current_thread().name, str(message)))
started, warned = threading.Event(), threading.Event()
def other():
    started.wait(); warnings.warn("pre-started"); warned.set()
t = threading.Thread(target=other, name="pre"); t.start()
old = warnings.showwarning; warnings.showwarning = rec
started.set(); warned.wait(); t.join()
t2 = threading.Thread(target=lambda: warnings.warn("child"), name="child"); t2.start(); t2.join()
# nested record block in another thread while main warns
inside, done = threading.Event(), threading.Event()
def recorder_thread():
    with warnings.catch_warnings(record=True):
        inside.set(); done.wait()
t3 = threading.Thread(target=recorder_thread); t3.start(); inside.wait()
warnings.warn("main during other thread's record block")
done.set(); t3.join()
warnings.showwarning = old
print(got)
```
