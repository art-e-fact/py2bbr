import dataclasses
import email
import gzip
import hashlib
import io
import struct
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from packaging.version import InvalidVersion

from py2bbr import metadata as M
from py2bbr.errors import MetadataError, MetadataNotFoundError
from py2bbr.metadata import (
    FetchedSource,
    PipOptions,
    _pick_artifact,
    classify_requires_dist,
    detect_license,
    marker_env,
)
from py2bbr.model import Dependency, DepKind, SourceKind
from py2bbr.yocto import (
    current_platform_machine,
    resolve_target_runtime,
    resolve_yocto_version,
)


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

    def test_index_flags(self):
        opts = PipOptions()
        assert opts.index_flags() == []

        opts = PipOptions(extra_index_urls=["https://extra1"])
        assert opts.index_flags() == ["--extra-index-url=https://extra1"]

        opts = PipOptions(index_url="https://i", trusted_hosts=["h"])
        assert opts.index_flags() == [
            "--index-url=https://i",
            "--trusted-host=h",
        ]


class TestPickArtifact:
    def test_prefers_sdist(self, tmp_path: Path):
        (tmp_path / "pkg-1.0.tar.gz").write_bytes(b"sdist")
        (tmp_path / "pkg-1.0-py3-none-any.whl").write_bytes(b"wheel")
        assert _pick_artifact(tmp_path).name.endswith(".tar.gz")

    def test_prefers_sdist_out_of_order(self, tmp_path: Path):
        (tmp_path / "pkg-1.0-py3-none-any.whl").write_bytes(b"wheel")
        (tmp_path / "pkg-1.0.tar.gz").write_bytes(b"sdist")
        assert _pick_artifact(tmp_path).name.endswith(".tar.gz")

    def test_picks_sdist(self, tmp_path: Path):
        (tmp_path / "pkg-1.0.tar.gz").write_bytes(b"sdist")
        assert _pick_artifact(tmp_path).name.endswith(".tar.gz")

    def test_falls_back_to_wheel(self, tmp_path: Path):
        (tmp_path / "pkg-1.0-py3-none-any.whl").write_bytes(b"wheel")
        assert _pick_artifact(tmp_path).suffix == ".whl"

    def test_none_when_empty(self, tmp_path: Path):
        assert _pick_artifact(tmp_path) is None


class TestClassifyRequiresDist:
    def setup_method(self):
        self.yo = resolve_yocto_version("5.0")  # Python 3.12

    def test_plain_dep_runtime(self):
        runtime, _, _ = classify_requires_dist(["click>=8.0"], self.yo)
        assert [d.name for d in runtime] == ["click"]

    def test_extra_marker_dropped(self):
        runtime, _, dropped = classify_requires_dist(
            ['colorama>=0.4.3; extra == "colorama"'], self.yo
        )
        assert runtime == []
        assert dropped[0].reason.startswith("optional extra")

    def test_python_version_marker_dropped_on_312(self):
        runtime, _, dropped = classify_requires_dist(
            ['tomli>=1.1.0; python_version < "3.11"'], self.yo
        )
        assert runtime == []
        assert dropped[0].name == "tomli"

    def test_python_version_marker_kept_when_satisfied(self):
        # Create a sound old Yocto version with Python 3.9
        # to test against
        from py2bbr.model import YoctoVersion

        y39 = YoctoVersion(
            "3.1",
            "dunfell",
            "dunfell",
            "3.9",
            meta_python_branch="dunfell",
        )

        # Evaluate with a constraint on Python version
        runtime, _, _ = classify_requires_dist(
            ['mepackage>=1.1.0; python_version < "3.11"'], y39
        )

        assert [d.name for d in runtime] == ["mepackage"]

    def test_windows_only_dropped_on_linux_target(self):
        runtime, _, dropped = classify_requires_dist(
            ['pywin32; sys_platform == "win32"'], self.yo
        )
        assert runtime == []
        assert dropped[0].name == "pywin32"

    def test_unparseable_dropped(self):
        runtime, _, dropped = classify_requires_dist(
            ["this is not valid !!!"], self.yo
        )
        assert runtime == []
        assert dropped[0].kind is DepKind.DROPPED

    def test_floor_capped_to_same_major(self):
        runtime, _, _ = classify_requires_dist(["numpy>=1.21.0"], self.yo)
        assert runtime[0].requirement == "numpy>=1.21.0,<2"
        assert runtime[0].specifier == ">=1.21.0,<2"

    def test_cap_disabled_keeps_upstream_bounds(self):
        runtime, _b, _d = classify_requires_dist(
            ["numpy>=1.21.0"], self.yo, cap_major_bump=False
        )
        assert runtime[0].requirement == "numpy>=1.21.0"
        assert runtime[0].specifier == ">=1.21.0"


class TestApplyMajorCap:
    def test_usual_suspect(self):
        assert M.apply_major_cap("numpy>=1.21.0") == (
            "numpy>=1.21.0,<2",
            ">=1.21.0,<2",
        )

    def test_user_example(self):
        assert M.apply_major_cap("mypackage>=2.3.1") == (
            "mypackage>=2.3.1,<3",
            ">=2.3.1,<3",
        )

    def test_strict_floor_is_capped(self):
        assert M.apply_major_cap("numpy>1.19") == ("numpy>1.19,<2", ">1.19,<2")

    def test_highest_floor_wins(self):
        req, spec = M.apply_major_cap("pkg>=2.0,>=2.5")
        assert req == "pkg>=2.0,>=2.5,<3"
        assert spec == ">=2.0,>=2.5,<3"

    def test_exclusions_survive(self):
        req, spec = M.apply_major_cap("pkg>=2.0,!=1.5")
        assert req.count("!=1.5") == 1
        assert req.endswith(",<3")
        assert spec.endswith(",<3")

    def test_marker_stays_trailing_and_valid(self):
        req, spec = M.apply_major_cap('numpy>=1.21.0; python_version < "3.11"')
        assert req == 'numpy>=1.21.0,<2; python_version < "3.11"'
        assert spec == ">=1.21.0,<2"

    def test_extras_preserved(self):
        assert M.apply_major_cap("numpy[test]>=1.21.0") == (
            "numpy[test]>=1.21.0,<2",
            ">=1.21.0,<2",
        )

    def test_already_bounded_untouched(self):
        assert M.apply_major_cap("foo>=1.0,<3")[0] == "foo>=1.0,<3"
        assert M.apply_major_cap("foo<2.0")[0] == "foo<2.0"
        assert M.apply_major_cap("foo<=2.0")[0] == "foo<=2.0"
        assert M.apply_major_cap("foo~=1.4.2")[0] == "foo~=1.4.2"

    def test_pinned_untouched(self):
        assert M.apply_major_cap("foo==2.3.1")[0] == "foo==2.3.1"
        assert M.apply_major_cap("foo===2.3.1")[0] == "foo===2.3.1"

    def test_no_floor_untouched(self):
        assert M.apply_major_cap("foo")[0] == "foo"
        assert M.apply_major_cap("foo!=1.0")[0] == "foo!=1.0"

    def test_zero_floor_untouched(self):
        assert M.apply_major_cap("foo>=0.5")[0] == "foo>=0.5"
        assert M.apply_major_cap("foo>=0")[0] == "foo>=0"

    def test_unparseable_untouched(self):
        assert M.apply_major_cap("not a requirement !!!")[0] == (
            "not a requirement !!!"
        )

    def test_unversionable_floor_untouched(self, monkeypatch):
        def explode(text, *a, **k):
            raise InvalidVersion(text)

        monkeypatch.setattr(M, "Version", explode)
        assert M.apply_major_cap("foo>=1.0")[0] == "foo>=1.0"


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
        msg = _msg(
            "Metadata-Version: 2.1\nName: jinxed\n"
            "Classifier: License :: OSI Approved :: "
            "Mozilla Public License 2.0 (MPL 2.0)\n"
        )
        spdx, _ = detect_license(msg)
        assert spdx == "MPL-2.0"

    def test_license_text_mplv2(self):
        msg = _msg("Metadata-Version: 2.1\nName: jinxed\nLicense: MPLv2.0\n")
        spdx, _ = detect_license(msg)
        assert spdx == "MPL-2.0"


