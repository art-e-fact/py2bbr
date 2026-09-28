"""
Unit tests on CLI functions
"""

import logging
import platform
import runpy
import subprocess
import sys
from unittest.mock import patch

import pytest
from packaging.version import Version

from py2bbr import __version__, cli
from py2bbr import metadata as M
from py2bbr.errors import MetadataNotFoundError, Py2BbrError
from py2bbr.metadata import FetchedSource
from py2bbr.model import (
    Dependency,
    DepKind,
    PackageMetadata,
    SourceKind,
)
from py2bbr.recipe import _TOOL_COMPANION_REQUIREMENTS, canonical_name
from py2bbr.yocto import current_python_version


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
        assert args.with_deps is True

    def test_target_runtime_defaults_to_this_machine(self):
        # The layer is built here and (usually) runs here, so the running
        # interpreter and this machine's CPU are the least surprising answers.
        args = cli.build_parser().parse_args(["black"])
        assert args.python_version == (
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )
        assert args.target_arch == (platform.machine().lower() or "x86_64")

    def test_deps_flag(self):
        assert (
            cli.build_parser().parse_args(["black", "--no-deps"]).with_deps
            is False
        )
        assert (
            cli.build_parser()
            .parse_args(["black", "--no-deps", "--with-deps"])
            .with_deps
            is True
        )

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
                "--python-version",
                "3.12",
                "--target-arch",
                "aarch64",
                "-o",
                "out",
                "--layer-name",
                "meta-x",
            ]
        )
        assert args.yocto_version == "6.0"
        assert args.python_version == "3.12"
        assert args.target_arch == "aarch64"
        assert args.check == "build"
        assert args.extra_index_url == ["https://i1", "https://i2"]
        assert args.trusted_host == ["h1"]
        assert args.pip_arg == ["--retries"]
        assert args.timeout == 30
        assert args.build_class == "setuptools3"
        assert args.machine == "qemarmv7a"
        assert args.output == "out"
        assert args.layer_name == "meta-x"

    def test_jobs_default_and_flag(self):
        assert cli.build_parser().parse_args(["black"]).jobs == 4
        args = cli.build_parser().parse_args(["black", "--jobs", "2"])
        assert args.jobs == 2


def _source(tmp_path) -> FetchedSource:
    p = tmp_path / "black-26.5.1.tar.gz"
    p.write_bytes(b"artifact")
    return FetchedSource(p, SourceKind.SDIST, "sha", p.name)


def _fake_build_metadata(meta, src):
    def fn(*args, **kwargs):
        return meta, src

    return fn


def _dep_aware_fake(meta, src, make_meta_fn):
    metas: dict[str, PackageMetadata] = {}

    def fn(name, *a, **k):
        if name == meta.name:
            return meta, src
        if name not in metas:
            metas[name] = make_meta_fn(
                name=name,
                canonical_name=name,
                version="1.0.0",
                runtime_deps=[],
            )
        return metas[name], src

    return fn


