# Streams, ownership, descriptors and file objects

Who owns what when output is captured, and how capture at the
file-descriptor level relates to capture at the file-object level. Settled in
the design session of 2026-10-05; the [decisions](#decisions) record what was
chosen and what it costs. Section numbers in brackets point into
[the research](../research.md).

## The three things people call "stdout"

| Thing | Example | Scope | Created by |
|---|---|---|---|
| **descriptor** | fd 1 | the process: every thread, every interpreter, every child that inherits it | the OS at process start, or whoever `open`s / `dup`s |
| **slot** | `sys.stdout`, a `StreamHandler.stream`, faulthandler's file, a library's cached `self.out` | wherever the reference lives | whoever assigns it |
| **file object** | the `TextIOWrapper` that `sys.__stdout__` refers to | one interpreter | the interpreter at start-up, or whoever wraps an fd |

A writer reaches output through a chain: a slot holds a file object, the file
object writes (through `BufferedWriter` and `FileIO`) to a descriptor, the
descriptor points at a file, pipe or terminal. Capture cuts the chain at one
of two places:

- **slot level**: put a capture file object in the slot. Sees only Python
  writes that look up that slot, in that interpreter.
- **descriptor level**: `dup2` a capture target over the descriptor. Sees
  everything that reaches the descriptor, from anyone, without attribution.

Almost every bug in the research is one of two mismatches:

1. **Early binding.** A writer copied the file object out of the slot, then
   kept using it after capture moved on
   ([2.1](../research.md#21-stale-references-to-sysstdoutsysstderr-the-largest-class)).
   The object people copy and misplace is the one capture put in the slot:
   the **proxy**.
2. **Wrong level for the writer.** Slot capture cannot see C code, children
   or other interpreters; descriptor capture cannot tell them apart
   ([2.2](../research.md#22-mode-specific-breakage),
   [2.5](../research.md#25-subinterpreters)).

## Ownership rules

**O1. Capture closes what it hands out.** Every proxy has a lifetime: the
scope that created it. When the scope ends, the proxy is closed and the slot
gets back what it held before. A proxy is never reused by a later scope.

**O2. Proxies carry their history.** Each proxy is annotated with the slot it
was made for, its owner (the scope's name), and where and when it was
installed and closed (code location and monotonic time). Every warning and
error about a proxy names these, so a misplaced reference can be traced to
whoever kept it (#5743).

**O3. Use after the lifetime warns.** Writing to, flushing or reading from a
closed proxy emits a `ProxyExpiredWarning` carrying the annotations, at the
caller's location. What happens to the data next is chosen per proxy:

- **write-back**, when requested: the data goes to the proxy's write-back
  target, by default whatever the slot holds at the time of the write (for
  `sys.stdout`, the current `sys.stdout`). If that is a closed proxy, the
  data goes to what that proxy replaced, and so on; a cycle or a dead end
  finds no target. The slot's original object (`sys.__stdout__`) is never
  a fallback ([D10](#d10)). A live proxy of
  another scope is a normal target, so write-back output is captured by
  whichever scope is current.
- otherwise, or when write-back finds no target: the operation raises
  `ProxyExpiredError` ([D2](#d2)).

**O4. Foreign replacement is tolerated and reported.** Code under test may put
its own object in a slot during a scope (Click's `CliRunner`,
`contextlib.redirect_stdout`, IDEs, execnet's worker set-up). cot.capture does
not fight it: writes to that object are not captured. When the scope ends and
the slot does not hold the scope's proxy, the scope records a
`ForeignReplacement` diagnostic and emits a `SlotReplacedWarning` naming what
it found, and leaves the slot as it is.

**O5. Descriptors are borrowed, never owned.** cot.capture owns only the
descriptors it creates: the saved copy (`dup`) and the capture target.
Redirecting fd 1 is a borrow: flush, save, `dup2` the target over it, and give
it back with `dup2(saved, 1)`. Borrows form one LIFO stack per descriptor per
process, guarded by a lock, because the descriptor table is process-wide.
Giving back out of order raises an error that names both borrowers.

**O6. Some descriptors are off limits.** A process can declare descriptors
that capture must never borrow: execnet keeps its protocol on dups of fd 0 and
1, runsomewhere moves its protocol off fd 0 and 1 before running user code
([2.6](../research.md#26-subprocesses-execnet-and-pytest-xdist)). Borrowing a
protected descriptor fails at once.

**O7. Only the main interpreter borrows descriptors.** Subinterpreters share
the descriptor table but not Python objects, so the borrow stack lives in one
interpreter.

**O8. The library writes only where it was told to.** It never prints on its
own account; write-back (O3) and terminal streams (O9) reach a real stream
only when the caller writes to them.

**O9. Terminal streams are never captured, and are owned by whoever asked.**
A tool that draws on the terminal while capture runs (pytest's
`TerminalWriter`, `rich`, live logging, tee) gets a
[terminal stream](#terminal-streams) that reaches the uncaptured output
whatever scopes are active. Like a proxy (O1, O2) it is annotated with its
owner and closed by it, which releases its descriptor, so sessions run one
after another leak nothing.

## Proxies

A proxy is a text file object that stands in a slot for one scope.

- **Target.** It writes to the scope's capture target: an in-memory buffer at
  slot level, a file object over the borrowed descriptor at descriptor level
  ([D3](#d3)).
- **Attributes.** `encoding` and `errors` come from the object the proxy
  replaced, so captured text is encoded the way the real stream would have
  encoded it (#4389). `isatty()` is `False`. `name` and `mode` describe the
  proxy.
- **`fileno()`** returns the borrowed descriptor at descriptor level and
  raises `io.UnsupportedOperation` at slot level ([D4](#d4)).
- **stdin.** The stdin proxy is minimal: reading raises an error naming the
  scope, unless the scope was given input text, which it then serves
  ([D6](#d6)).
- **Flushing.** Before a descriptor is borrowed or given back, the session
  flushes the slot objects of this interpreter, so buffered bytes land on the
  side of the switch they were written on.

## Descriptor capture

A borrow redirects one descriptor to a temporary file ([D1](#d1)) and returns
what arrived as bytes when it ends, decoded with the replaced stream's
encoding and `errors="replace"`. Python writes reach the same file through
the descriptor-level proxy ([D3](#d3)), so the result is one byte stream in
write order, without attribution.

**Capture files** ([D8](#d8)). A host that runs a set of scopes one after
another, such as the setup, call and teardown of one test, creates one
capture file per descriptor for the set and passes them to each scope. Each
borrow appends to the file and gets back only the bytes that arrived during
it; the file stays open. One borrow at a time may write to a file: a nested
scope given a file that is in use is refused, and a refused scope undoes
whatever it had already installed. Whoever created the files closes them;
closing a file that a borrow still writes to is refused. Without capture
files, each borrow creates its own temporary file and deletes it.

## Terminal streams

`terminal("stdout", owner=...)`, `terminal("stderr", owner=...)` and
`terminal("stdin", owner=...)` return a new terminal stream on every call. The caller owns it and closes it; it is a
context manager.

- **Backing.** A private `dup` of the *uncaptured* descriptor: if fd 1 is
  borrowed when the stream is created, the dup is taken from the bottom
  borrow's saved copy, otherwise from fd 1 itself. Later borrows redirect
  fd 1, not the dup, so the stream keeps reaching the terminal.
- **Closing** releases the dup. Every stream holds exactly one, so a session
  that closes what it obtained leaves the descriptor table as it found it.
- **After closing**, writes and flushes emit a `TerminalClosedWarning` naming
  the owner and where the stream was created and closed, and are
  discarded ([D10](#d10)). The warning keeps a misplaced reference visible
  without holding a descriptor.
- **Never closed**: when an open stream is garbage-collected, it emits a
  `ResourceWarning` and releases its dup, like an unclosed file.
- **No descriptor.** If the descriptor is invalid (no console, closed fd),
  the stream discards, and the stdin stream reads end of file
  ([D10](#d10)). If a
  write or flush fails (closed descriptor, broken pipe), the stream records
  the error in `failure` and discards from then on. Writing never raises.
- **Attributes.** `fileno()` is the private dup, so `isatty()`, terminal
  size queries and colour detection see the real terminal. `encoding` and
  `errors` come from `sys.__stdout__`, defaulting to UTF-8. Writes go
  through unbuffered (`write_through`), so they interleave with captured
  output in real time.
- **stdin.** The stdin stream reads from its dup of fd 0 and cannot be
  written to. It reads unbuffered, a line at a time, so it never takes input
  from the terminal beyond what it returns, and closing it loses nothing
  that `sys.__stdin__` or a child would have read next. End of file or a
  failed read returns `""` and records the error in `failure`; after
  closing, reads warn and return `""`.
- **Debuggers.** A debugger inside a scope gets a terminal stdin and stdout,
  for example `pdb.Pdb(stdin=terminal("stdin"), stdout=terminal("stdout"))`;
  the scope's proxies and borrows stay in place ([D9](#d9)).
- **Limits.** Whatever redirected fd 1 *before* cot.capture first borrowed it
  (pytest's own capture, a shell redirect) counts as the terminal. A host
  that knows better passes its own stream to its tools instead.

## Scopes

A scope is a context manager that selects, per standard stream, a level
(`slot` or `fd`) or no capture. Entering installs its proxies and borrows its
descriptors; leaving closes the proxies (O1), gives the descriptors back (O5),
checks for foreign replacement (O4) and makes the captured text available.
Scopes nest: an inner scope installs over the outer one's proxy and restores
it on exit.

A scope is never suspended ([D9](#d9)). Whatever must reach the terminal
while a scope is active (a debugger, progress output, live logging) writes
to a [terminal stream](#terminal-streams).

There is one current scope per slot per interpreter, not per thread. Routing
output by thread or task is future research ([R2](#r2)).

## Decisions

### D1

**Descriptor capture writes to a temporary file.**

No reader thread, no deadlock when a child writes more than a pipe buffer,
survives `fork`.

*Cost:* output is only read when the borrow ends: no live tee and no
timestamps at descriptor level. A separate process as intermediate is
research topic [R1](#r1).

### D2

**A closed proxy with nowhere to write raises `ProxyExpiredError`,** after the
warning. It is an `OSError` with `errno.EBADF`, what a closed descriptor
gives, and a `ValueError`, what a closed Python file object gives, the same
double base as `io.UnsupportedOperation`. That covers both "write-back was
not requested" and "write-back found no target".

*Cost:* an exception carrying two meanings; code catching only one still
works, code distinguishing them by type cannot.

### D3

**Under descriptor capture, Python writes go through the descriptor.**

The proxy at descriptor level writes to the borrowed descriptor, so Python
and C output share one file in write order.

*Cost:* no attribution of Python writes to threads or scopes. Attribution
into sinks is research topic [R2](#r2).

### D4

**`fileno()` raises at slot level.**

Returning a descriptor would mean creating one and switching to descriptor
capture behind the user's back, too error-prone for a default.

*Cost:* code that passes `sys.stdout` to `subprocess` or `faulthandler` fails
under slot capture (#10693), as it does in pytest's `sys` mode.

### D5

**Foreign slot replacement is tolerated and reported** (O4).

*Cost:* output written to the foreign object is not captured, and a foreign
object left in the slot at scope exit stays there.

### D6

**stdin starts as a minimal proxy** that refuses to read unless given input.

*Cost:* no interactive input inside a scope; a debugger needs its own
streams ([D9](#d9)).

### D7

**Tools that draw on the terminal get a terminal stream, not the current
`sys.stdout`, and its owner closes it** (O9).

Live logging, tee and progress output need a target that is valid whether or
not capture is active and survives every scope. Holding `sys.stdout` breaks
on the first scope (#5502); a dup of the uncaptured descriptor does not.
Making each stream owned and closeable, rather than one per interpreter,
means a host that runs several sessions in one process (pytest's own
`pytester.runpytest_inprocess`, an IDE) does not accumulate descriptors.

*Cost:* one descriptor per open stream; the owner must close it, and an
fd-level redirect made before cot.capture is taken for the terminal.

### D8

**A set of scopes shares one capture file per descriptor.** The host creates
and closes the files; each borrow appends and reads back its own slice.

pytest-style hosts run three scopes per test. Creating and deleting two
temporary files per scope was measurable; sharing them across a test's
phases removes four of six file creations per test.

*Cost:* the file grows until its owner closes it, so a long-lived set holds
all of its output on disk; one writer at a time, so nested scopes need their
own files or none.

### D9

**No suspend.** A scope stays in force from entry to exit; nothing puts the
real streams back temporarily.

Suspension exists in pytest so that a debugger, `--setup-show`, live logging
and plugins can reach the terminal, and much of its bug history is about
that choreography
([1](../research.md#1-how-pytest-captures-today), #3819, #12888). A
terminal stream gives those writers the terminal without touching the
scope, so the scope's proxies and borrows never change state mid-life.

*Cost:* code that writes to `sys.stdout` or fd 1 while a scope is active is
captured, even when a person is meant to see it, unless it is given a
terminal stream. A debugger has to be constructed with terminal streams
for stdin and stdout; one that reads `sys.stdin` itself gets the scope's
stdin proxy. Output the debugged code writes while stopped is captured,
like the rest of the scope.

### D10

**No stream ever falls back to the slot's original object** (`sys.__stdout__`,
`sys.__stderr__`, `sys.__stdin__`). A terminal stream without a descriptor,
or used after closing, discards (or reads end of file); a write-back that
finds no target raises ([D2](#d2)).

The original object writes to fd 1, which descriptor capture redirects, so
output sent there lands in whatever scope holds the descriptor, not where
the fallback meant it to go. Its buffer also outlives the stream: text
written to it with fd 1 closed fails again when the interpreter flushes it
at exit, and the process exits with status 120.

*Cost:* output written through a misplaced terminal stream or a dead-end
write-back is lost, and a terminal stream in a process without a usable
descriptor shows nothing; the warnings are what remains.

## Research topics

### R1

**A second process as intermediate handler.** A service process that owns the
capture targets, reads pipes without the deadlock and `fork` hazards of an
in-process reader thread, timestamps and tees output, and manages simulated
terminals (ptys), so code under test can see a terminal whose size and
capabilities the test controls (#11270, #13322). A candidate to build on
`cot.runsomewhere`.

### R2

**Attribution into sinks.** Recording output as events
`(source, stream, payload, time, thread, interpreter, scope)` in ordered,
bounded sinks; routing by thread or task through a `ContextVar` (3.14's
`thread_inherit_context`); subinterpreters forwarding to the main
interpreter's sink; and the
ordering between slot and descriptor events
([2.4](../research.md#24-threads), #5449,
[4.2](../research.md#42-building-blocks)).

### R3

**Flushing C stdio on descriptor switches.** Calling `fflush(NULL)` through
`ctypes` so `printf` output buffered before a borrow lands on the right side
of it; availability of libc per platform, and what it costs.

Logging, warnings and faulthandler integration get their own documents.