class TestMarkerEnv:
    def test_target_python_version(self):
        y = resolve_yocto_version("5.0")
        env = marker_env(y)
        assert env["python_version"] == "3.12"
        assert env["sys_platform"] == "linux"

    def test_no_yocto_keeps_host_python(self):
        env = marker_env(None)
        assert env["sys_platform"] == "linux"
        assert "python_version" in env

    def test_platform_machine_is_the_target_cpu(self):
        y = resolve_target_runtime(
            resolve_yocto_version("5.0"), platform_machine="arm64"
        )
        assert marker_env(y)["platform_machine"] == "aarch64"

    def test_platform_machine_falls_back_to_the_legacy_default(self):
        y = dataclasses.replace(
            resolve_yocto_version("5.0"), platform_machine=""
        )
        assert marker_env(y)["platform_machine"] == "x86_64"


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


# Metadata for a wheel-only package whose repository is known. There is no
# sdist, so build_metadata has to read [build-system] from the repository.
_WHEEL_META = """\
Metadata-Version: 2.4
Name: pyvizionsdk
Version: 26.9.1
Summary: A SDK that publishes only a binary wheel.
Requires-Python: >=3.10
Project-URL: Repository, https://github.com/vizion/pyvizionsdk
"""


def _wheel_src(tmp_path: Path, metadata: str = _WHEEL_META) -> FetchedSource:
    """A stub for a vendored wheel: payload plus ``.dist-info/METADATA``."""
    p = tmp_path / "pyvizionsdk-26.9.1-cp312-cp312-manylinux_2_31_aarch64.whl"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("pyvizionsdk/__init__.py", "")
        zf.writestr("pyvizionsdk-26.9.1.dist-info/METADATA", metadata)
    return FetchedSource(p, SourceKind.WHEEL, "sha", p.name)


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
    @staticmethod
    def _capture(monkeypatch, tmp_path, **kw) -> list[str]:
        """
        Run a pip download wrapped in test settings.
        """
        captured: dict = {}

        def fake_run(cmd, **run_kw):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        monkeypatch.setattr(M, "_pip_executable", lambda: ["fake", "pip"])
        monkeypatch.setattr(M.subprocess, "run", fake_run)
        M._run_pip_download(
            "pkg==1.0", tmp_path, PipOptions(), kw.pop("mode"), **kw
        )
        return captured["cmd"]

    @staticmethod
    def _values_after(cmd: list[str], flag: str) -> list[str]:
        return [cmd[n + 1] for n in range(len(cmd) - 1) if cmd[n] == flag]

    def test_command_plain(self, monkeypatch, tmp_path):
        """
        Plain check of the pip download function.

        Logic is the same as the `_capture` helper here, with broader checks
        on several details.
        """
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

    def test_routes_pip_scratch_under_dest(self, monkeypatch, tmp_path):
        """
        Checks specific to scratch

        Logic is the same as the `_capture` helper here, with specific checks.
        """
        captured: dict = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            captured["env"] = kw.get("env", {})
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        monkeypatch.setattr(M, "_pip_executable", lambda: ["fake", "pip"])
        monkeypatch.setattr(M.subprocess, "run", fake_run)
        M._run_pip_download(
            "black==26.5.1",
            tmp_path,
            PipOptions(),
            "--no-binary=:all:",
        )
        scratch = Path(captured["env"]["TMPDIR"])
        assert scratch.is_relative_to(tmp_path)
        assert scratch.exists() and scratch.is_dir()
        probe = scratch / "probe"
        probe.write_text("x")
        assert probe.stat().st_dev == tmp_path.stat().st_dev

    def test_target_interpreter_is_pinned(self, monkeypatch, tmp_path):
        cmd = self._capture(
            monkeypatch,
            tmp_path,
            mode="--only-binary=:all:",
            python_version="3.12",
        )
        assert self._values_after(cmd, "--python-version") == ["3.12"]

    def test_omitted_without_a_target_version(self, monkeypatch, tmp_path):
        cmd = self._capture(monkeypatch, tmp_path, mode="--only-binary=:all:")
        assert "--python-version" not in cmd

    def test_target_platform_is_pinned(self, monkeypatch, tmp_path):
        cmd = self._capture(
            monkeypatch,
            tmp_path,
            mode="--only-binary=:all:",
            python_version="3.12",
            platform_tags=["manylinux_2_17_aarch64", "linux_aarch64"],
        )
        assert self._values_after(cmd, "--platform") == [
            "manylinux_2_17_aarch64",
            "linux_aarch64",
        ]

    def test_platform_tags_are_never_comma_joined(self, monkeypatch, tmp_path):
        cross = (
            "x86_64" if current_platform_machine() == "aarch64" else "aarch64"
        )
        platforms = M._pip_platforms(
            resolve_target_runtime(
                resolve_yocto_version("5.0"), platform_machine=cross
            )
        )
        cmd = self._capture(
            monkeypatch,
            tmp_path,
            mode="--only-binary=:all:",
            python_version="3.12",
            platform_tags=platforms,
        )
        values = self._values_after(cmd, "--platform")
        assert values == platforms
        assert not any("," in v for v in values)
        assert all(v.endswith(f"_{cross}") for v in values)

    def test_abi_is_never_constrained(self, monkeypatch, tmp_path):
        cmd = self._capture(
            monkeypatch,
            tmp_path,
            mode="--only-binary=:all:",
            python_version="3.12",
            platform_tags=["manylinux_2_17_aarch64"],
        )
        assert "--abi" not in cmd

    def test_no_platform_pin_without_one(self, monkeypatch, tmp_path):
        cmd = self._capture(
            monkeypatch,
            tmp_path,
            mode="--only-binary=:all:",
            python_version="3.12",
        )
        assert "--platform" not in cmd


class TestPublicSdistUrl:
    def test_tar_gz_url_derived(self):
        assert M._public_pypi_sdist_url("pyvizionsdk-26.9.1.tar.gz") == (
            "https://files.pythonhosted.org/packages/source/"
            "p/pyvizionsdk/pyvizionsdk-26.9.1.tar.gz"
        )

    def test_dashed_name_rpartition_keeps_full_name(self):
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
        # Track calls to switch logic.
        # `calls` is not a plain integer, but a "non-primitive" object
        # so that `fake` can reference it.
        calls = [0]

        def fake(requirement, dest, *a, **k):
            calls[0] += 1
            if calls[0] == 1:
                return subprocess.CompletedProcess([], 1, "", "ERROR")
            (dest / "black-26.5.1-py3-none-any.whl").write_bytes(b"data")
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(M, "_run_pip_download", fake)
        with caplog.at_level("WARNING", logger="py2bbr.metadata"):
            src = M.fetch_source("black", "26.5.1", PipOptions(), tmp_path)
        assert src.kind is SourceKind.WHEEL
        assert "ERROR" in caplog.text
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
        assert M.read_build_system(src) is None

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
        # A wheel is a build product: It has no pyproject.toml.
        assert M.read_build_system(src) is None

    def test_no_metadata_raises(self, tmp_path):
        p = tmp_path / "x.tar.gz"
        p.write_bytes(gzip.compress(b"not a tarball"))
        src = FetchedSource(p, SourceKind.SDIST, "sha", "x.tar.gz")
        with pytest.raises(MetadataError, match="no PKG-INFO/METADATA"):
            M.parse_metadata_file(src)
        assert M.find_license_file(src) is None
        assert M._read_text_from_archive(p, lambda n: True) is None


