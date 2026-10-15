import subprocess

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
    def test_timestamp_residue_fails(self, tmp_path):
        """
        Note: A real problem in an early version of py2bbr.
        """
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


class TestStructuralEdges:
    def test_unreadable(self, tmp_path):
        r = structural_check(tmp_path / "missing.bb")
        assert r.status is Status.ERROR and "cannot read" in r.detail

    def test_empty_recipe(self, tmp_path):
        r = structural_check(_write(tmp_path, "   \n"))
        assert r.status is Status.FAIL and "empty" in r.detail

    def test_optional_missing_warns(self, tmp_path):
        p = _write(tmp_path, 'SUMMARY = "x"\nLICENSE = "MIT"\ninherit pypi\n')
        r = structural_check(p)
        assert r.status is Status.WARN


class TestVendoredStructural:
    def test_vendored_form_without_pypi_class_passes(self, tmp_path):
        text = _CLEAN.replace(
            "inherit pypi\n",
            'SRC_URI = "file://pyvizionsdk-26.9.1.tar.gz"\n'
            'S = "${WORKDIR}/pyvizionsdk-26.9.1"\n',
        )
        result = structural_check(_write(tmp_path, text))
        assert result.status is Status.PASS
        assert "inherit pypi" not in [i[0] for i in result.items]

    def test_vendored_form_requires_s(self, tmp_path):
        text = _CLEAN.replace(
            "inherit pypi\n", 'SRC_URI = "file://pyvizionsdk-26.9.1.tar.gz"\n'
        )
        result = structural_check(_write(tmp_path, text))
        item = next(i for i in result.items if i[0] == "S (vendored)")
        assert item[1] is Status.FAIL
        assert result.status is Status.FAIL

    def test_unbalanced_double_quotes_fail(self, tmp_path):
        p = _write(
            tmp_path,
            _CLEAN.replace(
                'RDEPENDS:${PN} += "python3-click python3-packaging"',
                'RDEPENDS:${PN} += "python3-click python3-packaging',
            ),
        )
        r = structural_check(p)
        item = next(i for i in r.items if i[0] == "balanced quotes")
        assert item[1] is Status.FAIL

    def test_comment_unbalanced_quotes_skipped(self, tmp_path):
        p = _write(tmp_path, _CLEAN + "# it's \"unbalanced\n")
        r = structural_check(p)
        item = next(i for i in r.items if i[0] == "balanced quotes")
        assert item[1] is Status.PASS

    def test_unbalanced_braces_warn(self, tmp_path):
        p = _write(tmp_path, _CLEAN + 'X = "${UNCLOSED"\n')
        r = structural_check(p)
        item = next(i for i in r.items if i[0] == "balanced braces")
        assert item[1] is Status.WARN

    def test_dep_line_without_append_ignored(self, tmp_path):
        p = _write(tmp_path, _CLEAN + "DEPENDS = -bad\n")
        r = structural_check(p)
        item = next(i for i in r.items if i[0] == "dep token prefix")
        assert item[1] is Status.PASS

    def test_comment_single_quote_ignored(self, tmp_path):
        p = _write(tmp_path, _CLEAN + "# don't\n")
        r = structural_check(p)
        item = next(i for i in r.items if i[0] == "balanced single-quotes")
        assert item[1] is Status.PASS


class TestResultOk:
    def test_ok_property(self):
        assert check.CheckResult("s", Status.PASS).ok
        assert check.CheckResult("s", Status.WARN).ok
        assert check.CheckResult("s", Status.SKIP).ok
        assert not check.CheckResult("s", Status.FAIL).ok
        assert not check.CheckResult("s", Status.ERROR).ok


