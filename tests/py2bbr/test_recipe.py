"""Tests for py2bbr.recipe rendering (no network required).

Regression: DEPENDS / RDEPENDS must render as single-line, double-quoted
assignments with a literal ``python3-`` package prefix (no ``${PYTHON_PN}`` in
values, no backslash line continuations). A line-oriented post-processing pass
(shell-style continuation joining, ``${VAR}`` expansion with the variable
unset, timestamp appending) corrupts multi-line continued values with
``${...}`` references (e.g. ``DEPENDS += \\`` + ``${PYTHON_PN}-x-native \\``
became the unparsable ``DEPENDS += -x-native '00:00`` seen in PetaLinux).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from unittest.mock import patch

import pytest
from packaging.version import Version

from py2bbr.model import (
    Dependency,
    DepKind,
    MachineDep,
    SourceKind,
    YoctoVersion,
)
from py2bbr.recipe import (
    _BUNDLED_TOOL_VERSIONS,
    _TOOL_COMPANION_REQUIREMENTS,
    _classifier_drop_pattern,
    _minimum_version,
    _newer_minor_pattern,
    _sdist_stem,
    build_version_gaps,
    canonical_name,
    effective_build_class,
    pick_build_class,
    render_recipe,
    render_recipe_to_path,
    tool_companion_gaps,
)


class TestDependsRendersSingleLine:
    def test_depends_single_line_quoted_literal_pn(self, meta, yocto):
        text, _ = render_recipe(meta, yocto)
        lines = [l for l in text.splitlines() if l.startswith("DEPENDS")]
        assert lines == ['DEPENDS += "python3-hatchling-native"']

    def test_rdepends_single_line_quoted_literal_pn(self, meta, yocto):
        text, _ = render_recipe(meta, yocto)
        lines = [l for l in text.splitlines() if l.startswith("RDEPENDS")]
        assert lines == ['RDEPENDS:${PN} += "python3-click python3-packaging"']

    def test_no_backslash_continuations(self, meta, yocto):
        text, _ = render_recipe(meta, yocto)
        assert not [l for l in text.splitlines() if l.rstrip().endswith("\\")]

    def test_no_python_pn_in_values(self, meta, yocto):
        text, _ = render_recipe(meta, yocto)
        assert "${PYTHON_PN}" not in text

    def test_uses_yocto_python_pn(self, meta):
        py2 = YoctoVersion(
            version="9.9",
            codename="test",
            poky_branch="test",
            python_version="2.7",
        )
        text, _ = render_recipe(meta, py2)
        assert 'DEPENDS += "python-hatchling-native"' in text
        assert 'RDEPENDS:${PN} += "python-click python-packaging"' in text


class TestToolDepsForKnownBackends:
    """PEP 517 backends that make pypa/build's --no-isolation check demand
    non-Python tools must have those tools declared as literal ``-native``
    DEPENDS (e.g. ``meson-native ninja-native`` for meson-python). Regression:
    numpy 2.5.3 under PetaLinux aborted at do_compile with "Missing
    dependencies: meson-python / meson>=1.2.3 / ninja>=1.8.2" because bitbake
    does not transitively stage a native dependency's own ``-native`` deps into
    the consuming recipe's sysroot, so the recipe must name them itself.
    """

    def test_mesonpy_backend_declares_meson_ninja_native(
        self, make_meta_fn, yocto
    ):
        meta = make_meta_fn(
            build_backend="mesonpy",
            build_deps=[
                Dependency(
                    name="meson-python",
                    kind=DepKind.BUILD,
                    requirement="meson-python>=0.18.0",
                ),
                Dependency(
                    name="Cython",
                    kind=DepKind.BUILD,
                    requirement="Cython>=3.1.0",
                ),
            ],
        )
        text, _ = render_recipe(meta, yocto)
        lines = [l for l in text.splitlines() if l.startswith("DEPENDS")]
        assert lines == [
            (
                'DEPENDS += "python3-meson-python-native python3-cython-native '
                'meson-native ninja-native patchelf-native"'
            )
        ]

    def test_meson_python_backend_name_also_maps(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            build_backend="meson-python",
            build_deps=[
                Dependency(
                    name="meson-python",
                    kind=DepKind.BUILD,
                    requirement="meson-python>=0.15",
                ),
            ],
        )
        text, _ = render_recipe(meta, yocto)
        assert "meson-native ninja-native" in text

    def test_scikit_build_core_declares_cmake_ninja(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            build_backend="scikit_build_core",
            build_deps=[
                Dependency(
                    name="scikit-build-core",
                    kind=DepKind.BUILD,
                    requirement="scikit-build-core>=0.10",
                ),
            ],
        )
        text, _ = render_recipe(meta, yocto)
        lines = [l for l in text.splitlines() if l.startswith("DEPENDS")]
        assert lines == [
            'DEPENDS += "python3-scikit-build-core-native cmake-native ninja-native"'
        ]

    def test_setuptools_backend_adds_no_tool_deps(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            build_backend="setuptools.build_meta",
            build_deps=[
                Dependency(
                    name="setuptools",
                    kind=DepKind.BUILD,
                    requirement="setuptools>=70",
                ),
            ],
        )
        text, _ = render_recipe(meta, yocto)
        assert not any(
            t in text for t in ("meson-native", "ninja-native", "cmake-native")
        )

    def test_unknown_backend_adds_no_tool_deps(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            build_backend="flit_core.buildapi",
            build_deps=[
                Dependency(
                    name="flit_core",
                    kind=DepKind.BUILD,
                    requirement="flit_core>=3.9",
                ),
            ],
        )
        text, _ = render_recipe(meta, yocto)
        assert "ninja-native" not in text


class TestBareToolProviders:
    """Non-Python build tools have no ``python3-*`` recipe in poky core.

    ``meson``, ``ninja``, ``cmake`` and ``patchelf`` are bare tool recipes
    (``meson`` / ``meson-native``, ...) shipped by poky core, so a dependency
    on them must render WITHOUT the ``python3-`` prefix. Regression: the
    provisioned ``python3-meson-python_0.21.1.bb`` failed under PetaLinux with
    "ERROR: Nothing RPROVIDES 'python3-meson-native' (but
    virtual:native:.../python3-meson-python_0.21.1.bb RDEPENDS on or otherwise
    requires it)". The RDEPENDS entry ``python3-meson`` combined with
    ``BBCLASSEXTEND = "native nativesdk"`` makes the native variant demand
    ``python3-meson-native``, which nothing provides (poky's
    ``meta/recipes-devtools/meson/meson_*.bb`` builds bare ``meson`` /
    ``meson-native``). poky's own scarthgap for meson-python renders
    ``RDEPENDS:${PN} = "meson ninja patchelf python3-pyproject-metadata"`` and
    ``DEPENDS = "meson-native ninja-native patchelf-native
    python3-pyproject-metadata-native"``: bare names survive the native suffix
    rename (``meson`` -> ``meson-native``), the ``python3-`` prefix does not.
    """

    def test_all_bare_tools_render_unprefixed(self, make_meta_fn, yocto):
        for tool in ("meson", "ninja", "cmake", "patchelf"):
            meta = make_meta_fn(
                build_deps=[
                    Dependency(name=tool, kind=DepKind.BUILD, requirement=tool)
                ],
            )
            text, _ = render_recipe(meta, yocto)
            assert f"{tool}-native" in text, tool
            assert f"python3-{tool}-native" not in text, tool

    def test_runtime_meson_renders_bare(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            runtime_deps=[
                Dependency(
                    name="meson",
                    kind=DepKind.RUNTIME,
                    requirement="meson>=1.6.0",
                )
            ],
        )
        text, _ = render_recipe(meta, yocto)
        assert 'RDEPENDS:${PN} += "meson"' in text

    def test_build_meson_renders_bare_native(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            build_deps=[
                Dependency(
                    name="meson", kind=DepKind.BUILD, requirement="meson>=1.6.0"
                ),
                Dependency(
                    name="pyproject-metadata",
                    kind=DepKind.BUILD,
                    requirement="pyproject-metadata>=0.7.1",
                ),
            ],
        )
        text, _ = render_recipe(meta, yocto)
        # meson is a bare tool -> meson-native; pyproject-metadata stays a
        # python package -> python3-pyproject-metadata-native.
        assert "meson-native" in text
        assert "python3-pyproject-metadata-native" in text
        assert "python3-meson-native" not in text

    def test_meson_python_renders_poky_shape(self, make_meta_fn, yocto):
        # Model of the real meson-python 0.21.1 metadata (the repro recipe).
        meta = make_meta_fn(
            name="meson-python",
            canonical_name="meson-python",
            version="0.21.1",
            source_filename="meson_python-0.21.1.tar.gz",
            build_backend="mesonpy",
            build_deps=[
                Dependency(
                    name="meson", kind=DepKind.BUILD, requirement="meson>=1.6.0"
                ),
                Dependency(
                    name="packaging",
                    kind=DepKind.BUILD,
                    requirement="packaging>=19.0",
                ),
                Dependency(
                    name="pyproject-metadata",
                    kind=DepKind.BUILD,
                    requirement="pyproject-metadata>=0.9.1",
                ),
                Dependency(
                    name="tomli", kind=DepKind.BUILD, requirement="tomli>=1.1.0"
                ),
            ],
            runtime_deps=[
                Dependency(
                    name="meson",
                    kind=DepKind.RUNTIME,
                    requirement="meson>=1.6.0",
                ),
                Dependency(
                    name="packaging",
                    kind=DepKind.RUNTIME,
                    requirement="packaging>=19.0",
                ),
                Dependency(
                    name="pyproject-metadata",
                    kind=DepKind.RUNTIME,
                    requirement="pyproject-metadata>=0.9.1",
                ),
            ],
        )
        text, _ = render_recipe(meta, yocto)
        # The native variant renames every RDEPENDS entry: bare ``meson``
        # becomes ``meson-native`` (provided by poky's meson recipe), while
        # ``python3-meson`` would become the unprovidable
        # ``python3-meson-native``.
        assert (
            'RDEPENDS:${PN} += "meson python3-packaging '
            'python3-pyproject-metadata"' in text
        )
        assert "python3-meson" not in text
        # meson-native must appear exactly once in DEPENDS: the build
        # requirement and the mesonpy backend tool deps both name it, and the
        # unprovidable python3-meson-native must never be emitted.
        depends = [l for l in text.splitlines() if l.startswith("DEPENDS +=")]
        assert len(depends) == 1
        assert depends[0].count("meson-native") == 1
        assert "python3-meson-native" not in depends[0]
        # The mesonpy backend's get_requires_for_build_wheel always appends
        # patchelf>=0.11.0 on Linux even when the metadata omits it (meson-
        # python 0.21.1's own pyproject has no patchelf); poky hardcodes
        # patchelf-native in its meson-python DEPENDS for the same reason.
        assert "patchelf-native" in depends[0]


class TestPypiPackage:
    def test_no_override_when_filename_matches_canonical(self, meta, yocto):
        text, _ = render_recipe(meta, yocto)
        assert "PYPI_PACKAGE" not in text

    def test_override_for_underscore_sdist(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            name="async-pyserial",
            canonical_name="async-pyserial",
            version="0.2.4",
            source_filename="async_pyserial-0.2.4.tar.gz",
        )
        text, _ = render_recipe(meta, yocto)
        lines = [l for l in text.splitlines() if "PYPI_PACKAGE" in l]
        assert lines == ['PYPI_PACKAGE = "async_pyserial"']
        # It must come after the inherit line so it overrides the weak
        # default the pypi class computes from BPN.
        inherit_at = text.index("inherit pypi")
        assert text.index("PYPI_PACKAGE") > inherit_at


class TestLicenseRendering:
    def test_multi_license_expression_uses_yocto_operators(
        self, make_meta_fn, yocto
    ):
        # Regression: numpy 2.5.3 PEP 639 expression passed verbatim
        # ("BSD-3-Clause AND 0BSD AND ...") failed Yocto's license-format QA,
        # which requires & | ( ) separators, not SPDX AND/OR words.
        meta = make_meta_fn(
            license="BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0"
        )
        text, _ = render_recipe(meta, yocto)
        assert 'LICENSE = "BSD-3-Clause & 0BSD & MIT & Zlib & CC0-1.0"' in text

    def test_unknown_license_defaults_closed_with_warning(
        self, make_meta_fn, yocto
    ):
        # PetaLinux errors on recipes without a LICENSE field; default to the
        # Yocto convention for unknown/proprietary (CLOSED) and warn.
        meta = make_meta_fn(license="", license_raw="")
        text, warnings = render_recipe(meta, yocto)
        assert 'LICENSE = "CLOSED"' in text
        assert any("CLOSED" in w for w in warnings)

    def test_known_spdx_without_file_falls_back_to_common_licenses(
        self, make_meta_fn, yocto
    ):
        # runs 1.3.0: the sdist ships no license file, but do_populate_lic
        # fails fatally without LIC_FILES_CHKSUM; emit the standard OE idiom
        # pointing at the license text the target ships in common-licenses.
        meta = make_meta_fn(license="MIT", license_file=None, license_md5=None)
        text, warnings = render_recipe(meta, yocto)
        assert (
            'LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;'
            'md5=0835ade698e0bcf8506ecda2f7b4f302"' in text
        )
        assert not any("LIC_FILES_CHKSUM" in w for w in warnings)

    def test_multi_known_spdx_without_file_emit_each_common_license(
        self, make_meta_fn, yocto
    ):
        meta = make_meta_fn(
            license="MIT OR Apache-2.0", license_file=None, license_md5=None
        )
        text, _ = render_recipe(meta, yocto)
        assert (
            "file://${COMMON_LICENSE_DIR}/MIT;"
            "md5=0835ade698e0bcf8506ecda2f7b4f302" in text
        )
        assert (
            "file://${COMMON_LICENSE_DIR}/Apache-2.0;"
            "md5=89aea4e17d99a7cacdbeed46a0096b10" in text
        )

    def test_unknown_spdx_without_file_still_warns(self, make_meta_fn, yocto):
        # Unknown ids have no common-licenses file to point at: the warning
        # (manual LIC_FILES_CHKSUM) must survive so the user can fill it in.
        meta = make_meta_fn(
            license="AGPL-3.0-only", license_file=None, license_md5=None
        )
        text, warnings = render_recipe(meta, yocto)
        assert any("LIC_FILES_CHKSUM" in w for w in warnings)
        assert "COMMON_LICENSE_DIR" not in text


class TestBuildSystemRequiresRelax:
    """S4: PEP 517 builds must not abort on over-new build-system.requires.

    ``python_pep517``'s ``do_compile`` runs ``nativepython3 -m build
    --no-isolation --wheel``; pypa/build's ``--no-isolation`` path compares the
    ``requires`` version bounds against the *native* sysroot versions and
    aborts when the project demands newer ones than the Yocto series ships
    (Scarthgap caps at setuptools 69.1.1 / wheel 0.42.0; ``async-pyserial``
    0.2.4 wants ``setuptools>=71.0.1`` / ``wheel>=0.43.0``). The recipe must
    render a ``do_configure:append`` that strips those bounds with a
    ``[build-system]``-scoped sed, and only when it is actually needed.
    """

    def test_pep517_bounded_requires_emit_relax_block(self, meta, yocto):
        text, _ = render_recipe(meta, yocto)
        assert "do_configure:append" in text

    def test_setuptools_build_meta_bounded_requires_emit_relax_block(
        self, make_meta_fn, yocto
    ):
        meta = make_meta_fn(
            build_backend="setuptools.build_meta",
            build_deps=[
                Dependency(
                    name="setuptools",
                    kind=DepKind.BUILD,
                    requirement="setuptools>=71.0.1",
                ),
                Dependency(
                    name="wheel",
                    kind=DepKind.BUILD,
                    requirement="wheel>=0.43.0",
                ),
                Dependency(
                    name="pybind11",
                    kind=DepKind.BUILD,
                    requirement="pybind11>=2.12.0",
                ),
            ],
        )
        text, _ = render_recipe(meta, yocto)
        assert "do_configure:append" in text
        assert "\\[build-system\\]" in text
        assert "sed -i -E" in text

    def test_unbounded_requires_no_bounds_sed(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            build_deps=[
                Dependency(
                    name="hatchling",
                    kind=DepKind.BUILD,
                    requirement="hatchling",
                ),
            ],
        )
        text, _ = render_recipe(meta, yocto)
        # No build-system bounds to strip, so no [build-system] sed; a
        # do_configure may still appear for the classifier prune.
        assert r"\[build-system\]" not in text

    def test_malformed_requirement_no_bounds_sed(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            build_deps=[
                Dependency(
                    name="hatchling",
                    kind=DepKind.BUILD,
                    requirement="hatchling!!not-a-specifier",
                ),
            ],
        )
        text, _ = render_recipe(meta, yocto)
        assert r"\[build-system\]" not in text

    def test_setuptools3_bounded_requires_no_relax_block(
        self, make_meta_fn, yocto
    ):
        meta = make_meta_fn(
            build_backend="",
            build_deps=[
                Dependency(
                    name="setuptools",
                    kind=DepKind.BUILD,
                    requirement="setuptools>=71.0.1",
                ),
            ],
        )
        text, _ = render_recipe(meta, yocto)
        assert "do_configure:append" not in text


class TestExternalBuildTooling:
    """S5: external dev tooling backends (uv, ...) must not enter DEPENDS.

    Regression: runs 1.3.0 declares ``[build-system] requires =
    ["uv_build>=0.9.0,<0.10.0"]`` with ``build-backend = "uv_build"``. Vanilla
    Yocto/PetaLinux ships no ``python3-uv-build-native`` provider, so the
    emitted ``DEPENDS += "python3-uv-build-native"`` made the whole closure
    (pyvizionsdk -> inquirer -> editor -> runs) unbuildable with "Nothing
    PROVIDES 'python3-uv-build-native'". These backends are standalone build
    *tooling* (not runtime libraries) with no ``-native`` recipe in the target;
    the recipe must exclude them from DEPENDS, route to
    ``python_setuptools_build_meta``, and rewrite ``[build-system]`` to
    ``setuptools.build_meta`` in ``do_configure:append``.
    """

    def test_uv_build_backend_excluded_from_depends(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            name="runs",
            canonical_name="runs",
            version="1.3.0",
            build_backend="uv_build",
            build_deps=[
                Dependency(
                    name="uv_build",
                    kind=DepKind.BUILD,
                    requirement="uv_build>=0.9.0,<0.10.0",
                ),
            ],
        )
        text, _ = render_recipe(meta, yocto)
        assert "python3-uv-build-native" not in text
        assert "python3-uv_build-native" not in text

    def test_uv_build_routes_to_setuptools_build_meta(
        self, make_meta_fn, yocto
    ):
        meta = make_meta_fn(build_backend="uv_build")
        text, _ = render_recipe(meta, yocto)
        assert "inherit pypi python_setuptools_build_meta" in text

    def test_uv_build_rewrites_backend_in_do_configure(
        self, make_meta_fn, yocto
    ):
        meta = make_meta_fn(build_backend="uv_build")
        text, warnings = render_recipe(meta, yocto)
        assert text.count("do_configure:append") == 1
        assert "setuptools.build_meta" in text
        assert any("external" in w for w in warnings)

    def test_uv_build_normalizes_pep639_license(self, make_meta_fn, yocto):
        # Regression: runs 1.3.0 declares `license = "MIT"` (PEP 639 string
        # form). The native sysroot's setuptools 69.x (which the rewrite points
        # [build-system] at) only accepts the PEP 621 table form
        # `license = {text = "..."}`, so metadata generation dies on
        # `project.license` unless the rewrite normalizes it too.
        meta = make_meta_fn(build_backend="uv_build")
        text, _ = render_recipe(meta, yocto)
        assert text.count("do_configure:append") == 1
        assert r"\[project\]" in text
        assert r"license = {text = \"\1\"}" in text

    def test_poetry_backend_keeps_depends(self, make_meta_fn, yocto):
        # Regression: poetry-core HAS a provider in the target PetaLinux, and
        # inquirer's metadata lives in [tool.poetry] (not PEP 621), so its
        # backend must NOT be denylisted or rewritten.
        meta = make_meta_fn(
            build_backend="poetry.core.masonry.api",
            build_deps=[
                Dependency(
                    name="poetry-core",
                    kind=DepKind.BUILD,
                    requirement="poetry-core>=1.0.0",
                ),
            ],
        )
        text, _ = render_recipe(meta, yocto)
        assert 'DEPENDS += "python3-poetry-core-native"' in text
        assert "python_pep517" in text
        assert "setuptools.build_meta" not in text

    def test_hatchling_backend_unchanged(self, meta, yocto):
        # Regression: hatchling has a provider; keep DEPENDS + python_pep517
        # (bounds relax still fires for the pinned requirement).
        text, _ = render_recipe(meta, yocto)
        assert 'DEPENDS += "python3-hatchling-native"' in text
        assert "inherit pypi python_pep517" in text
        assert "setuptools.build_meta" not in text


class TestVendoredRecipe:
    """S6: sdists absent from public PyPI ship inside the layer (file://).

    pyvizionsdk-type packages are only reachable through a private index, so
    ``inherit pypi``'s fetch URL 404s in do_fetch: the recipe must point
    SRC_URI at a copied sdist and derive ``S`` from it instead.
    """

    def test_vendored_uses_file_uri_and_workdir_s(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            name="pyvizionsdk",
            canonical_name="pyvizionsdk",
            version="26.9.1",
            source_filename="pyvizionsdk-26.9.1.tar.gz",
            source_sha256="abc",
            vendored=True,
        )
        text, warnings = render_recipe(meta, yocto)
        assert 'SRC_URI = "file://pyvizionsdk-26.9.1.tar.gz"' in text
        assert 'S = "${WORKDIR}/pyvizionsdk-26.9.1"' in text
        assert 'SRC_URI[sha256sum] = "abc"' in text
        assert "inherit pypi" not in text
        assert "inherit python_pep517" in text
        assert "PYPI_PACKAGE" not in text
        assert any("vendored" in w for w in warnings)

    def test_vendored_zip_sdist_stem(self, make_meta_fn, yocto):
        meta = make_meta_fn(source_filename="pkg-1.0.zip", vendored=True)
        text, _ = render_recipe(meta, yocto)
        assert 'SRC_URI = "file://pkg-1.0.zip"' in text
        assert 'S = "${WORKDIR}/pkg-1.0"' in text

    def test_vendored_skips_pypi_package_pin(self, make_meta_fn, yocto):
        # Even when the sdist filename name differs from the canonical name,
        # a vendored recipe must not emit PYPI_PACKAGE: there is no pypi class
        # (and thus no BPN-derived fetch URL) for it to override.
        meta = make_meta_fn(
            canonical_name="pyvizionsdk",
            version="26.9.1",
            source_filename="pyvizionsdk_pkg-26.9.1.tar.gz",
            vendored=True,
        )
        text, _ = render_recipe(meta, yocto)
        assert "PYPI_PACKAGE" not in text
        assert 'SRC_URI = "file://pyvizionsdk_pkg-26.9.1.tar.gz"' in text

    def test_non_vendored_keeps_pypi_inherit(self, meta, yocto):
        text, _ = render_recipe(meta, yocto)
        assert "inherit pypi python_pep517" in text
        assert 'SRC_URI = "file://' not in text
        assert 'S = "${WORKDIR}' not in text

    def test_vendored_render_recipe_to_path_copies_source(
        self, make_meta_fn, yocto, tmp_path
    ):
        src = tmp_path / "pyvizionsdk-26.9.1.tar.gz"
        src.write_bytes(b"fake-sdist")
        meta = make_meta_fn(
            name="pyvizionsdk",
            canonical_name="pyvizionsdk",
            version="26.9.1",
            source_filename="pyvizionsdk-26.9.1.tar.gz",
            vendored=True,
        )
        path, _ = render_recipe_to_path(
            meta, yocto, tmp_path / "out", vendor_source=src
        )
        assert path.is_file()
        copied = tmp_path / "out" / "files" / "pyvizionsdk-26.9.1.tar.gz"
        assert copied.read_bytes() == b"fake-sdist"

    def test_vendored_render_recipe_to_path_requires_source(
        self, make_meta_fn, yocto, tmp_path
    ):
        meta = make_meta_fn(
            name="pyvizionsdk",
            canonical_name="pyvizionsdk",
            version="26.9.1",
            source_filename="pyvizionsdk-26.9.1.tar.gz",
            vendored=True,
        )
        with pytest.raises(ValueError, match="vendor_source"):
            render_recipe_to_path(meta, yocto, tmp_path / "out")


class TestClassifierPruning:
    """S7: classifiers claiming Python newer than the target are pruned.

    The native sysroot's hatchling validates ``project.classifiers`` against
    its bundled trove list; a classifier for a Python release newer than the
    target (e.g. ``Python :: 3.14`` on a 3.12 image) is unknown to that list,
    so ``build_wheel`` aborts with "Unknown classifier in field
    `project.classifiers`" (xmod 1.10.0 under PetaLinux). Those lines are
    metadata-only and false for the target Python, so the PEP 517
    ``do_configure`` drops them.
    """

    def test_pep517_drops_classifiers_newer_than_target(self, meta, yocto):
        text, warnings = render_recipe(meta, yocto)
        # Scarthgap target python is 3.12 -> drop 3.13-3.99 and any 4.x lines.
        assert (
            r"Programming Language :: Python :: (3\.(1[3-9]|[2-9][0-9])" in text
        )
        assert r"[4-9])/d" in text
        assert any("classifier" in w for w in warnings)

    def test_setuptools3_does_not_prune_classifiers(self, make_meta_fn, yocto):
        meta = make_meta_fn(build_backend="")
        text, _ = render_recipe(meta, yocto)
        assert "Programming Language :: Python ::" not in text

    def test_setuptools_build_meta_does_not_prune_classifiers(
        self, make_meta_fn, yocto
    ):
        # setuptools passes unknown classifiers through (no hard failure), so
        # the prune is only emitted for python_pep517 recipes.
        meta = make_meta_fn(build_backend="setuptools.build_meta")
        text, _ = render_recipe(meta, yocto)
        assert "Programming Language :: Python ::" not in text

    def test_newer_minor_pattern_boundaries(self):
        assert _newer_minor_pattern(12) == "1[3-9]|[2-9][0-9]"
        assert _newer_minor_pattern(10) == "1[1-9]|[2-9][0-9]"
        assert _newer_minor_pattern(9) == "[1-9][0-9]"
        assert _newer_minor_pattern(13) == "1[4-9]|[2-9][0-9]"
        assert _newer_minor_pattern(19) == "[2-9][0-9]"
        assert _newer_minor_pattern(5) == "0[6-9]|[1-9][0-9]"

    def test_classifier_drop_pattern(self):
        pattern = _classifier_drop_pattern("3.12")
        assert r"3\.(1[3-9]|[2-9][0-9])" in pattern
        assert "Programming Language :: Python ::" in pattern
        # trailing-comma and leading-comma are the two empty-replacement ops
        assert pattern.count("//g") == 2
        assert ")/d" in pattern
        assert _classifier_drop_pattern("2.7") is None

    def test_classifier_sed_prunes_xmod_single_line_array(
        self, meta, yocto, tmp_path
    ):
        # xmod 1.10.0 declares every classifier on one line; the recipe's sed
        # must prune too-new entries out of that array (not just whole
        # classifier-only lines) and leave valid TOML behind.
        if shutil.which("sed") is None:
            pytest.skip("sed not available")
        text, _ = render_recipe(meta, yocto)
        program = next(
            line.split('"', 1)[1].rsplit('"', 1)[0]
            for line in text.splitlines()
            if "Programming Language :: Python ::" in line
        )
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\n"
            'name = "xmod"\n'
            "classifiers = ["
            '"Programming Language :: Python :: 3", '
            '"Programming Language :: Python :: 3.10", '
            '"Programming Language :: Python :: 3.11", '
            '"Programming Language :: Python :: 3.12", '
            '"Programming Language :: Python :: 3.13", '
            '"Programming Language :: Python :: 3.14"]\n'
        )
        subprocess.run(["sed", "-E", "-i", program, str(pyproject)], check=True)
        pruned = pyproject.read_text()
        assert (
            "classifiers = ["
            '"Programming Language :: Python :: 3", '
            '"Programming Language :: Python :: 3.10", '
            '"Programming Language :: Python :: 3.11", '
            '"Programming Language :: Python :: 3.12"]'
        ) in pruned
        assert '"Programming Language :: Python :: 3.13"' not in pruned
        assert '"Programming Language :: Python :: 3.14"' not in pruned


class TestLicenseFilesDroppedWithTableLicense:
    """A table-form license may not keep ``project.license-files``.

    The recipes normalize a PEP 639 string license (``license = "MIT"``) to
    the legacy PEP 621 table form (``license = {text = "MIT"}``), which the
    native sysroot's setuptools 69.x / flit_core 3.9.0 validators accept. PEP
    639 only allows ``project.license-files`` next to an SPDX *string*, so the
    key has to go with the rewrite. Leaving it broke
    ``python3-meson-python-native 0.22.0`` under PetaLinux 2024.2, whose
    do_compile aborted with ``"project.license-files" must not be used when
    "project.license" is not a SPDX license expression``.
    """

    def _run_license_sed(self, text: str, pyproject) -> str:
        """Apply the recipe's license-related sed programs, in order."""
        if shutil.which("sed") is None:
            pytest.skip("sed not available")
        programs = [
            line.split('"', 1)[1].rsplit('"', 1)[0]
            for line in text.splitlines()
            if "license" in line and line.strip().startswith("sed -i -E")
        ]
        assert programs, "recipe has no license-rewriting sed"
        for program in programs:
            subprocess.run(
                ["sed", "-E", "-i", program, str(pyproject)], check=True
            )
        return pyproject.read_text()

    def test_mesonpy_recipe_drops_license_files(self, make_meta_fn, yocto):
        meta = make_meta_fn(build_backend="mesonpy")
        text, _ = render_recipe(meta, yocto)
        assert "license-files" in text

    def test_single_line_license_files_removed(
        self, make_meta_fn, yocto, tmp_path
    ):
        # meson-python 0.22.0's own shape: one line, single quotes.
        meta = make_meta_fn(build_backend="mesonpy")
        text, _ = render_recipe(meta, yocto)
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\n"
            'name = "meson-python"\n'
            'license = "MIT"\n'
            "license-files = ['LICENSES/MIT.txt']\n"
            'readme = "README.rst"\n'
        )
        out = self._run_license_sed(text, pyproject)
        assert "license-files" not in out
        assert 'license = {text = "MIT"}' in out
        # Sibling keys survive.
        assert 'name = "meson-python"' in out
        assert 'readme = "README.rst"' in out

    def test_multiline_license_files_array_removed(
        self, make_meta_fn, yocto, tmp_path
    ):
        # An array opened on one line and closed on another must lose the whole
        # span, closing bracket included, or the TOML no longer parses.
        meta = make_meta_fn(build_backend="mesonpy")
        text, _ = render_recipe(meta, yocto)
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\n"
            'name = "x"\n'
            'license = "MIT"\n'
            "license-files = [\n"
            '    "LICENSE",\n'
            '    "AUTHORS.rst",\n'
            "]\n"
            'dependencies = ["a"]\n'
            "\n[build-system]\n"
            'requires = ["meson-python"]\n'
        )
        out = self._run_license_sed(text, pyproject)
        assert "license-files" not in out
        assert "AUTHORS.rst" not in out
        assert 'dependencies = ["a"]' in out
        # [build-system] is a different section: untouched.
        assert 'requires = ["meson-python"]' in out

    def test_other_tables_license_files_untouched(
        self, make_meta_fn, yocto, tmp_path
    ):
        # Only [project] carries a PEP 639 license; an identically named key in
        # another table is not ours to delete.
        meta = make_meta_fn(build_backend="mesonpy")
        text, _ = render_recipe(meta, yocto)
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[project]\n"
            'name = "x"\n'
            'license = "MIT"\n'
            'license-files = ["LICENSES/MIT.txt"]\n'
            "\n[tool.mine]\n"
            'license-files = ["keep-me"]\n'
        )
        out = self._run_license_sed(text, pyproject)
        assert "LICENSES/MIT.txt" not in out
        assert 'license-files = ["keep-me"]' in out

    def test_no_license_files_key_is_a_noop(
        self, make_meta_fn, yocto, tmp_path
    ):
        meta = make_meta_fn(build_backend="mesonpy")
        text, _ = render_recipe(meta, yocto)
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nlicense = "MIT"\ndependencies = []\n'
        )
        out = self._run_license_sed(text, pyproject)
        assert 'license = {text = "MIT"}' in out
        assert "dependencies = []" in out

    def test_absent_for_classes_that_do_not_rewrite_licenses(
        self, make_meta_fn, yocto
    ):
        # A setuptools3 recipe (no PEP 517 metadata rewrite) leaves its
        # pyproject.toml alone, so it must not carry a license-files deletion.
        meta = make_meta_fn(build_backend="")
        text, _ = render_recipe(meta, yocto)
        assert "license-files" not in text