class TestMessageHelpers:
    def test_reject_duplicates(self):
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

    def test_major_cap_applied_by_default(self, tmp_path, monkeypatch):
        src = _sdist_src(_sdist(tmp_path, _FULL_MEMBERS))
        monkeypatch.setattr(M, "fetch_source", lambda *a, **k: src)
        meta, _ = M.build_metadata(
            "black", "26.5.1", PipOptions(), tmp_path, yocto=M.default_yocto()
        )
        assert meta.runtime_deps[0].requirement == "click>=8.0.0,<9"
        assert meta.runtime_deps[0].specifier == ">=8.0.0,<9"
        assert meta.build_deps[0].requirement == "hatchling>=1.27.0,<2"

    def test_major_cap_disabled_keeps_bounds(self, tmp_path, monkeypatch):
        src = _sdist_src(_sdist(tmp_path, _FULL_MEMBERS))
        monkeypatch.setattr(M, "fetch_source", lambda *a, **k: src)
        meta, _ = M.build_metadata(
            "black",
            "26.5.1",
            PipOptions(),
            tmp_path,
            yocto=M.default_yocto(),
            cap_major_bump=False,
        )
        assert meta.runtime_deps[0].requirement == "click>=8.0.0"
        assert meta.runtime_deps[0].specifier == ">=8.0.0"
        assert meta.build_deps[0].requirement == "hatchling>=1.27.0"

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
        deps = M._build_deps_from_requires(["ok>=1", "not ok !!!"])
        assert [d.name for d in deps] == ["ok"]


class TestMiscMetadata:
    def test_canonical(self):
        assert M._canonical("Async_PySerial") == "async-pyserial"

    def test_default_yocto(self):
        assert M.default_yocto().version == "5.0"


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

    def test_parallel_releases_children_before_sibling_finishes(
        self, make_meta_fn, tmp_path
    ):
        import threading

        top = make_meta_fn(
            runtime_deps=[
                Dependency("a", DepKind.RUNTIME),
                Dependency("slow", DepKind.RUNTIME),
            ]
        )
        a = make_meta_fn(
            name="a",
            canonical_name="a",
            version="1",
            runtime_deps=[Dependency("b", DepKind.RUNTIME)],
        )
        b = make_meta_fn(
            name="b", canonical_name="b", version="1", runtime_deps=[]
        )
        slow = make_meta_fn(
            name="slow", canonical_name="slow", version="1", runtime_deps=[]
        )
        registry = {"a": a, "b": b, "slow": slow}
        slow_release = threading.Event()
        b_started = threading.Event()

        def _builder(dep):
            if dep.name == "slow":
                assert slow_release.wait(timeout=5), "slow never released"
            elif dep.name == "b":
                b_started.set()
            return (
                registry[dep.name],
                _sdist_src(tmp_path / f"{dep.name}.tar.gz"),
            )

        def _run():
            return M.resolve_closure(top, _builder, workers=2)

        import threading as _t

        result_holder: list = []
        t = _t.Thread(target=lambda: result_holder.append(_run()))
        t.start()
        # Greedy: a finishes, its child b must be fetched while slow is
        # still blocked. A level barrier would wait for slow first.
        assert b_started.wait(timeout=5), "b never started before slow finished"
        slow_release.set()
        t.join(timeout=10)
        assert not t.is_alive()
        closure, warnings = result_holder[0]
        assert [m.name for m in closure] == ["black", "a", "slow", "b"]
        assert warnings == []


class TestDetectNativeCode:
    def _src(self, tmp_path, members):
        return _sdist_src(_sdist(tmp_path, members))

    def test_c_source(self, tmp_path):
        src = self._src(
            tmp_path,
            {
                "black-26.5.1/PKG-INFO": _PKG_INFO,
                "black-26.5.1/src/x.c": "int x;",
            },
        )
        native, evidence = M.detect_native_code(src)
        assert native is True
        assert evidence[0].startswith("C source")
        assert "src/x.c" in evidence[0]

    def test_one_evidence_per_language(self, tmp_path):
        src = self._src(
            tmp_path,
            {
                "p-1.0/a.c": "1",
                "p-1.0/b.cpp": "2",
                "p-1.0/d.cpp": "3",
                "p-1.0/mod.pyx": "4",
            },
        )
        _native, evidence = M.detect_native_code(src)
        assert len(evidence) == 3
        assert any(e.startswith("C++") for e in evidence)
        assert any(e.startswith("Cython") for e in evidence)

    def test_pure_python(self, tmp_path):
        src = self._src(
            tmp_path,
            {"black-26.5.1/PKG-INFO": _PKG_INFO, "black-26.5.1/x.py": "x = 1"},
        )
        assert M.detect_native_code(src) == (False, [])

    def test_headers_alone_are_not_evidence(self, tmp_path):
        src = self._src(tmp_path, {"p-1.0/api.h": "int x;"})
        assert M.detect_native_code(src) == (False, [])

    @pytest.mark.parametrize(
        "member",
        [
            "p-1.0/tests/test_x.c",
            "p-1.0/Lib/test/test_x.c",
            "p-1.0/src/test_thing.c",
            "p-1.0/src/thing_test.c",
            "p-1.0/conftest.c",
            "p-1.0/docs/example.c",
            "p-1.0/examples/demo.cpp",
            "p-1.0/benchmarks/bench.rs",
            "top.c",
        ],
    )
    def test_test_and_doc_trees_are_ignored(self, tmp_path, member):
        src = self._src(tmp_path, {member: "x"})
        assert M.detect_native_code(src) == (False, [])

    def test_build_requirement_is_evidence(self, tmp_path):
        src = self._src(tmp_path, {"p-1.0/PKG-INFO": _PKG_INFO})
        native, evidence = M.detect_native_code(
            src, ["setuptools", "cython>=3"]
        )
        assert native is True
        assert "build requires cython" in evidence

    def test_unparseable_build_requirement_is_ignored(self, tmp_path):
        src = self._src(tmp_path, {"p-1.0/PKG-INFO": _PKG_INFO})
        assert M.detect_native_code(src, ["this is not a requirement"]) == (
            False,
            [],
        )

    def test_rust_build_requirement(self, tmp_path):
        src = self._src(tmp_path, {"p-1.0/PKG-INFO": _PKG_INFO})
        _native, evidence = M.detect_native_code(src, ["setuptools-rust"])
        assert "build requires setuptools-rust" in evidence

    def test_setup_py_ext_modules(self, tmp_path):
        src = self._src(
            tmp_path,
            {
                "p-1.0/PKG-INFO": _PKG_INFO,
                "p-1.0/setup.py": "ext_modules=[Extension('a', ['a.c'])]",
            },
        )
        _native, evidence = M.detect_native_code(src)
        assert "setup.py declares ext_modules" in evidence

    def test_nested_setup_py_does_not_count(self, tmp_path):
        # Only the top-level setup.py describes the built extension.
        src = self._src(
            tmp_path,
            {
                "p-1.0/PKG-INFO": _PKG_INFO,
                "p-1.0/sub/setup.py": "ext_modules=[]",
            },
        )
        assert M.detect_native_code(src) == (False, [])

    def test_evidence_is_capped(self, tmp_path):
        members = {f"p-1.0/d{i}.c": "x" for i in range(12)}
        members["p-1.0/a.pyx"] = "x"
        members["p-1.0/a.rs"] = "x"
        members["p-1.0/a.f90"] = "x"
        _native, evidence = M.detect_native_code(
            self._src(tmp_path, members), ["cython", "cffi", "maturin"]
        )
        assert len(evidence) == M._MAX_EVIDENCE

    def test_wheel_members_count(self, tmp_path):
        """
        Check report nothing to compile when there is no source.
        """
        path = tmp_path / "pkg-1.0-cp312-cp312-manylinux_2_17_x86_64.whl"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("pkg/_core.cpython-312-x86_64-linux-gnu.so", b"\0")
        native, _ = M.detect_native_code(
            FetchedSource(path, SourceKind.WHEEL, "sha", path.name)
        )
        assert native is False

    def test_unreadable_archive(self, tmp_path):
        path = tmp_path / "black-26.5.1.tar.gz"
        path.write_bytes(b"not a tarball")
        src = FetchedSource(path, SourceKind.SDIST, "sha", path.name)
        assert M._member_names(path) == []
        assert M.detect_native_code(src) == (False, [])

    def test_unreadable_zip(self, tmp_path):
        path = tmp_path / "pkg-1.0-py3-none-any.whl"
        path.write_bytes(b"not a zip")
        assert M._member_names(path) == []


