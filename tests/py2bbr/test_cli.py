"""
Unit tests on CLI functions
"""

import runpy
import sys

import pytest

from py2bbr import cli
from py2bbr.errors import Py2BbrError
from py2bbr.metadata import FetchedSource
from py2bbr.model import SourceKind


class TestParsePackageSpec:
    def test_name_no_spec(self):
        assert cli._parse_package_spec("name") == ("name", None)

    def test_name_exact_version(self):
        assert cli._parse_package_spec("name==1.2.3") == ("name", "1.2.3")
        assert cli._parse_package_spec("name===1.2.3") == ("name", "1.2.3")

    def test_name_fuzzy_version(self):
        assert cli._parse_package_spec("name>1.2.3") == ("name", None)

    def test_name_fuzzy_error(self):
        with pytest.raises(
            Py2BbrError, match="invalid package spec 'bad name'"
        ):
            cli._parse_package_spec("bad name")


class TestBuildParser:
    def test_defaults(self):
        args = cli.build_parser().parse_args(["black"])
        assert args.package == "black"
        assert args.output == "."
        assert args.check == "parse"
        assert args.yocto_version == "5.0"
        assert args.extra_index_url == []
        assert args.trusted_host == []
        assert args.pip_arg == []
        assert args.timeout == 0
        assert args.check_timeout == 1800
        assert not args.quiet and not args.verbose

    def test_flags(self):
        args = cli.build_parser().parse_args(
            [
                "black==1.0",
                "--yocto-version",
                "6.0",
                "--check",
                "build",
                "--extra-index-url",
                "https://i1",
                "--extra-index-url",
                "https://i2",
                "--trusted-host",
                "h1",
                "--pip-arg",
                "--retries",
                "--timeout",
                "30",
                "--build-class",
                "setuptools3",
                "--machine",
                "qemarmv7a",
                "-o",
                "out",
                "--layer-name",
                "meta-x",
            ]
        )
        assert args.yocto_version == "6.0"
        assert args.check == "build"
        assert args.extra_index_url == ["https://i1", "https://i2"]
        assert args.trusted_host == ["h1"]
        assert args.pip_arg == ["--retries"]
        assert args.timeout == 30
        assert args.build_class == "setuptools3"
        assert args.machine == "qemarmv7a"
        assert args.output == "out"
        assert args.layer_name == "meta-x"


def _source(tmp_path) -> FetchedSource:
    p = tmp_path / "black-26.5.1.tar.gz"
    p.write_bytes(b"artifact")
    return FetchedSource(p, SourceKind.SDIST, "sha", p.name)


def _fake_build_metadata(meta, src):
    def fn(*args, **kwargs):
        return meta, src

    return fn


class TestMain:
    def test_end_to_end(self, tmp_path, monkeypatch, meta):
        monkeypatch.setattr(
            cli, "build_metadata", _fake_build_metadata(meta, _source(tmp_path))
        )
        rc = cli.main(
            ["black", "-o", str(tmp_path / "out"), "--check", "structural"]
        )
        assert rc == 0
        out = tmp_path / "out"
        assert (
            out
            / "meta-black"
            / "recipes-devtools"
            / "python"
            / "python3-black_26.5.1.bb"
        ).is_file()
        assert (out / "meta-black" / "conf" / "layer.conf").is_file()
        assert (out / "py2bbr.kas.yml").is_file()

    def test_no_layer_quiet_keep_sources(
        self, tmp_path, monkeypatch, meta, capsys
    ):
        monkeypatch.setattr(
            cli, "build_metadata", _fake_build_metadata(meta, _source(tmp_path))
        )
        rc = cli.main(
            [
                "black",
                "-o",
                str(tmp_path / "o"),
                "--no-layer",
                "-q",
                "--keep-sources",
                "--check",
                "structural",
            ]
        )
        assert rc == 0
        assert (tmp_path / "o" / "python3-black_26.5.1.bb").is_file()
        assert not (tmp_path / "o" / "py2bbr.kas.yml").exists()
        assert "Sources kept in" in capsys.readouterr().out

    def test_error_exit_code(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise Py2BbrError("boom")

        monkeypatch.setattr(cli, "build_metadata", boom)
        assert cli.main(["black", "-o", str(tmp_path)]) == 2

    def test_interrupt_exit_code(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "build_metadata", boom)
        assert cli.main(["black", "-o", str(tmp_path)]) == 130

    def test_module_entry_points(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["py2bbr", "--version"])
        for mod in ("py2bbr", "py2bbr.cli"):
            with pytest.raises(SystemExit) as exc:
                runpy.run_module(mod, run_name="__main__")
            assert exc.value.code == 0
