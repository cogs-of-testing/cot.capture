# Logging

How a scope captures `logging` records. Issue #7. This is a design draft. The
pull request that carries it grows into the implementation.
Section numbers in brackets point into [the research](../research.md). Rules
O1 to O9 and decisions D1 to D12 are in [streams.md](streams.md).

## What there is to capture

A log record reaches a handler through a chain: `Logger._log` checks the
logger's effective level and `logging.disable`. It then builds the record with
the **record factory** (`logging.setLogRecordFactory`) and runs
`Logger.handle`, which applies the logger's filters and calls the handlers of
the logger and of its ancestors up to the first one with `propagate = False`.
All of this is module state, once per interpreter, and nothing in it is per
thread or per context on any build ([2.4](../research.md#24-threads)).

The state a capture could touch:

| State | Scope | Who changes it |
|---|---|---|
| the root logger's handlers | one interpreter | `logging.basicConfig`, `dictConfig`, applications, pytest per test |
| any logger's handlers and `propagate` | one interpreter | libraries, applications |
| logger levels and `logging.disable` | one interpreter | `caplog.set_level`, `log_level`, code under test |
| the record factory | one interpreter | rarely: structured-logging libraries |
| a `StreamHandler`'s stream | wherever the handler is | covered by [streams.md](streams.md) (O3: a stale proxy warns) |

pytest attaches its handlers to the root logger, and also to every logger that
is non-propagating at the moment it attaches. It also lowers the root logger's
level for each test. Most of its logging bugs come from that
([2.7](../research.md#27-logging-capture)): a logger that becomes
non-propagating later is missed (#7335), levels leak (#7904, #7656), and
records are kept without bound (#8307, #9215).

## Rules

**L1. Capture never changes what is logged.** A scope does not lower or raise
logger levels, does not call `logging.disable`, and does not remove other
handlers. A scope sees the records that the application's own configuration
lets through. Levels are changed only when the scope is asked to, as in L4.

**L2. One router per interpreter, scopes switch its target.** The first
logging scope installs a single router; nested scopes do not install another.
Each record the router sees goes to the current scope. When the outermost
scope ends, the router is removed (O1). Nested scopes stack and restore, as
proxies do ([Scopes](streams.md#scopes)). The router is annotated like a proxy
(O2). If it is no longer installed when the outermost scope ends, the scope
reports a `ForeignReplacement` (O4); `logging.config.dictConfig` with
`disable_existing_loggers` or a reset of the root handlers does this.

The router is a handler, attached the way pytest PR #15138 does it (Ronny's
fix for pytest #15064, open on 2026-10-08):

- The router itself sits on the root logger.
- Each logger that is non-propagating when a scope is entered gets a small
  **stand-in** handler instead of the router. At emit time the stand-in
  walks up from its logger while `propagate` is true. It forwards to the
  router only if that walk ends below the root, at a logger that nothing
  else delivers on. Otherwise the root, or a stand-in further up, delivers
  the record. Each record therefore reaches the router once, even after a
  logger starts propagating again in the middle of a scope (the duplicate
  in #15064, which came from attaching the handler itself, #14375).
- The router's level and filters apply exactly once. The stand-in has none
  of its own. It overrides `handle()`, not `emit()`, and takes no lock, as
  `logging.NullHandler` does.
- Each scope entry scans again. On exit, only what was added is removed.

The known gap is shared with pytest: a logger that turns non-propagating
while a scope is active is missed, unless a logger at or below it was
already non-propagating when the scope was entered. Keeping stand-ins on
every logger would close the gap, but it cost 1.4 to 1.5 times as much per
emitted record in #15138's measurements.

**L3. Records, not text.** A scope keeps the `LogRecord`s it captured and
nothing else ([LD2](#ld2)). How they are formatted is up to the host. A record already carries its thread name
and time, so records from several threads stay distinguishable after the fact
(this differs from streams, [D3](streams.md#d3)).

**L4. A level the scope sets is put back, and a level changed under it is
reported.** A scope can be given levels to apply for its lifetime
(`levels={"": "DEBUG", "app.db": "INFO"}`, where `""` is the root logger). It
sets them on entry and restores the previous values on exit. If a logger's
level is not what the scope set when the scope ends, the scope reports it and
leaves it, like a foreign replacement (O4). A capture threshold that only
filters what the scope keeps (`threshold=`) is separate and never touches
loggers. This is the split pytest had to reach through #7133, #7159, #7569
and #7672.

**L5. One current scope per interpreter, as for streams.** A record from any
thread goes to the scope that is current when it is emitted (W6 in
[warnings.md](warnings.md) and [Scopes](streams.md#scopes)). Routing by thread
or task belongs with [R2](streams.md#r2).

**L6. Installed per interpreter.** Logging configuration is module state, so a
subinterpreter has its own ([2.5](../research.md#25-subinterpreters)). Whoever
creates the subinterpreter enters a scope inside it.

**L7. `take()` splits a scope.** As with [D12](streams.md#d12), a host takes
the records at the end of setup, call and teardown instead of running three
scopes. That also covers teardown records when a fixture raises (#12203),
because the scope stays in force until the test's last part ends.

**L8. Live output is the host's handler on a terminal stream.** A host that
shows records while a scope is active (pytest's `log_cli`) passes its own
handler to the scope (`handlers=`), which hands it every record the scope
receives, with the handler's own level and filters, so it also sees
non-propagating loggers. The handler writes to a
[terminal stream](streams.md#terminal-streams). Nothing is
suspended ([D9](streams.md#d9)), so live logging does not touch `sys.stdout`
(#10553) and is not captured.

## Decisions

### LD1

**The router does not lower levels to see more** (L1).

pytest lowers the root logger's level so that `caplog` sees `DEBUG` records.
Doing that silently changes what the code under test logs everywhere else
(other handlers, files), and is the source of the level leaks in 2.7.

*Cost:* a scope only sees records at or above the application's effective
levels. A test that wants `DEBUG` records asks for them with `levels=` (L4).
A pytest binding that keeps `log_level` working does the same per test.

### LD2

**The router stores records only; it never writes to a stream.** pytest's
`LogCaptureHandler` is a `StreamHandler` over a `StringIO`. It formats every
record into the text buffer as it arrives, and also keeps the record. The
router appends the record and stops there. Text (the report section,
`caplog.text`) is formatted from the records when someone reads it
(Ronny, 2026-10-08).

That removes the formatting cost of records nobody reads, keeps one copy
instead of two, and lets the reader pick the format.

*Cost:* a record is formatted when it is read, not when it is logged. If an
argument is a mutable object that changes later, the text shows the later
state. A formatting error surfaces at read time, in the reader. Records keep
their `exc_info`, and with it the traceback's frames, alive as long as the
scope's records are kept.

### LD3

**What a scope keeps is decided by a discard policy.** The scope hands each
batch of records to a `DiscardPolicy` when the batch is complete: at
`take()` and at the end of the scope. The policy is told what the host knows
about the batch, and decides whether the records are kept or thrown away.
For example, a pytest binding can drop the records of a passing phase that
nobody asked for, and keep everything when the phase failed. The core
default keeps everything. A policy can also cap how many records a batch
holds (#8307, #9215).

*Cost:* records that were thrown away are gone. A report can't show them,
and a `caplog` read after the decision finds nothing. The decision is made
per batch, so the memory a single long phase uses is bounded only by a cap.

## The API

```python
with capture_logs(levels={"": "DEBUG"}, threshold="INFO",
                  discard=DiscardPolicy(keep=..., max_records=10_000)) as logs:
    ...
    setup = logs.take(passed=True)   # the batch so far, or [] if discarded
    ...
logs.records       # the last batch, as the policy decided at the end
logs.dropped       # records dropped by max_records
logs.diagnostics   # ForeignReplacement for a level changed or a router removed
```

`LevelChangedWarning` reports a level changed under the scope (L4);
`SlotReplacedWarning` a router removed by someone else (L2).

## The pytest binding

This is host policy, not core. It is split in two, as in
[warnings.md](warnings.md#the-pytest-binding): cot.capture ships the pytest
parts of its own capture in `cot.capture.overtake_pytest_logging`, and
cot.pytest is the glue that switches the parts on and owns what spans several
cot packages.

To replace pytest's `logging` plugin, the cot.capture part:

- runs one scope per test, split with `take()` (L7), and adds a `log` report
  section per phase with `item.add_report_section`;
- provides `caplog` with its public API (`records`, `record_tuples`,
  `messages`, `text`, `get_records(when)`, `clear`, `set_level`,
  `at_level`, `filtering`), with `set_level` and `at_level` going through
  L4;
- applies `log_level` as the scope's `levels` (LD1), and `log_format`,
  `log_date_format` when formatting;
- adds `log_cli` as a handler on a terminal stream (L8), and `log_file` as a
  plain `FileHandler` for the session;
- declares the `--log-*` options, the `log_*` ini keys and `--log-disable`
  through cot.config.ingest, whose acceptance test already models exactly
  this set. pytest imports its logging plugin, and declares those options,
  before any `-p` plugin loads, so the binding adopts them
  (`add_config(..., adopt=True)`, cot.config.ingest P9);
- sets the scopes' `DiscardPolicy` (LD3).

cot.pytest provides replacement objects wherever pytest or a plugin looks up
the old plugin by name, for example `logging-plugin`, which `subtests` reads
for `log_level`
([replacement research 4.2](../research-pytest-replacement.md#42-logging-logging)).