class TestDetectHostAssumptions:
    def _src(self, tmp_path, members):
        return _sdist_src(_sdist(tmp_path, members))

    def test_march_native(self, tmp_path):
        src = self._src(
            tmp_path,
            {"p-1.0/setup.py": 'extra_compile_args=["-march=native"]'},
        )
        assert M.detect_host_assumptions(src) == ["setup.py: -march="]

    def test_avx_flags(self, tmp_path):
        src = self._src(
            tmp_path, {"p-1.0/setup.py": 'cflags = "-O3 -mavx2 -mfma"'}
        )
        assert M.detect_host_assumptions(src) == [
            "setup.py: -mavx, -mfma, avx2"
        ]

    def test_machine_name_alone_is_not_flagged(self, tmp_path):
        # numpy's pyproject only mentions x86_64 in cibuildwheel image names;
        # flagging that would be noise on every cross build.
        src = self._src(
            tmp_path,
            {
                "p-1.0/pyproject.toml": 'skip = ["*_i686"]\n'
                'manylinux-x86_64-image = "manylinux_2_28"\n',
            },
        )
        assert M.detect_host_assumptions(src) == []

    def test_guarded_machine_test_is_not_flagged(self, tmp_path):
        src = self._src(
            tmp_path,
            {
                "p-1.0/setup.py": 'if platform.machine() == "x86_64":\n'
                "    pass\n"
            },
        )
        assert M.detect_host_assumptions(src) == []

    def test_only_top_level_files(self, tmp_path):
        src = self._src(
            tmp_path,
            {"p-1.0/vendor/pyproject.toml": 'cflags = "-march=native"'},
        )
        assert M.detect_host_assumptions(src) == []

    def test_each_file_is_reported_once(self, tmp_path):
        src = self._src(
            tmp_path,
            {
                "p-1.0/setup.py": 'x = "-march=native -mavx"',
                "p-1.0/pyproject.toml": 'y = "-msse4.2"',
            },
        )
        assert M.detect_host_assumptions(src) == [
            "setup.py: -march=, -mavx",
            "pyproject.toml: -msse, sse4.2",
        ]

    def test_capped(self, tmp_path):
        members = {
            f"p-1.0/{name}": 'x = "-march=native"'
            for name in (
                "setup.py",
                "setup.cfg",
                "pyproject.toml",
                "meson.build",
            )
        }
        members["p-1.0/extra.c"] = ""
        assert len(M.detect_host_assumptions(self._src(tmp_path, members))) <= (
            M._MAX_EVIDENCE
        )


class TestMachineGatedDeps:
    def test_split_aarch64_marker(self):
        gated, plain = M.machine_gated_deps(
            [
                'arm-only>=1; platform_machine == "aarch64"',
                "click>=8",
            ],
            resolve_yocto_version("5.0"),
        )
        assert plain == ["click>=8"]
        assert [g.name for g in gated] == ["arm-only"]
        assert gated[0].machines == ("aarch64",)
        assert 'platform_machine == "aarch64"' in gated[0].marker

    def test_x86_marker_only_matches_x86(self):
        gated, _plain = M.machine_gated_deps(
            ['x; platform_machine == "x86_64"'], resolve_yocto_version("5.0")
        )
        assert gated[0].machines == ("x86_64",)

    def test_disjunction_covers_both_families(self):
        gated, _plain = M.machine_gated_deps(
            [
                'both; platform_machine in "x86_64 aarch64"',
            ],
            resolve_yocto_version("5.0"),
        )
        assert set(gated[0].machines) == {"x86_64", "aarch64"}

    def test_negated_marker(self):
        gated, _plain = M.machine_gated_deps(
            [
                'not-aarch64; platform_machine != "aarch64"',
            ],
            resolve_yocto_version("5.0"),
        )
        assert "aarch64" not in gated[0].machines
        assert "x86_64" in gated[0].machines

    def test_combined_with_python_version(self):
        gated, _plain = M.machine_gated_deps(
            [
                'n; platform_machine == "aarch64" and python_version >= "3.11"',
            ],
            resolve_yocto_version("5.0"),
        )
        assert gated[0].machines == ("aarch64",)

    def test_marker_no_target_satisfies_is_dropped(self):
        gated, plain = M.machine_gated_deps(
            ['win; platform_machine == "windows"'], resolve_yocto_version("5.0")
        )
        assert gated == []
        assert plain == []

    def test_no_marker_is_plain(self):
        gated, plain = M.machine_gated_deps(["click>=8"], None)
        assert gated == []
        assert plain == ["click>=8"]

    def test_other_markers_stay_plain(self):
        gated, plain = M.machine_gated_deps(
            ['colorama; sys_platform == "win32"'], resolve_yocto_version("5.0")
        )
        assert gated == []
        assert plain == ['colorama; sys_platform == "win32"']

    def test_unparseable_requirement_stays_plain(self):
        gated, plain = M.machine_gated_deps(
            ["this is not a requirement"], resolve_yocto_version("5.0")
        )
        assert gated == []
        assert plain == ["this is not a requirement"]

    def test_without_yocto_keeps_host_python(self):
        # No target Python: only the machine part of the marker decides.
        gated, _plain = M.machine_gated_deps(
            ['x; platform_machine == "aarch64"'], None
        )
        assert gated[0].machines == ("aarch64",)


class TestSdistDownloadRetry:
    def test_retry_drops_build_isolation(self, tmp_path, monkeypatch):
        seen: list[dict] = []

        def fake(requirement, dest, pip_opts, binary_mode, **kw):
            seen.append({"mode": binary_mode, **kw})
            if len(seen) == 1:
                return subprocess.CompletedProcess([], 1, "", "no backend")
            (dest / "pkg-1.0.tar.gz").write_bytes(b"sdist")
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(M, "_run_pip_download", fake)
        src = M.fetch_source("pkg", "1.0", PipOptions(), tmp_path)
        assert src.kind is SourceKind.SDIST
        assert len(seen) == 2
        assert seen[0].get("no_build_isolation") is not True
        assert seen[1]["no_build_isolation"] is True
        # The retry still demands an sdist
        assert seen[1]["mode"] == "--no-binary=:all:"

    def test_no_retry_when_the_sdist_arrives(self, tmp_path, monkeypatch):
        calls = {"n": 0}

        def fake(requirement, dest, *a, **kw):
            calls["n"] += 1
            (dest / "pkg-1.0.tar.gz").write_bytes(b"sdist")
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(M, "_run_pip_download", fake)
        M.fetch_source("pkg", "1.0", PipOptions(), tmp_path)
        assert calls["n"] == 1

    def test_wheel_on_a_no_binary_attempt_is_not_an_sdist(
        self, tmp_path, monkeypatch, caplog
    ):
        def fake(requirement, dest, *a, **kw):
            (dest / "pkg-1.0-py3-none-any.whl").write_bytes(b"data")
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(M, "_run_pip_download", fake)
        with caplog.at_level("WARNING", logger="py2bbr.metadata"):
            src = M.fetch_source("pkg", "1.0", PipOptions(), tmp_path)
        assert src.kind is SourceKind.WHEEL
        assert "sdist fetch failed" in caplog.text