class TestInstallPrefixOverride:
    """Every recipe must pin an install ``--prefix``.

    PetaLinux's ``python_pep517_do_install`` runs pypa installer with only
    ``--destdir=${D}``; installer 0.7.0 then derives its scheme from the
    *native* python's absolute ``recipe-sysroot-native`` base and pathlib-joins
    it onto ``${D}``, so the payload lands under
    ``${D}/output/.../recipe-sysroot-native/...`` and do_package fails with
    "installed but not shipped" (xmod 1.10.0 under PetaLinux 2024.2).
    """

    def test_override_emitted_with_relative_prefix(self, meta, yocto):
        text, _ = render_recipe(meta, yocto)
        assert "python_pep517_do_install() {" in text
        assert "--prefix ${prefix}" in text
        assert "--destdir=${D}" in text

    def test_override_for_every_build_class(self, make_meta_fn, yocto):
        # setuptools3, python_setuptools_build_meta, python_mesonpy and
        # python_pep517 all route their install through
        # python_pep517_do_install.
        for backend in (
            "",
            "setuptools.build_meta",
            "mesonpy",
            "flit_core.buildapi",
        ):
            meta = make_meta_fn(build_backend=backend)
            text, _ = render_recipe(meta, yocto)
            assert "--prefix ${prefix}" in text, backend

    def test_single_override(self, meta, yocto):
        text, _ = render_recipe(meta, yocto)
        assert text.count("python_pep517_do_install() {") == 1


