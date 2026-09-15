"""Tests for py2bbr.check stage plumbing (no bitbake / network required)."""

from __future__ import annotations

from py2bbr import check
from py2bbr.check import Status, bitbake_parse
from py2bbr.model import PackageMetadata


def _meta() -> PackageMetadata:
    return PackageMetadata(
        name="pyserial", canonical_name="pyserial", version="3.5"
    )


class TestBitbakeParse:
    def test_uses_bitbake_parse_only_flag(self, monkeypatch):
        calls: list[list[str]] = []

        def fake_run_cmd(cmd, cwd, timeout, extra_env=None):
            calls.append(cmd)
            return 0, "parsed"

        monkeypatch.setattr(check, "_run_cmd", fake_run_cmd)
        result = bitbake_parse(_meta(), "/nonexistent-builddir", 60)

        # bitbake's real parse-only mode is '-p' / '--parse-only'.
        # '-c parse' selects a do_parse task that does not exist, so it can
        # never succeed ("ERROR: Task do_parse does not exist ...").
        assert calls and calls[0] == ["bitbake", "-p", "python3-pyserial"]
        assert result.status is Status.PASS