class TestWheelTagCompatibility:
    @pytest.mark.parametrize(
        "filename",
        [
            "pkg-1.0-py3-none-any.whl",
            "pkg-1.0-py2.py3-none-any.whl",
            "pkg-1.0-py312-none-any.whl",
            "pkg-1.0-cp312-cp312-linux_aarch64.whl",
            # Stable ABI: compiled once against the limited API, valid on every
            # later CPython 3.
            "pkg-1.0-cp38-abi3-manylinux_2_17_aarch64.whl",
            "pkg-1.0-cp312-abi3-manylinux_2_17_aarch64.whl",
        ],
    )
    def test_serves_the_target(self, filename):
        assert (
            M._wheel_rejection_reason(filename, resolve_yocto_version("5.0"))
            is None
        )

    @pytest.mark.parametrize(
        "filename",
        [
            # The reported pyvizionsdk artifact: built for CPython 3.14.
            "pyvizionsdk-26.9.1-cp314-cp314-manylinux_2_31_x86_64.whl",
            "pkg-1.0-py2-none-any.whl",
            "pkg-1.0-cp311-cp311-linux_aarch64.whl",
            "pkg-1.0-pp39-pypy39_pp73-linux_x86_64.whl",
        ],
    )
    def test_cannot_serve_the_target(self, filename):
        reason = M._wheel_rejection_reason(
            filename, resolve_yocto_version("5.0")
        )
        assert reason is not None
        assert filename in reason
        assert "3.12" in reason

    def test_unparsable_name_is_left_to_the_build(self):
        assert M._wheel_rejection_reason("not-a-wheel.txt", None) is None
        assert (
            M._wheel_rejection_reason(
                "not-a-wheel.txt", resolve_yocto_version("5.0")
            )
            is None
        )

    def test_non_cpython_target_is_not_checked(self):
        assert (
            M._wheel_rejection_reason("pkg-1.0-cp314-cp314-any.whl", None)
            is None
        )

    def test_platform_tag_alone_does_not_reject(self):
        assert (
            M._wheel_rejection_reason(
                "pkg-1.0-cp312-cp312-manylinux_2_31_x86_64.whl",
                resolve_yocto_version("5.0"),
            )
            is None
        )

    def test_fetch_source_rejects_an_unusable_wheel(
        self, tmp_path, monkeypatch
    ):
        def fake(requirement, dest, *a, **kw):
            (
                dest
                / "pyvizionsdk-26.9.1-cp314-cp314-manylinux_2_31_x86_64.whl"
            ).write_bytes(b"data")
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(M, "_run_pip_download", fake)
        with pytest.raises(MetadataNotFoundError) as excinfo:
            M.fetch_source(
                "pyvizionsdk",
                "26.9.1",
                PipOptions(),
                tmp_path,
                yocto=resolve_yocto_version("5.0"),
            )
        message = str(excinfo.value)
        assert "serves no artifact the target can use" in message
        assert "cp314-cp314" in message
        assert "3.12" in message
        assert "Yocto 5.0" in message
        assert "--sdist" in message
        assert "--index-url" in message

    def test_wheel_miss_names_the_pin(self, tmp_path, monkeypatch):
        proc = subprocess.CompletedProcess(
            [], 1, "", "ERROR: No matching distribution found for multi==1.0"
        )
        monkeypatch.setattr(M, "_run_pip_download", lambda *a, **kw: proc)
        with pytest.raises(MetadataNotFoundError) as excinfo:
            M.fetch_source(
                "multi",
                "1.0",
                PipOptions(index_url="https://i"),
                tmp_path,
                yocto=resolve_yocto_version("5.0"),
            )
        message = str(excinfo.value)
        assert "restricted to CPython 3.12" in message
        assert "--python-version 3.12" in message
        assert "No matching distribution" in message

    def test_wheel_miss_without_a_target_says_nothing_about_a_pin(
        self, tmp_path, monkeypatch
    ):
        proc = subprocess.CompletedProcess([], 1, "", "ERROR: nope")
        monkeypatch.setattr(M, "_run_pip_download", lambda *a, **kw: proc)
        with pytest.raises(MetadataNotFoundError) as excinfo:
            M.fetch_source("multi", "1.0", PipOptions(), tmp_path)
        assert "python-version" not in str(excinfo.value)

    def test_fetch_source_accepts_a_serving_wheel(self, tmp_path, monkeypatch):
        def fake(requirement, dest, *a, **kw):
            (dest / "pkg-1.0-py3-none-any.whl").write_bytes(b"data")
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(M, "_run_pip_download", fake)
        src = M.fetch_source(
            "pkg",
            "1.0",
            PipOptions(),
            tmp_path,
            yocto=resolve_yocto_version("5.0"),
        )
        assert src.kind is SourceKind.WHEEL


class TestTargetInterpreterPinned:
    def test_major_minor_only(self):
        yocto = dataclasses.replace(
            resolve_yocto_version("5.0"), python_version="3.12.6"
        )
        assert M._pip_python_version(yocto) == "3.12"

    @pytest.mark.parametrize("version", ["", "3", "2.7", "python"])
    def test_unpinnable_target_version(self, version):
        yocto = dataclasses.replace(
            resolve_yocto_version("5.0"), python_version=version
        )
        assert M._pip_python_version(yocto) is None

    def test_unknown_target_is_unpinned(self):
        assert M._pip_python_version(None) is None

    @pytest.mark.parametrize(
        ("version", "codename", "expected"),
        [
            ("5.0", "scarthgap", "Yocto 5.0 (scarthgap)"),
            # A series named after its own codename must not read "5.0 (5.0)".
            ("scarthgap", "scarthgap", "Yocto scarthgap"),
            ("", "", "the Yocto target"),
        ],
    )
    def test_target_label(self, version, codename, expected):
        yocto = dataclasses.replace(
            resolve_yocto_version("5.0"), version=version, codename=codename
        )
        assert M._target_label(yocto) == expected

    def test_target_label_without_a_target(self):
        assert M._target_label(None) == "the Yocto target"

    def test_only_the_wheel_fallback_is_pinned(self, tmp_path, monkeypatch):
        calls: list[tuple[str, str | None]] = []

        def fake(requirement, dest, pip_opts, mode, *a, **kw):
            calls.append((mode, kw.get("python_version")))
            if mode == "--no-binary=:all:":
                # Fail the first sdist attempt so the no-build-isolation
                # retry and then the wheel fallback both run.
                if len([c for c in calls if c[0] == mode]) == 1:
                    return subprocess.CompletedProcess([], 1, "", "no sdist")
                (dest / "pkg-1.0.tar.gz").write_bytes(b"data")
                return subprocess.CompletedProcess([], 0, "", "")
            (dest / "pkg-1.0-py3-none-any.whl").write_bytes(b"data")
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(M, "_run_pip_download", fake)
        M.fetch_source(
            "pkg",
            "1.0",
            PipOptions(),
            tmp_path,
            yocto=resolve_yocto_version("5.0"),
        )
        assert calls == [
            ("--no-binary=:all:", None),
            ("--no-binary=:all:", None),
        ]

    def test_wheel_fallback_is_pinned(self, tmp_path, monkeypatch):
        calls: list[tuple[str, str | None]] = []

        def fake(requirement, dest, pip_opts, mode, *a, **kw):
            calls.append((mode, kw.get("python_version")))
            if mode == "--only-binary=:all:":
                (dest / "pkg-1.0-py3-none-any.whl").write_bytes(b"data")
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(M, "_run_pip_download", fake)
        M.fetch_source(
            "pkg",
            "1.0",
            PipOptions(),
            tmp_path,
            yocto=resolve_yocto_version("5.0"),
        )
        assert calls == [
            ("--no-binary=:all:", None),
            ("--no-binary=:all:", None),
            ("--only-binary=:all:", "3.12"),
        ]

    def test_unpinned_when_target_is_unknown(self, tmp_path, monkeypatch):
        calls: list[tuple[str, str | None]] = []

        def fake(requirement, dest, pip_opts, mode, *a, **kw):
            calls.append((mode, kw.get("python_version")))
            (dest / "pkg-1.0.tar.gz").write_bytes(b"data")
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(M, "_run_pip_download", fake)
        M.fetch_source("pkg", "1.0", PipOptions(), tmp_path)
        assert calls == [("--no-binary=:all:", None)]