class TestPep639LicenseNormalize:
    """String-license normalization for PEP 517 classes.

    blessed 1.50.0's ``license = 'MIT'`` (plus ``license-files = [...]``) makes
    the native sysroot's flit_core 3.9.0 abort the wheel build with "license
    field should be <class 'dict'>, not <class 'str'>"; the recipe's
    do_configure:append rewrites both quote styles to the PEP 621 table form,
    which every supported backend accepts.
    """

    def assert_normalized(self, text, label):
        # double-quote program (external-backend block or the general one)
        # and the single-quote program, each with even raw-quote counts
        assert '{text = \\"\\1\\"}' in text, label
        assert "{text = '\\1'}" in text, label

    def test_pep517_family_normalizes_string_license(self, meta, yocto):
        text, _ = render_recipe(meta, yocto)
        self.assert_normalized(text, "hatchling.build")

    def test_all_pep517_classes_get_the_sed(self, make_meta_fn, yocto):
        for backend in (
            "hatchling.build",
            "setuptools.build_meta",
            "flit_core.buildapi",
            "mesonpy",
        ):
            text, _ = render_recipe(make_meta_fn(build_backend=backend), yocto)
            self.assert_normalized(text, backend)

    def test_general_sed_also_catches_single_quotes(self, make_meta_fn, yocto):
        # Adjacent to the external-backend rewrite (double-quote-only), the
        # quote-agnostic sed must own single-quoted strings too.
        text, _ = render_recipe(make_meta_fn(build_backend="uv-build"), yocto)
        self.assert_normalized(text, "uv-build")

    def test_setuptools3_does_not_normalize(self, make_meta_fn, yocto):
        text, _ = render_recipe(make_meta_fn(build_backend=""), yocto)
        assert "{text" not in text