class TestAvailability:
    def test_bitbake_not_found(self, monkeypatch):
        monkeypatch.setattr(check.shutil, "which", lambda c: None)
        ok, reason = check._bitbake_available()
        assert not ok and "bitbake" in reason

    def test_bitbake_no_builddir(self, monkeypatch):
        monkeypatch.setattr(check.shutil, "which", lambda c: "/bin/bitbake")
        monkeypatch.delenv("BUILDDIR", raising=False)
        ok, reason = check._bitbake_available()
        assert not ok and "BUILDDIR" in reason

    def test_bitbake_ok(self, monkeypatch):
        monkeypatch.setattr(check.shutil, "which", lambda c: "/bin/bitbake")
        monkeypatch.setenv("BUILDDIR", "/bd")
        assert check._bitbake_available() == (True, "")

    def test_checklayer_not_found(self, monkeypatch):
        monkeypatch.setattr(check.shutil, "which", lambda c: None)
        ok, reason = check._checklayer_available()
        assert not ok and "yocto-check-layer" in reason

    def test_checklayer_no_builddir(self, monkeypatch):
        monkeypatch.setattr(check.shutil, "which", lambda c: "/tool")
        monkeypatch.delenv("BUILDDIR", raising=False)
        assert not check._checklayer_available()[0]

    def test_checklayer_ok(self, monkeypatch):
        monkeypatch.setattr(check.shutil, "which", lambda c: "/tool")
        monkeypatch.setenv("BUILDDIR", "/bd")
        assert check._checklayer_available() == (True, "")

    def test_kas_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(check.shutil, "which", lambda c: None)
        assert not check._kas_available(tmp_path / "k.yml")[0]

    def test_kas_no_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(check.shutil, "which", lambda c: "/kas")
        ok, reason = check._kas_available(None)
        assert not ok and "no kas" in reason
        ok, reason = check._kas_available(tmp_path / "missing.yml")
        assert not ok and "no kas" in reason

    def test_kas_ok(self, monkeypatch, tmp_path):
        f = tmp_path / "k.yml"
        f.write_text("header:\n  version: 5\n")
        monkeypatch.setattr(check.shutil, "which", lambda c: "/kas")
        assert check._kas_available(f) == (True, "")


class TestRunCmd:
    def test_success(self, monkeypatch):
        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, "out", "err")

        monkeypatch.setattr(check.subprocess, "run", fake_run)
        code, out = check._run_cmd(["x"], None, 5)
        assert code == 0 and "out" in out and "err" in out

    def test_stdout_only(self, monkeypatch):
        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, "out", None)

        monkeypatch.setattr(check.subprocess, "run", fake_run)
        assert check._run_cmd(["x"], None, 5) == (0, "out")

    def test_timeout(self, monkeypatch):
        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 5)

        monkeypatch.setattr(check.subprocess, "run", fake_run)
        assert check._run_cmd(["x"], None, 5) == (124, "timed out after 5s")

    def test_exec_failure(self, monkeypatch):
        def fake_run(cmd, **kw):
            raise FileNotFoundError("nope")

        monkeypatch.setattr(check.subprocess, "run", fake_run)
        code, msg = check._run_cmd(["x"], None, 5)
        assert code == 127 and "nope" in msg


class TestStages:
    def test_parse_failure(self, monkeypatch):
        monkeypatch.setattr(
            check, "_run_cmd", lambda *a, **k: (1, "line\nerror here")
        )
        r = bitbake_parse(_meta(), "/bd", 60)
        assert r.status is Status.FAIL and "error here" in r.detail

    def test_checklayer_pass(self, monkeypatch):
        monkeypatch.setattr(check, "_run_cmd", lambda *a, **k: (0, "ok"))
        assert check.checklayer("/layer", "/bd", 60).status is Status.PASS

    def test_checklayer_fail(self, monkeypatch):
        monkeypatch.setattr(check, "_run_cmd", lambda *a, **k: (2, "bad"))
        assert check.checklayer("/layer", "/bd", 60).status is Status.FAIL

    def test_checklayer_error(self, monkeypatch):
        monkeypatch.setattr(check, "_run_cmd", lambda *a, **k: (3, "weird"))
        assert check.checklayer("/layer", "/bd", 60).status is Status.ERROR

    def test_kas_build_pass(self, monkeypatch):
        monkeypatch.setattr(check, "_run_cmd", lambda *a, **k: (0, "ok"))
        assert check.kas_build("/k.yml", "/wd", 60).status is Status.PASS

    def test_kas_build_fail(self, monkeypatch):
        monkeypatch.setattr(check, "_run_cmd", lambda *a, **k: (1, "boom"))
        r = check.kas_build("/k.yml", "/wd", 60)
        assert r.status is Status.FAIL and "exit 1" in r.detail


