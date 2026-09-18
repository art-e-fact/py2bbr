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

from py2bbr.model import Dependency, DepKind, YoctoVersion
from py2bbr.recipe import render_recipe


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

    def test_unbounded_requires_no_relax_block(self, make_meta_fn, yocto):
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
        assert "do_configure:append" not in text

    def test_malformed_requirement_no_relax_block(self, make_meta_fn, yocto):
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
        assert "do_configure:append" not in text

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
