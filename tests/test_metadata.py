"""Tests for py2bbr.metadata (pure functions, no network)."""

from __future__ import annotations

import email
import gzip
import hashlib
import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from py2bbr import metadata as M
from py2bbr.errors import MetadataError, MetadataNotFoundError
from py2bbr.metadata import (
    FetchedSource,
    PipOptions,
    _marker_env,
    _pick_artifact,
    classify_requires_dist,
    detect_license,
)
from py2bbr.model import Dependency, DepKind, SourceKind
from py2bbr.yocto import resolve_yocto_version


def _msg(text: str) -> email.message.Message:
    return email.message_from_string(text)


class TestPipOptions:
    def test_build_args_default_empty(self):
        assert PipOptions().build_args() == []

    def test_build_args_full(self):
        opts = PipOptions(
            index_url="https://pypi.example/simple",
            extra_index_urls=["https://extra1", "https://extra2"],
            trusted_hosts=["pypi.example"],
            pip_args=["--retries", "3"],
            timeout=30,
        )
        args = opts.build_args()
        assert args[:2] == ["--index-url", "https://pypi.example/simple"]
        assert ["--extra-index-url", "https://extra1"] == args[2:4]
        assert ["--extra-index-url", "https://extra2"] == args[4:6]
        assert ["--trusted-host", "pypi.example"] == args[6:8]
        assert ["--timeout", "30"] == args[8:10]
        assert args[-2:] == ["--retries", "3"]

    def test_describe(self):
        opts = PipOptions(extra_index_urls=["https://extra1"])
        assert opts.describe() == ["--extra-index-url=https://extra1"]


class TestPickArtifact:
    def test_prefers_sdist(self, tmp_path: Path):
        (tmp_path / "pkg-1.0.tar.gz").write_bytes(b"sdist")
        (tmp_path / "pkg-1.0-py3-none-any.whl").write_bytes(b"wheel")
        assert _pick_artifact(tmp_path, prefer_sdist=True).name.endswith(
            ".tar.gz"
        )

    def test_falls_back_to_wheel(self, tmp_path: Path):
        (tmp_path / "pkg-1.0-py3-none-any.whl").write_bytes(b"wheel")
        assert _pick_artifact(tmp_path, prefer_sdist=True).suffix == ".whl"

    def test_none_when_empty(self, tmp_path: Path):
        assert _pick_artifact(tmp_path, prefer_sdist=True) is None

    def test_tar_gz_detected_via_name(self, tmp_path: Path):
        # Regression: Path.suffix for x.tar.gz is '.gz', must still be found.
        (tmp_path / "black-26.5.1.tar.gz").write_bytes(b"x")
        assert _pick_artifact(tmp_path, prefer_sdist=True) is not None


class TestClassifyRequiresDist:
    def setup_method(self):
        self.y = resolve_yocto_version("5.0")  # Python 3.12

    def test_plain_dep_runtime(self):
        runtime, _build, _dropped = classify_requires_dist(
            ["click>=8.0"], self.y
        )
        assert [d.name for d in runtime] == ["click"]

    def test_extra_marker_dropped(self):
        runtime, _b, dropped = classify_requires_dist(
            ['colorama>=0.4.3; extra == "colorama"'], self.y
        )
        assert runtime == []
        assert dropped[0].reason.startswith("optional extra")

    def test_python_version_marker_dropped_on_312(self):
        runtime, _b, dropped = classify_requires_dist(
            ['tomli>=1.1.0; python_version < "3.11"'], self.y
        )
        assert runtime == []
        assert dropped[0].name == "tomli"

    # def test_python_version_marker_kept_when_satisfied(self):
    #     # tomli is needed only for < 3.11; on a 3.9 target it should be kept.
    #     y39 = resolve_yocto_version("3.1")  # Python 3.9
    #     runtime, _b, _d = classify_requires_dist(
    #         ['tomli>=1.1.0; python_version < "3.11"'], y39
    #     )
    #     assert [d.name for d in runtime] == ["tomli"]

    def test_windows_only_dropped_on_linux_target(self):
        runtime, _b, dropped = classify_requires_dist(
            ['pywin32; sys_platform == "win32"'], self.y
        )
        assert runtime == []
        assert dropped[0].name == "pywin32"

    def test_unparseable_dropped(self):
        runtime, _b, dropped = classify_requires_dist(
            ["this is not valid !!!"], self.y
        )
        assert runtime == []
        assert dropped[0].kind is DepKind.DROPPED


