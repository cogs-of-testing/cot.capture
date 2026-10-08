# Examples: what cot.capture is for, and how it differs from pytest

cot.capture captures what a program writes to its standard streams. pytest
does that too, and has for many years; this page shows what cot.capture does
differently and why, first with the library on its own and then with the
pytest binding next to pytest's builtin capture.

Every file on this page is written to a directory and every command is run
by `testing/test_examples.py`; the output shown is what the command printed.
A `...` stands for text that differs between machines (paths, addresses) or
that the page leaves out.

## The intent

Most of pytest's capture bugs come down to two mismatches
([research](research.md),
[design](design/streams.md#the-three-things-people-call-stdout)):

- **Early binding.** Code copies `sys.stdout` (a logging handler, a CLI
  framework, a thread) and keeps writing to it after capture has moved on.
  With pytest, the text silently lands in another test's report, or fails
  with `ValueError: I/O operation on closed file` at exit.
- **The wrong level.** Capturing `sys.stdout` cannot see C code, child
  processes or `os.write`; capturing file descriptor 1 sees everything but
  cannot tell writers apart.

cot.capture answers with ownership rules
([O1 to O9](design/streams.md#ownership-rules)) rather than a global capture
that is suspended and resumed:

- **Capture closes what it hands out.** The object a scope puts in
  `sys.stdout`, a *proxy*, lives exactly as long as the scope.
- **Proxies carry their history**: the slot, the owner, where and when they
  were installed and closed. Using one after its lifetime warns with that
  history; it then raises, or, when asked for, writes back to whatever the
  slot holds now.
- **Foreign replacement is tolerated and reported.** Code may put its own
  object in `sys.stdout`; the scope does not fight it and says so at exit.
- **Descriptors are borrowed**, in a LIFO stack per descriptor, into a
  temporary file, never a pipe with a reader thread.
- **The original streams are never a fallback.** Nothing writes to
  `sys.__stdout__` behind your back.
- **Nothing is suspended.** Whatever must reach the terminal while a scope is
  active (a debugger, progress output, live logging) gets a *terminal
  stream*, owned and closed by whoever asked for it.
- **The library writes only where it was told to.** It never prints on its
  own account.

Not yet: logging and warnings capture are not sources of cot.capture. Under
the pytest binding, `caplog`, the "Captured log" sections and the warnings
summary are still pytest's own.

## The library on its own

### Slot level: what Python writes to `sys.stdout`

`capture()` is a context manager. By default it captures `sys.stdout` and
`sys.stderr` at slot level: it puts a proxy in each slot and puts the old
object back at exit.

<!-- file: slot.py -->
```python
import sys

from cot.capture import capture

before = sys.stdout
with capture() as scope:
    print("hello")
    print("oops", file=sys.stderr)
    print(type(sys.stdout).__name__, file=sys.stderr)

print(repr(scope.out))
print(repr(scope.err))
print(sys.stdout is before)
```

<!-- run: python slot.py -->
```text
'hello\n'
'oops\nStreamProxy\n'
True
```

### Descriptor level: everything that reaches fd 1

At slot level, `os.write` and child processes bypass the proxy and reach the
real stream at once. At descriptor level (`"fd"`) the scope borrows the
descriptor, so they are captured too, in the order they were written,
together with `print`.

<!-- file: levels.py -->
```python
import os
import subprocess
import sys

from cot.capture import capture

with capture(stdout="slot", stderr=None) as slot:
    print("print")
    os.write(1, b"os.write\n")
    subprocess.run([sys.executable, "-c", "print('child')"])

with capture(stdout="fd", stderr=None) as fd:
    print("print")
    os.write(1, b"os.write\n")
    subprocess.run([sys.executable, "-c", "print('child')"])

print("slot:", repr(slot.out))
print("fd:  ", repr(fd.out))
```

<!-- run: python levels.py -->
```text
os.write
child
slot: 'print\n'
fd:   'print\nos.write\nchild\n'
```

The two lines at the top come from the slot-level scope: they were never
captured. `stderr=None` leaves a stream alone.

### `fileno()` tells the truth

At descriptor level the proxy's `fileno()` is the borrowed descriptor
itself, so code that hands `sys.stdout` to `subprocess` or `faulthandler`
writes into the capture. At slot level there is no descriptor, and
`fileno()` raises rather than creating one behind your back
([D4](design/streams.md#d4)).

<!-- file: fileno.py -->
```python
import io
import sys

from cot.capture import capture

with capture(stdout="fd", stderr=None):
    number = sys.stdout.fileno()
print("fd level:", number)

with capture(stdout="slot", stderr=None, name="slot-only"):
    try:
        sys.stdout.fileno()
    except io.UnsupportedOperation as exc:
        error = exc
print("slot level:", error)
```

<!-- run: python fileno.py -->
```text
fd level: 1
slot level: fileno() on proxy for sys.stdout owned by 'slot-only', installed at ...fileno.py:10: slot-level capture has no descriptor
```

### A proxy kept past its scope

A library that caches `sys.stdout` keeps the proxy after the scope ended.
Using it warns with the proxy's history, then raises `ProxyExpiredError`,
which is both a `ValueError` (what a closed file raises) and an `OSError`
with `EBADF` (what a closed descriptor raises), so existing error handling
keeps working ([D2](design/streams.md#d2)).

<!-- file: lifetime.py -->
```python
import sys
import warnings

from cot.capture import ProxyExpiredError, capture

with capture(name="setup-logging"):
    kept = sys.stdout  # a library caches the stream it was given

print(kept.closed)
print(kept.annotations.owner, kept.annotations.slot)

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    try:
        kept.write("late\n")
    except ProxyExpiredError as exc:
        print(type(exc).__name__, isinstance(exc, ValueError), isinstance(exc, OSError))
for warning in caught:
    print(warning.category.__name__, warning.message)
```

<!-- run: python lifetime.py -->
```text
True
setup-logging stdout
ProxyExpiredError True True
ProxyExpiredWarning write on closed proxy for sys.stdout owned by 'setup-logging', installed at ...lifetime.py:6, closed at ...lifetime.py:6
```

### Write-back: late output goes where output goes now

With `write_back=True`, a proxy used after its scope still warns, and then
writes to whatever the slot holds at the time of the write: here, the proxy
of a later scope, which captures it. A dead end (nothing usable in the slot)
raises instead of reaching for `sys.__stdout__`
([D10](design/streams.md#d10)).

<!-- file: writeback.py -->
```python
import sys
import warnings

from cot.capture import capture

with capture(name="first", write_back=True):
    kept = sys.stdout

with capture(name="second") as second:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        kept.write("late\n")

print(repr(second.out))
print(caught[0].category.__name__)
```

<!-- run: python writeback.py -->
```text
'late\n'
ProxyExpiredWarning
```

The pytest binding turns write-back on, so a stale reference never breaks an
unrelated test.

### Someone else replaces `sys.stdout`

`contextlib.redirect_stdout`, Click's `CliRunner` and IDEs put their own
object in the slot. Writes to it are theirs, not the scope's. If the object
is still there when the scope ends, the scope leaves it in place, warns with
`SlotReplacedWarning` and records a `ForeignReplacement` in `diagnostics`
([O4](design/streams.md#ownership-rules)).

<!-- file: foreign.py -->
```python
import contextlib
import io
import sys
import warnings

from cot.capture import capture

mine = io.StringIO()
with capture() as scope:
    with contextlib.redirect_stdout(mine):
        print("to the foreign object")
    print("to the scope")
print(repr(mine.getvalue()), repr(scope.out), scope.diagnostics)

before = sys.stdout
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    with capture(name="cli") as scope:
        sys.stdout = mine  # like a CLI runner that never restores it
left = sys.stdout is mine
sys.stdout = before
print(left)
print(caught[0].category.__name__, caught[0].message)
print(scope.diagnostics[0].slot, scope.diagnostics[0].found == repr(mine))
```

<!-- run: python foreign.py -->
```text
'to the foreign object\n' 'to the scope\n' []
True
SlotReplacedWarning scope 'cli' ended with sys.stdout holding <_io.StringIO object at ...>, not its proxy; left in place
stdout True
```

### stdin: refused, or served

`stdin=True` installs a stdin proxy that refuses to read, naming its scope;
a string is served as input ([D6](design/streams.md#d6)).

<!-- file: stdin.py -->
```python
from cot.capture import StdinRefusedError, capture

with capture(stdin=True, name="quiet") as quiet:
    try:
        input()
    except StdinRefusedError as exc:
        error = exc
print(error)

with capture(stdin="yes\n") as scope:
    answer = input("continue? ")
print(repr(answer), repr(scope.out))
```

<!-- run: python stdin.py -->
```text
readline from proxy for sys.stdin owned by 'quiet', installed at ...stdin.py:3, which was given no input
'yes' 'continue? '
```

### One scope, several parts

`take()` returns and drops what a live scope captured so far; the proxies
stay in place ([D12](design/streams.md#d12)). This is how the pytest binding
splits one test into setup, call and teardown.

<!-- file: take.py -->
```python
from cot.capture import capture

with capture(stdout="fd", stderr="fd", name="test_example") as scope:
    print("setup")
    setup = scope.take()
    print("call")
    call = scope.take()
    print("teardown")

print(setup, call, (scope.out, scope.err))
```

<!-- run: python take.py -->
```text
('setup\n', '') ('call\n', '') ('teardown\n', '')
```

### Reaching the terminal while capturing

There is no suspend. A writer that must be seen while a scope is active asks
for a terminal stream: a private `dup` of the uncaptured descriptor, owned
and closed by whoever asked for it
([terminal streams](design/streams.md#terminal-streams)).

<!-- file: terminal.py -->
```python
import os

from cot.capture import capture, terminal

with terminal("stdout", owner="progress") as term:
    with capture(stdout="fd", stderr="fd") as scope:
        term.write("progress: 1/2\n")
        os.write(1, b"captured\n")
        term.write("progress: 2/2\n")
print(repr(scope.out))
```

<!-- run: python terminal.py -->
```text
progress: 1/2
progress: 2/2
'captured\n'
```

## pytest: the builtin capture and the binding side by side

`-p cot.capture.overtake_pytest` replaces pytest's capture for a run; to make
it the default, put it in `addopts`. The examples below run the same test
file twice: first with pytest's builtin capture, then with the binding.

```ini
[pytest]
addopts = -p cot.capture.overtake_pytest
```

What stays the same:

- each test's output becomes the usual "Captured stdout setup / call /
  teardown" sections, shown for failed tests and with `-rA`;
- `--capture=fd` (the default) captures at descriptor level, `--capture=sys`
  at slot level, `-s` captures nothing; `tee-sys` is left to pytest;
- `capsys` and `capfd` work, and so does pytest's `capturemanager` plugin
  object, which other plugins call;
- live logging (`log_cli`) and `--setup-show` reach the terminal.

With the binding, the session header says `capture: cot.capture (fd level)`
(or `slot level` under `--capture=sys`). What differs is below.

### A `sys.stdout` kept from an earlier test

pytest installs one capture object for the whole session, so a reference
kept from `test_keep` writes into whichever test runs later, without a word.
The binding gives each test its own proxy, installed before setup and closed
after teardown; the late write warns, naming the test that owned the proxy
and that lifetime, and is written back into the current test.

<!-- file: test_kept.py -->
```python
import sys

kept = []


def test_keep():
    kept.append(sys.stdout)


def test_use():
    kept[0].write("late\n")
    assert False
```

<!-- run: pytest test_kept.py -->
```text
test_kept.py .F                                                          [100%]

=================================== FAILURES ===================================
___________________________________ test_use ___________________________________
...
----------------------------- Captured stdout call -----------------------------
late
=========================== short test summary info ============================
FAILED test_kept.py::test_use - assert False
========================= 1 failed, 1 passed in ...s ==========================
```

<!-- run: pytest -p cot.capture.overtake_pytest test_kept.py -->
```text
capture: cot.capture (fd level)
...
test_kept.py .F                                                          [100%]

=================================== FAILURES ===================================
___________________________________ test_use ___________________________________
...
----------------------------- Captured stdout call -----------------------------
late
=============================== warnings summary ===============================
test_kept.py::test_use
  ...test_kept.py:11: ProxyExpiredWarning: write on closed proxy for sys.stdout owned by 'test_kept.py::test_keep', installed before setup, closed after teardown
    kept[0].write("late\n")
...
=========================== short test summary info ============================
FAILED test_kept.py::test_use - assert False
==================== 1 failed, 1 passed, 1 warning in ...s ====================
```

### A logging handler created during a test

The most common way to keep a reference: a `StreamHandler` takes the
`sys.stderr` of the moment. pytest's own logging capture is unchanged (the
"Captured log" section); the binding adds a warning for each write and flush
through the stale proxy, and one more after the session, when `logging`
flushes its handlers at exit. The text itself is written back into the
current test, as with pytest. The "Captured log" section now comes first,
because the binding adds its sections after pytest's logging plugin does.

<!-- file: test_handler.py -->
```python
import logging
import sys

log = logging.getLogger("demo")


def test_configure():
    log.addHandler(logging.StreamHandler(sys.stderr))
    log.warning("from the first test")


def test_later():
    log.warning("from a later test")
    assert False
```

<!-- run: pytest test_handler.py -->
```text
__________________________________ test_later __________________________________
...
----------------------------- Captured stderr call -----------------------------
from a later test
------------------------------ Captured log call -------------------------------
WARNING  demo:test_handler.py:13 from a later test
=========================== short test summary info ============================
FAILED test_handler.py::test_later - assert False
========================= 1 failed, 1 passed in ...s ==========================
```

<!-- run: pytest -p cot.capture.overtake_pytest test_handler.py -->
```text
__________________________________ test_later __________________________________
...
------------------------------ Captured log call -------------------------------
WARNING  demo:test_handler.py:13 from a later test
----------------------------- Captured stderr call -----------------------------
from a later test
=============================== warnings summary ===============================
test_handler.py::test_later
  ...logging...: ProxyExpiredWarning: write on closed proxy for sys.stderr owned by 'test_handler.py::test_configure', installed before setup, closed after teardown
    stream.write(msg + self.terminator)

test_handler.py::test_later
  ...logging...: ProxyExpiredWarning: flush on closed proxy for sys.stderr owned by 'test_handler.py::test_configure', installed before setup, closed after teardown
    self.stream.flush()
...
=========================== short test summary info ============================
FAILED test_handler.py::test_later - assert False
=================== 1 failed, 1 passed, 2 warnings in ...s ====================
...logging...: ProxyExpiredWarning: flush on closed proxy for sys.stderr owned by 'test_handler.py::test_configure', installed before setup, closed after teardown
  self.stream.flush()
```

### A stream kept until the interpreter exits

pytest closes its capture object at the end of the session, so an `atexit`
callback that still holds it fails. The binding's proxy warns and writes
back to the `sys.stdout` of that moment, the real one.

<!-- file: test_atexit.py -->
```python
import atexit
import sys


def test_register():
    out = sys.stdout
    atexit.register(lambda: out.write("written at exit\n"))
```

<!-- run: pytest -q test_atexit.py -->
```text
.                                                                        [100%]
1 passed in ...s
Exception ignored in atexit callback...
Traceback (most recent call last):
  File "...test_atexit.py", line 7, in <lambda>
    atexit.register(lambda: out.write("written at exit\n"))
ValueError: I/O operation on closed file.
```

<!-- run: pytest -q -p cot.capture.overtake_pytest test_atexit.py -->
```text
.                                                                        [100%]
1 passed in ...s
...test_atexit.py:7: ProxyExpiredWarning: write on closed proxy for sys.stdout owned by 'test_atexit.py::test_register', installed before setup, closed after teardown
  atexit.register(lambda: out.write("written at exit\n"))
written at exit
```

### Reading stdin

Both refuse; the binding's error names the test whose stdin proxy refused.

<!-- file: test_stdin.py -->
```python
def test_read():
    input()
```

<!-- run: pytest test_stdin.py -->
```text
    def test_read():
>       input()
...
E       OSError: pytest: reading from stdin while output is captured!  Consider using `-s`.
...
FAILED test_stdin.py::test_read - OSError: pytest: reading from stdin while o...
```

<!-- run: pytest -p cot.capture.overtake_pytest test_stdin.py -->
```text
    def test_read():
>       input()
...
E           cot.capture._errors.StdinRefusedError: readline from proxy for sys.stdin owned by 'test_stdin.py::test_read', installed before setup, which was given no input
...
FAILED test_stdin.py::test_read - cot.capture._errors.StdinRefusedError: read...
```

### `capsys.disabled()`

pytest suspends its capture inside `capsys.disabled()`, so even `os.write`
to fd 1 reaches the terminal. The binding suspends nothing: it points
`sys.stdout` and `sys.stderr` at terminal streams for the duration. `print`
reaches the terminal; a write to the descriptor stays captured under fd
capture.

<!-- file: test_disabled.py -->
```python
import os


def test_disabled(capsys):
    with capsys.disabled():
        print("print inside disabled()")
        os.write(1, b"os.write inside disabled()\n")
    assert False
```

<!-- run: pytest test_disabled.py -->
```text
test_disabled.py print inside disabled()
os.write inside disabled()
F                                                       [100%]

=================================== FAILURES ===================================
________________________________ test_disabled _________________________________
...
E       assert False

test_disabled.py:8: AssertionError
=========================== short test summary info ============================
```

<!-- run: pytest -p cot.capture.overtake_pytest test_disabled.py -->
```text
test_disabled.py print inside disabled()
F                                                       [100%]

=================================== FAILURES ===================================
________________________________ test_disabled _________________________________
...
E       assert False

test_disabled.py:8: AssertionError
----------------------------- Captured stdout call -----------------------------
os.write inside disabled()
=========================== short test summary info ============================
```

### `breakpoint()` and pdb

pytest suspends capture while the debugger runs and announces it. The
binding leaves the test's scope in place and gives pdb terminal streams for
its stdin and stdout ([D9](design/streams.md#d9)), so the session works the
same. pytest's debugging plugin writes its `PDB set_trace` banner to
`sys.stdout`, though, which is the test's proxy: the banner lands in the
captured output, between what the test printed before and after the
breakpoint.

<!-- file: answers.txt -->
```text
p value
c
```

<!-- file: test_pdb.py -->
```python
def test_debug():
    print("before the breakpoint")
    value = 6 * 7
    breakpoint()
    print("after the breakpoint")
    assert False
```

<!-- run: pytest test_pdb.py < answers.txt -->
```text
test_pdb.py
>>>>>>>>>>>>>>>>>>> PDB set_trace (IO-capturing turned off) >>>>>>>>>>>>>>>>>>>>
> ...test_pdb.py(...)test_debug()
...
(Pdb) 42
(Pdb)
>>>>>>>>>>>>>>>>>>>>> PDB continue (IO-capturing resumed) >>>>>>>>>>>>>>>>>>>>>>
F                                                            [100%]
...
----------------------------- Captured stdout call -----------------------------
before the breakpoint
after the breakpoint
=========================== short test summary info ============================
```

<!-- run: pytest -p cot.capture.overtake_pytest test_pdb.py < answers.txt -->
```text
test_pdb.py > ...test_pdb.py(...)test_debug()
...
(Pdb) 42
(Pdb) F                                                            [100%]
...
----------------------------- Captured stdout call -----------------------------
before the breakpoint

>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> PDB set_trace >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>

>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> PDB continue >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
after the breakpoint
=========================== short test summary info ============================
```

### `sys.stdout.fileno()`

Under fd capture, pytest's `sys.stdout` is a wrapper around its temporary
file, so `fileno()` is that file's descriptor. The binding's proxy reports
descriptor 1, the one it borrowed. Under `--capture=sys` both raise
`io.UnsupportedOperation`; the binding's message names the test.

<!-- file: test_fileno.py -->
```python
import sys


def test_fileno():
    print("fileno() == 1:", sys.stdout.fileno() == 1)
    assert False
```

<!-- run: pytest test_fileno.py -->
```text
----------------------------- Captured stdout call -----------------------------
fileno() == 1: False
```

<!-- run: pytest -p cot.capture.overtake_pytest test_fileno.py -->
```text
----------------------------- Captured stdout call -----------------------------
fileno() == 1: True
```

<!-- run: pytest --capture=sys test_fileno.py -->
```text
E       io.UnsupportedOperation: fileno
```

<!-- run: pytest --capture=sys -p cot.capture.overtake_pytest test_fileno.py -->
```text
capture: cot.capture (slot level)
...
E           io.UnsupportedOperation: fileno() on proxy for sys.stdout owned by 'test_fileno.py::test_fileno', installed before setup: slot-level capture has no descriptor
```

### Threads: no difference yet

There is one current scope per slot per process, not per thread, in both. A
thread that started in one test and prints after it ended lands in the test
that is running then. Routing output by thread is a research topic
([R2](design/streams.md#r2)).

<!-- file: test_thread.py -->
```python
import threading

started = threading.Event()
go = threading.Event()
worker = None


def work():
    print("worker: started")
    started.set()
    go.wait()
    print("worker: after its test ended")


def test_start_thread():
    global worker
    worker = threading.Thread(target=work)
    worker.start()
    started.wait()


def test_next():
    go.set()
    worker.join()
    assert False
```

<!-- run: pytest -rA test_thread.py -->
```text
__________________________________ test_next ___________________________________
...
----------------------------- Captured stdout call -----------------------------
worker: after its test ended
==================================== PASSES ====================================
______________________________ test_start_thread _______________________________
----------------------------- Captured stdout call -----------------------------
worker: started
```

<!-- run: pytest -rA -p cot.capture.overtake_pytest test_thread.py -->
```text
__________________________________ test_next ___________________________________
...
----------------------------- Captured stdout call -----------------------------
worker: after its test ended
==================================== PASSES ====================================
______________________________ test_start_thread _______________________________
----------------------------- Captured stdout call -----------------------------
worker: started
```
