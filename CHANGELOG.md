# Changelog

<!-- towncrier release notes start -->

## 0.2.0 (2026-10-08)

### Added

- Add the `installed` and `closed` arguments to `capture()` and `Scope`: labels a host that drives scopes from its own machinery gives for where in its run they are entered and left, shown instead of a file and line. `Location` gains `label`, its `filename` and `lineno` are `None` for a labelled location, and `str(location)` reads `at FILE:LINE` or the label.

### Fixed

- Under the pytest binding, a proxy's history names when in the test it lived instead of a line inside the binding: `ProxyExpiredWarning` now reads `... owned by 'test_x.py::test_keep', installed before setup, closed after teardown` (`before collection`/`after collection` for a file's collection, `before loading conftests`/`after loading conftests` for conftests). Outside pytest, the recorded location skips `contextlib` and cot.capture's own frames, so a scope entered through an `ExitStack` names the code that entered it.

### Documentation

- Add `docs/examples.md`: what cot.capture is for, the library used directly, and the same tests run under pytest's builtin capture and under `-p cot.capture.overtake_pytest`, with their real output. Every example on the page is executed by the test suite.
- The README no longer calls the package unreleased.

## 0.1.1 (2026-10-07)

### Fixed

- Under the pytest binding, `capsys.disabled()` reaches the terminal: `sys.stdout` and `sys.stderr` point at terminal streams inside it. It was captured into the test's report instead. `os.write` to descriptors 1 and 2 inside it stays captured under fd capture.

## 0.1.0 (2026-10-07)

### Added

- Add `Scope.take()`, which returns and drops what a live scope captured so far, so a host can split one scope into parts.
- Add `cot.capture.overtake_pytest`, an opt-in pytest plugin (`-p cot.capture.overtake_pytest`) that captures test output with cot.capture: one scope per test, split into "Captured stdout/stderr" sections per phase, with pdb and pytest's terminal writer on terminal streams.