class TestDetectLicense:
    def test_license_expression(self):
        msg = _msg("Metadata-Version: 2.4\nName: x\nLicense-Expression: MIT\n")
        spdx, _ = detect_license(msg)
        assert spdx == "MIT"

    def test_classifier(self):
        msg = _msg(
            "Metadata-Version: 2.1\nName: x\n"
            "Classifier: License :: OSI Approved :: Apache Software License\n"
        )
        spdx, _ = detect_license(msg)
        assert spdx == "Apache-2.0"

    def test_license_text_sniffing(self):
        msg = _msg(
            "Metadata-Version: 2.1\nName: x\nLicense: Apache License 2.0\n"
        )
        spdx, _ = detect_license(msg)
        assert spdx == "Apache-2.0"

    def test_none_detected(self):
        msg = _msg("Metadata-Version: 2.1\nName: x\n")
        spdx, _ = detect_license(msg)
        assert spdx == ""

    def test_classifier_mozilla_public_license_2(self):
        # Real jinxed 2.1.0 metadata: classifier tail "Mozilla Public License
        # 2.0 (MPL 2.0)" — must map to MPL-2.0 (PetaLinux required a LICENSE).
        msg = _msg(
            "Metadata-Version: 2.1\nName: jinxed\n"
            "Classifier: License :: OSI Approved :: "
            "Mozilla Public License 2.0 (MPL 2.0)\n"
        )
        spdx, _ = detect_license(msg)
        assert spdx == "MPL-2.0"

    def test_license_text_mplv2(self):
        # Real jinxed 2.1.0 also ships free-form "License: MPLv2.0"; the
        # keyword sniff must catch the MPLv2 shorthand.
        msg = _msg("Metadata-Version: 2.1\nName: jinxed\nLicense: MPLv2.0\n")
        spdx, _ = detect_license(msg)
        assert spdx == "MPL-2.0"


class TestMarkerEnv:
    def test_target_python_version(self):
        y = resolve_yocto_version("5.0")
        env = _marker_env(y)
        assert env["python_version"] == "3.12"
        assert env["sys_platform"] == "linux"

    def test_no_yocto_keeps_host_python(self):
        env = _marker_env(None)
        assert env["sys_platform"] == "linux"
        assert "python_version" in env


_PKG_INFO = """\
Metadata-Version: 2.1
Name: black
Version: 26.5.1
Summary: The uncompromising code formatter.
Description: A longer description of black.
Home-page: https://github.com/psf/black
License: MIT
Requires-Python: >=3.10
Project-URL: Repository, https://github.com/psf/black
Requires-Dist: click>=8.0.0
Requires-Dist: tomli>=1.1.0; python_version < "3.11"
Requires-Dist: colorama>=0.4; extra == "colorama"
"""

_PYPROJECT = """\
[build-system]
requires = ["hatchling>=1.27.0"]
build-backend = "hatchling.build"
"""


def _sdist(tmp_path: Path, members: dict[str, str]) -> Path:
    p = tmp_path / "black-26.5.1.tar.gz"
    with tarfile.open(p, "w:gz") as tf:
        for name, data in members.items():
            blob = data.encode()
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))
    return p


def _sdist_src(p: Path) -> FetchedSource:
    return FetchedSource(p, SourceKind.SDIST, "sha", p.name)


_FULL_MEMBERS = {
    "black-26.5.1/PKG-INFO": _PKG_INFO,
    "black-26.5.1/pyproject.toml": _PYPROJECT,
    "black-26.5.1/LICENSE": "MIT license text\n",
}