class TestMain:
    def test_end_to_end(self, tmp_path, monkeypatch, meta, make_meta_fn):
        src = _source(tmp_path)
        monkeypatch.setattr(
            cli, "build_metadata", _dep_aware_fake(meta, src, make_meta_fn)
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
        # Default: the layer is self-contained, i.e. one recipe per closure
        # member (click, packaging) is generated next to the top recipe.
        assert (
            out
            / "meta-black"
            / "recipes-devtools"
            / "python"
            / "python3-click_1.0.0.bb"
        ).is_file()
        assert (
            out
            / "meta-black"
            / "recipes-devtools"
            / "python"
            / "python3-packaging_1.0.0.bb"
        ).is_file()
        assert (out / "meta-black" / "conf" / "layer.conf").is_file()
        assert (out / "py2bbr.kas.yml").is_file()

    def test_wheel_report_and_recipe_waive_the_strip_check(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        # A vendored wheel carries no build config, so build_metadata read
        # [build-system] from the project's repository; the report says where
        # the answer came from instead of letting it read as the release's own
        # declaration. And upstream shipped the extension modules already
        # stripped, so the recipe waives bitbake's fatal already-stripped QA
        # check - the reported do_package failure for pyvizionsdk.
        meta = make_meta_fn(
            name="pyvizionsdk",
            canonical_name="pyvizionsdk",
            version="26.9.1",
            homepage="",
            project_urls=[],
            source_kind=SourceKind.WHEEL,
            source_filename=(
                "pyvizionsdk-26.9.1-cp312-cp312-manylinux_2_31_aarch64.whl"
            ),
            vendored=True,
            ships_prebuilt_binaries=True,
            build_backend="mesonpy",
            build_backend_source="github.com/vizion/pyvizionsdk@pyproject.toml",
        )
        src_path = tmp_path / meta.source_filename
        src_path.write_bytes(b"wheel payload")
        src = FetchedSource(
            src_path, SourceKind.WHEEL, "sha", meta.source_filename
        )
        monkeypatch.setattr(
            cli, "build_metadata", _fake_build_metadata(meta, src)
        )
        rc = cli.main(
            [
                "pyvizionsdk",
                "-o",
                str(tmp_path / "out"),
                "--check",
                "structural",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "Build backend: mesonpy" in out
        assert "(read from github.com/vizion/pyvizionsdk@pyproject.toml)" in out
        assert "waives bitbake's fatal already-stripped QA check" in out
        recipe = (
            tmp_path
            / "out"
            / "meta-pyvizionsdk"
            / "recipes-devtools"
            / "python"
            / "python3-pyvizionsdk_26.9.1.bb"
        )
        assert recipe.is_file()
        assert 'INSANE_SKIP:${PN} = "already-stripped"' in recipe.read_text()

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

    def test_module_entry_point(self, monkeypatch):
        # ``python -m py2bbr`` goes through py2bbr/__main__.py. runpy can
        # re-execute that here: the target is the package itself, so nothing
        # is shadowed and runpy stays quiet.
        monkeypatch.setattr(sys, "argv", ["py2bbr", "--version"])
        with pytest.raises(SystemExit) as exc:
            runpy.run_module("py2bbr", run_name="__main__")
        assert exc.value.code == 0

    def test_submodule_entry_point(self):
        # ``python -m py2bbr.cli`` cannot be checked with runpy in-process:
        # this test module has already imported py2bbr.cli, and re-executing
        # that source under the name __main__ here is not what -m does (it
        # imports it fresh). runpy warns about exactly that ("found in
        # sys.modules after import of package ... but prior to execution"),
        # and the warning is right - a subprocess is the faithful check.
        proc = subprocess.run(
            [sys.executable, "-m", "py2bbr.cli", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == f"py2bbr {__version__}"

    def test_jobs_passed_to_resolve_closure(
        self, tmp_path, monkeypatch, meta, make_meta_fn
    ):
        seen = {}

        def fake_resolve(top, builder, **kwargs):
            seen.update(kwargs)
            return [top], []

        monkeypatch.setattr(M, "resolve_closure", fake_resolve)
        monkeypatch.setattr(
            cli,
            "build_metadata",
            _dep_aware_fake(meta, _source(tmp_path), make_meta_fn),
        )
        rc = cli.main(
            [
                "black",
                "-o",
                str(tmp_path / "out"),
                "--jobs",
                "2",
                "--check",
                "structural",
            ]
        )
        assert rc == 0
        assert seen["workers"] == 2


class TestFetchProgress:
    def test_disabled_is_noop(self):
        prog = cli._FetchProgress(enabled=False)
        assert prog._progress is None
        with prog.fetch("click>=8.0.0"):
            pass  # must not raise
        prog.start()
        prog.stop()

    def test_enabled_tracks_task_lifecycle(self):
        prog = cli._FetchProgress(enabled=True)
        assert prog._progress is not None
        with prog.fetch("click>=8.0.0"):
            task_ids = frozenset(prog._progress.task_ids)
            assert len(task_ids) == 1
            (tid,) = task_ids
            description = prog._progress.tasks[tid].description
            assert "click>=8.0.0" in description
        assert len(prog._progress.task_ids) == 0


class TestLogging:
    """INFO stage progress on stderr; -q suppresses it, -v adds DEBUG."""

    def test_info_stage_progress_by_default(
        self, tmp_path, monkeypatch, meta, make_meta_fn, caplog
    ):
        monkeypatch.setattr(
            cli,
            "build_metadata",
            _dep_aware_fake(meta, _source(tmp_path), make_meta_fn),
        )
        with caplog.at_level(logging.INFO):
            rc = cli.main(
                ["black", "-o", str(tmp_path / "out"), "--check", "structural"]
            )
        assert rc == 0
        text = caplog.text
        assert "fetching black==latest" in text
        assert "closure: 3 package(s) resolved" in text
        assert "wrote layer" in text
        assert "wrote kas file" in text
        assert "check structural" in text
        assert not any(r.levelno == logging.DEBUG for r in caplog.records)

    def test_quiet_suppresses_info(self, tmp_path, monkeypatch, meta, caplog):
        monkeypatch.setattr(
            cli, "build_metadata", _fake_build_metadata(meta, _source(tmp_path))
        )
        with caplog.at_level(logging.INFO):
            rc = cli.main(
                [
                    "black",
                    "-o",
                    str(tmp_path / "o"),
                    "-q",
                    "--check",
                    "structural",
                ]
            )
        assert rc == 0
        assert not any(r.levelno == logging.INFO for r in caplog.records)

    def test_verbose_enables_debug(self, tmp_path, monkeypatch, meta, caplog):
        monkeypatch.setattr(
            cli, "build_metadata", _fake_build_metadata(meta, _source(tmp_path))
        )
        with caplog.at_level(logging.DEBUG):
            rc = cli.main(
                [
                    "black",
                    "-o",
                    str(tmp_path / "o"),
                    "-v",
                    "--no-layer",
                    "--no-kas",
                    "--check",
                    "structural",
                ]
            )
        assert rc == 0
        assert any(r.levelno == logging.DEBUG for r in caplog.records)

    def test_configure_logging_routes_through_rich_handler_when_console_given(
        self,
    ):
        from rich.console import Console

        lg = logging.getLogger("py2bbr")
        saved = list(lg.handlers)
        lg.handlers.clear()
        try:
            console = Console()
            args = type("Args", (), {"verbose": False, "quiet": False})()
            cli._configure_logging(args, console=console)
            assert len(lg.handlers) == 1
            assert isinstance(lg.handlers[0], cli._RichLogHandler)
            assert lg.handlers[0]._console is console
        finally:
            lg.handlers[:] = saved

    def test_rich_log_handler_prints_full_line_through_console(self):
        import io

        from rich.console import Console

        out = io.StringIO()
        console = Console(file=out, force_terminal=False)
        lg = logging.getLogger("py2bbr")
        saved = list(lg.handlers)
        lg.handlers.clear()
        try:
            args = type("Args", (), {"verbose": True, "quiet": False})()
            cli._configure_logging(args, console=console)
            lg.debug("some detail line")
            assert "some detail line" in out.getvalue()
        finally:
            lg.handlers[:] = saved


class TestTargetReport:
    """The build-for-target report: the cross-build answer for a recipe.

    A layer is built on an x86_64 host for an ARM target, so the report has to
    be printed unconditionally (a recipe can parse and build on the host and
    still be useless for the target), and it has to name the *class* it judged,
    because ``--build-class`` changes the verdict.
    """

    def _run(
        self,
        tmp_path,
        monkeypatch,
        capsys,
        meta,
        make_meta_fn,
        *extra,
    ) -> str:
        monkeypatch.setattr(
            cli,
            "build_metadata",
            _dep_aware_fake(meta, _source(tmp_path), make_meta_fn),
        )
        assert (
            cli.main(
                [
                    "black",
                    "-o",
                    str(tmp_path / "out"),
                    "--check",
                    "structural",
                    *extra,
                ]
            )
            == 0
        )
        return capsys.readouterr().out

    def _native(self, make_meta_fn):
        return make_meta_fn(
            has_native_code=True,
            native_code_evidence=["C source (pkg/src/mod.c)"],
        )

    def test_pure_python_is_a_one_line_summary(
        self, tmp_path, monkeypatch, capsys, meta, make_meta_fn
    ):
        out = self._run(tmp_path, monkeypatch, capsys, meta, make_meta_fn)
        assert (
            "Build for target: OK on 10 target(s) (build class python_pep517, "
            "pure Python)" in out
        )

    def test_native_code_with_host_class_is_rendered(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        out = self._run(
            tmp_path,
            monkeypatch,
            capsys,
            self._native(make_meta_fn),
            make_meta_fn,
        )
        assert "Build for target (build class python_pep517" in out
        assert "RISK" in out
        assert "Zynq UltraScale+ MPSoC" in out

    def test_incompatible_target_adds_a_warning(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        meta = make_meta_fn(requires_python=">=3.13")
        out = self._run(tmp_path, monkeypatch, capsys, meta, make_meta_fn)
        assert "INCOMPATIBLE" in out
        assert "cannot build for at least one target" in out

    def test_build_class_override_is_reflected(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        out = self._run(
            tmp_path,
            monkeypatch,
            capsys,
            self._native(make_meta_fn),
            make_meta_fn,
            "--build-class",
            "setuptools3",
        )
        assert "build class setuptools3" in out
        assert "RISK" not in out

    def test_targets_flag_narrows_the_matrix(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        out = self._run(
            tmp_path,
            monkeypatch,
            capsys,
            self._native(make_meta_fn),
            make_meta_fn,
            "--targets",
            "zynqmp,versal",
        )
        assert "Zynq UltraScale+ MPSoC" in out
        assert "Versal / Versal AI" in out
        assert "Cortex-A53 (32-bit ARM)" not in out

    def test_unknown_target_warns_and_falls_back(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        out = self._run(
            tmp_path,
            monkeypatch,
            capsys,
            self._native(make_meta_fn),
            make_meta_fn,
            "--targets",
            "sparc64",
        )
        assert "unknown target 'sparc64' in --targets" in out
        # Fall back to the whole matrix rather than losing the report.
        assert "Cortex-A53 (32-bit ARM)" in out

    def test_duplicate_target_keys_are_collapsed(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        out = self._run(
            tmp_path,
            monkeypatch,
            capsys,
            self._native(make_meta_fn),
            make_meta_fn,
            "--targets",
            "zynqmp,aarch64",
        )
        assert out.count("Zynq UltraScale+ MPSoC") == 1

    def test_report_absent_is_a_noop(self):
        cli._print_target_report(None)


class TestGapProvisioning:
    """``--with-deps`` provisions a newer build tool into the layer.

    A ``build-system.requires`` floor above the target series' native sysroot
    (numpy wants Cython>=3.1.0, Scarthgap ships 3.0.8) otherwise fails the
    ``--no-isolation`` check inside the build. With a layer to put it in, the
    newer tool becomes a generated recipe of the layer itself.
    """

    def _gapped(self, make_meta_fn, **kw):
        return make_meta_fn(
            build_deps=[
                Dependency(
                    name="Cython",
                    kind=DepKind.BUILD,
                    requirement="Cython>=3.1.0",
                    specifier=">=3.1.0",
                )
            ],
            **kw,
        )

    def test_provisions_and_reports(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        meta = self._gapped(make_meta_fn)
        monkeypatch.setattr(
            cli,
            "build_metadata",
            _dep_aware_fake(meta, _source(tmp_path), make_meta_fn),
        )
        rc = cli.main(
            ["black", "-o", str(tmp_path / "out"), "--check", "structural"]
        )
        assert rc == 0
        out = tmp_path / "out" / "meta-black" / "recipes-devtools" / "python"
        assert (out / "python3-cython_1.0.0.bb").is_file()
        text = capsys.readouterr().out
        assert "provisioned newer build-backend recipe(s)" in text
        assert "use --with-deps" not in text

    def test_no_layer_warns_instead(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        # Without a layer there is nowhere to put the recipe, so the gap has to
        # be reported instead of silently ignored.
        meta = self._gapped(make_meta_fn)
        monkeypatch.setattr(
            cli,
            "build_metadata",
            _dep_aware_fake(meta, _source(tmp_path), make_meta_fn),
        )
        rc = cli.main(
            [
                "black",
                "-o",
                str(tmp_path / "out"),
                "--no-layer",
                "--check",
                "structural",
            ]
        )
        assert rc == 0
        assert "use --with-deps" in capsys.readouterr().out

    def test_dep_already_in_the_closure_is_not_provisioned_twice(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        # Cython is both a runtime dep and a gapped build tool: it is already
        # a closure member, so provisioning it again would write a second
        # recipe for the same package into the layer.
        meta = self._gapped(
            make_meta_fn,
            runtime_deps=[
                Dependency(
                    name="Cython",
                    kind=DepKind.RUNTIME,
                    requirement="Cython",
                )
            ],
        )
        seen = []
        inner = _dep_aware_fake(meta, _source(tmp_path), make_meta_fn)

        def fake_build(name, *a, **k):
            seen.append(name)
            return inner(name, *a, **k)

        monkeypatch.setattr(cli, "build_metadata", fake_build)
        rc = cli.main(
            ["black", "-o", str(tmp_path / "out"), "--check", "structural"]
        )
        assert rc == 0
        assert seen.count("Cython") == 1
        out = tmp_path / "out" / "meta-black" / "recipes-devtools" / "python"
        assert len(list(out.glob("python3-cython*.bb"))) == 1

    def test_unprovisionable_tool_is_a_warning(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        meta = self._gapped(make_meta_fn)
        inner = _dep_aware_fake(meta, _source(tmp_path), make_meta_fn)

        def fake_build(name, *a, **k):
            if name == "Cython":
                raise MetadataNotFoundError("no Cython on the index")
            return inner(name, *a, **k)

        monkeypatch.setattr(cli, "build_metadata", fake_build)
        rc = cli.main(
            ["black", "-o", str(tmp_path / "out"), "--check", "structural"]
        )
        assert rc == 0
        assert "could not provision Cython" in capsys.readouterr().out
        out = tmp_path / "out" / "meta-black" / "recipes-devtools" / "python"
        assert not (out / "python3-cython_1.0.0.bb").exists()

    def test_shadowing_recipe_warns_with_the_recovery(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        # Cython is in the series' bundled-tool table, so provisioning it does
        # not add a provider: it takes over poky's python3-cython. That swap
        # invalidates the previous version's sstate manifests, and a
        # signature-locked cache keeps the old setscene stamps, so the report
        # has to name the shadowed recipe and the command that repairs it.
        meta = self._gapped(make_meta_fn)
        monkeypatch.setattr(
            cli,
            "build_metadata",
            _dep_aware_fake(meta, _source(tmp_path), make_meta_fn),
        )
        rc = cli.main(
            ["black", "-o", str(tmp_path / "out"), "--check", "structural"]
        )
        assert rc == 0
        text = capsys.readouterr().out
        assert "python3-cython" in text
        assert "shadow" in text
        assert "petalinux-build -c cleansstate python3-cython-native" in text
        assert "could not be found" in text


class TestTransitiveProvisioning:
    """A provisioned build tool drags its own native companions with it.

    Regression: numpy 2.5.3 wants ``meson-python>=0.18.0`` (Scarthgap's native
    sysroot ships 0.15.0), so the layer gained ``python3-meson-python_0.22.0``.
    That recipe could not build: meson-python 0.18.0+ passes a third
    ``metadata_version`` argument to ``StandardMetadata.from_pyproject``, but the
    sysroot's ``pyproject-metadata`` is 0.7.1, whose method takes two --
    ``TypeError: ... takes from 2 to 3 positional arguments but 4 were given``.
    The companion is declared *unversioned* by meson-python, so only the
    companion table can supply the bound. Provisioning has to iterate: a single
    pass would stop at meson-python and still emit the unbuildable recipe.
    """

    def _gapped(self, make_meta_fn, **kw):
        return make_meta_fn(
            build_deps=[
                Dependency(
                    name="meson-python",
                    kind=DepKind.BUILD,
                    requirement="meson-python>=0.18.0",
                    specifier=">=0.18.0",
                )
            ],
            **kw,
        )

    def _fake_for(self, meta, tmp_path, make_meta_fn, versions):
        """Fake ``build_metadata`` returning a canned version per package."""
        inner = _dep_aware_fake(meta, _source(tmp_path), make_meta_fn)

        def fake_build(name, *a, **k):
            if name in versions:
                return (
                    make_meta_fn(
                        name=name,
                        canonical_name=canonical_name(name),
                        version=versions[name],
                    ),
                    _source(tmp_path),
                )
            return inner(name, *a, **k)

        return fake_build

    def test_provisions_tool_and_companion(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        meta = self._gapped(make_meta_fn)
        monkeypatch.setattr(
            cli,
            "build_metadata",
            self._fake_for(
                meta,
                tmp_path,
                make_meta_fn,
                {"meson-python": "0.22.0", "pyproject-metadata": "0.9.1"},
            ),
        )
        rc = cli.main(
            ["black", "-o", str(tmp_path / "out"), "--check", "structural"]
        )
        assert rc == 0
        out = tmp_path / "out" / "meta-black" / "recipes-devtools" / "python"
        assert (out / "python3-meson-python_0.22.0.bb").is_file()
        # The companion that makes the provisioned tool buildable.
        assert (out / "python3-pyproject-metadata_0.9.1.bb").is_file()

    def test_companion_skipped_when_sysroot_is_new_enough(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        # Nothing to drag in: the target sysroot already ships a new-enough
        # pyproject-metadata, so no shadowing recipe is written.
        meta = self._gapped(make_meta_fn)
        monkeypatch.setattr(
            cli,
            "build_metadata",
            self._fake_for(
                meta, tmp_path, make_meta_fn, {"meson-python": "0.17.0"}
            ),
        )
        rc = cli.main(
            ["black", "-o", str(tmp_path / "out"), "--check", "structural"]
        )
        assert rc == 0
        out = tmp_path / "out" / "meta-black" / "recipes-devtools" / "python"
        assert (out / "python3-meson-python_0.17.0.bb").is_file()
        assert not list(out.glob("python3-pyproject-metadata*.bb"))

    def test_provisioning_terminates_on_a_cycle(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        # A companion that demands the tool that pulled it in must not loop
        # forever: the "already in the closure" check ends the fixpoint.
        meta = self._gapped(make_meta_fn)
        inner = _dep_aware_fake(meta, _source(tmp_path), make_meta_fn)
        calls = []

        def fake_build(name, *a, **k):
            calls.append(name)
            if name == "meson-python":
                return (
                    make_meta_fn(
                        name="meson-python",
                        canonical_name="meson-python",
                        version="0.22.0",
                        # demands the tool that pulled it in, plus a new companion
                        build_deps=[
                            Dependency(
                                name="meson-python",
                                kind=DepKind.BUILD,
                                requirement="meson-python>=0.18.0",
                                specifier=">=0.18.0",
                            )
                        ],
                    ),
                    _source(tmp_path),
                )
            return inner(name, *a, **k)

        monkeypatch.setattr(cli, "build_metadata", fake_build)
        with patch.dict(
            _TOOL_COMPANION_REQUIREMENTS,
            {
                "pyproject-metadata": (
                    (Version("0.8.0"), "meson-python>=0.18.0"),
                )
            },
        ):
            rc = cli.main(
                ["black", "-o", str(tmp_path / "out"), "--check", "structural"]
            )
        assert rc == 0
        assert calls.count("meson-python") == 1


class TestTargetRuntimeFlags:
    """--python-version / --target-arch must reach the generated layer.

    The series table only knows the Python a Yocto release ships, and nothing
    at all about the CPU. Without these flags an x86_64 developer generating
    for a PetaLinux board running aarch64 gets a layer pinned to the host's
    wheel (pyvizionsdk publishes one version per architecture, so that is the
    wrong upstream version, not just the wrong file).
    """

    def _run(self, tmp_path, monkeypatch, capsys, meta, *extra) -> str:
        monkeypatch.setattr(
            cli, "build_metadata", _fake_build_metadata(meta, _source(tmp_path))
        )
        rc = cli.main(
            [
                "black",
                "-o",
                str(tmp_path / "out"),
                "--no-kas",
                "--check",
                "structural",
                *extra,
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        return (
            out
            + (
                tmp_path
                / "out"
                / "meta-black"
                / "recipes-devtools"
                / "python"
                / "python3-black_26.5.1.bb"
            ).read_text()
            + (tmp_path / "out" / "meta-black" / "README").read_text()
        )

    def test_runtime_is_recorded_everywhere(
        self, tmp_path, monkeypatch, capsys, meta
    ):
        text = self._run(
            tmp_path,
            monkeypatch,
            capsys,
            meta,
            "--python-version",
            "3.12",
            "--target-arch",
            "aarch64",
        )
        # The report header, the recipe's provenance comment and the layer
        # README: an artifact built for one runtime must never be mistaken for
        # one built for the machine that happened to run py2bbr.
        assert "Runtime      : Python 3.12 on aarch64" in text
        assert "# Runtime: Python 3.12 on aarch64" in text
        assert "Python 3.12 on aarch64" in text.split("README")[-1]

    def test_target_report_names_the_target_arch(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        # The per-target matrix is judged for every machine of the family, so
        # its header has to say which one the recipe is actually for.
        out = self._run(
            tmp_path,
            monkeypatch,
            capsys,
            make_meta_fn(
                has_native_code=True,
                native_code_evidence=["C source (pkg/src/mod.c)"],
            ),
            "--target-arch",
            "aarch64",
        )
        assert "target arch aarch64" in out

    def test_default_runtime_is_this_machine(
        self, tmp_path, monkeypatch, capsys, meta
    ):
        text = self._run(tmp_path, monkeypatch, capsys, meta)
        assert f"Runtime      : Python {current_python_version()} on " in text

    def test_python_version_drives_the_requires_python_verdict(
        self, tmp_path, monkeypatch, capsys, make_meta_fn
    ):
        # black's metadata here requires >=3.13, which the series' own 3.12
        # rejects and an explicitly targeted 3.13 accepts. The override has to
        # reach the report, not just the comment.
        meta = make_meta_fn(requires_python=">=3.13")
        rejected = self._run(
            tmp_path,
            monkeypatch,
            capsys,
            meta,
            "--python-version",
            "3.12",
        )
        assert "INCOMPATIBLE" in rejected
        accepted = self._run(
            tmp_path,
            monkeypatch,
            capsys,
            meta,
            "--python-version",
            "3.13",
        )
        assert "INCOMPATIBLE" not in accepted

    @pytest.mark.parametrize(
        ("flag", "value", "named"),
        [
            ("--python-version", "3", "--python-version"),
            ("--python-version", "three", "--python-version"),
            ("--target-arch", "a b", "--target-arch"),
            # An alias is reported under the name that documents it.
            ("--arch", "aarch64/64", "--target-arch"),
        ],
    )
    def test_unparsable_value_is_a_clean_error(
        self, tmp_path, capsys, flag, value, named
    ):
        # Not an argparse "invalid choice" traceback: the value is validated
        # once the target is being resolved, and has to read as a message.
        assert cli.main(["black", "-o", str(tmp_path), flag, value]) == 2
        err = capsys.readouterr().err
        assert f"error: invalid {named} '{value}'" in err