class TestMesonpyBuildClass:
    def test_mesonpy_maps_to_python_mesonpy(self):
        assert pick_build_class("mesonpy") == ("python_mesonpy", False)
        assert pick_build_class("meson-python") == ("python_mesonpy", False)

    def test_mesonpy_recipe_inherits_python_mesonpy(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            build_backend="mesonpy",
            build_deps=[
                Dependency(
                    name="meson-python",
                    kind=DepKind.BUILD,
                    requirement="meson-python>=0.18.0,<0.19",
                    specifier=">=0.18.0,<0.19",
                ),
                Dependency(
                    name="Cython",
                    kind=DepKind.BUILD,
                    requirement="Cython>=3.1.0",
                    specifier=">=3.1.0",
                ),
            ],
        )
        text, _ = render_recipe(meta, yocto)
        assert "inherit pypi python_mesonpy" in text
        # --no-isolation runs the dependency check too, so version bounds in
        # [build-system] must still be relaxed.
        assert "\\[build-system\\]" in text

    def test_mesonpy_bounded_requires_emit_relax_block(
        self, make_meta_fn, yocto
    ):
        meta = make_meta_fn(
            build_backend="mesonpy",
            build_deps=[
                Dependency(
                    name="Cython",
                    kind=DepKind.BUILD,
                    requirement="Cython>=3.1.0",
                    specifier=">=3.1.0",
                )
            ],
        )
        text, _ = render_recipe(meta, yocto)
        assert "s/([A-Za-z0-9_.]+)" in text  # version-bound stripping sed


class TestPep517ShipSitePackages:
    """Generic ``python_pep517`` recipes must inherit ``setuptools3-base``.

    Regression: xmod / inquirer / blessed / editor failed ``do_package`` with
    "Files/directories were installed but not shipped" for
    ``/usr/lib/python3.12/site-packages/...`` under PetaLinux: bare
    ``inherit pypi python_pep517`` defines neither ``PYTHON_SITEPACKAGES_DIR``
    (``python3native`` -> ``python3-dir``) nor the
    ``FILES:${PN} += "${PYTHON_SITEPACKAGES_DIR}"`` rule that ships the
    install payload. poky's own wrapper classes (``python_flit_core`` /
    ``python_setuptools_build_meta`` / ``python_mesonpy`` / ``setuptools3``)
    all inherit ``setuptools3-base``; the generic shape must match so its
    recipes ship the site-packages tree.
    """

    def test_pep517_recipe_inherits_setuptools3_base(self, meta, yocto):
        text, _ = render_recipe(meta, yocto)
        assert "inherit pypi python_pep517 setuptools3-base" in text

    def test_vendored_pep517_inherits_setuptools3_base(
        self, make_meta_fn, yocto
    ):
        meta = make_meta_fn(vendored=True)
        text, _ = render_recipe(meta, yocto)
        assert "inherit python_pep517 setuptools3-base" in text
        assert "inherit pypi" not in text

    def test_flit_consumer_inherits_setuptools3_base(self, make_meta_fn, yocto):
        # blessed 1.50.0: flit_core.buildapi on a non-knot package keeps the
        # generic python_pep517 shape (which now ships the site-packages).
        meta = make_meta_fn(build_backend="flit_core.buildapi")
        text, _ = render_recipe(meta, yocto)
        assert "inherit pypi python_pep517 setuptools3-base" in text

    def test_wrapper_classes_do_not_double_inherit_setuptools3_base(
        self, make_meta_fn, yocto
    ):
        # setuptools3 / python_setuptools_build_meta / python_mesonpy and the
        # bootstrap shapes already get setuptools3-base from their own class;
        # the generic shape must not add a second copy on top (the recipe text
        # names only the wrapper class).
        for backend in ("", "setuptools.build_meta", "mesonpy"):
            text, _ = render_recipe(make_meta_fn(build_backend=backend), yocto)
            assert "setuptools3-base" not in text, backend
        knot = make_meta_fn(
            name="flit-core",
            canonical_name="flit-core",
            version="3.12.0",
            source_filename="flit_core-3.12.0.tar.gz",
            build_backend="flit_core.buildapi",
            build_deps=[
                Dependency(
                    name="flit-core",
                    kind=DepKind.BUILD,
                    requirement="flit-core>=3.2,<4",
                )
            ],
        )
        text, _ = render_recipe(knot, yocto)
        assert "inherit pypi python_flit_core" in text
        assert "setuptools3-base" not in text


