# Streams, ownership, descriptors and file objects

Draft for the design session. It proposes who owns what when output is
captured, and how capture at the file-descriptor level relates to capture at
the file-object level. Everything here is a proposal until the open questions
at the end are settled; section numbers in brackets point into
[the research](../research.md).

## The three things people call "stdout"

| Thing | Example | Scope | Created by |
|---|---|---|---|
| **descriptor** | fd 1 | the process: every thread, every interpreter, every child that inherits it | the OS, at process start, or whoever `open`s / `dup`s |
| **slot** | `sys.stdout`, a `StreamHandler.stream`, faulthandler's file, a library's cached `self.out` | wherever the reference lives | whoever assigns it |
| **file object** | the `TextIOWrapper` that `sys.__stdout__` refers to | one interpreter | the interpreter at start-up, or whoever wraps an fd |

A writer reaches output through a chain: a slot holds a file object, the file
object (through `BufferedWriter` and `FileIO`) writes to a descriptor, the
descriptor points at a file, pipe or terminal. Capture can cut the chain at
two places:

- **slot level**: put a different file object in the slot. Sees only Python
  writes that look up that slot, in that interpreter.
- **descriptor level**: `dup2` a different target over the descriptor. Sees
  everything that reaches the descriptor, from anyone, without attribution.

Almost every bug in the research is one of two mismatches between these:

