"""Tests for py2bbr.check stage plumbing (no bitbake / network required)."""

from __future__ import annotations

from py2bbr import check
from py2bbr.check import Status, bitbake_parse, structural_check
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


_CLEAN = """\
SUMMARY = "async-pyserial"
HOMEPAGE = "https://github.com/t184256/async-pyserial"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://LICENSE;md5=abc"
SRC_URI[sha256sum] = "def"
DEPENDS += "python3-pybind11-native"
RDEPENDS:${PN} += "python3-click python3-packaging"
inherit pypi
"""


def _write(tmp_path, text: str):
    p = tmp_path / "recipe.bb"
    p.write_text(text, encoding="utf-8")
    return p


class TestStructuralCorruptionChecks:
    """The PetaLinux field corruption leaves signatures we must reject.

    The broken line looked like ``DEPENDS +=     -pybind11-native '00:00``
    ('00:00 is a timestamp residue; ``-pybind11-native`` shows the
    ``${PYTHON_PN}-`` prefix was gutted).
    """

    def test_timestamp_residue_fails(self, tmp_path):
        p = _write(
            tmp_path,
            _CLEAN.replace(
                'DEPENDS += "python3-pybind11-native"',
                "DEPENDS +=     -pybind11-native '00:00",
            ),
        )
        result = structural_check(p)
        item = next(i for i in result.items if i[0] == "timestamp residue")
        assert item[1] is Status.FAIL
        assert result.status is Status.FAIL

    def test_dep_leading_dash_token_fails(self, tmp_path):
        p = _write(
            tmp_path,
            _CLEAN.replace(
                'DEPENDS += "python3-pybind11-native"',
                "DEPENDS +=     -pybind11-native",
            ),
        )
        result = structural_check(p)
        item = next(i for i in result.items if i[0] == "dep token prefix")
        assert item[1] is Status.FAIL
        assert result.status is Status.FAIL

    def test_single_quote_imbalance_warns(self, tmp_path):
        # Apostrophe inside a double-quoted value (legit in a free-text
        # SUMMARY): double quotes stay balanced, single quotes do not.
        p = _write(tmp_path, _CLEAN.replace("python3-click", "python'3-click"))
        result = structural_check(p)
        item = next(i for i in result.items if i[0] == "balanced single-quotes")
        assert item[1] is Status.WARN
        # WARN only: an apostrophe in SUMMARY is legitimate, so imbalance alone
        # must not fail the recipe.
        assert result.status is Status.WARN

    def test_clean_recipe_passes(self, tmp_path):
        result = structural_check(_write(tmp_path, _CLEAN))
        assert result.status is Status.PASS
        for item in result.items:
            assert item[1] is Status.PASS