class TestBuildVersionGaps:
    def test_cython_and_meson_python_gaps(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            build_deps=[
                Dependency(
                    name="Cython",
                    kind=DepKind.BUILD,
                    requirement="Cython>=3.1.0,<4",
                    specifier=">=3.1.0,<4",
                ),
                Dependency(
                    name="meson-python",
                    kind=DepKind.BUILD,
                    requirement="meson-python>=0.18.0,<0.19",
                    specifier=">=0.18.0,<0.19",
                ),
            ]
        )
        gaps = build_version_gaps(meta, yocto)
        assert [g.name for g in gaps] == ["Cython", "meson-python"]

    def test_satisfied_requirements_no_gap(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            build_deps=[
                Dependency(
                    name="flit-core",
                    kind=DepKind.BUILD,
                    requirement="flit_core>=3.5",
                    specifier=">=3.5",
                ),
                Dependency(
                    name="build",
                    kind=DepKind.BUILD,
                    requirement="build>=1.1.1",
                    specifier=">=1.1.1",
                ),
            ]
        )
        assert build_version_gaps(meta, yocto) == []

    def test_setuptools_and_wheel_exempt(self, make_meta_fn, yocto):
        # The PEP 517 classes always satisfy them from the sysroot (bounds are
        # relaxed); provisioning newer ones would churn every build env.
        meta = make_meta_fn(
            build_deps=[
                Dependency(
                    name="setuptools",
                    kind=DepKind.BUILD,
                    requirement="setuptools>=71.0.1",
                    specifier=">=71.0.1",
                ),
                Dependency(
                    name="wheel",
                    kind=DepKind.BUILD,
                    requirement="wheel>=0.43.0",
                    specifier=">=0.43.0",
                ),
            ]
        )
        assert build_version_gaps(meta, yocto) == []

    def test_unknown_and_unbounded_no_gap(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            build_deps=[
                Dependency(
                    name="custom-tool",
                    kind=DepKind.BUILD,
                    requirement="custom-tool>=2.0",
                    specifier=">=2.0",
                ),
                Dependency(
                    name="hatchling",
                    kind=DepKind.BUILD,
                    requirement="hatchling",
                    specifier="",
                ),
            ]
        )
        assert build_version_gaps(meta, yocto) == []

    def test_gap_compared_by_canonical_name(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            build_deps=[
                Dependency(
                    name="Cython",
                    kind=DepKind.BUILD,
                    requirement="Cython>=3.1.0",
                    specifier=">=3.1.0",
                )
            ]
        )
        gaps = build_version_gaps(meta, yocto)
        assert [canonical_name(g.name) for g in gaps] == ["cython"]

    def test_bootstrap_tools_never_gap(self, make_meta_fn, yocto):
        # Regression: blessed 1.50.0 demands ``flit_core >=3.11,<4`` while
        # Scarthgap's native sysroot ships flit-core 3.9.0. Provisioning the
        # newer 3.12.0 shadows poky's signature-locked recipe and PetaLinux
        # reports SIGGEN_LOCKEDSIGS mismatch warnings for
        # python3-flit-core-native (do_recipe_qa / do_fetch). The four PEP 517
        # bootstrap tools always come from the sysroot at locked signatures
        # (their [build-system] bounds are relaxed in render_recipe), so they
        # must never be provisioned. Other gapped tools still are.
        meta = make_meta_fn(
            build_deps=[
                Dependency(
                    name="flit-core",
                    kind=DepKind.BUILD,
                    requirement="flit_core>=3.11,<4",
                    specifier=">=3.11,<4",
                ),
                Dependency(
                    name="build",
                    kind=DepKind.BUILD,
                    requirement="build>=1.2.0",
                    specifier=">=1.2.0",
                ),
                Dependency(
                    name="installer",
                    kind=DepKind.BUILD,
                    requirement="installer>=0.8.0",
                    specifier=">=0.8.0",
                ),
                Dependency(
                    name="pyproject-hooks",
                    kind=DepKind.BUILD,
                    requirement="pyproject_hooks>=1.1.0",
                    specifier=">=1.1.0",
                ),
                Dependency(
                    name="Cython",
                    kind=DepKind.BUILD,
                    requirement="Cython>=3.1.0",
                    specifier=">=3.1.0",
                ),
            ]
        )
        gaps = build_version_gaps(meta, yocto)
        assert [g.name for g in gaps] == ["Cython"]

    def test_marker_gated_floor_skipped(self, make_meta_fn, yocto):
        # A pin that cannot apply on the target is not a floor the build has to
        # satisfy. meson-python 0.22.0 pairs ``packaging>=23.2; sys_platform !=
        # "ios"`` (satisfied by Scarthgap's 23.2) with ``packaging>=24.2;
        # sys_platform == "ios"``. Treating the iOS pin as a gap provisioned a
        # recipe for a platform the target never runs -- and pip then had to
        # resolve a requirement that cannot resolve off-iOS.
        meta = make_meta_fn(
            build_deps=[
                Dependency(
                    name="packaging",
                    kind=DepKind.BUILD,
                    requirement='packaging >= 23.2; sys_platform != "ios"',
                    specifier=">=23.2",
                    marker='sys_platform != "ios"',
                ),
                Dependency(
                    name="packaging",
                    kind=DepKind.BUILD,
                    requirement='packaging >= 24.2; sys_platform == "ios"',
                    specifier=">=24.2",
                    marker='sys_platform == "ios"',
                ),
            ]
        )
        assert build_version_gaps(meta, yocto) == []

    def test_marker_true_floor_still_counts(self, make_meta_fn, yocto):
        # The marker is satisfied on the target, so the floor stands.
        meta = make_meta_fn(
            build_deps=[
                Dependency(
                    name="pyproject-metadata",
                    kind=DepKind.BUILD,
                    requirement='pyproject-metadata >= 0.9.0; sys_platform != "win32"',
                    specifier=">=0.9.0",
                    marker='sys_platform != "win32"',
                )
            ]
        )
        assert [g.name for g in build_version_gaps(meta, yocto)] == [
            "pyproject-metadata"
        ]

    def test_unresolvable_marker_keeps_floor(self, make_meta_fn, yocto):
        # A marker we cannot evaluate must not silently drop a real floor.
        meta = make_meta_fn(
            build_deps=[
                Dependency(
                    name="pyproject-metadata",
                    kind=DepKind.BUILD,
                    requirement="pyproject-metadata >= 0.9.0; bogus_marker == 1",
                    specifier=">=0.9.0",
                    marker="bogus_marker == 1",
                )
            ]
        )
        assert [g.name for g in build_version_gaps(meta, yocto)] == [
            "pyproject-metadata"
        ]


class TestToolCompanionGaps:
    """A provisioned build tool's native companions are provisioned with it.

    Regression: provisioning ``python3-meson-python_0.22.0`` (numpy 2.5.3 wants
    ``meson-python>=0.18.0``; Scarthgap's native sysroot ships 0.15.0) emitted a
    recipe that could not build. meson-python 0.18.0+ forwards a third
    ``metadata_version`` argument to ``StandardMetadata.from_pyproject``, but
    Scarthgap's ``pyproject-metadata`` is 0.7.1, whose method takes two. The
    result was ``TypeError: StandardMetadata.from_pyproject() takes from 2 to 3
    positional arguments but 4 were given`` in ``do_compile``. The companion
    requirement is unversioned in meson-python's own metadata, so only the
    table can close the loop.
    """

    def test_meson_python_needs_newer_pyproject_metadata(
        self, make_meta_fn, yocto
    ):
        meta = make_meta_fn(
            name="meson-python",
            canonical_name="meson-python",
            version="0.22.0",
        )
        gaps = tool_companion_gaps(meta, yocto)
        assert [g.name for g in gaps] == ["pyproject-metadata"]
        assert gaps[0].specifier == ">=0.8.0"

    def test_older_meson_python_needs_nothing(self, make_meta_fn, yocto):
        # 0.17.0 and earlier do not pass metadata_version, so the 0.7.1 copy
        # in the sysroot is fine and no companion is provisioned.
        meta = make_meta_fn(
            name="meson-python",
            canonical_name="meson-python",
            version="0.17.0",
        )
        assert tool_companion_gaps(meta, yocto) == []

    def test_companion_at_or_above_bundled_is_no_gap(self, make_meta_fn, yocto):
        # Once the sysroot itself ships a new-enough pyproject-metadata, the
        # companion table must not provision a shadowing recipe.
        yocto_new = replace(yocto, version="99.0")
        with patch.dict(
            _BUNDLED_TOOL_VERSIONS,
            {"99.0": {"pyproject-metadata": "0.9.1"}},
        ):
            meta = make_meta_fn(
                name="meson-python",
                canonical_name="meson-python",
                version="0.22.0",
            )
            assert tool_companion_gaps(meta, yocto_new) == []

    def test_companion_absent_from_sysroot_is_a_gap(self, make_meta_fn, yocto):
        # A series table that lists no pyproject-metadata at all: provisioning
        # the companion is the only way the tool can run.
        with patch.dict(_BUNDLED_TOOL_VERSIONS, {"5.0": {}}, clear=True):
            meta = make_meta_fn(
                name="meson-python",
                canonical_name="meson-python",
                version="0.22.0",
            )
            assert [g.name for g in tool_companion_gaps(meta, yocto)] == [
                "pyproject-metadata"
            ]

    def test_non_tool_package_has_no_companions(self, make_meta_fn, yocto):
        assert tool_companion_gaps(make_meta_fn(name="numpy"), yocto) == []

    def test_unparsable_version_is_ignored(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            name="meson-python",
            canonical_name="meson-python",
            version="not-a-version",
        )
        assert tool_companion_gaps(meta, yocto) == []

    def test_malformed_table_entry_is_skipped(self, make_meta_fn, yocto):
        # The table is hand-maintained data; a bad entry must be ignored rather
        # than abort provisioning of every other tool.
        meta = make_meta_fn(
            name="meson-python",
            canonical_name="meson-python",
            version="0.22.0",
        )
        with patch.dict(
            _TOOL_COMPANION_REQUIREMENTS,
            {"meson-python": ((Version("0.18.0"), "not a requirement!!"),)},
        ):
            assert tool_companion_gaps(meta, yocto) == []

    def test_table_entry_without_floor_is_skipped(self, make_meta_fn, yocto):
        # A companion with no positive bound constrains nothing, so the sysroot
        # copy satisfies it and nothing is provisioned.
        meta = make_meta_fn(
            name="meson-python",
            canonical_name="meson-python",
            version="0.22.0",
        )
        with patch.dict(
            _TOOL_COMPANION_REQUIREMENTS,
            {"meson-python": ((Version("0.18.0"), "pyproject-metadata"),)},
        ):
            assert tool_companion_gaps(meta, yocto) == []


