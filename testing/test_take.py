"""Splitting one active scope into parts with take(), as a test's phases."""

from __future__ import annotations

import os
import sys

import pytest

from cot.capture import Level, capture


@pytest.mark.parametrize("level", ["slot", "fd"])
def test_take_splits_without_replacing_the_proxy(level: Level) -> None:
    with capture(stdout=level, stderr=level) as scope:
        proxy = sys.stdout
        print("one")
        sys.stderr.write("err one\n")
        first = scope.take()
        print("two")
        second = scope.take()
        assert sys.stdout is proxy
        print("three")
    assert first == ("one\n", "err one\n")
    assert second == ("two\n", "")
    assert (scope.out, scope.err) == ("three\n", "")


def test_take_includes_descriptor_writes() -> None:
    with capture(stdout="fd", stderr=None) as scope:
        os.write(1, b"a\r\n")
        assert scope.take() == ("a\n", "")
        os.write(1, b"b\n")
    assert scope.out == "b\n"


def test_take_keeps_a_split_character_whole() -> None:
    with capture(stdout="fd", stderr=None) as scope:
        data = "é".encode()
        os.write(1, data[:1])
        assert scope.take() == ("", "")
        os.write(1, data[1:])
    assert scope.out == "é"