1. **Early binding.** A writer copied the file object out of the slot before
   capture changed the slot, then kept writing to it after capture closed or
   replaced it ([2.1](../research.md#21-stale-references-to-sysstdoutsysstderr-the-largest-class)).
2. **Wrong level for the writer.** A slot-level capture cannot see C code,
   child processes or other interpreters; a descriptor-level capture cannot
   tell them apart ([2.2](../research.md#22-mode-specific-breakage),
   [2.5](../research.md#25-subinterpreters)).

## Ownership rules

**O1. Capture never closes what it did not open, and never closes what it
handed out.** pytest closes its capture file objects after every phase; that
is the closed-file error in #5502, #5282 and #3344. Anything cot.capture puts
in a slot stays a valid, writable object until the interpreter exits.

**O2. Slots are filled once, then switched inside.** cot.capture installs one
proxy file object per standard slot per interpreter, and redirects by
changing the proxy's target, never by putting another object in the slot.
Early-bound references therefore follow the current capture.

**O3. Descriptors are borrowed, never owned.** cot.capture owns only the
descriptors it creates: the saved copy (`dup`) and the capture target.
Redirecting fd 1 is a borrow: save, `dup2` the target over it, and give it
back with `dup2(saved, 1)`. Borrows form one LIFO stack per descriptor per
process, guarded by a lock, because the descriptor table is process-wide.
Giving back out of order is an error that names both borrowers, not a silent
corruption.

**O4. Some descriptors are off limits.** A process can declare descriptors
capture must never borrow: execnet's protocol lives on dups of fd 0 and 1,
runsomewhere moves its protocol off fd 0 and 1 before running user code
([2.6](../research.md#26-subprocesses-execnet-and-pytest-xdist)). Borrowing a
protected descriptor fails at once.

**O5. Only the main interpreter borrows descriptors.** Subinterpreters share
the descriptor table but not Python objects, so one stack can only live in
one interpreter. A subinterpreter installs its own slot proxies and sends its
events to the main interpreter's sink; it never calls `dup2`.

**O6. Foreign slot replacement is tolerated, not fought.** Code under test may
put its own object in `sys.stdout` (Click's `CliRunner`,
`contextlib.redirect_stdout`, IDEs, execnet's worker set-up). Writes to that
object are not ours. When it puts ours back, capture resumes. cot.capture
never re-installs over a foreign object.

**O7. The library never writes to a stream it was not given.** It renders and
returns; the host decides what reaches the terminal (as in
`cot.config.ingest` I4).

## The proxy

One `StreamProxy` per standard slot (`stdout`, `stderr`, `stdin`) per
interpreter, installed by the session.

- **Target resolution per write.** Each `write` looks up the current scope in
  a `ContextVar`; with no scope it writes to the *fallback*: the object that
  was in the slot when the proxy was installed.
- **Real stream attributes.** `encoding`, `errors`, `newlines`, `name`,
  `mode`, `isatty()`, `buffer` come from the fallback, so libraries that
  inspect the stream see the real terminal's answers
  ([2.2](../research.md#22-mode-specific-breakage), #4389, #11270).
- **`fileno()`** is the open question [Q3](#q3-proxy-fileno-with-no-descriptor-capture).
- **Flush before every borrow and give-back.** A buffered file object holds
  bytes destined for the descriptor it wrapped; `dup2` under it sends them to
  the wrong target. The session flushes every proxy and the fallbacks in this
  interpreter before switching a descriptor. Buffers in other interpreters
  and in C stdio are [Q5](#q5-flush-c-stdio-on-descriptor-switches).

## Descriptor capture

A borrow redirects one descriptor to a target and records what arrives as
`raw` bytes. Python writes do not take this path while a proxy is installed
([Q2](#q2-python-writes-under-descriptor-capture)), so what arrives is what
only the descriptor level can see: C extensions, child processes,
subinterpreters, `os.write`. It is not attributed to a thread or scope.

The target is [Q1](#q1-descriptor-target-temporary-file-or-pipe).

## Events and the sink

A sink is an ordered, bounded list of events
`(source, stream, payload, time, thread, interpreter, scope)`:

- `source`: `slot`, `fd`, later `logging`, `warnings`, `remote`;
- `payload`: `str` from slots, `bytes` from descriptors; views decode
  `bytes` with the fallback's encoding and `errors="replace"`;
- ordering is exact among slot events and approximate between slot and
  descriptor events.

Events use only values a runsomewhere channel carries (`None`, `bool`, `int`,
`float`, `str`, `bytes`, tuples, lists, dicts), so a worker can forward them
unchanged ([4.3](../research.md#43-concessions-for-execnet-and-cotrunsomewhere)).

## Scopes and threads

A scope is a context manager that selects levels and a sink. It sets the
`ContextVar` for its block and, if it asks for descriptors, borrows them.
Scopes nest; the inner one wins for slot writes in its context; descriptor
borrows stack per O3.

Threads see the scope that is current in their context. On 3.14 with
`thread_inherit_context` that is the scope they were started in; otherwise a
thread started with `contextvars.copy_context().run` carries it; any other
thread writes to the fallback, or to a session-level sink if one is set
([2.4](../research.md#24-threads)).

## Out of scope for this document

Logging, warnings, faulthandler and remote forwarding are sources and sinks
on top of this model; they get their own documents once the questions below
are settled.

## Open questions

### Q1. Descriptor target: temporary file or pipe

- **temporary file** *(recommended)*: no reader thread, no deadlock when a
  child writes more than a pipe buffer, survives `fork`; output is read when
  the borrow ends, so there are no timestamps and no live tee at this level.
- **pipe with a reader thread** (wurlitzer's approach): live data and
  timestamps; a stalled reader blocks the writer once the pipe buffer (64 KiB
  on Linux) fills, and a forked child writes into a pipe nobody reads.

### Q2. Python writes under descriptor capture

- **straight from the proxy to the sink** *(recommended)*: Python writes stay
  attributed and ordered; the descriptor only carries what Python could not
  see.
- **through the descriptor**: one byte stream in true write order between
  Python and C, but no attribution at all, which is pytest's `fd` mode.

### Q3. Proxy `fileno()` with no descriptor capture

- **return the fallback's descriptor** *(recommended)*: `subprocess`,
  `faulthandler` and C code keep working (#10693); what they write escapes
  slot capture, which is honest.
- **raise `io.UnsupportedOperation`**: what pytest's `sys` mode does; callers
  break instead of escaping.

### Q4. Foreign slot replacement during a scope

- **tolerate and report** *(recommended)*: O6, plus a diagnostic on the
  scope saying output went to a foreign object while it was active.
- **tolerate silently.**

### Q5. Flush C stdio on descriptor switches

- **yes, `fflush(NULL)` via `ctypes` where libc is available**
  *(recommended)*: C `printf` output buffered before a borrow lands in the
  right place. Costs a `ctypes` dependency on the path and does nothing on
  platforms without a findable libc.
- **no**: C output may appear in the next scope.

### Q6. stdin in the first cut

- **yes, minimal** *(recommended)*: a proxy that raises a clear error on read
  inside a scope unless the scope supplies input.
- **defer** until after stdout and stderr work.
