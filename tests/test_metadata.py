"""Tests for py2bbr.metadata (pure functions, no network)."""

from __future__ import annotations

import email
from pathlib import Path

from py2bbr.metadata import (
    PipOptions,
    _marker_env,
    _pick_artifact,
    classify_requires_dist,
    detect_license,
)
from py2bbr.model import DepKind
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


class TestMarkerEnv:
    def test_target_python_version(self):
        y = resolve_yocto_version("5.0")
        env = _marker_env(y)
        assert env["python_version"] == "3.12"
        assert env["sys_platform"] == "linux"