class TestTargetArchitecturePinned:
    @staticmethod
    def _cross(arch: str = "aarch64"):
        return resolve_target_runtime(
            resolve_yocto_version("5.0"), platform_machine=arch
        )

    def test_native_target_is_left_to_pip(self):
        assert M._pip_platforms(self._cross(current_platform_machine())) == []
        assert M._pip_platforms(None) == []

    def test_family_is_named_for_the_target_cpu(self):
        platforms = M._pip_platforms(self._cross())
        assert platforms == [
            *(f"manylinux_2_{minor}_aarch64" for minor in range(5, 46)),
            "manylinux1_aarch64",
            "manylinux2010_aarch64",
            "manylinux2014_aarch64",
            "musllinux_1_1_aarch64",
            "musllinux_1_2_aarch64",
            "linux_aarch64",
        ]
        assert not any("_x86_64" in p for p in platforms)

    def test_family_uses_the_canonical_cpu_name(self):
        assert "linux_aarch64" in M._pip_platforms(self._cross("arm64"))

    def test_host_that_cannot_name_its_cpu_leaves_pip_in_charge(
        self, monkeypatch
    ):
        monkeypatch.setattr(M.platform, "machine", lambda: "")
        assert M._pip_platforms(self._cross("aarch64")) == []
        assert M._pip_platforms(self._cross("x86_64")) == []

    def test_only_the_wheel_fallback_is_pinned(self, tmp_path, monkeypatch):
        calls: list[tuple[str, list[str] | None]] = []

        def fake(requirement, dest, pip_opts, mode, *a, **kw):
            calls.append((mode, kw.get("platform_tags")))
            if mode == "--only-binary=:all:":
                (dest / "pkg-1.0-py3-none-any.whl").write_bytes(b"data")
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(M, "_run_pip_download", fake)
        M.fetch_source(
            "pkg",
            "1.0",
            PipOptions(),
            tmp_path,
            yocto=self._cross(),
        )
        assert calls == [
            ("--no-binary=:all:", None),
            ("--no-binary=:all:", None),
            ("--only-binary=:all:", M._pip_platforms(self._cross())),
        ]

    def test_failure_names_the_cpu_it_sought(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            M,
            "_run_pip_download",
            lambda *a, **kw: subprocess.CompletedProcess(
                [], 1, "", "ERROR: No matching distribution found for pkg"
            ),
        )
        with pytest.raises(MetadataNotFoundError) as exc:
            M.fetch_source(
                "pkg", "1.0", PipOptions(), tmp_path, yocto=self._cross()
            )
        message = str(exc.value)
        assert "aarch64 wheels" in message
        assert "--python-version 3.12" in message
        assert "--platform" not in message
        assert "carries nothing for aarch64" in message


class TestWheelShipsPrebuiltBinaries:
    def test_platform_tag_admits_prebuilt_binaries(self):
        assert M.wheel_ships_prebuilt_binaries(
            "pyvizionsdk-26.9.1-cp312-cp312-manylinux_2_31_aarch64.whl"
        )

    @pytest.mark.parametrize(
        "filename",
        [
            "black-26.5.1-py3-none-any.whl",
            "packaging-26.3-py3-none-any.whl",
        ],
    )
    def test_pure_python_wheel_keeps_the_check(self, filename):
        assert not M.wheel_ships_prebuilt_binaries(filename)

    def test_stable_abi_wheel_on_a_foreign_cpu(self):
        assert M.wheel_ships_prebuilt_binaries(
            "foo-1.0-cp38-abi3-manylinux_2_17_x86_64.whl"
        )

    def test_unparsable_name_keeps_the_check(self):
        assert not M.wheel_ships_prebuilt_binaries("not-a-wheel.txt")
        assert not M.wheel_ships_prebuilt_binaries("")


def _elf64_bytes(
    needed: list[str],
    soname: str | None = None,
    *,
    vaddr_base: int = 0x400000,
    elf_class: int = 2,
    elf_data: int = 1,
    versions: tuple[str, ...] = (),
) -> bytes:
    """
    A minimal dynamic ELF64 that py2bbr's reader understands.

    TODO At this point completely generated by an LLM (forgot which one)
         and used as-is.
    """
    strings: dict[str, int] = {}
    table = b"\x00"
    version_names = list(versions)
    base_name = soname or "libbase.so.1"  # the .gnu.version_d file-name node
    for name in (
        list(needed)
        + ([soname] if soname else [])
        + version_names
        + [base_name]
    ):
        if name not in strings:
            strings[name] = len(table)
            table += name.encode() + b"\x00"
    vd_count = len(version_names) + 1  # base node names the library file
    verdef_size = vd_count * 28
    entries = [
        (5, vaddr_base + 0),  # DT_STRTAB placeholder, patched below
        (10, len(table)),  # DT_STRSZ
    ]
    for name in needed:
        entries.append((1, strings[name]))  # DT_NEEDED
    if soname is not None:
        entries.append((14, strings[soname]))  # DT_SONAME
    entries.append((0x6FFFFFFC, 0))  # DT_VERDEF placeholder, patched below
    entries.append((0x6FFFFFFD, vd_count))  # DT_VERDEFNUM
    entries.append((0, 0))  # DT_NULL

    phdr_off = 64
    dyn_off = phdr_off + 2 * 56
    strtab_off = dyn_off + len(entries) * 16
    verdef_off = strtab_off + len(table)
    size = verdef_off + verdef_size
    entries[0] = (5, vaddr_base + strtab_off)  # real string-table vaddr
    entries[-3] = (0x6FFFFFFC, vaddr_base + verdef_off)  # real verdef vaddr

    out = bytearray(size)
    out[0:4] = b"\x7fELF"
    out[4] = elf_class
    out[5] = elf_data
    struct.pack_into("<H", out, 16, 3)  # ET_DYN
    struct.pack_into("<H", out, 18, 183)  # EM_AARCH64
    struct.pack_into("<I", out, 20, 1)
    struct.pack_into("<Q", out, 32, phdr_off)
    struct.pack_into("<H", out, 52, 64)  # e_ehsize
    struct.pack_into("<H", out, 54, 56)  # e_phentsize
    struct.pack_into("<H", out, 56, 2)  # e_phnum

    # PT_LOAD covering the whole file at the given virtual base.
    struct.pack_into("<I", out, phdr_off, 1)
    struct.pack_into("<I", out, phdr_off + 4, 6)  # RW
    struct.pack_into("<Q", out, phdr_off + 8, 0)
    struct.pack_into("<Q", out, phdr_off + 16, vaddr_base)
    struct.pack_into("<Q", out, phdr_off + 24, vaddr_base)
    struct.pack_into("<Q", out, phdr_off + 32, size)
    struct.pack_into("<Q", out, phdr_off + 40, size)
    struct.pack_into("<Q", out, phdr_off + 48, 8)

    # PT_DYNAMIC.
    struct.pack_into("<I", out, phdr_off + 56, 2)
    struct.pack_into("<I", out, phdr_off + 60, 6)
    struct.pack_into("<Q", out, phdr_off + 64, dyn_off)
    struct.pack_into("<Q", out, phdr_off + 72, vaddr_base + dyn_off)
    struct.pack_into("<Q", out, phdr_off + 80, vaddr_base + dyn_off)
    struct.pack_into("<Q", out, phdr_off + 88, len(entries) * 16)
    struct.pack_into("<Q", out, phdr_off + 96, len(entries) * 16)
    struct.pack_into("<Q", out, phdr_off + 104, 8)

    for i, (tag, val) in enumerate(entries):
        struct.pack_into("<q", out, dyn_off + i * 16, tag)
        struct.pack_into("<Q", out, dyn_off + i * 16 + 8, val)
    # Bounded write: a bare slice assignment would resize the buffer down to
    # strtab + table, truncating the verdef region that follows it.
    out[strtab_off : strtab_off + len(table)] = table

    # .gnu.version_d: a base node (the library file name, dropped by the
    # reader) then one 20-byte Elf64_Verdef + 8-byte Elf64_Verdaux per version.
    for i, vname in enumerate([base_name] + version_names):
        node = verdef_off + i * 28
        struct.pack_into("<H", out, node, 1)  # vd_version
        struct.pack_into("<H", out, node + 2, 0)  # vd_flags
        struct.pack_into("<H", out, node + 4, 1)  # vd_cnt
        struct.pack_into("<I", out, node + 8, 0)  # vd_hash
        struct.pack_into("<I", out, node + 12, 20)  # vd_aux -> first aux
        # vd_next: 28 for every node but the last.
        struct.pack_into(
            "<I", out, node + 16, 28 if i < len(version_names) else 0
        )
        aux = node + 20
        struct.pack_into("<I", out, aux, strings[vname])  # vda_name
        struct.pack_into("<I", out, aux + 4, 0)  # vda_next
    return bytes(out)