class TestBootstrapToolShapes:
    """PEP 517 bootstrap tools render with poky's own ``python_flit_core`` shape.

    Regression: py2bbr provisioned ``python3-flit-core_3.12.0`` into a
    PetaLinux layer (a dependency demanded ``flit_core>=3.12``; Scarthgap ships
    3.9.0). The generic ``inherit pypi python_pep517`` shape made that recipe
    append ``" python3-build-native python3-installer-native"`` to DEPENDS
    (python_pep517.bbclass), while poky's own build/installer/pyproject-hooks
    recipes ``inherit python_flit_core`` (whose class adds ``DEPENDS +=
    "python3 python3-flit-core-native"``). The higher-PV provisioned flit-core
    shadowed poky's, closing the loop
    ``flit-core -> build/installer-native -> flit-core-native``; PetaLinux then
    failed with hundreds of circular create-spdx dependencies. poky breaks the
    knot by building these tools *by hand* in their own recipes
    (``python_flit_core_do_manual_build`` / ``python_pep517_do_bootstrap_install``,
    removing the class-native build/installer DEPENDS); py2bbr mirrors those
    shapes verbatim.
    """

    def _knot(self, make_meta_fn, name, version, filename):
        return make_meta_fn(
            name=name,
            canonical_name=name,
            version=version,
            source_filename=filename,
            build_backend="flit_core.buildapi",
            build_deps=[
                Dependency(
                    name=name,
                    kind=DepKind.BUILD,
                    requirement="flit-core>=3.2,<4",
                )
            ],
        )

    def test_flit_core_renders_bootstrap_shape(self, make_meta_fn, yocto):
        meta = self._knot(
            make_meta_fn, "flit-core", "3.12.0", "flit_core-3.12.0.tar.gz"
        )
        text, warnings = render_recipe(meta, yocto)
        assert "inherit pypi python_flit_core" in text
        assert "inherit pypi python_pep517" not in text
        # poky's loop-breaking lines, verbatim.
        assert (
            'DEPENDS:remove:class-native = " python3-build-native '
            'python3-installer-native"' in text
        )
        assert 'DEPENDS:append:class-native = " unzip-native"' in text
        assert "python_flit_core_do_manual_build" in text
        assert "python_pep517_do_bootstrap_install" in text
        # The class provides python3-flit-core-native itself; no extra DEPENDS.
        assert 'DEPENDS += "python3-flit-core-native"' not in text
        assert 'PYPI_PACKAGE = "flit_core"' in text
        # No generic "not setuptools" warning for the known knot member.
        assert not any("not setuptools" in w for w in warnings)

    def test_build_renders_bootstrap_shape(self, make_meta_fn, yocto):
        meta = self._knot(make_meta_fn, "build", "1.1.1", "build-1.1.1.tar.gz")
        text, _ = render_recipe(meta, yocto)
        assert "inherit pypi python_flit_core" in text
        assert "inherit pypi python_pep517" not in text
        assert 'DEPENDS += "python3-pyproject-hooks-native"' in text
        assert 'DEPENDS:remove:class-native = "python3-build-native"' in text
        assert (
            'PEP517_BUILD_OPTS:class-native = "--skip-dependency-check"' in text
        )
        assert 'export PYTHONPATH="${S}/src"' in text
        # build compiles through the standard pep517 compile with the source
        # tree on PYTHONPATH (no manual flit build in poky's shape).
        assert "python_flit_core_do_manual_build" not in text

    def test_installer_renders_bootstrap_shape(self, make_meta_fn, yocto):
        meta = self._knot(
            make_meta_fn, "installer", "0.7.0", "installer-0.7.0.tar.gz"
        )
        text, _ = render_recipe(meta, yocto)
        assert "inherit pypi python_flit_core" in text
        assert (
            'DEPENDS:remove:class-native = "python3-build-native '
            'python3-installer-native"' in text
        )
        assert (
            "INSTALL_WHEEL_COMPILE_BYTECODE:class-native = "
            '"--no-compile-bytecode"' in text
        )
        assert "python_flit_core_do_manual_build" in text
        assert 'export PYTHONPATH="${S}/src"' in text

    def test_pyproject_hooks_renders_bootstrap_shape(self, make_meta_fn, yocto):
        meta = self._knot(
            make_meta_fn,
            "pyproject-hooks",
            "1.0.0",
            "pyproject_hooks-1.0.0.tar.gz",
        )
        text, _ = render_recipe(meta, yocto)
        assert "inherit pypi python_flit_core" in text
        assert "inherit pypi python_pep517" not in text
        assert 'DEPENDS:remove:class-native = "python3-build-native"' in text
        assert "python_flit_core_do_manual_build" in text
        assert 'PYPI_PACKAGE = "pyproject_hooks"' in text

    def test_knot_is_gated_by_canonical_name_not_backend(
        self, make_meta_fn, yocto
    ):
        # The same flit_core.buildapi backend on a non-knot package must keep
        # the generic python_pep517 shape and its warning.
        meta = make_meta_fn(
            name="somepkg",
            canonical_name="somepkg",
            version="1.0",
            source_filename="somepkg-1.0.tar.gz",
            build_backend="flit_core.buildapi",
            build_deps=[
                Dependency(
                    name="flit_core",
                    kind=DepKind.BUILD,
                    requirement="flit_core>=3.9",
                ),
            ],
        )
        text, warnings = render_recipe(meta, yocto)
        assert "inherit pypi python_pep517" in text
        assert "python_flit_core" not in text
        assert any("not setuptools" in w for w in warnings)

    def test_explicit_build_class_wins_over_knot_shape(
        self, make_meta_fn, yocto
    ):
        meta = self._knot(
            make_meta_fn, "flit-core", "3.12.0", "flit_core-3.12.0.tar.gz"
        )
        text, _ = render_recipe(meta, yocto, build_class="python_pep517")
        assert "inherit pypi python_pep517" in text
        assert "DEPENDS:remove:class-native" not in text

    def test_knot_shapes_stay_single_line_quoted(self, make_meta_fn, yocto):
        # The structural checks (no \ continuations / no ${PYTHON_PN} in values)
        # must hold for the bootstrap shape lines too.
        for name, version, filename in (
            ("flit-core", "3.12.0", "flit_core-3.12.0.tar.gz"),
            ("build", "1.1.1", "build-1.1.1.tar.gz"),
            ("installer", "0.7.0", "installer-0.7.0.tar.gz"),
            ("pyproject-hooks", "1.0.0", "pyproject_hooks-1.0.0.tar.gz"),
        ):
            meta = self._knot(make_meta_fn, name, version, filename)
            text, _ = render_recipe(meta, yocto)
            assert not [
                l for l in text.splitlines() if l.rstrip().endswith("\\")
            ], name
            assert "${PYTHON_PN}" not in text, name
            assert "inherit pypi python_flit_core" in text, name
            # The manual build path runs no pypa-build dependency check, so no
            # [build-system] bounds-relax sed for knot recipes.
            assert r"\[build-system\]" not in text, name


class TestCrossBuildNote:
    """A cross-built layer compiles for the target, tools stay -native.

    The generated recipe has to say *how* the target toolchain reaches the
    build, because that is the part a reviewer (or the next failure) needs to
    see: the PEP 517 frontend is a build-host tool, the extension modules must
    not be.
    """

    @staticmethod
    def _comments(text: str) -> str:
        """Recipe comments joined, so assertions ignore the wrapping."""
        return " ".join(
            line[2:] for line in text.splitlines() if line.startswith("# ")
        )

    def _native(self, make_meta_fn, **kw):
        return make_meta_fn(
            has_native_code=True,
            native_code_evidence=["C source (pkg/src/mod.c)"],
            **kw,
        )

    def test_pure_python_gets_no_cross_note(self, make_meta_fn, yocto):
        text, _ = render_recipe(make_meta_fn(), yocto)
        assert "Cross-build" not in text

    def test_mesonpy_note_mentions_the_cross_file(self, make_meta_fn, yocto):
        text, _ = render_recipe(
            self._native(make_meta_fn, build_backend="mesonpy"), yocto
        )
        note = self._comments(text)
        assert "python_mesonpy runs meson with the target cross file" in note
        assert "compiled for the target CPU" in note

    def test_setuptools_note_mentions_exported_toolchain(
        self, make_meta_fn, yocto
    ):
        text, _ = render_recipe(
            self._native(make_meta_fn, build_backend=""), yocto
        )
        note = self._comments(text)
        assert "setuptools3 inherits setuptools3-base" in note
        assert "LDSHARED" in note
        # Build tools must stay host binaries; the note has to say so.
        assert "Build tools stay -native" in note

    def test_setuptools_build_meta_is_also_cross_capable(
        self, make_meta_fn, yocto
    ):
        text, _ = render_recipe(
            self._native(make_meta_fn, build_backend="setuptools.build_meta"),
            yocto,
        )
        note = self._comments(text)
        assert "python_setuptools_build_meta inherits setuptools3-base" in note

    def test_host_only_class_warns_about_the_backend(self, make_meta_fn, yocto):
        text, warnings = render_recipe(self._native(make_meta_fn), yocto)
        note = self._comments(text)
        assert "generic python_pep517 class drives the PEP 517 frontend" in note
        assert "nativepython3" in note
        assert any(
            "ships compiled code" in w and "nativepython3" in w
            for w in warnings
        )

    def test_host_only_class_warns_once(self, make_meta_fn, yocto):
        _text, warnings = render_recipe(self._native(make_meta_fn), yocto)
        cross = [w for w in warnings if "ships compiled code" in w]
        assert len(cross) == 1

    def test_explicit_cross_class_skips_the_warning(self, make_meta_fn, yocto):
        _text, warnings = render_recipe(
            self._native(make_meta_fn), yocto, build_class="setuptools3"
        )
        assert not [w for w in warnings if "ships compiled code" in w]

    def test_x86_flags_warn(self, make_meta_fn, yocto):
        _text, warnings = render_recipe(
            self._native(
                make_meta_fn, host_assumptions=["setup.py: -march=native"]
            ),
            yocto,
        )
        assert any(
            "x86-specific CPU/ISA flags" in w and "aarch64" in w
            for w in warnings
        )

    def test_note_is_wrapped_to_a_readable_width(self, make_meta_fn, yocto):
        # The note is a comment block, so it can be multi-line, but each line
        # has to stay inside a readable width.
        text, _ = render_recipe(self._native(make_meta_fn), yocto)
        note = [l for l in text.splitlines() if l.startswith("# ")]
        assert note, "no cross note rendered"
        assert max(len(l) for l in note) <= 76


class TestMachineGatedRdepends:
    def _meta(self, make_meta_fn, *deps):
        return make_meta_fn(runtime_deps=[], machine_deps=list(deps))

    def test_rendered_as_machine_overrides(self, make_meta_fn, yocto):
        meta = self._meta(
            make_meta_fn,
            MachineDep(
                name="arm-only",
                requirement='arm-only; platform_machine == "aarch64"',
                marker='platform_machine == "aarch64"',
                machines=("aarch64",),
            ),
            MachineDep(
                name="x86-only",
                requirement='x86-only; platform_machine == "x86_64"',
                marker='platform_machine == "x86_64"',
                machines=("x86_64",),
            ),
        )
        text, _ = render_recipe(meta, yocto)
        lines = [l for l in text.splitlines() if l.startswith("RDEPENDS")]
        assert lines == [
            'RDEPENDS:${PN}:append:aarch64 += "python3-arm-only"',
            'RDEPENDS:${PN}:append:x86_64 += "python3-x86-only"',
        ]

    def test_32bit_arm_token(self, make_meta_fn, yocto):
        meta = self._meta(
            make_meta_fn,
            MachineDep(
                name="arm7",
                requirement='arm7; platform_machine == "armv7l"',
                marker='platform_machine == "armv7l"',
                machines=("armv7l",),
            ),
        )
        text, _ = render_recipe(meta, yocto)
        assert 'RDEPENDS:${PN}:append:armv7a += "python3-arm7"' in text

    def test_markers_are_not_baked_into_unconditional_rdepends(
        self, make_meta_fn, yocto
    ):
        meta = self._meta(
            make_meta_fn,
            MachineDep(
                name="arm-only",
                requirement='arm-only; platform_machine == "aarch64"',
                marker='platform_machine == "aarch64"',
                machines=("aarch64",),
            ),
        )
        text, _ = render_recipe(meta, yocto)
        assert "RDEPENDS:${PN} +=" not in text

    def test_unconditional_deps_and_machine_deps_coexist(
        self, make_meta_fn, yocto
    ):
        meta = make_meta_fn(
            machine_deps=[
                MachineDep(
                    name="arm-only",
                    requirement='arm-only; platform_machine == "aarch64"',
                    marker='platform_machine == "aarch64"',
                    machines=("aarch64",),
                )
            ]
        )
        text, _ = render_recipe(meta, yocto)
        assert 'RDEPENDS:${PN} += "python3-click python3-packaging"' in text
        assert 'RDEPENDS:${PN}:append:aarch64 += "python3-arm-only"' in text

    def test_explanatory_comment_is_emitted(self, make_meta_fn, yocto):
        meta = self._meta(
            make_meta_fn,
            MachineDep(
                name="arm-only",
                requirement='arm-only; platform_machine == "aarch64"',
                marker='platform_machine == "aarch64"',
                machines=("aarch64",),
            ),
        )
        text, _ = render_recipe(meta, yocto)
        assert "# Machine-gated dependencies" in text

    def test_no_machine_deps_no_comment(self, make_meta_fn, yocto):
        text, _ = render_recipe(make_meta_fn(), yocto)
        assert "# Machine-gated dependencies" not in text


