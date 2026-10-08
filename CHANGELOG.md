# Changelog

<!-- towncrier release notes start -->

## 0.1.1 (2026-10-07)

### Fixed

- Under the pytest binding, `capsys.disabled()` reaches the terminal: `sys.stdout` and `sys.stderr` point at terminal streams inside it. It was captured into the test's report instead. `os.write` to descriptors 1 and 2 inside it stays captured under fd capture.

## 0.1.0 (2026-10-07)

### Added

- Add `Scope.take()`, which returns and drops what a live scope captured so far, so a host can split one scope into parts.
- Add `cot.capture.overtake_pytest`, an opt-in pytest plugin (`-p cot.capture.overtake_pytest`) that captures test output with cot.capture: one scope per test, split into "Captured stdout/stderr" sections per phase, with pdb and pytest's terminal writer on terminal streams.