class TestWheelExternalNeededLibs:
    @staticmethod
    def _wheel(
        tmp_path: Path, members: dict[str, bytes], name: str = "pkg-1.0-any.whl"
    ) -> Path:
        path = tmp_path / name
        with zipfile.ZipFile(path, "w") as zf:
            for member, data in members.items():
                zf.writestr(member, data)
        return path

    def test_external_vs_self_provided_split(self, tmp_path):
        core = _elf64_bytes(
            [
                "libglib-2.0.so.0",
                "libgobject-2.0.so.0",
                "libself-a1b2c3.so.1",
                "libc.so.6",
            ]
        )
        sidecar = _elf64_bytes(
            ["libglib-2.0.so.0", "libc.so.6"], soname="libself-a1b2c3.so.1"
        )
        whl = self._wheel(
            tmp_path,
            {
                "pkg/_core.cpython-312-aarch64-linux-gnu.so": core,
                "pkg.libs/libself-a1b2c3.so.1": sidecar,
            },
            "pkg-1.0-cp312-cp312-manylinux_2_31_aarch64.whl",
        )
        # All that are leaf libs, and no part of OE rootfs already.
        assert M.wheel_external_needed_libs(whl) == [
            "libglib-2.0.so.0",
            "libgobject-2.0.so.0",
        ]

    def test_toolchain_runtime_is_not_a_finding(self, tmp_path):
        # All OE rootfs with python3 has these, so not "external"
        core = _elf64_bytes(
            [
                "libc.so.6",
                "libm.so.6",
                "libpthread.so.0",
                "librt.so.1",
                "libdl.so.2",
                "libgcc_s.so.1",
                "libstdc++.so.6",
                "ld-linux-aarch64.so.1",
            ]
        )
        whl = self._wheel(tmp_path, {"pkg/_core.so": core})
        assert M.wheel_external_needed_libs(whl) == []

    def test_toolchain_soname_match(self):
        assert M._is_toolchain_soname("libc.so.6")
        assert M._is_toolchain_soname("ld-linux-aarch64.so.1")
        assert M._is_toolchain_soname("ld-musl-x86_64.so.1")
        assert not M._is_toolchain_soname("libgstreamer-1.0.so.0")
        assert not M._is_toolchain_soname("libz.so.1")

    def test_unparsable_members_and_archives_yield_empty(self, tmp_path):
        whl = self._wheel(tmp_path, {"pkg/__init__.py": ""})
        assert M.wheel_external_needed_libs(whl) == []
        # A member that is not an ELF but has ".so" in its name.
        whl = self._wheel(
            tmp_path, {"pkg/README.so": b"text, definitely not ELF"}
        )
        assert M.wheel_external_needed_libs(whl) == []
        # A 32-bit ELF is not the shape manylinux wheels take; it is skipped.
        whl = self._wheel(
            tmp_path,
            {"pkg/_core.so": _elf64_bytes(["libglib-2.0.so.0"], elf_class=1)},
        )
        assert M.wheel_external_needed_libs(whl) == []
        # Not a zip at all: Best-effort failure, no error.
        bogus = tmp_path / "pkg-1.0-cp312-cp312-manylinux_2_31_aarch64.whl"
        bogus.write_bytes(b"not a zip archive")
        assert M.wheel_external_needed_libs(bogus) == []

    def test_elf_reader_defensive_branches(self):
        assert M._elf64_dynamic_tags(b"") == (None, [], [])

        def header_head():
            header = bytearray(64)
            header[0:4] = b"\x7fELF"
            header[4] = 2
            header[5] = 1
            return header

        # e_phentsize == 0 (no program header table).
        header = header_head()
        struct.pack_into("<H", header, 16, 3)
        struct.pack_into("<H", header, 56, 1)
        assert M._elf64_dynamic_tags(bytes(header)) == (None, [], [])

        # phpnum points past EOF -> the phdr scan breaks early and there is no
        # dynamic section.
        header = header_head()
        struct.pack_into("<Q", header, 32, 64)
        struct.pack_into("<H", header, 54, 56)
        struct.pack_into("<H", header, 56, 2)
        assert M._elf64_dynamic_tags(bytes(header)) == (None, [], [])

        # A PT_DYNAMIC that never sets DT_STRTAB.
        header = header_head()
        phoff, doff, dynsz = 64, 64 + 56, 32
        blob = bytearray(phoff + 56 + dynsz)  # header + 1 phdr + 2 dyn entries
        blob[0:64] = header
        struct.pack_into("<Q", blob, 32, phoff)
        struct.pack_into("<H", blob, 54, 56)
        struct.pack_into("<H", blob, 56, 1)
        struct.pack_into("<I", blob, phoff, 2)  # PT_DYNAMIC
        struct.pack_into("<Q", blob, phoff + 8, doff)
        struct.pack_into("<Q", blob, phoff + 16, doff)
        struct.pack_into("<Q", blob, phoff + 32, dynsz)
        struct.pack_into("<q", blob, doff, 1)  # DT_NEEDED
        struct.pack_into("<Q", blob, doff + 8, 98)  # index into nothing
        struct.pack_into("<q", blob, doff + 16, 0)  # DT_NULL
        assert M._elf64_dynamic_tags(bytes(blob)) == (None, [], [])

        # String-table lookups: out-of-range index, and a pointer with no NUL
        # terminator within the cap.
        assert M._elf_strtab_lookup(b"ab\x00cd", 0, 5, 99) == ""
        assert M._elf_strtab_lookup(b"ab" + b"\xff" * 600, 0, 602, 2) == ""


class TestWheelBundledLibVersions:
    @staticmethod
    def _wheel(
        tmp_path: Path, members: dict[str, bytes], name: str = "pkg-1.0-any.whl"
    ) -> Path:
        path = tmp_path / name
        with zipfile.ZipFile(path, "w") as zf:
            for member, data in members.items():
                zf.writestr(member, data)
        return path

    def test_bundled_sidecar_versions(self, tmp_path):
        blkid = _elf64_bytes(
            [],
            soname="libblkid-e4a77f31.so.1.1.0",
            versions=("BLKID_1.0", "BLKID_2.17"),
        )
        udev = _elf64_bytes(
            [], soname="libudev-8d5af18d.so.1.6.11", versions=("LIBUDEV_183",)
        )
        whl = self._wheel(
            tmp_path,
            {
                "pyvizionsdk.libs/libblkid-e4a77f31.so.1.1.0": blkid,
                "pyvizionsdk.libs/libudev-8d5af18d.so.1.6.11": udev,
            },
        )
        assert M.wheel_bundled_lib_versions(whl) == {
            "libblkid-e4a77f31.so.1.1.0": ["BLKID_1.0", "BLKID_2.17"],
            "libudev-8d5af18d.so.1.6.11": ["LIBUDEV_183"],
        }

    def test_extension_modules_contribute_nothing(self, tmp_path):
        core = _elf64_bytes(["libglib-2.0.so.0"])
        sidecar = _elf64_bytes(
            [], soname="libgstvideo-1.0.so.0", versions=("GSTVIDEO_1_0",)
        )
        whl = self._wheel(
            tmp_path,
            {
                "pyvizionsdk/_core.cpython-312-aarch64-linux-gnu.so": core,
                "pyvizionsdk.libs/libgstvideo-1.0.so.0": sidecar,
            },
        )
        assert M.wheel_bundled_lib_versions(whl) == {
            "libgstvideo-1.0.so.0": ["GSTVIDEO_1_0"]
        }

    def test_sorted_and_deduplicated(self, tmp_path):
        blob = _elf64_bytes([], soname="libm.so.6", versions=("GLIBC_2.17",))
        whl = self._wheel(
            tmp_path,
            {
                "pkg.libs/libm.so.6": blob,
                "pkg.libs/dupe/libm.so.6": blob,
            },
        )
        assert M.wheel_bundled_lib_versions(whl) == {
            "libm.so.6": ["GLIBC_2.17"]
        }

    def test_best_effort_failures(self, tmp_path):
        bogus = tmp_path / "pkg-1.0-cp312-cp312-manylinux_2_31_aarch64.whl"
        bogus.write_bytes(b"not a zip archive")
        assert M.wheel_bundled_lib_versions(bogus) == {}
        empty = self._wheel(tmp_path, {"pkg/README.txt": b"no binaries"})
        assert M.wheel_bundled_lib_versions(empty) == {}