class TestPipExecutable:
    def test_module_preferred(self, monkeypatch):
        import importlib.util

        monkeypatch.setattr(importlib.util, "find_spec", lambda name: "pip")
        assert M._pip_executable() == [sys.executable, "-m", "pip"]

    def test_path_fallback(self, monkeypatch):
        import importlib.util

        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
        monkeypatch.setattr(
            M.shutil,
            "which",
            lambda c: "/usr/bin/pip3" if c == "pip3" else None,
        )
        assert M._pip_executable() == ["/usr/bin/pip3"]

    def test_last_resort(self, monkeypatch):
        import importlib.util

        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
        monkeypatch.setattr(M.shutil, "which", lambda c: None)
        assert M._pip_executable() == [sys.executable, "-m", "pip"]


class TestRunPipDownload:
    def test_command_shape(self, monkeypatch, tmp_path):
        captured: dict = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        monkeypatch.setattr(M, "_pip_executable", lambda: ["fake", "pip"])
        monkeypatch.setattr(M.subprocess, "run", fake_run)
        M._run_pip_download(
            "black==26.5.1",
            tmp_path,
            PipOptions(index_url="https://i", pip_args=["--x"]),
            "--no-binary=:all:",
        )
        cmd = captured["cmd"]
        assert cmd[:2] == ["fake", "pip"]
        assert "download" in cmd
        assert "black==26.5.1" in cmd
        assert "--no-binary=:all:" in cmd
        assert ["-d", str(tmp_path)] in [
            cmd[i : i + 2] for i in range(len(cmd) - 1)
        ]
        assert cmd[-3:] == ["--index-url", "https://i", "--x"]


class TestPublicSdistUrl:
    """S6: packages absent from public PyPI need vendoring, not a pypi fetch.

    py2bbr must predict whether ``inherit pypi``'s fetch URL will 404 (the
    pyvizionsdk do_fetch failure) so the recipe can vendor the sdist instead.
    The prediction re-derives that exact URL from the sdist filename.
    """

    def test_tar_gz_url_derived(self):
        assert M._public_pypi_sdist_url("pyvizionsdk-26.9.1.tar.gz") == (
            "https://files.pythonhosted.org/packages/source/"
            "p/pyvizionsdk/pyvizionsdk-26.9.1.tar.gz"
        )

    def test_dashed_name_rpartition_keeps_full_name(self):
        # Versions never contain '-' (PEP 440), so the LAST '-' always splits
        # name from version; `typed-ast` must not be cut down to `typed`.
        assert M._public_pypi_sdist_url("typed-ast-1.5.4.tar.gz") == (
            "https://files.pythonhosted.org/packages/source/"
            "t/typed-ast/typed-ast-1.5.4.tar.gz"
        )

    def test_underscore_name_kept(self):
        assert M._public_pypi_sdist_url("async_pyserial-0.2.4.tar.gz") == (
            "https://files.pythonhosted.org/packages/source/"
            "a/async_pyserial/async_pyserial-0.2.4.tar.gz"
        )

    def test_zip_sdist_maps_to_tar_gz_url(self):
        # The pypi class always fetches .tar.gz (PYPI_PACKAGE_EXT default), so
        # even a zip sdist maps to the .tar.gz URL the build will request.
        assert M._public_pypi_sdist_url("black-26.5.1.zip") == (
            "https://files.pythonhosted.org/packages/source/"
            "b/black/black-26.5.1.tar.gz"
        )

    def test_wheel_returns_none(self):
        assert M._public_pypi_sdist_url("black-26.5.1-py3-none-any.whl") is None

    def test_undecodable_returns_none(self):
        assert M._public_pypi_sdist_url("not-an-sdist.txt") is None


