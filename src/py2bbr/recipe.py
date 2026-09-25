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

* PEP 517 backends that make pypa/build's ``--no-isolation`` dependency check
  demand non-Python tools (meson-python's recursive ``meson`` requirement plus
  the ninja binary its hook appends; scikit-build-core's cmake/ninja) get them
  declared as literal ``-native`` DEPENDS (``meson-native ninja-native`` /
  ``cmake-native ninja-native``). Bitbake does not stage a native dependency's
  own natives into the consuming recipe's sysroot, so the recipe must name them
  itself (numpy 2.5.3 under PetaLinux aborted with "Missing dependencies:
  meson-python / meson>=1.2.3 / ninja>=1.8.2").

* ``python_pep517`` recipes additionally prune ``project.classifiers`` lines
  that claim Python versions newer than the target (e.g. ``Python :: 3.14`` on
  a 3.12 image): the native sysroot's hatchling validates classifiers against
  its bundled trove list, and ``build_wheel`` aborts on unknown ones (xmod
  1.10.0 under PetaLinux). The entries are metadata-only, so the same
  ``do_configure:append`` drops them.
"""

from __future__ import annotations

import re
import shutil

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from py2bbr.model import Dependency, PackageMetadata, YoctoVersion


def canonical_name(name: str) -> str:
    """PEP 503 canonical form: lowercase, ``-``/``_``/``.`` runs -> ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


# PEP 517 build backends that are standalone external dev tooling (uv, ...)
# with no python3-*-native provider in vanilla Yocto/PetaLinux, so a DEPENDS on
# them breaks recipe resolution ("Nothing PROVIDES 'python3-uv-build-native'").
# Value: canonical (PEP 503) distribution name as it would appear in DEPENDS.
_EXTERNAL_BUILD_BACKENDS = frozenset({"uv-build"})


# Non-Python build tools that PEP 517 backends make pypa/build's
# ``--no-isolation`` dependency check demand: directly via their
# ``get_requires_for_build_*`` hook (meson-python appends ``ninja>=...`` when no
# ninja binary is on PATH; scikit-build-core appends cmake/ninja), or
# transitively via the backend's own Requires-Dist chain (meson-python's
# ``meson>=1.2.3`` on py>=3.12, resolved recursively by build 1.1's
# check_dependency). Bitbake does not stage a native dependency's own
# ``-native`` deps into the consuming recipe's sysroot, so the recipe must name
# the tools itself. Key: canonical (PEP 503) build-backend name as found in
# ``[build-system].build-backend``; value: literal OE-native packages to add to
# DEPENDS (all shipped by poky core for the supported series).
_BUILD_BACKEND_TOOL_DEPS: dict[str, str] = {
    "mesonpy": "meson-native ninja-native",
    "meson-python": "meson-native ninja-native",
    "scikit-build-core": "cmake-native ninja-native",
}


# Emitted into every recipe: PetaLinux's ``python_pep517_do_install`` (the
# install step all build classes route through: setuptools3, python_pep517,
# python_setuptools_build_meta, python_mesonpy) runs pypa installer with only
# ``--destdir=${D}`` and no ``--prefix``. Installer 0.7.0 then derives its
# scheme from the *native* python's sysconfig, whose base is the absolute
# recipe-sysroot-native path, and pathlib-joins that onto ${D} -- so the wheel
# payload lands under ``${D}/output/.../recipe-sysroot-native/...`` and
# do_package fails with "installed but not shipped" (xmod 1.10.0 under
# PetaLinux 2024.2). Pinning ``--prefix ${prefix}`` makes every scheme path
# relative to ${D} again (target /usr, native /usr, nativesdk /usr/local).
_INSTALL_PREFIX_OVERRIDE = """\
# Work around PetaLinux's python_pep517_do_install calling pypa installer
# without --prefix: the wheel payload would land under
# ${D}/output/.../recipe-sysroot-native/... and do_package would fail with
# "installed but not shipped". A --prefix keeps every path relative to ${D}.
python_pep517_do_install() {
    nativepython3 -m installer ${INSTALL_WHEEL_COMPILE_BYTECODE} --interpreter "${USRBINPATH}/env ${PEP517_INSTALL_PYTHON}" --prefix ${prefix} --destdir=${D} ${PEP517_WHEEL_PATH}/*.whl
}
"""


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


def _yocto_license_expr(expr: str) -> str:
    """Translate an SPDX license expression to Yocto's ``&|()`` operator syntax.

    PEP 639 expressions use ``AND``/``OR`` words; Yocto's license-format QA
    only accepts ``&`` (conjunction) / ``|`` (disjunction) operators. Word
    boundaries are space-delimited, so SPDX ids such as ``GPL-2.0-or-later``
    (hyphenated, lowercase) are never corrupted.
    """
    expr = expr.replace(" AND ", " & ")
    expr = expr.replace(" OR ", " | ")
    return expr


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


def _sdist_stem(source_filename: str) -> str:
    """The unpack directory a vendored sdist lands in, e.g.
    ``pyvizionsdk-26.9.1.tar.gz`` -> ``pyvizionsdk-26.9.1``.

    ``do_unpack`` extracts a file:// archive under ``${WORKDIR}`` keyed by the
    filename minus its archive extension, which is what ``S`` must name (the
    ``pypi`` class derives the same shape from ``PYPI_PACKAGE``/``PV``).
    """
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".zip"):
        if source_filename.endswith(ext):
            return source_filename[: -len(ext)]
    raise ValueError(
        f"cannot derive the unpack directory of {source_filename!r}: "
        "not a known sdist archive extension"
    )


# MD5 digests of the license texts the Yocto series ships in
# ``meta/files/common-licenses`` (scarthgap values). Fallback for
# ``LIC_FILES_CHKSUM`` when the sdist carries no license file: the diet of
# well-known SPDX ids points at the copy the target already ships, so
# ``do_populate_lic`` never fails with ``[license-checksum]``.
_COMMON_LICENSES: dict[str, str] = {
    "MIT": "0835ade698e0bcf8506ecda2f7b4f302",
    "Apache-2.0": "89aea4e17d99a7cacdbeed46a0096b10",
    "BSD-2-Clause": "cb641bc04cda31daea161b1bc15da69f",
    "BSD-3-Clause": "550794465ba0ec5312d6919e203a55f9",
    "ISC": "f3b90e78ea0cffb20bf5cca7947a896d",
    "GPL-2.0-only": "801f80980d171dd6425610833a22dbe6",
    "GPL-3.0-only": "c79ff39f19dfec6d293b95dea7b07891",
    "LGPL-2.1-only": "1a6d268fd218675ffea8be556788b780",
    "MPL-2.0": "815ca599c9df247a0c7f619bab123dad",
    "CC0-1.0": "0ceb3372c9595f0a8067e55da801e4a1",
    "Zlib": "87f239f408daca8a157858e192597633",
    "Python-2.0": "a5c8025e305fb49e6d405769358851f6",
    "PSF-2.0": "76c1502273262a5ebefb50dfb20d7c4f",
}


def _common_licenses_checksum(expr: str) -> str | None:
    """Space-joined ``file://${COMMON_LICENSE_DIR}/<id>;md5=<md5>`` entries.

    Returns ``None`` when the expression mentions an id Yocto does not ship,
    so the caller keeps the manual-``LIC_FILES_CHKSUM`` warning.
    """
    ids = [
        part
        for part in re.split(r"[&|()\s]+", _yocto_license_expr(expr.strip()))
        if part
    ]
    checks = {
        i: md5 for i in ids if (md5 := _COMMON_LICENSES.get(i)) is not None
    }
    if len(checks) != len(ids):
        return None
    return " ".join(
        f"file://${{COMMON_LICENSE_DIR}}/{i};md5={md5}"
        for i, md5 in checks.items()
    )


def pick_build_class(backend: str) -> tuple[str, bool]:
    """
    Return ``(build_class, needs_warning)`` for the detected PEP 517 backend.

    In Scarthgap, ``setuptools3`` runs a ``do_check_backend`` QA check that FAILS
    when a ``pyproject.toml`` declares a ``build-backend``. So:

    * no backend (setup.py / implicit)   -> ``setuptools3``
    * ``setuptools.build_meta`` (etc.)   -> ``python_setuptools_build_meta``
    * ``mesonpy`` / ``meson-python``     -> ``python_mesonpy`` (first-class OE
      class: meson cross build with the backend from the native sysroot)
    * external dev tooling (uv, ...)     -> ``python_setuptools_build_meta``
      (+ warning; the pyproject backend is rewritten in do_configure, see
      ``render_recipe``)
    * any other backend (hatchling, ...) -> ``python_pep517`` (+ warning)

    ``python_setuptools_build_meta`` and ``python_pep517`` both exist in
    Scarthgap and drive PEP 517 builds, with the backend package supplied via
    DEPENDS.
    """
    backend = (backend or "").lower()
    if not backend:
        return "setuptools3", False
    if canonical_name(backend) in ("mesonpy", "meson-python"):
        return "python_mesonpy", False
    if canonical_name(backend) in _EXTERNAL_BUILD_BACKENDS:
        return "python_setuptools_build_meta", True
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
    if build_class not in (
        "python_pep517",
        "python_setuptools_build_meta",
        "python_mesonpy",
    ):
        return False
    return any(_has_version_bound(d.requirement) for d in build_deps)


def _newer_minor_pattern(minor: int) -> str:
    """Two-digit regex for Python minor versions strictly greater than ``minor``.

    ``minor`` is the target Python's minor (e.g. 12 for ``3.12``); the pattern
    matches all 3.X with X numerically above it. Split per decade: within the
    target's decade only the higher second digits match (``1[3-9]`` for 12),
    every later decade matches freely (``[2-9][0-9]``).
    """
    hi, lo = divmod(minor, 10)
    if lo == 9:
        return rf"[{hi + 1}-9][0-9]"
    return rf"{hi}[{lo + 1}-9]|[{hi + 1}-9][0-9]"


def _classifier_drop_pattern(target_python: str) -> str | None:
    """sed program removing classifiers for Python releases newer than the target.

    The native sysroot's hatchling validates ``project.classifiers`` against
    its bundled trove list, and a classifier for a Python newer than the
    target (e.g. ``Python :: 3.14`` on a 3.12 image) aborts ``build_wheel``
    (xmod 1.10.0 under PetaLinux). The entries are metadata-only, so the
    recipe deletes them: whole matching lines (one-entry-per-line TOML style)
    and inline array entries (xmod's single-line style), dropping the entry
    together with one adjacent comma, plus the one-element-array edge.
    ``None`` for non-3.x targets, which have no versioned classifier scheme.
    """
    if not target_python.startswith("3."):
        return None
    minor = int(target_python.split(".", 1)[1])
    newer = _newer_minor_pattern(minor)
    q = '\\"'
    entry = (
        q + "Programming Language :: Python :: (3\\.(" + newer + ")|[4-9])" + q
    )
    return (
        f"/^[[:space:]]*Programming Language :: Python :: (3\\.({newer})|[4-9])/d; "
        f"s/{entry}[[:space:]]*,[[:space:]]*//g; "
        f"s/,[[:space:]]*{entry}//g; "
        f"s/\\[[[:space:]]*{entry}[[:space:]]*\\]/[]/g"
    )


# Build-backend / tool versions shipped in the *native sysroot* of each Yocto
# series, verified from the dist-info of a PetaLinux 2024.2 build under
# recipe-sysroot-native (poetry-core implied from its transitive deps). Key:
# canonical (PEP 503) distribution name as it appears in build-system requires.
_BUNDLED_TOOL_VERSIONS: dict[str, dict[str, str]] = {
    "5.0": {
        "build": "1.1.1",
        "cython": "3.0.8",
        "flit-core": "3.9.0",
        "hatchling": "1.21.1",
        "installer": "0.7.0",
        "meson": "1.3.1",
        "meson-python": "0.15.0",
        "ninja": "1.11.1",
        "packaging": "23.2",
        "poetry-core": "1.9.0",
        "pyproject-hooks": "1.0.0",
        "pyproject-metadata": "0.7.1",
        "setuptools": "69.1.1",
        "wheel": "0.42.0",
    },
}


def _minimum_version(specifier: str) -> Version | None:
    """Lowest version a PEP 508 specifier set demands, or ``None``.

    Only positive bounds are floors: ``>=`` / ``>`` / ``==`` and the ``~=``
    compatible-release prefix. ``<`` / ``<=`` / ``!=`` are ceilings or
    exclusions, and wildcard pins (``==3.1.*``) carry no usable floor.
    """
    try:
        spec = SpecifierSet(specifier)
    except InvalidSpecifier:
        return None
    floors: list[Version] = []
    for item in spec:
        if item.operator not in (">=", ">", "==", "~="):
            continue
        try:
            floors.append(Version(item.version))
        except InvalidVersion:
            continue  # wildcard segment -- not a floor
    return max(floors) if floors else None


def build_version_gaps(
    meta: PackageMetadata, yocto: YoctoVersion
) -> list[Dependency]:
    """Build deps the target series' native sysroot ships too old to satisfy.

    A build-system requires bound above the sysroot's copy (numpy 2.5.3 wants
    ``Cython>=3.1.0``; Scarthgap ships 3.0.8) would abort the PEP 517 build
    class' ``--no-isolation`` dependency check. The caller provisions a newer
    recipe of each gapped dep into the generated layer, so the layer builds
    the tool itself instead of failing the check.

    setuptools and wheel are exempt: the build classes always satisfy them
    with the sysroot copies (bounds are relaxed in ``render_recipe``), and
    provisioning newer ones would churn every recipe's build environment.
    """
    bundled = _BUNDLED_TOOL_VERSIONS.get(yocto.version, {})
    gaps: list[Dependency] = []
    for dep in meta.build_deps:
        name = canonical_name(dep.name)
        if name in ("setuptools", "wheel") or name not in bundled:
            continue
        needed = _minimum_version(dep.specifier)
        if needed is not None and needed > Version(bundled[name]):
            gaps.append(dep)
    return gaps


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
    external_backend = (
        canonical_name(meta.build_backend or "") in _EXTERNAL_BUILD_BACKENDS
    )

    if warn_backend:
        if external_backend:
            warnings.append(
                f"build-backend '{meta.build_backend}' is external dev tooling "
                f"(uv, ...) with no python3-*-native provider in the target Yocto; "
                f"the recipe excludes it from DEPENDS and rewrites [build-system] "
                f"to setuptools.build_meta in do_configure:append instead. "
                f"Pure-Python packages build this way; override with --build-class "
                f"if the package needs its original backend."
            )
        else:
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
        lines.append(f'LICENSE = "{_yocto_license_expr(_q(meta.license))}"')
        if meta.license_file and meta.license_md5:
            lines.append(
                f'LIC_FILES_CHKSUM = "file://{meta.license_file};md5={meta.license_md5}"'
            )
        elif fallback := _common_licenses_checksum(meta.license):
            # sdist ships no license file (e.g. runs 1.3.0); do_populate_lic
            # fails without LIC_FILES_CHKSUM, so point at the license text the
            # target ships in meta/files/common-licenses.
            lines.append(f'LIC_FILES_CHKSUM = "{fallback}"')
        else:
            warnings.append(
                "no license file found in the source tree; "
                "add LIC_FILES_CHKSUM manually (license is declared via metadata only)."
            )
    else:
        # PetaLinux errors on recipes without a LICENSE field; emit the Yocto
        # convention for an unknown/proprietary license so the build proceeds.
        lines.append('LICENSE = "CLOSED"')
        warnings.append(
            "no license detected; defaulted LICENSE to CLOSED; "
            "set LICENSE manually before building."
        )
    lines.append("")

    if meta.vendored:
        # S6: the sdist is not reachable at the pypi class' public URL (e.g.
        # pyvizionsdk, only on a private index). Ship it in the layer (file://
        # SRC_URI resolves through FILESPATH) and derive S from its archive
        # name; without inherit pypi there is no BPN-derived fetch URL or S.
        lines.append(f'SRC_URI = "file://{_q(meta.source_filename)}"')
        lines.append(
            f'S = "${{WORKDIR}}/{_q(_sdist_stem(meta.source_filename))}"'
        )
        warnings.append(
            f"{meta.canonical_name} {meta.version} is not reachable on public "
            "PyPI at the URL the pypi class fetches; the sdist is vendored "
            "into the layer (file:// SRC_URI) so do_fetch succeeds offline."
        )
    if meta.source_sha256:
        lines.append(f'SRC_URI[sha256sum] = "{meta.source_sha256}"')
    if meta.requires_python:
        # Record the upstream Python constraint (informational; bitbake enforces
        # its own Python version).
        lines.append(f"# Upstream requires Python {meta.requires_python}")
    lines.append("")

    if meta.vendored:
        lines.append(f"inherit {build_class}")
    else:
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
    # External dev tooling (uv-build) is excluded: it has no -native provider
    # in the target Yocto, and the do_configure rewrite below points the build
    # at the always-present setuptools.build_meta instead.
    extra_build = [
        d.name
        for d in meta.build_deps
        if d.name.lower() not in ("setuptools", "wheel")
        and canonical_name(d.name) not in _EXTERNAL_BUILD_BACKENDS
    ]
    tools = _BUILD_BACKEND_TOOL_DEPS.get(
        canonical_name(meta.build_backend or "")
    )
    deps = [
        f"{var}-native"
        for var in _bitbake_deps(extra_build, f"{yocto.python_pn}-")
    ]
    if tools:
        deps.append(tools)
    if deps:
        lines.append(f'DEPENDS += "{" ".join(deps)}"')
        lines.append("")

    if meta.runtime_deps:
        deps = " ".join(
            _bitbake_deps(
                [d.name for d in meta.runtime_deps], f"{yocto.python_pn}-"
            )
        )
        lines.append(f'RDEPENDS:${{PN}} += "{deps}"')
        lines.append("")

    sed_programs: list[str] = []
    if _needs_bounds_relax(build_class, meta.build_deps):
        warnings.append(
            "build-system.requires pins versions that may exceed the target "
            "Yocto's native setuptools/wheel; the recipe relaxes them in "
            "do_configure:append (the native sysroot versions are authoritative)."
        )
        # The spec class already excludes quotes, so the match stops before the
        # closing quote: no need to capture/re-emit it (which would unbalance
        # the line's raw quote counts for the structural check).
        sed_programs.append(
            r"/^[[:space:]]*\[build-system\]/,/^[[:space:]]*\[/ "
            r"s/([A-Za-z0-9_.]+)([[:space:]]*[<>=!~][^\"';]*[0-9][^\"';]*)/\1/g"
        )
    if external_backend:
        # The declared backend has no -native provider, so replace the whole
        # [build-system] section with the canonical setuptools pair: the
        # --no-isolation dependency check then only requires packages the
        # native sysroot ships. Backslash-escaped quotes keep the emitted sed
        # line shell-safe with even raw-quote counts for the structural check.
        sed_programs.append(
            r"/^[[:space:]]*\[build-system\]/,/^[[:space:]]*\[/ "
            r"{ "
            r"s/^[[:space:]]*requires[[:space:]]*=.*/requires = [\"setuptools\", \"wheel\"]/; "
            r"s/^[[:space:]]*build-backend[[:space:]]*=.*/build-backend = \"setuptools.build_meta\"/ "
            r"}"
        )
        # PEP 639 string licenses (``license = "MIT"``) are rejected by the
        # sysroot setuptools 69.x metadata validator, which only parses the
        # PEP 621 table form ``license = {text = ...}``; normalize it so the
        # wheel build survives metadata generation.
        sed_programs.append(
            r"/^[[:space:]]*\[project\]/,/^[[:space:]]*\[/ "
            r"s/^[[:space:]]*license[[:space:]]*=[[:space:]]*\"(.*)\"/license = {text = \"\1\"}/"
        )
    if build_class in (
        "python_pep517",
        "python_setuptools_build_meta",
        "python_mesonpy",
    ):
        # String licenses in any quote style (``license = "MIT"`` /
        # ``license = 'MIT'``) are rejected by the sysroot's flit_core 3.9.0
        # metadata validator (blessed 1.50.0 under PetaLinux) and setuptools'
        # PEP 639 check; normalize to the table form every backend accepts.
        # One program per quote style: the structural quote-balance check
        # counts raw ``"`` chars per line, and a single ``\"`` would leave an
        # odd count. No-ops when the field is absent or already a table; the
        # double-quote one duplicates the external-backend rewrite above.
        quoted_license = [
            (
                r"/^[[:space:]]*\[project\]/,/^[[:space:]]*\[/ "
                r"s/^[[:space:]]*license[[:space:]]*=[[:space:]]*\"(.*)\"/"
                r"license = {text = \"\1\"}/"
            ),
            (
                r"/^[[:space:]]*\[project\]/,/^[[:space:]]*\[/ "
                r"s/^[[:space:]]*license[[:space:]]*=[[:space:]]*'(.*)'/"
                r"license = {text = '\1'}/"
            ),
        ]
        if external_backend:
            quoted_license = quoted_license[1:]
        sed_programs.extend(quoted_license)
    if build_class == "python_pep517":
        classifier_drop = _classifier_drop_pattern(yocto.python_version)
        if classifier_drop:
            warnings.append(
                "project.classifiers claim Python versions newer than the "
                "target; the recipe drops those lines in do_configure:append "
                "(the native sysroot's hatchling rejects unknown classifiers)."
            )
            sed_programs.append(classifier_drop)
    if sed_programs:
        lines.append("do_configure:append() {")
        lines.append("    if [ -f ${S}/pyproject.toml ]; then")
        for program in sed_programs:
            lines.append(f'        sed -i -E "{program}" ${{S}}/pyproject.toml')
        lines.append("    fi")
        lines.append("}")
        lines.append("")

    # Every build class installs through python_pep517_do_install; override it
    # with a --prefix so pypa installer keeps its scheme relative to ${D}
    # (see _INSTALL_PREFIX_OVERRIDE).
    lines.append(_INSTALL_PREFIX_OVERRIDE.rstrip())
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
    *,
    vendor_source=None,
) -> tuple:
    """Write the recipe into ``out_dir``; return ``(path, warnings)``.

    A vendored recipe also copies its sdist into ``out_dir/files/`` (the
    ``file://`` SRC_URI resolves through FILESPATH); ``vendor_source`` is the
    downloaded artifact to copy. Its absence for a vendored recipe is an
    error: the emitted recipe would reference a file that does not exist.
    """
    from pathlib import Path

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pn = bitbake_pn(meta.name, yocto.python_pn)
    path = out_dir / recipe_filename(pn, meta.version)
    text, warnings = render_recipe(meta, yocto, build_class=build_class)
    path.write_text(text, encoding="utf-8")
    if meta.vendored:
        if vendor_source is None:
            raise ValueError(
                f"{meta.canonical_name} vendors its sdist; vendor_source path "
                "is required to copy it next to the recipe"
            )
        files_dir = out_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vendor_source, files_dir / meta.source_filename)
    return path, warnings
