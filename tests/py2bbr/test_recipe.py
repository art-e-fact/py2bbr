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

import pytest

from py2bbr.model import Dependency, DepKind, YoctoVersion
from py2bbr.recipe import (
    _classifier_drop_pattern,
    _newer_minor_pattern,
    render_recipe,
    render_recipe_to_path,
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