class TestFetchSource:
    @staticmethod
    def _writer(filename, data=b"data"):
        def fake(requirement, dest, *a, **k):
            (dest / filename).write_bytes(data)
            return subprocess.CompletedProcess([], 0, "", "")

        return fake

    def test_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            M, "_run_pip_download", self._writer("black-26.5.1.tar.gz")
        )
        src = M.fetch_source("black", "26.5.1", PipOptions(), tmp_path)
        assert src.path.name == "black-26.5.1.tar.gz"
        assert src.kind is SourceKind.SDIST
        assert src.sha256 == hashlib.sha256(b"data").hexdigest()

    def test_keep_true(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            M, "_run_pip_download", self._writer("black-26.5.1.tar.gz")
        )
        src = M.fetch_source(
            "black", "26.5.1", PipOptions(), tmp_path, keep=True
        )
        assert src.path.name == "black-26.5.1.tar.gz"

    def test_wheel_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            M,
            "_run_pip_download",
            self._writer("black-26.5.1-py3-none-any.whl"),
        )
        src = M.fetch_source("black", "26.5.1", PipOptions(), tmp_path)
        assert src.kind is SourceKind.WHEEL

    def test_wheel_fallback_warns_when_sdist_fails(
        self, tmp_path, monkeypatch, caplog
    ):
        # Regression: pip's isolated metadata build can fail environmentally
        # (e.g. ENOSPC on a full /tmp) even though the sdist exists. The
        # wheel fallback must surface pip's diagnostics, not silently lose
        # [build-system] detection.
        calls = {"n": 0}

        def fake(requirement, dest, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return subprocess.CompletedProcess(
                    [], 1, "", "ERROR: No space left on device"
                )
            (dest / "black-26.5.1-py3-none-any.whl").write_bytes(b"data")
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(M, "_run_pip_download", fake)
        with caplog.at_level("WARNING", logger="py2bbr.metadata"):
            src = M.fetch_source("black", "26.5.1", PipOptions(), tmp_path)
        assert src.kind is SourceKind.WHEEL
        assert "No space left on device" in caplog.text
        assert "[build-system]" in caplog.text

    def test_not_found(self, tmp_path, monkeypatch):
        (tmp_path / "download").mkdir()
        proc = subprocess.CompletedProcess([], 1, "", "no sdist\nno wheel")
        monkeypatch.setattr(M, "_run_pip_download", lambda *a, **k: proc)
        with pytest.raises(MetadataNotFoundError, match="no sdist"):
            M.fetch_source("nope", None, PipOptions(), tmp_path)

    def test_fetch_after_other_packages_picks_own_artifact(
        self, tmp_path, monkeypatch
    ):
        # Regression: all fetches share workdir/download, and _pick_artifact
        # used to return min() over the whole dir, so after "blessed" landed
        # every later fetch (editor, readchar, ...) resolved to blessed.
        # Each fetch must select the artifact pip placed for THIS requirement.
        def fake_download(req, dest_dir, *a, **k):
            name = req.split(">")[0].split("=")[0].strip()
            (dest_dir / f"{name}-1.0.tar.gz").write_bytes(name.encode())

        monkeypatch.setattr(M, "_run_pip_download", fake_download)
        M.fetch_source("blessed", "1.0", PipOptions(), tmp_path)
        src = M.fetch_source("readchar", "1.0", PipOptions(), tmp_path)
        assert src.filename == "readchar-1.0.tar.gz"

    def test_second_fetch_same_package_picks_new_artifact(
        self, tmp_path, monkeypatch
    ):
        # Regression: a re-run resolving a NEW upstream version must not pick
        # the artifact left in workdir/download by the previous run.
        def fake_download(req, dest_dir, *a, **k):
            version = req.split("==")[1]
            (dest_dir / f"black-{version}.tar.gz").write_bytes(version.encode())

        monkeypatch.setattr(M, "_run_pip_download", fake_download)
        M.fetch_source("black", "26.5.1", PipOptions(), tmp_path)
        src = M.fetch_source("black", "26.5.2", PipOptions(), tmp_path)
        assert src.filename == "black-26.5.2.tar.gz"

    def test_public_not_checked_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            M, "_run_pip_download", self._writer("black-26.5.1.tar.gz")
        )
        src = M.fetch_source("black", "26.5.1", PipOptions(), tmp_path)
        assert src.public_reachable is None

    def test_check_public_reachable_true(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            M, "_run_pip_download", self._writer("black-26.5.1.tar.gz")
        )
        monkeypatch.setattr(M, "_sdist_reachable", lambda url: True)
        src = M.fetch_source(
            "black", "26.5.1", PipOptions(), tmp_path, check_public=True
        )
        assert src.public_reachable is True

    def test_check_public_unreachable_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            M, "_run_pip_download", self._writer("black-26.5.1.tar.gz")
        )
        monkeypatch.setattr(M, "_sdist_reachable", lambda url: False)
        src = M.fetch_source(
            "black", "26.5.1", PipOptions(), tmp_path, check_public=True
        )
        assert src.public_reachable is False


