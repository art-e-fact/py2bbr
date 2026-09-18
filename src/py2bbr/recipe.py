"""
Render a Bitbake recipe (``python3-<name>.bb``) from package metadata.

The emitted recipe follows the canonical meta-python / OE-core pattern:

* ``inherit pypi``  - fetch from PyPI (the class strips the ``python3-`` prefix
  to derive the upstream name, so no explicit ``BPN`` is needed).
* ``inherit <build_class>`` (default ``setuptools3``) - drive the build.
* ``DEPENDS += "python3-<dep>-native"`` / ``RDEPENDS:${PN} += "python3-<dep>"``
  as single-line, double-quoted assignments with a literal ``python3-`` prefix.
* ``LIC_FILES_CHKSUM`` from the detected license file.
* ``SRC_URI[sha256sum]`` from the sdist we downloaded.

DEPENDS / RDEPENDS are deliberately NOT rendered as multi-line ``\\``
continuations with ``${PYTHON_PN}`` references: a line-oriented post-processing
pass (shell-style continuation joining, ``${VAR}`` expansion with the variable
unset, timestamp appending) corrupts that construct into unparsable lines such
as ``DEPENDS += -x-native '00:00`` (seen in PetaLinux). Single-line quoted
values with a literal prefix are byte-identical in shape to every other
assignment line and survive such passes intact.

* When the recipe uses a PEP 517 build class (``python_pep517`` /
  ``python_setuptools_build_meta``) and ``build-system.requires`` pins versions,
  a ``do_configure:append`` relaxes those bounds with a ``[build-system]``-scoped
  sed. Rationale: the class' ``do_compile`` runs ``nativepython3 -m build
  --no-isolation --wheel``, and pypa/build's ``--no-isolation`` path compares
  the ``requires`` bounds against the *native* sysroot versions, aborting when
  the project demands newer ones than the Yocto series ships (Scarthgap caps at
  setuptools 69.1.1 / wheel 0.42.0; ``async-pyserial`` 0.2.4 wants
  ``setuptools>=71.0.1`` / ``wheel>=0.43.0``). The native versions are
  authoritative, so the bounds are stripped; ``[project]`` dependency bounds
  are left intact.
"""

from __future__ import annotations

import re

from packaging.requirements import InvalidRequirement, Requirement

from py2bbr.model import Dependency, PackageMetadata, YoctoVersion


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


def _bitbake_deps(names: list[str], prefix: str) -> list[str]:
    """Render dependency names as ``<prefix><canonical name>``, deduplicated.

    ``prefix`` is the literal package prefix (e.g. ``python3-``) — never a
    ``${...}`` reference, see the module docstring.
    """
    seen: dict[str, str] = {}
    for n in names:
        var = f"{prefix}{canonical_name(n)}"
        seen.setdefault(var, n)
    return list(seen.keys())


def _pypi_package_name(source_filename: str, version: str) -> str | None:
    """The upstream package name an sdist filename encodes, if derivable.

    e.g. ``async_pyserial-0.2.4.tar.gz`` -> ``async_pyserial``. The ``pypi``
    class derives ``PYPI_PACKAGE`` from ``BPN`` (dashes), but PyPI publishes
    sdist filenames with the real package name (often underscores); when the
    two differ the class's fetch URL 404s, so the recipe must pin the real
    name. Returns ``None`` when the version cannot be matched.
    """
    for ext in ("tar.gz", "zip"):
        suffix = f"-{version}.{ext}"
        if source_filename.endswith(suffix):
            return source_filename[: -len(suffix)]
    return None


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


def _has_version_bound(req: str) -> bool:
    """Whether a PEP 508 requirement string pins versions (e.g. ``>=1.2``)."""
    try:
        return bool(Requirement(req).specifier)
    except InvalidRequirement:
        return False


def _needs_bounds_relax(build_class: str, build_deps: list[Dependency]) -> bool:
    """Whether the recipe must relax ``build-system.requires`` version bounds.

    Only the PEP 517 classes run ``python3 -m build --no-isolation``, whose
    dependency check compares the ``requires`` bounds against the native
    sysroot. The legacy ``setuptools3`` path installs the provided setuptools
    from the sysroot, so it never hits the check.
    """
    if build_class not in ("python_pep517", "python_setuptools_build_meta"):
        return False
    return any(_has_version_bound(d.requirement) for d in build_deps)


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
    pypi_name = _pypi_package_name(meta.source_filename, meta.version)
    if pypi_name and pypi_name != meta.canonical_name:
        # The pypi class derives PYPI_PACKAGE from BPN (e.g. ``async-pyserial``),
        # but PyPI publishes the sdist under the real package name (often
        # ``async_pyserial``). Pinning the real name keeps both the fetch URL
        # and ``S = ${WORKDIR}/${PYPI_PACKAGE}-${PV}`` on the actual artifact.
        lines.append(f'PYPI_PACKAGE = "{pypi_name}"')
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
        deps = " ".join(
            f"{var}-native"
            for var in _bitbake_deps(extra_build, f"{yocto.python_pn}-")
        )
        lines.append(f'DEPENDS += "{deps}"')
        lines.append("")

    if meta.runtime_deps:
        deps = " ".join(
            _bitbake_deps(
                [d.name for d in meta.runtime_deps], f"{yocto.python_pn}-"
            )
        )
        lines.append(f'RDEPENDS:${{PN}} += "{deps}"')
        lines.append("")

    if _needs_bounds_relax(build_class, meta.build_deps):
        warnings.append(
            "build-system.requires pins versions that may exceed the target "
            "Yocto's native setuptools/wheel; the recipe relaxes them in "
            "do_configure:append (the native sysroot versions are authoritative)."
        )
        sed_program = (
            r"/^[[:space:]]*\[build-system\]/,/^[[:space:]]*\[/ "
            r"s/([A-Za-z0-9_.]+)([[:space:]]*[<>=!~][^\"';]*[0-9][^\"';]*)(\"|')/\1\3/g"
        )
        lines.append("do_configure:append() {")
        lines.append("    if [ -f ${S}/pyproject.toml ]; then")
        lines.append(f'        sed -i -E "{sed_program}" ${{S}}/pyproject.toml')
        lines.append("    fi")
        lines.append("}")
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