class TestEffectiveBuildClass:
    def test_explicit_override_wins(self, make_meta_fn):
        meta = make_meta_fn(build_backend="mesonpy")
        assert effective_build_class(meta, "python_pep517") == "python_pep517"

    def test_bootstrap_knot_shape(self, make_meta_fn):
        meta = make_meta_fn(
            name="flit-core", canonical_name="flit-core", version="3.12.0"
        )
        assert effective_build_class(meta) == "python_flit_core"

    def test_knot_shape_does_not_override_an_explicit_class(self, make_meta_fn):
        meta = make_meta_fn(
            name="flit-core", canonical_name="flit-core", version="3.12.0"
        )
        assert effective_build_class(meta, "setuptools3") == "setuptools3"

    def test_detected_class(self, make_meta_fn):
        assert (
            effective_build_class(make_meta_fn(build_backend="mesonpy"))
            == "python_mesonpy"
        )

    def test_matches_what_render_recipe_inherits(self, make_meta_fn, yocto):
        meta = make_meta_fn(build_backend="hatchling.build")
        text, _ = render_recipe(meta, yocto)
        assert f"inherit pypi {effective_build_class(meta)}" in text


class TestSdistStem:
    """``S`` has to name the directory ``do_unpack`` really creates."""

    def test_strips_every_known_archive_suffix(self):
        assert _sdist_stem("pkg-1.0.tar.gz") == "pkg-1.0"
        assert _sdist_stem("pkg-1.0.tar.bz2") == "pkg-1.0"
        assert _sdist_stem("pkg-1.0.tar.xz") == "pkg-1.0"
        assert _sdist_stem("pkg-1.0.zip") == "pkg-1.0"

    def test_unknown_suffix_is_an_error_not_a_guess(self):
        # A wrong ``S`` fails far away from the cause, so refuse to invent one.
        with pytest.raises(ValueError, match="not a known sdist"):
            _sdist_stem("pkg-1.0.rar")


class TestMinimumVersion:
    def test_floor(self):
        assert str(_minimum_version(">=3.1.0")) == "3.1.0"

    def test_compatible_release_counts(self):
        assert str(_minimum_version("~=1.4.2")) == "1.4.2"

    def test_highest_floor_wins(self):
        assert str(_minimum_version(">=1.0,>=2.5")) == "2.5"

    def test_ceilings_only_is_no_floor(self):
        assert _minimum_version("<3.0") is None

    def test_wildcard_pin_is_not_a_floor(self):
        # ``==3.1.*`` parses, but the version is not a usable floor.
        assert _minimum_version("==3.1.*") is None

    def test_unparseable_specifier_is_ignored(self):
        assert _minimum_version("not a specifier") is None


class TestVendoredWheelRecipe:
    """A wheel-only package renders an install-the-wheel recipe, not a build.

    ``inherit pypi`` can only ever fetch a ``.tar.gz`` derived from
    BPN/PV, so a recipe whose recorded sha256 is a wheel's would either 404
    (no sdist upstream) or die on "sha256 mismatch" (one exists) while
    pointing at an artifact it was not hashed from. Vendoring the wheel and
    installing it directly is the only shape where do_fetch and
    ``SRC_URI[sha256sum]`` agree.
    """

    def _wheel_meta(self, make_meta_fn, **overrides):
        fields = {
            "source_kind": SourceKind.WHEEL,
            "source_filename": "pyvizionsdk-26.9.1-py3-none-any.whl",
            "source_sha256": "a" * 64,
            "vendored": True,
        }
        fields.update(overrides)
        return make_meta_fn(**fields)

    def test_does_not_inherit_pypi_or_set_s(self, make_meta_fn, yocto):
        # Both are fatal in the wheel shape: the pypi fetch URL is derived from
        # BPN/PV, and there is no archive to unpack into an S directory.
        text, _ = render_recipe(self._wheel_meta(make_meta_fn), yocto)
        assert "inherit pypi" not in text
        assert not [ln for ln in text.splitlines() if ln.startswith("S =")]

    def test_vendors_the_wheel_by_name(self, make_meta_fn, yocto):
        meta = self._wheel_meta(make_meta_fn)
        text, _ = render_recipe(meta, yocto)
        assert 'SRC_URI = "file://pyvizionsdk-26.9.1-py3-none-any.whl"' in text
        assert f'SRC_URI[sha256sum] = "{meta.source_sha256}"' in text

    def test_installs_the_wheel_with_installer(self, make_meta_fn, yocto):
        text, _ = render_recipe(self._wheel_meta(make_meta_fn), yocto)
        # installer itself is a build tool, so it comes from the -native side.
        assert 'DEPENDS += "python3-installer-native"' in text
        assert "do_install() {" in text
        # nativepython3, not ${PYTHON}: in a poky target recipe ${PYTHON} is
        # the *native* interpreter, and the target one is a target-arch binary
        # that cannot execute on the build host. The wheel's prebuilt extension
        # modules are unpacked, never loaded.
        assert "nativepython3 -m installer" in text
        # --prefix for the same reason as every other recipe: installer derives
        # its scheme from the running interpreter's absolute sysconfig base.
        # For class-native ${prefix} is the sysroot-prefixed path, which is
        # exactly poky's ${D}${prefix} convention there - verified in the
        # PetaLinux 2024.2 build.
        assert "--prefix ${prefix}" in text
        assert "--destdir=${D}" in text
        # The recorded shebang is the interpreter that will actually run the
        # script: the target's for the target wheel, the build host's for the
        # -native/-nativesdk variants.
        assert 'WHEEL_INSTALL_PYTHON = "python3"' in text
        assert 'WHEEL_INSTALL_PYTHON:class-native = "nativepython3"' in text
        assert 'WHEEL_INSTALL_PYTHON:class-nativesdk = "nativepython3"' in text
        assert (
            '--interpreter "${USRBINPATH}/env ${WHEEL_INSTALL_PYTHON}"' in text
        )

    def test_inherits_the_sitepackages_default(self, make_meta_fn, yocto):
        # setuptools3-base defines PYTHON_SITEPACKAGES_DIR and the FILES
        # default, without which do_package reports "installed but not
        # shipped" for every payload file.
        text, _ = render_recipe(self._wheel_meta(make_meta_fn), yocto)
        assert "inherit python3targetconfig setuptools3-base" in text

    def test_carries_no_build_machinery(self, make_meta_fn, yocto):
        # There is no source and no backend, so none of the PEP 517 output
        # applies: no build-backend -native deps (only installer, which is the
        # install tool itself), no pyproject.toml rewrites, and no PEP 517
        # build-class override.
        text, _ = render_recipe(self._wheel_meta(make_meta_fn), yocto)
        depends = [
            ln
            for ln in text.splitlines()
            if ln.startswith(("DEPENDS", "RDEPENDS"))
        ]
        # installer is the install tool itself, not a PEP 517 build backend.
        assert depends == [
            'DEPENDS += "python3-installer-native"',
            'RDEPENDS:${PN} += "python3-click python3-packaging"',
        ]
        assert "do_configure" not in text
        assert "pyproject.toml" not in text
        # No PEP 517 build class and none of its task overrides (the
        # explanatory comment does mention python_pep517_do_install by name).
        assert "inherit python_pep517" not in text
        assert "python_pep517_do_install() {" not in text
        assert "python_pep517_do_compile" not in text

    def test_runtime_and_machine_deps_are_carried(self, make_meta_fn, yocto):
        meta = self._wheel_meta(
            make_meta_fn,
            machine_deps=[MachineDep(name="libfoo", machines=("aarch64",))],
        )
        text, _ = render_recipe(meta, yocto)
        assert 'RDEPENDS:${PN} += "python3-click python3-packaging"' in text
        assert 'RDEPENDS:${PN}:append:aarch64 += "python3-libfoo"' in text

    def test_warns_about_the_artifact_shape(self, make_meta_fn, yocto):
        _, warnings = render_recipe(self._wheel_meta(make_meta_fn), yocto)
        assert any("no source distribution" in w for w in warnings)

    def test_waives_already_stripped_only_for_prebuilt_wheels(
        self, make_meta_fn, yocto
    ):
        # The reported do_package failure: a wheel's extension modules arrive
        # already stripped, and bitbake fails the build with the fatal QA error
        # "already-stripped". The recipe waives it - but only when the wheel's
        # tag admits prebuilt binaries, so a pure-Python wheel keeps the check.
        text, warnings = render_recipe(self._wheel_meta(make_meta_fn), yocto)
        assert "already-stripped" not in text
        assert "file-rdeps" not in text
        assert not any("already-stripped" in w for w in warnings)
        meta = self._wheel_meta(
            make_meta_fn,
            source_filename=(
                "pyvizionsdk-26.9.1-cp312-cp312-manylinux_2_31_aarch64.whl"
            ),
            ships_prebuilt_binaries=True,
        )
        text, warnings = render_recipe(meta, yocto)
        assert 'INSANE_SKIP:${PN} = "already-stripped file-rdeps"' in text
        assert any("already-stripped QA check" in w for w in warnings)

    def test_lists_external_system_libraries_the_wheel_links_against(
        self, make_meta_fn, yocto
    ):
        # Waiving file-rdeps (same _WHEEL_QA_SKIP block) must not hide what
        # that check would otherwise have enforced: the real system libraries
        # the bundled binaries link against that are not bundled in the wheel.
        # They are listed in the recipe and warned about, each with the typical
        # OE package when py2bbr knows one - but never guessed into RDEPENDS,
        # since the provider name differs between distros.
        meta = self._wheel_meta(
            make_meta_fn,
            source_filename=(
                "pyvizionsdk-26.9.1-cp312-cp312-manylinux_2_31_aarch64.whl"
            ),
            ships_prebuilt_binaries=True,
            external_needed_libs=[
                "libgstreamer-1.0.so.0",
                "libgobject-2.0.so.0",
                "libweird-2.0.so.0",
            ],
        )
        text, warnings = render_recipe(meta, yocto)
        assert (
            "# in the wheel and must already be in the target rootfs:" in text
        )
        assert "typically packaged as gstreamer1.0" in text
        assert "typically packaged as glib-2.0" in text
        assert "no OE provider in py2bbr's knowledge" in text
        assert 'RDEPENDS:${PN} += "python3-click python3-packaging"' in text
        rdeps_lines = [
            ln for ln in text.splitlines() if ln.startswith("RDEPENDS")
        ]
        assert all(
            "gstreamer1.0" not in ln and "glib-2.0" not in ln
            for ln in rdeps_lines
        )
        warned = next(w for w in warnings if "not bundled in the wheel" in w)
        assert (
            "libgstreamer-1.0.so.0, libgobject-2.0.so.0, libweird-2.0.so.0"
            in warned
        )
        assert "Typical OE providers are glib-2.0, gstreamer1.0" in warned

    def test_reprovides_bundled_sidecar_lib_tokens(self, make_meta_fn, yocto):
        # The wheel bundles auditwheel-renamed copies of system libraries
        # (pyvizionsdk.libs/*.so*). rpm emits requires for the versioned
        # symbols under the renamed soname - libblkid-e4a77f31.so.1.1.0(BLKID_1.0)
        # (64bit) - which no other package can provide; the libraries ship in
        # this wheel, so the recipe provides those tokens itself or do_rootfs
        # fails with "nothing provides".
        meta = self._wheel_meta(
            make_meta_fn,
            source_filename=(
                "pyvizionsdk-26.9.1-cp312-cp312-manylinux_2_31_aarch64.whl"
            ),
            ships_prebuilt_binaries=True,
            external_needed_libs=["libgstreamer-1.0.so.0"],
            bundled_lib_versions={
                "libblkid-e4a77f31.so.1.1.0": [
                    "BLKID_1.0",
                    "BLKID_2.15",
                    "BLKID_2.17",
                ],
                "libudev-8d5af18d.so.1.6.11": ["LIBUDEV_183"],
            },
        )
        text, _ = render_recipe(meta, yocto)
        # One RPROVIDES assignment per bundled library, each with balanced
        # quotes (bitbake line continuations would fail py2bbr's own structural
        # check). The unversioned DT_NEEDED reference yields a bare
        # soname()(64bit) require; rpm needs that token provided next to the
        # versioned ones.
        assert (
            'RPROVIDES:${PN} += "libblkid-e4a77f31.so.1.1.0()(64bit) '
            "libblkid-e4a77f31.so.1.1.0(BLKID_1.0)(64bit) "
            "libblkid-e4a77f31.so.1.1.0(BLKID_2.15)(64bit) "
            'libblkid-e4a77f31.so.1.1.0(BLKID_2.17)(64bit)"' in text
        )
        assert (
            'RPROVIDES:${PN} += "libudev-8d5af18d.so.1.6.11()(64bit) '
            'libudev-8d5af18d.so.1.6.11(LIBUDEV_183)(64bit)"' in text
        )
        # The dialog right before the block names the failure it prevents.
        assert 'do_rootfs fails with "nothing' in text
        # The runtime package dependencies stay as generated.
        assert 'RDEPENDS:${PN} += "python3-click python3-packaging"' in text

    def test_pure_python_wheel_omits_bundled_lib_provides(
        self, make_meta_fn, yocto
    ):
        # No bundled libraries -> no RPROVIDES block at all.
        text, _ = render_recipe(self._wheel_meta(make_meta_fn), yocto)
        assert "RPROVIDES:${PN}" not in text

    def test_pure_python_wheel_stays_quiet_about_external_libs(
        self, make_meta_fn, yocto
    ):
        # No prebuilt binaries -> no file-rdeps waiver and no external-library
        # commentary; an empty list renders nothing.
        text, warnings = render_recipe(self._wheel_meta(make_meta_fn), yocto)
        assert "NOT bundled" not in text
        assert not any("not bundled in the wheel" in w for w in warnings)

    def test_warns_where_the_recovered_backend_came_from(
        self, make_meta_fn, yocto
    ):
        # A wheel carries no build config; when it was read from the project's
        # repository, the recipe says so - that answer describes the default
        # branch, not necessarily this release.
        meta = self._wheel_meta(
            make_meta_fn,
            build_backend="mesonpy",
            build_backend_source="github.com/vizion/pyvizionsdk@pyproject.toml",
        )
        _, warnings = render_recipe(meta, yocto)
        assert any("default branch" in w for w in warnings)
        assert any("github.com/vizion/pyvizionsdk" in w for w in warnings)

    def test_names_the_runtime_the_wheel_must_match(self, make_meta_fn, yocto):
        # A vendored wheel is a build product for one exact Python ABI *and*
        # CPU, so the recipe has to say which pair it was picked for - and the
        # warning has to name it, since that pair is what a later retarget has
        # to reproduce.
        target = replace(
            yocto, python_version="3.12", platform_machine="aarch64"
        )
        text, warnings = render_recipe(self._wheel_meta(make_meta_fn), target)
        assert "# Runtime: Python 3.12 on aarch64" in text
        assert any("Python 3.12 on aarch64" in w for w in warnings)

    def test_explicit_build_class_is_reported_not_crashed(
        self, make_meta_fn, yocto
    ):
        # A --build-class names a class that would build the package, and a
        # wheel has no source to build. That is a contradiction to report, not
        # a request to emit a build recipe (which would crash looking for an
        # unpack directory in a .whl).
        text, warnings = render_recipe(
            self._wheel_meta(make_meta_fn),
            yocto,
            build_class="python_pep517",
        )
        assert "do_install() {" in text
        assert any("python_pep517" in w for w in warnings)

    def test_sdist_still_takes_the_build_path(self, make_meta_fn, yocto):
        # The other vendored shape (private-index sdist) is unaffected: it
        # unpacks an S and builds through the PEP 517 classes.
        meta = make_meta_fn(
            source_filename="pyvizionsdk-26.9.1.tar.gz", vendored=True
        )
        text, _ = render_recipe(meta, yocto)
        assert 'S = "${WORKDIR}/pyvizionsdk-26.9.1"' in text
        # The install-prefix override renames python_pep517_do_install, so a
        # bare do_install would mean the wheel shape.
        assert "\ndo_install() {" not in text