class TestPickArtifactExtra:
    def test_sdist_when_not_preferred(self, tmp_path):
        (tmp_path / "pkg-1.0.tar.gz").write_bytes(b"x")
        assert _pick_artifact(tmp_path, prefer_sdist=False) is not None

    def test_kind_of(self, tmp_path):
        w = tmp_path / "a.whl"
        w.write_bytes(b"")
        s = tmp_path / "a.tar.gz"
        s.write_bytes(b"")
        assert M._kind_of(w) is SourceKind.WHEEL
        assert M._kind_of(s) is SourceKind.SDIST


class TestArchiveReading:
    def test_parse_metadata_sdist(self, tmp_path):
        src = _sdist_src(_sdist(tmp_path, _FULL_MEMBERS))
        msg = M.parse_metadata_file(src)
        assert msg["Name"] == "black"
        assert msg["Version"] == "26.5.1"

    def test_find_license_sdist(self, tmp_path):
        src = _sdist_src(_sdist(tmp_path, _FULL_MEMBERS))
        name, md5 = M.find_license_file(src)
        assert name == "LICENSE"
        assert md5 == hashlib.md5(b"MIT license text\n").hexdigest()

    def test_license_nested_ignored(self, tmp_path):
        src = _sdist_src(_sdist(tmp_path, {"black-26.5.1/src/LICENSE": "x"}))
        assert M.find_license_file(src) is None

    def test_license_mixed_case_extension(self, tmp_path):
        # Real numpy sdist ships top-level LICENSE.txt; the name set is
        # mixed-case while comparisons upper() the member, so lowercase
        # extensions were never matched (no LIC_FILES_CHKSUM in the recipe).
        src = _sdist_src(
            _sdist(tmp_path, {"black-26.5.1/LICENSE.txt": "MIT license text\n"})
        )
        name, md5 = M.find_license_file(src)
        assert name == "LICENSE.txt"
        assert md5 == hashlib.md5(b"MIT license text\n").hexdigest()

    def test_read_build_system_top_level(self, tmp_path):
        src = _sdist_src(_sdist(tmp_path, _FULL_MEMBERS))
        assert M.read_build_system(src) == (
            "hatchling.build",
            ["hatchling>=1.27.0"],
        )

    def test_build_system_top_level_no_bs(self, tmp_path):
        members = dict(_FULL_MEMBERS)
        members["black-26.5.1/pyproject.toml"] = "[project]\nname = 'black'\n"
        src = _sdist_src(_sdist(tmp_path, members))
        assert M.read_build_system(src) == ("", [])

    def test_build_system_no_pyproject(self, tmp_path):
        src = _sdist_src(_sdist(tmp_path, {"black-26.5.1/PKG-INFO": _PKG_INFO}))
        assert M.read_build_system(src) == ("", [])

    def test_build_system_nested_fallback(self, tmp_path):
        src = _sdist_src(
            _sdist(
                tmp_path,
                {"black-26.5.1/vendor/pyproject.toml": _PYPROJECT},
            )
        )
        assert M.read_build_system(src) == (
            "hatchling.build",
            ["hatchling>=1.27.0"],
        )

    def test_build_system_bad_toml(self, tmp_path):
        src = _sdist_src(
            _sdist(
                tmp_path,
                {"black-26.5.1/vendor/pyproject.toml": "not [valid toml"},
            )
        )
        assert M.read_build_system(src) == ("", [])
        assert M._parse_toml(b"not [valid toml") is None

    def test_wheel_paths(self, tmp_path):
        p = tmp_path / "black-26.5.1-py3-none-any.whl"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("black-26.5.1.dist-info/METADATA", _PKG_INFO)
            zf.writestr("black-26.5.1.dist-info/LICENSE", "MIT license text\n")
        src = FetchedSource(p, SourceKind.WHEEL, "sha", p.name)
        assert M.parse_metadata_file(src)["Version"] == "26.5.1"
        name, _md5 = M.find_license_file(src)
        assert name == "LICENSE"
        assert M.read_build_system(src) == ("", [])

    def test_no_metadata_raises(self, tmp_path):
        p = tmp_path / "x.tar.gz"
        p.write_bytes(gzip.compress(b"not a tarball"))
        src = FetchedSource(p, SourceKind.SDIST, "sha", "x.tar.gz")
        with pytest.raises(MetadataError, match="no PKG-INFO/METADATA"):
            M.parse_metadata_file(src)
        assert M.find_license_file(src) is None
        assert M._read_text_from_archive(p, lambda n: True) is None


