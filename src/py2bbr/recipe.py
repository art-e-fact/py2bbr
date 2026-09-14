"""
Render a Bitbake recipe (``python3-<name>.bb``) from package metadata.

The emitted recipe follows the canonical meta-python / OE-core pattern:

* ``inherit pypi``  - fetch from PyPI (the class strips the ``python3-`` prefix
  to derive the upstream name, so no explicit ``BPN`` is needed).
* ``inherit <build_class>`` (default ``setuptools3``) - drive the build.
* ``RDEPENDS:${PN} += "${PYTHON_PN}-<dep>"`` for runtime deps.
* ``LIC_FILES_CHKSUM`` from the detected license file.
* ``SRC_URI[sha256sum]`` from the sdist we downloaded.
"""

from __future__ import annotations

import re

from py2bbr.model import PackageMetadata, YoctoVersion


def canonical_name(name: str) -> str:
    """PEP 503 canonical form: lowercase, ``-``/``_``/``.`` runs -> ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def bitbake_pn(name: str, python_pn: str = "python3") -> str:
    """The Bitbake package name, e.g. ``python3-black``."""
    return f"{python_pn}-{canonical_name(name)}"


def recipe_filename(pn: str, version: str) -> str:
    """The recipe file name, e.g. ``python3-black_26.5.1.bb``."""
    return f"{pn}_{version}.bb"


def _q(value: str) -> str:
    """Quote a value for a Bitbake double-quoted string literal."""
    value = value.replace("\\", "\\\\")
    value = value.replace('"', '\\"')
    # Collapse to a single line: Bitbake string values are single-line.
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _dep_var(dep_name: str) -> str:
    """Render a dependency as ``${PYTHON_PN}-<name>``."""
    return f"${{PYTHON_PN}}-{canonical_name(dep_name)}"


def pick_build_class(backend: str) -> tuple[str, bool]:
    """
    Return ``(build_class, needs_warning)`` for the detected PEP 517 backend.

    In Scarthgap, ``setuptools3`` runs a ``do_check_backend`` QA check that FAILS
    when a ``pyproject.toml`` declares a ``build-backend``. So:

    * no backend (setup.py / implicit)   -> ``setuptools3``
    * ``setuptools.build_meta`` (etc.)   -> ``python_setuptools_build_meta``
    * any other backend (hatchling, ...) -> ``python_pep517`` (+ warning)

    ``python_setuptools_build_meta`` and ``python_pep517`` both exist in
    Scarthgap and drive PEP 517 builds, with the backend package supplied via
    DEPENDS.
    """
    backend = (backend or "").lower()
    if not backend:
        return "setuptools3", False
    if "setuptools" in backend:
        return "python_setuptools_build_meta", False
    return "python_pep517", True


def _bitbake_deps(names: list[str], prefix: str = "${PYTHON_PN}-") -> list[str]:
    seen: dict[str, str] = {}
    for n in names:
        var = f"{prefix}{canonical_name(n)}"
        seen.setdefault(var, n)
    return list(seen.keys())


def render_recipe(
    meta: PackageMetadata,
    yocto: YoctoVersion,
    build_class: str | None = None,
) -> tuple[str, list[str]]:
    """
    Render the ``.bb`` content.

    Returns ``(recipe_text, warnings)``.
    """
    warnings: list[str] = []

    detected_class, warn_backend = pick_build_class(meta.build_backend)
    build_class = build_class or detected_class

    if warn_backend:
        warnings.append(
            f"build-backend '{meta.build_backend}' is not setuptools; the recipe "
            f"uses the generic PEP 517 class '{build_class}' and adds the backend "
            f"to DEPENDS. Verify the backend package (e.g. python3-hatchling) is "
            f"available in your Yocto environment, or override with --build-class."
        )

    lines: list[str] = []
    lines.append(f'SUMMARY = "{_q(meta.summary or meta.name)}"')
    if meta.description and meta.description != meta.summary:
        lines.append(f'DESCRIPTION = "{_q(meta.description)}"')
    if meta.homepage:
        lines.append(f'HOMEPAGE = "{_q(meta.homepage)}"')
    if meta.license:
        lines.append(f'LICENSE = "{_q(meta.license)}"')
        if meta.license_file and meta.license_md5:
            lines.append(
                f'LIC_FILES_CHKSUM = "file://{meta.license_file};md5={meta.license_md5}"'
            )
        else:
            warnings.append(
                "no license file found in the source tree; "
                "add LIC_FILES_CHKSUM manually (license is declared via metadata only)."
            )
    else:
        warnings.append(
            "no license detected; set LICENSE manually before building."
        )
    lines.append("")

    if meta.source_sha256:
        lines.append(f'SRC_URI[sha256sum] = "{meta.source_sha256}"')
    if meta.requires_python:
        # Record the upstream Python constraint (informational; bitbake enforces
        # its own Python version).
        lines.append(f"# Upstream requires Python {meta.requires_python}")
    lines.append("")

    lines.append(f"inherit pypi {build_class}")
    lines.append("")

    # Build-time deps: the build class already provides setuptools + wheel as
    # ``-native``. Add any *extra* PEP 517 build requirements as ``-native``
    # variants (e.g. python3-setuptools-scm-native, python3-hatchling-native).
    extra_build = [
        d.name
        for d in meta.build_deps
        if d.name.lower() not in ("setuptools", "wheel")
    ]
    if extra_build:
        lines.append("DEPENDS += \\")
        for var in _bitbake_deps(extra_build):
            lines.append(f"    {var}-native \\")
        lines.append("")

    # Runtime dependencies.
    if meta.runtime_deps:
        rendered = _bitbake_deps([d.name for d in meta.runtime_deps])
        lines.append("RDEPENDS:${PN} += \\")
        for var in rendered:
            lines.append(f"    {var} \\")
        lines.append("")

    lines.append('BBCLASSEXTEND = "native nativesdk"')
    lines.append("")

    # Trailing comment block for provenance / dropped deps (debug aid).
    lines.append(
        f"# Generated by py2bbr for {meta.canonical_name} {meta.version}"
    )
    lines.append(
        f"# Target: {yocto.label} | source: {meta.source_filename} "
        f"({meta.source_kind})"
    )
    if meta.dropped_deps:
        lines.append(
            "# Dropped dependencies (optional extras / non-target markers):"
        )
        for dep in meta.dropped_deps:
            reason = f" - {dep.reason}" if dep.reason else ""
            lines.append(f"#   {dep.name}{reason}")
    lines.append("")

    return "\n".join(lines), warnings


def render_recipe_to_path(
    meta: PackageMetadata,
    yocto: YoctoVersion,
    out_dir,
    build_class: str | None = None,
) -> tuple:
    """Write the recipe into ``out_dir``; return ``(path, warnings)``."""
    from pathlib import Path

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pn = bitbake_pn(meta.name, yocto.python_pn)
    path = out_dir / recipe_filename(pn, meta.version)
    text, warnings = render_recipe(meta, yocto, build_class=build_class)
    path.write_text(text, encoding="utf-8")
    return path, warnings