class TestRunChecks:
    def _patch_all_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            check, "_bitbake_available", lambda: (False, "no bitbake")
        )
        monkeypatch.setattr(
            check, "_checklayer_available", lambda: (False, "no tool")
        )
        monkeypatch.setattr(
            check, "_kas_available", lambda f: (False, "no kas")
        )

    def test_invalid_level_defaults(self, tmp_path, monkeypatch):
        self._patch_all_unavailable(monkeypatch)
        results = check.run_checks(
            _meta(), _write(tmp_path, _CLEAN), None, None, level="bogus"
        )
        assert [r.level for r in results] == ["structural", "parse"]

    def test_all_skipped(self, tmp_path, monkeypatch):
        p = _write(tmp_path, _CLEAN)
        kfile = tmp_path / "py2bbr.kas.yml"
        kfile.write_text("header:\n  version: 5\n")
        self._patch_all_unavailable(monkeypatch)
        results = check.run_checks(
            _meta(), p, tmp_path / "layer", kfile, level="build"
        )
        assert [r.status for r in results[1:]] == [
            Status.SKIP,
            Status.SKIP,
            Status.SKIP,
        ]
        assert (tmp_path / "kas-workdir").is_dir()

    def test_checklayer_skipped_without_layer(self, tmp_path, monkeypatch):
        p = _write(tmp_path, _CLEAN)
        self._patch_all_unavailable(monkeypatch)
        results = check.run_checks(_meta(), p, None, None, level="checklayer")
        assert results[2].status is Status.SKIP
        assert "no layer" in results[2].detail


class TestRunChecksAvailable:
    def _run(self, tmp_path, monkeypatch, level, **kwargs):
        monkeypatch.setattr(
            check, "_bitbake_available", lambda: (True, "bitbake found")
        )
        monkeypatch.setattr(
            check, "_checklayer_available", lambda: (True, "check-layer found")
        )
        monkeypatch.setattr(check, "_kas_available", lambda f: (True, "kas"))
        monkeypatch.setattr(check, "_run_cmd", lambda *a, **k: (0, "ok"))
        return check.run_checks(
            _meta(),
            _write(tmp_path, _CLEAN),
            tmp_path / "layer",
            tmp_path / "py2bbr.kas.yml",
            level=level,
            workdir=tmp_path / "wd",
            **kwargs,
        )

    def test_parse_runs_bitbake(self, tmp_path, monkeypatch):
        results = self._run(tmp_path, monkeypatch, "parse")
        assert results[1].status is Status.PASS
        assert "bitbake parsed" in results[1].detail

    def test_checklayer_runs_the_tool(self, tmp_path, monkeypatch):
        results = self._run(tmp_path, monkeypatch, "checklayer")
        assert results[2].status is Status.PASS
        assert "check-layer" in results[2].detail

    def test_build_runs_kas(self, tmp_path, monkeypatch):
        results = self._run(tmp_path, monkeypatch, "build")
        assert results[3].status is Status.PASS
        assert "kas build" in results[3].detail


class TestRunCmdEnv:
    def test_extra_env_is_merged(self, monkeypatch):
        seen = {}

        def fake_run(cmd, cwd, env, **kwargs):
            seen.update(env)
            return subprocess.CompletedProcess(cmd, 0, "out", "")

        monkeypatch.setattr(check.subprocess, "run", fake_run)
        monkeypatch.setenv("PY2BBR_SENTINEL", "from-parent")
        code, out = check._run_cmd(
            ["true"], cwd=None, timeout=5, extra_env={"BB_ENV_PY2BBR": "extra"}
        )
        assert (code, out) == (0, "out")
        # The caller's overrides win over the inherited environment.
        assert seen["BB_ENV_PY2BBR"] == "extra"
        assert seen["PY2BBR_SENTINEL"] == "from-parent"