class TestMessageHelpers:
    def test_first_non_string(self):
        m = email.message.Message()
        m.add_header("X", "a")
        m.add_header("X", "b")
        assert M._first(m, "X") == ""
        assert M._first(m, "Missing") == ""

    def test_project_urls_default_label(self):
        msg = _msg("Project-URL: , https://x.example\n")
        assert M._project_urls(msg) == [("Homepage", "https://x.example")]

    def test_homepage_field_wins(self):
        assert (
            M._homepage(_msg("Home-page: https://h.example\n"), [])
            == "https://h.example"
        )

    def test_homepage_label_priority(self):
        urls = [
            ("Docs", "https://d.example"),
            ("Repository", "https://r.example"),
        ]
        assert M._homepage(_msg(""), urls) == "https://r.example"

    def test_homepage_first_url_fallback(self):
        assert (
            M._homepage(_msg(""), [("Docs", "https://d.example")])
            == "https://d.example"
        )


class TestBuildMetadata:
    def test_full_flow(self, tmp_path, monkeypatch):
        src = _sdist_src(_sdist(tmp_path, _FULL_MEMBERS))
        monkeypatch.setattr(M, "fetch_source", lambda *a, **k: src)
        meta, _ = M.build_metadata(
            "black", "26.5.1", PipOptions(), tmp_path, yocto=M.default_yocto()
        )
        assert meta.name == "black"
        assert meta.version == "26.5.1"
        assert meta.license == "MIT"
        assert meta.license_file == "LICENSE"
        assert meta.build_backend == "hatchling.build"
        assert [d.name for d in meta.runtime_deps] == ["click"]
        assert [d.name for d in meta.build_deps] == ["hatchling"]
        assert [d.name for d in meta.dropped_deps] == ["tomli", "colorama"]
        assert meta.resolved_from == "https://pypi.org"

    def test_resolved_from_index(self, tmp_path, monkeypatch):
        src = _sdist_src(_sdist(tmp_path, _FULL_MEMBERS))
        monkeypatch.setattr(M, "fetch_source", lambda *a, **k: src)
        meta, _ = M.build_metadata(
            "black", "26.5.1", PipOptions(index_url="https://idx"), tmp_path
        )
        assert meta.resolved_from == "https://idx"
        assert meta.index_flags == ["--index-url=https://idx"]

    def test_resolved_from_extra_index(self, tmp_path, monkeypatch):
        src = _sdist_src(_sdist(tmp_path, _FULL_MEMBERS))
        monkeypatch.setattr(M, "fetch_source", lambda *a, **k: src)
        meta, _ = M.build_metadata(
            "black",
            "26.5.1",
            PipOptions(extra_index_urls=["https://extra"]),
            tmp_path,
        )
        assert meta.resolved_from == "https://extra"

    def test_build_deps_invalid_requirement_skipped(self):
        deps = M._build_deps_from_requires(["ok>=1", "not a requirement !!!"])
        assert [d.name for d in deps] == ["ok"]


class TestMiscMetadata:
    def test_canonical(self):
        assert M._canonical("Async_PySerial") == "async-pyserial"

    def test_default_yocto(self):
        assert M.default_yocto().version == "5.0"

    def test_describe_full(self):
        opts = PipOptions(index_url="https://i", trusted_hosts=["h"])
        assert opts.describe() == [
            "--index-url=https://i",
            "--trusted-host=h",
        ]


