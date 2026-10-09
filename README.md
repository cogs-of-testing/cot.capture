# cot.capture

Building blocks for capturing output at different levels: Python's
`sys.stdout`/`sys.stderr`/`sys.stdin` slots and the process's file
descriptors, with proxies that are closed when their scope ends and say who
created them when misused afterwards.

Early: released on PyPI as `cot-capture` (see the
[changelog](CHANGELOG.md)), and the API may still change. The design is in
[docs/design/streams.md](docs/design/streams.md); the research it starts from
is [docs/research.md](docs/research.md).

## pytest

`cot.capture.overtake_pytest` is an opt-in pytest plugin that captures test
output with cot.capture instead of pytest. Enable it per project:

```ini
[pytest]
addopts = -p cot.capture.overtake_pytest
```

`--capture=fd` (the default) and `--capture=sys` map to descriptor and slot
level; `-s` captures nothing; `tee-sys` is left to pytest. `capsys`, `capfd`
and pytest's `capturemanager` keep working. The module docstring lists what
differs from pytest's own capture.

`cot.capture.overtake_pytest_warnings` does the same for warnings,
unraisable exceptions and exceptions in threads (`-p
cot.capture.overtake_pytest_warnings`), replacing pytest's `warnings`,
`unraisableexception` and `threadexception` plugins. pytest's filters and
the warnings summary stay as they are; garbage is collected at the end of
every test, so a finalizer's error is reported in the test that caused it.
The design is [docs/design/warnings.md](docs/design/warnings.md).

## Examples

[docs/examples.md](docs/examples.md) shows the intent with worked examples:
the library used directly, and the same tests run under pytest's builtin
capture and under the binding, with their real output. `testing/test_examples.py`
runs every example on that page, so the output shown is what the code does.