class TestVcsBuildSystem:
    @staticmethod
    def _serve(monkeypatch, body: bytes | None, *, seen: list | None = None):
        """Replace the network with a one-URL answer, recording the request."""

        def fake(url, timeout):
            if seen is not None:
                seen.append(url)
            return body

        monkeypatch.setattr(M, "_fetch_bytes", fake)

    def test_backend_is_read_from_the_project_repository(self, monkeypatch):
        self._serve(
            monkeypatch,
            b'[build-system]\nrequires = ["meson-python>=0.13"]\n'
            b'build-backend = "mesonpy"\n',
        )
        found = M.build_system_from_vcs(
            [("Source", "https://github.com/vizion/pyvizionsdk")],
        )
        assert found == (
            "mesonpy",
            ["meson-python>=0.13"],
            "github.com/vizion/pyvizionsdk@pyproject.toml",
        )

    def test_url_shapes_all_reduce_to_the_repository(self, monkeypatch):
        seen: list[str] = []
        self._serve(
            monkeypatch,
            b'[build-system]\nbuild-backend = "hatchling.build"\n',
            seen=seen,
        )
        for url in (
            "https://github.com/o/r",
            "https://github.com/o/r.git",
            "git+https://github.com/o/r.git@v1.0#egg=r",
            "https://github.com/o/r/tree/main/sub/pkg",
        ):
            assert M.build_system_from_vcs([("Repository", url)]) is not None
        assert (
            seen
            == ["https://raw.githubusercontent.com/o/r/HEAD/pyproject.toml"] * 4
        )

    def test_forged_host_is_not_read_as_the_real_forge(self, monkeypatch):
        assert M._vcs_repo("https://github.com.evil.example/o/r") is None
        assert M._vcs_repo("https://notgithub.example/o/r") is None

    def test_non_repository_paths_are_rejected(self):
        assert M._vcs_repo("") is None
        assert M._vcs_repo("https://github.com/just-owner") is None
        assert M._vcs_repo("https://github.com") is None

    @pytest.mark.parametrize(
        ("url", "raw"),
        [
            (
                "https://github.com/o/r",
                "https://raw.githubusercontent.com/o/r/HEAD/pyproject.toml",
            ),
            (
                "https://gitlab.com/g/o/r",
                "https://gitlab.com/g/o/r/-/raw/HEAD/pyproject.toml",
            ),
            (
                "https://bitbucket.org/o/r",
                "https://bitbucket.org/o/r/raw/HEAD/pyproject.toml",
            ),
            (
                "https://github.com/o/r/tree/main/sub",
                "https://raw.githubusercontent.com/o/r/HEAD/pyproject.toml",
            ),
        ],
    )
    def test_forge_urls_map_to_their_raw_endpoint(self, monkeypatch, url, raw):
        seen: list[str] = []
        self._serve(
            monkeypatch,
            b'[build-system]\nbuild-backend = "hatchling.build"\n',
            seen=seen,
        )
        assert M.build_system_from_vcs([("Repository", url)]) is not None
        assert seen == [raw]

    def test_fetch_bytes_network_calls(self, monkeypatch):
        import urllib.error
        import urllib.request

        class _Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"pyproject"

        def ok(request, timeout):
            sent["url"] = request.full_url
            return _Response()

        sent: dict[str, str] = {}
        monkeypatch.setattr(urllib.request, "urlopen", ok)
        url = "https://raw.githubusercontent.com/o/r/HEAD/pyproject.toml"
        assert M._fetch_bytes(url, 0.1) == b"pyproject"
        assert sent["url"] == url

        class _404:
            status = 404

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                raise AssertionError("must not read a 404 body")

        monkeypatch.setattr(urllib.request, "urlopen", lambda r, **kw: _404())
        assert M._fetch_bytes(url, 0.1) is None

        def down(request, timeout):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", down)
        assert M._fetch_bytes(url, 0.1) is None

    def test_self_hosted_forge_is_skipped(self, monkeypatch):
        seen: list[str] = []
        self._serve(monkeypatch, b"x", seen=seen)
        assert (
            M.build_system_from_vcs([("Repo", "https://git.corp/o/r")]) is None
        )
        assert seen == []

    def test_labelled_url_beats_a_homepage_that_only_looks_like_a_repo(
        self, monkeypatch
    ):
        seen: list[str] = []
        self._serve(
            monkeypatch,
            b'[build-system]\nbuild-backend = "flit_core.buildapi"\n',
            seen=seen,
        )
        M.build_system_from_vcs(
            [("Homepage", "https://github.com/other/wrong")],
            "https://github.com/right/right",
        )
        assert seen == [
            "https://raw.githubusercontent.com/right/right/HEAD/pyproject.toml"
        ]

    def test_duplicate_urls_are_fetched_once(self, monkeypatch):
        seen: list[str] = []
        self._serve(
            monkeypatch,
            b'[build-system]\nbuild-backend = "hatchling.build"\n',
            seen=seen,
        )
        M.build_system_from_vcs(
            [
                ("Repository", "https://github.com/o/r"),
                ("Source", "https://github.com/o/r/tree/main"),
            ]
        )
        assert len(seen) == 1

    def test_an_unreachable_forge_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(M, "_fetch_bytes", lambda url, timeout: None)
        assert (
            M.build_system_from_vcs([("Repo", "https://github.com/o/r")])
            is None
        )

    def test_an_html_error_page_is_not_a_backend(self, monkeypatch):
        self._serve(monkeypatch, b"<!doctype html><html>404</html>")
        assert (
            M.build_system_from_vcs([("Repo", "https://github.com/o/r")])
            is None
        )

    def test_file_without_build_system_is_weak_and_skipped(self, monkeypatch):
        seen: list[str] = []
        self._serve(
            monkeypatch,
            b"[project]\nname = 'r'\n",
            seen=seen,
        )
        assert (
            M.build_system_from_vcs(
                [("Repo", "https://github.com/o/r")],
            )
            is None
        )
        assert len(seen) == 1

    def test_metadata_records_the_provenance_it_used(
        self, tmp_path, monkeypatch
    ):
        self._serve(
            monkeypatch,
            b'[build-system]\nbuild-backend = "mesonpy"\nrequires = ["cython"]\n',
        )
        monkeypatch.setattr(
            M,
            "fetch_source",
            lambda *a, **kw: _wheel_src(tmp_path, _WHEEL_META),
        )
        meta, _source = M.build_metadata(
            "pyvizionsdk", "26.9.1", PipOptions(), tmp_path
        )
        assert meta.build_backend == "mesonpy"
        assert meta.build_deps[0].name == "cython"
        assert meta.build_backend_source == (
            "github.com/vizion/pyvizionsdk@pyproject.toml"
        )

    def test_wheel_that_declares_nothing_stays_unknown(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(M, "_fetch_bytes", lambda url, timeout: None)
        monkeypatch.setattr(
            M,
            "fetch_source",
            lambda *a, **kw: _wheel_src(tmp_path, _WHEEL_META),
        )
        meta, _source = M.build_metadata(
            "pyvizionsdk", "26.9.1", PipOptions(), tmp_path
        )
        assert meta.build_backend == ""
        assert meta.build_backend_source == ""

    def test_legacy_sdist_is_not_answered_by_the_repository(
        self, tmp_path, monkeypatch
    ):
        seen: list[str] = []
        self._serve(
            monkeypatch,
            b'[build-system]\nbuild-backend = "mesonpy"\n',
            seen=seen,
        )
        src = _sdist_src(
            _sdist(tmp_path, {"pyvizionsdk-26.9.1/PKG-INFO": _WHEEL_META})
        )
        monkeypatch.setattr(M, "fetch_source", lambda *a, **kw: src)
        meta, _source = M.build_metadata(
            "pyvizionsdk", "26.9.1", PipOptions(), tmp_path
        )
        assert meta.build_backend == ""
        assert meta.build_backend_source == ""
        assert seen == []