class TestResolveClosure:
    def _builder(self, registry, tmp_path):
        def _b(dep):
            meta = registry[dep.name]
            return meta, _sdist_src(tmp_path / "pkg.tar.gz")

        return _b

    def test_no_runtime_deps(self, make_meta_fn, tmp_path):
        top = make_meta_fn(runtime_deps=[])
        closure, warnings = M.resolve_closure(top, self._builder({}, tmp_path))
        assert closure == [top]
        assert warnings == []

    def test_bfs_order_and_dedup(self, make_meta_fn, tmp_path):
        top = make_meta_fn(
            runtime_deps=[
                Dependency("a", DepKind.RUNTIME, requirement="a>=1"),
                Dependency("b", DepKind.RUNTIME, requirement="b>=2"),
            ]
        )
        a = make_meta_fn(
            name="a",
            canonical_name="a",
            version="1",
            runtime_deps=[Dependency("c", DepKind.RUNTIME, requirement="c>=3")],
        )
        b = make_meta_fn(
            name="b", canonical_name="b", version="2", runtime_deps=[]
        )
        c = make_meta_fn(
            name="c", canonical_name="c", version="3", runtime_deps=[]
        )
        closure, warnings = M.resolve_closure(
            top, self._builder({"a": a, "b": b, "c": c}, tmp_path)
        )
        assert closure == [top, a, b, c]
        assert warnings == []

    def test_canonical_dedup(self, make_meta_fn, tmp_path):
        top = make_meta_fn(
            runtime_deps=[
                Dependency("async-pyserial", DepKind.RUNTIME),
                Dependency("async_pyserial", DepKind.RUNTIME),
            ]
        )
        member = make_meta_fn(
            name="async-pyserial",
            canonical_name="async-pyserial",
            version="1",
            runtime_deps=[],
        )
        closure, warnings = M.resolve_closure(
            top,
            self._builder(
                {"async-pyserial": member, "async_pyserial": member}, tmp_path
            ),
        )
        assert closure == [top, member]
        assert warnings == []

    def test_cycle_safe(self, make_meta_fn, tmp_path):
        a = make_meta_fn(
            name="a",
            canonical_name="a",
            version="1",
            runtime_deps=[Dependency("b", DepKind.RUNTIME)],
        )
        b = make_meta_fn(
            name="b",
            canonical_name="b",
            version="2",
            runtime_deps=[Dependency("a", DepKind.RUNTIME)],
        )
        closure, warnings = M.resolve_closure(
            a, self._builder({"a": a, "b": b}, tmp_path)
        )
        assert closure == [a, b]
        assert warnings == []

    def test_skip_member_silently_and_warn_absent(self, make_meta_fn, tmp_path):
        top = make_meta_fn(runtime_deps=[Dependency("a", DepKind.RUNTIME)])
        a = make_meta_fn(
            name="a", canonical_name="a", version="1", runtime_deps=[]
        )
        closure, warnings = M.resolve_closure(
            top, self._builder({"a": a}, tmp_path), skip=frozenset({"a"})
        )
        assert closure == [top]
        assert warnings == []

        closure2, warnings2 = M.resolve_closure(
            top, self._builder({"a": a}, tmp_path), skip=frozenset({"zzz"})
        )
        assert closure2 == [top, a]
        assert warnings2 == ["--skip-dep zzz: not in the runtime closure"]

    def test_max_depth(self, make_meta_fn, tmp_path):
        top = make_meta_fn(runtime_deps=[Dependency("a", DepKind.RUNTIME)])
        a = make_meta_fn(
            name="a",
            canonical_name="a",
            version="1",
            runtime_deps=[Dependency("c", DepKind.RUNTIME)],
        )
        c = make_meta_fn(
            name="c", canonical_name="c", version="3", runtime_deps=[]
        )
        builder = self._builder({"a": a, "c": c}, tmp_path)
        closure0, _ = M.resolve_closure(top, builder, max_depth=0)
        assert closure0 == [top]
        closure1, _ = M.resolve_closure(top, builder, max_depth=1)
        assert closure1 == [top, a]
        closure_all, _ = M.resolve_closure(top, builder)
        assert closure_all == [top, a, c]

    def test_builder_receives_raw_requirement(self, make_meta_fn, tmp_path):
        seen = []
        top = make_meta_fn(
            runtime_deps=[
                Dependency("a", DepKind.RUNTIME, requirement="a>=1.0")
            ]
        )

        def _b(dep):
            seen.append(dep.requirement)
            member = make_meta_fn(
                name=dep.name,
                canonical_name=dep.name,
                version="1",
                runtime_deps=[],
            )
            return member, _sdist_src(tmp_path / "p.tar.gz")

        closure, warnings = M.resolve_closure(top, _b)
        assert [m.name for m in closure] == ["black", "a"]
        assert seen == ["a>=1.0"]
        assert warnings == []

    def test_parallel_workers_preserve_order_and_dedup(
        self, make_meta_fn, tmp_path
    ):
        top = make_meta_fn(
            runtime_deps=[
                Dependency("d", DepKind.RUNTIME),
                Dependency("c", DepKind.RUNTIME),
                Dependency("b", DepKind.RUNTIME),
            ]
        )
        b = make_meta_fn(
            name="b", canonical_name="b", version="2", runtime_deps=[]
        )
        c = make_meta_fn(
            name="c",
            canonical_name="c",
            version="3",
            runtime_deps=[Dependency("e", DepKind.RUNTIME)],
        )
        d = make_meta_fn(
            name="d", canonical_name="d", version="4", runtime_deps=[]
        )
        e = make_meta_fn(
            name="e", canonical_name="e", version="5", runtime_deps=[]
        )
        registry = {"b": b, "c": c, "d": d, "e": e}
        seq_closure, seq_warnings = M.resolve_closure(
            top, self._builder(registry, tmp_path), workers=1
        )
        par_closure, par_warnings = M.resolve_closure(
            top, self._builder(registry, tmp_path), workers=4
        )
        assert par_closure == seq_closure
        assert par_warnings == seq_warnings
        assert [m.name for m in par_closure] == ["black", "d", "c", "b", "e"]

    def test_parallel_workers_honor_skip_and_max_depth(
        self, make_meta_fn, tmp_path
    ):
        top = make_meta_fn(
            runtime_deps=[
                Dependency("a", DepKind.RUNTIME),
                Dependency("b", DepKind.RUNTIME),
            ]
        )
        a = make_meta_fn(
            name="a",
            canonical_name="a",
            version="1",
            runtime_deps=[Dependency("c", DepKind.RUNTIME)],
        )
        b = make_meta_fn(
            name="b", canonical_name="b", version="2", runtime_deps=[]
        )
        c = make_meta_fn(
            name="c", canonical_name="c", version="3", runtime_deps=[]
        )
        registry = {"a": a, "b": b, "c": c}
        closure, warnings = M.resolve_closure(
            top,
            self._builder(registry, tmp_path),
            skip=frozenset({"b"}),
            max_depth=1,
            workers=2,
        )
        assert closure == [top, a]
        assert warnings == []

    def test_parallel_closures_run_concurrently(self, make_meta_fn, tmp_path):
        import threading

        top = make_meta_fn(
            runtime_deps=[
                Dependency("a", DepKind.RUNTIME),
                Dependency("b", DepKind.RUNTIME),
                Dependency("c", DepKind.RUNTIME),
            ]
        )
        barrier = threading.Barrier(3)
        active = 0
        max_active = 0
        lock = threading.Lock()

        def _parallel_builder(dep):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            barrier.wait(timeout=5)
            member = make_meta_fn(
                name=dep.name,
                canonical_name=dep.name,
                version="1",
                runtime_deps=[],
            )
            with lock:
                active -= 1
            return member, _sdist_src(tmp_path / f"{dep.name}.tar.gz")

        closure, warnings = M.resolve_closure(top, _parallel_builder, workers=3)
        assert [m.name for m in closure] == ["black", "a", "b", "c"]
        assert warnings == []
        assert max_active >= 2