class TestToolInstallAdaptations:
    """A provisioned tool must keep the fixups poky's own recipe applied.

    Bitbake keys tasks by recipe name, so a generated recipe *replaces* the
    distro's for that name. Anything the distro's recipe did after install is
    then silently lost, and the consumers that relied on it stop resolving.
    poky's python3-cython renames its console scripts to the versioned names
    (``cython`` -> ``cython3``), and meson resolves its cython compiler by
    running ``cython3 -V`` and nothing else - so a provisioned Cython keeping
    upstream's script names left numpy 2.5.3's do_compile aborting with
    ``ERROR: Unknown compiler(s): [['cython3']]``.
    """

    def test_cython_scripts_are_renamed(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            name="cython", canonical_name="cython", version="3.3.0"
        )
        text, _ = render_recipe(meta, yocto)
        assert "do_install:append() {" in text
        assert "mv ${D}${bindir}/cython ${D}${bindir}/cython3" in text
        assert "mv ${D}${bindir}/cythonize ${D}${bindir}/cythonize3" in text
        assert "mv ${D}${bindir}/cygdb ${D}${bindir}/cygdb3" in text

    def test_renames_are_guarded(self, make_meta_fn, yocto):
        # poky uses a bare ``mv``, which would fail the build if a future
        # Cython dropped a script. The guard keeps the rename best-effort.
        meta = make_meta_fn(name="cython", canonical_name="cython")
        text, _ = render_recipe(meta, yocto)
        assert text.count('if [ -x "${D}${bindir}/') == 3

    def test_absent_for_other_packages(self, make_meta_fn, yocto):
        meta = make_meta_fn(name="black", canonical_name="black")
        text, _ = render_recipe(meta, yocto)
        assert "cython3" not in text


class TestWheelRecipeLicense:
    """A wheel recipe must give ``do_populate_lic`` something it can resolve.

    ``do_populate_lic`` searches ``${S}`` for the file named in
    ``LIC_FILES_CHKSUM``, and a wheel recipe has no source tree - the license
    text is only inside the vendored zip. Pointing at the in-wheel path alone
    would fail the task, so the common-license copy the target already ships is
    preferred, and the in-wheel path is only used (with a prefunc that unpacks
    it) for an SPDX id outside that table.
    """

    def _wheel_meta(self, make_meta_fn, **overrides):
        base = {
            "source_kind": SourceKind.WHEEL,
            "source_filename": "pkg-1.0-py3-none-any.whl",
            "vendored": True,
        }
        base.update(overrides)
        return make_meta_fn(**base)

    def test_common_license_is_preferred(self, make_meta_fn, yocto):
        # MIT is in the table, and the target ships that text: it resolves on
        # any Yocto and needs no ${S} content.
        meta = self._wheel_meta(
            make_meta_fn, license="MIT", license_file="LICENSE", license_md5="x"
        )
        text, _ = render_recipe(meta, yocto)
        assert "COMMON_LICENSE_DIR" in text
        assert "unpack_wheel_license" not in text
        assert "unzip-native" not in text

    def test_rare_license_is_extracted_from_the_wheel(
        self, make_meta_fn, yocto
    ):
        meta = self._wheel_meta(
            make_meta_fn,
            license="BUSL-1.1",
            license_file="LICENSE",
            license_md5="d3465a2a183908c9cb95bf490bd1e7ab",
        )
        text, _ = render_recipe(meta, yocto)
        assert (
            'LIC_FILES_CHKSUM = "file://LICENSE;md5=d3465a2a183908c9cb95bf490bd1e7ab"'
            in text
        )
        # do_populate_lic is a Python task, so the shell prefunc is the hook.
        assert 'do_populate_lic[prefuncs] += "unpack_wheel_license"' in text
        assert "unzip -jo ${WORKDIR}/*.whl" in text
        # Both PEP 639 and the older flat dist-info layout.
        assert '"*dist-info/licenses/LICENSE"' in text
        assert '"*dist-info/LICENSE"' in text
        assert 'DEPENDS += "python3-installer-native unzip-native"' in text

    def test_license_absent_from_metadata_warns(self, make_meta_fn, yocto):
        # Nothing to checksum at all: say so instead of emitting a
        # LIC_FILES_CHKSUM that do_populate_lic cannot resolve.
        meta = self._wheel_meta(
            make_meta_fn, license="", license_file="", license_md5=""
        )
        text, warnings = render_recipe(meta, yocto)
        assert "LIC_FILES_CHKSUM" not in text
        assert 'LICENSE = "CLOSED"' in text
        assert any("add LIC_FILES_CHKSUM manually" in w for w in warnings)

    def test_rare_license_without_a_file_warns(self, make_meta_fn, yocto):
        meta = self._wheel_meta(
            make_meta_fn,
            license="BUSL-1.1",
            license_file="",
            license_md5="",
        )
        _, warnings = render_recipe(meta, yocto)
        assert any("no license file to checksum" in w for w in warnings)
