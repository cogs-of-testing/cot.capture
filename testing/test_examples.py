"""docs/examples.md is executed, so its examples cannot drift from the code.

The page marks what it shows with HTML comments that render as nothing:

- ``<!-- file: NAME -->`` before a code block writes the block to ``NAME``;
- ``<!-- run: COMMAND -->`` before a ``text`` block runs ``COMMAND`` (``python``
  or ``pytest``, optionally ``< FILE`` for stdin) in a directory holding every
  file of the page, and checks its output (stdout and stderr, merged) against
  the block.

A ``...`` in an expected line matches any text, as in doctest. The output of
``python`` must match line for line; the output of ``pytest`` is long, so the
page shows an excerpt whose lines must appear in that order, and a line that
is only ``...`` marks a gap.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

DOC = Path(__file__).parent.parent / "docs" / "examples.md"

_BLOCK = re.compile(r"^<!-- (file|run): (.+?) -->\n+```[\w-]*\n(.*?)^```$", re.M | re.S)


@dataclasses.dataclass(frozen=True)
class Run:
    command: str
    expected: str
    line: int


def _parse(text: str) -> tuple[dict[str, str], list[Run]]:
    files: dict[str, str] = {}
    runs: list[Run] = []
    for match in _BLOCK.finditer(text):
        kind, arg, body = match.groups()
        if kind == "file":
            assert arg not in files, f"{arg} is shown twice"
            files[arg] = body
        else:
            line = text.count("\n", 0, match.start()) + 1
            runs.append(Run(arg, body, line))
    return files, runs


FILES, RUNS = _parse(DOC.read_text(encoding="utf-8"))


def _pattern(line: str) -> re.Pattern[str]:
    return re.compile(".*".join(re.escape(part) for part in line.split("...")))


def _check(expected: str, actual: str, *, exact: bool) -> None:
    wanted = [line.rstrip() for line in expected.splitlines()]
    lines = [line.rstrip() for line in actual.splitlines()]
    if exact:
        assert len(lines) == len(wanted), actual
        for want, line in zip(wanted, lines):
            assert _pattern(want).fullmatch(line), f"{want!r} != {line!r}\n{actual}"
        return
    found = iter(lines)
    for want in wanted:
        if want == "...":
            continue
        pattern = _pattern(want)
        assert any(pattern.fullmatch(line) for line in found), f"{want!r} missing\n{actual}"


def _run(run: Run, where: Path) -> str:
    args = shlex.split(run.command)
    stdin = b""
    if "<" in args:
        at = args.index("<")
        stdin, args = (where / args[at + 1]).read_bytes(), args[:at]
    tool, *rest = args
    command = [sys.executable, *(["-m", "pytest"] if tool == "pytest" else []), *rest]
    assert tool in ("python", "pytest"), run.command
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update(COLUMNS="80", PY_COLORS="0", PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run(
        command,
        cwd=where,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        timeout=60,
    )
    # decoded like a text stream: Windows line endings read as "\n"
    return done.stdout.decode("utf-8", "replace").replace("\r\n", "\n")


def test_the_page_has_examples() -> None:
    assert FILES and RUNS


@pytest.mark.parametrize("run", RUNS, ids=[f"L{r.line}-{r.command}" for r in RUNS])
def test_example(run: Run, tmp_path: Path) -> None:
    for name, content in FILES.items():
        (tmp_path / name).write_text(content, encoding="utf-8", newline="\n")
    # an empty ini file keeps pytest from finding configuration above tmp_path
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    _check(run.expected, _run(run, tmp_path), exact=run.command.startswith("python"))
