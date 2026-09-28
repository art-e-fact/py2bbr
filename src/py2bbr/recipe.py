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

* PEP 517 bootstrap chain tools (``flit-core``, ``build``, ``installer``,
  ``pyproject-hooks``) render with poky's own bootstrap recipe shape
  (``inherit pypi python_flit_core`` + the loop-breaking
  ``DEPENDS:remove:class-native`` / manual-build lines) instead of the generic
  ``python_pep517`` shape. The generic class appends ``" python3-build-native
  python3-installer-native"`` to DEPENDS, while poky's build/installer/
  pyproject-hooks DEPEND (via ``python_flit_core``) on
  ``python3-flit-core-native``: provisioning a newer flit-core (e.g. ``3.12.0``
  for a build requirement of ``flit_core>=3.12``) shadows poky's ``3.9.0`` and
  re-closes that native dependency loop (PetaLinux reported hundreds of
  circular create-spdx dependencies). The bootstrap shape breaks it exactly as
  poky's own recipes do. An explicit ``--build-class`` override opts out.
"""

from __future__ import annotations

import re
import shutil
import textwrap

from packaging.markers import InvalidMarker, Marker, UndefinedEnvironmentName
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from py2bbr.model import (
    Dependency,
    DepKind,
    PackageMetadata,
    SourceKind,
    YoctoVersion,
    machine_override_tokens,
)


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
# DEPENDS (all shipped by poky core for the supported series). The mesonpy
# backend's ``get_requires_for_build_wheel`` appends ``patchelf>=0.11.0`` on
# every Linux wheel build even when the project's metadata omits it (meson-
# python 0.21.1's own pyproject does not), so patchelf-native is part of the
# backend tool deps, exactly as poky's meson-python recipe hardcodes it.
_BUILD_BACKEND_TOOL_DEPS: dict[str, str] = {
    "mesonpy": "meson-native ninja-native patchelf-native",
    "meson-python": "meson-native ninja-native patchelf-native",
    "scikit-build-core": "cmake-native ninja-native",
}


# Non-Python tool recipes poky core ships WITHOUT the ``python3-`` prefix:
# ``meson`` / ``meson-native`` (meta/recipes-devtools/meson), ``ninja``,
# ``cmake`` and ``patchelf``. A dependency on them must render as the bare
# name (``meson`` in RDEPENDS, ``meson-native`` in DEPENDS): the ``python3-``
# prefix yields ``python3-meson``, which the native-variant rename (from
# ``BBCLASSEXTEND = "native nativesdk"``) turns into the unprovidable
# ``python3-meson-native`` ("Nothing RPROVIDES", seen for the provisioned
# python3-meson-python_0.21.1.bb under PetaLinux). poky's own scarthgap
# python3-meson-python recipe renders exactly these bare names.
_OE_BARE_TOOL_PROVIDERS = frozenset({"meson", "ninja", "cmake", "patchelf"})


# Build classes that compile extension modules against the *target* toolchain.
# All of them inherit ``setuptools3-base``, which exports the target compiler
# and sysroot into the build environment and DEPENDS the target interpreter;
# ``python_mesonpy`` additionally runs meson with the target cross file. The
# generic ``python_pep517`` class does not - its PEP 517 frontend is driven by
# ``nativepython3`` (a build-host tool, which is correct: the frontend has to
# run on the machine doing the building) - so a package with compiled code that
# lands on it is cross-built only if its backend picks up the exported
# toolchain. See ``py2bbr.targets`` for the per-target report, which reuses
# these sets.
CROSS_CAPABLE_CLASSES = frozenset(
    {
        "python_flit_core",
        "python_mesonpy",
        "python_setuptools_build_meta",
        "setuptools3",
    }
)

#: Classes that run the PEP 517 frontend under the *host* interpreter. Usable
#: for pure-Python packages; for compiled code the outcome depends on the
#: backend honouring the exported target toolchain, so they earn a RISK.
HOST_ONLY_CLASSES = frozenset({"python_pep517"})


# PEP 517 bootstrap-tool recipes that must mirror poky's own bootstrap shape.
#
# These four tools are the native build chain python_pep517 recipes pull in:
# python_pep517.bbclass appends ``DEPENDS += " python3-build-native
# python3-installer-native"``, while poky's build/installer/pyproject-hooks all
# ``inherit python_flit_core``, whose class adds ``DEPENDS += "python3
# python3-flit-core-native"`` (scarthgap; walnascar drops the bare ``python3``).
# When py2bbr provisions a *newer* flit-core (e.g. ``3.12.0`` because a package
# demands ``flit_core>=3.12`` while Scarthgap ships ``3.9.0``), the provisioned
# recipe shadows poky's and closes the loop
# ``flit-core -> build/installer-native -> flit-core-native`` (the SPDX-task
# cycle do_create_spdx <-> do_collect_spdx_deps PetaLinux reported). Poky breaks
# the knot in its own recipes by inheriting ``python_flit_core`` and
# overriding the *class-native* compile/install to build by hand, removing the
# self-referential class-native DEPENDS. py2bbr mirrors those shapes verbatim
# (minus poky's ``PYPI_PACKAGE = "flit"`` tarball trick: the provisioned sdist
# is the standalone ``flit_core`` artifact, so the generic PYPI_PACKAGE logic
# already pins the underscore name).
#
# Key: canonical (PEP 503) distribution name; value: recipe lines emitted in
# place of the inherit-time DEPENDS block (the class provides
# ``python3-flit-core-native`` itself, so no extra_* DEPENDS is rendered).
_BOOTSTRAP_TOOL_SHAPES: dict[str, tuple[str, ...]] = {
    "flit-core": (
        (
            "# flit-core is the deepest knot of the PEP 517 bootstrap chain: "
            "poky's build/installer/pyproject-hooks all DEPEND on "
            "python3-flit-core-native (via python_flit_core.bbclass), while "
            "python_pep517.bbclass appends python3-build-native "
            "python3-installer-native. A newer provisioned flit-core would close "
            "the loop flit-core -> build/installer-native -> flit-core-native; "
            "mirror poky's own bootstrap recipe: build/install by hand and drop "
            "the class-native build/installer deps."
        ),
        'DEPENDS:remove:class-native = " python3-build-native python3-installer-native"',
        'DEPENDS:append:class-native = " unzip-native"',
        "",
        "do_compile:class-native () {",
        "    python_flit_core_do_manual_build",
        "}",
        "",
        "do_install:class-native () {",
        "    python_pep517_do_bootstrap_install",
        "}",
    ),
    "build": (
        (
            "# Bootstrap knot: build's class-native do_compile runs `python3 -m "
            "build --no-isolation` on this source tree, but must not depend on "
            "build (or its own deps); PYTHONPATH resolves the imported `build` "
            "module back to ${S}/src and --skip-dependency-check disables the "
            "recursive dependency demand. python3-flit-core-native comes from "
            "python_flit_core.bbclass."
        ),
        'DEPENDS += "python3-pyproject-hooks-native"',
        'DEPENDS:remove:class-native = "python3-build-native"',
        'PEP517_BUILD_OPTS:class-native = "--skip-dependency-check"',
        "",
        "do_compile:prepend:class-native() {",
        '    export PYTHONPATH="${S}/src"',
        "}",
    ),
    "installer": (
        (
            "# Bootstrap knot: installer's class-native do_compile is the manual "
            "flit_core wheel build (building the installer wheel must not require "
            "pypa/build), and the un-overridden class-native do_install runs "
            "`python3 -m installer` with PYTHONPATH pointing at this source tree. "
            "python3-flit-core-native comes from python_flit_core.bbclass."
        ),
        'DEPENDS:remove:class-native = "python3-build-native python3-installer-native"',
        'INSTALL_WHEEL_COMPILE_BYTECODE:class-native = "--no-compile-bytecode"',
        "",
        "do_compile:class-native () {",
        "    python_flit_core_do_manual_build",
        "}",
        "",
        "do_install:prepend:class-native() {",
        '    export PYTHONPATH="${S}/src"',
        "}",
    ),
    "pyproject-hooks": (
        (
            "# Bootstrap knot: pyproject-hooks' class-native do_compile is the "
            "manual flit_core wheel build (building it must not require pypa/"
            "build); it keeps the installer-native DEPENDS for its un-overridden "
            "class-native do_install. python3-flit-core-native comes from "
            "python_flit_core.bbclass."
        ),
        'DEPENDS:remove:class-native = "python3-build-native"',
        "",
        "do_compile:class-native () {",
        "    python_flit_core_do_manual_build",
        "}",
    ),
}


#: Post-install fixups that poky's *own* recipes for a build tool apply, which a
#: py2bbr-generated recipe has to mirror. Bitbake keys tasks by recipe name, so
#: provisioning a newer tool *replaces* the distro's recipe wholesale: any
#: adaptation that recipe performed is silently lost, and the consumers relying
#: on it stop resolving.
#:
#: cython: poky's ``python3-cython`` renames its console scripts to the
#: versioned names (``cython`` -> ``cython3``) because the unversioned ones
#: collide with a Python 2 build of Cython. That rename is also what consumers
#: look for: meson resolves its ``cython`` compiler by running ``cython3 -V``
#: and nothing else (meson 1.11's ``defaults['cython'] = ['cython3']``), so a
#: provisioned Cython that keeps upstream's script names leaves the compiler
#: unfindable and numpy 2.5.3's do_compile aborts with ``meson.build:1:0: ERROR:
#: Unknown compiler(s): [['cython3']]``. Mirroring poky keeps the generated
#: recipe a drop-in replacement; the guards keep a future Cython that drops a
#: script from failing the build.
#:
#: Key: canonical (PEP 503) distribution name; value: recipe lines appended.
_TOOL_INSTALL_ADAPTATIONS: dict[str, tuple[str, ...]] = {
    "cython": (
        "do_install:append() {",
        "    # Mirror poky's python3-cython: the unversioned script names collide",
        "    # with a Python 2 build of Cython, and meson resolves the cython",
        "    # compiler by running `cython3 -V`.",
        '    if [ -x "${D}${bindir}/cython" ]; then',
        "        mv ${D}${bindir}/cython ${D}${bindir}/cython3",
        "    fi",
        '    if [ -x "${D}${bindir}/cythonize" ]; then',
        "        mv ${D}${bindir}/cythonize ${D}${bindir}/cythonize3",
        "    fi",
        '    if [ -x "${D}${bindir}/cygdb" ]; then',
        "        mv ${D}${bindir}/cygdb ${D}${bindir}/cygdb3",
        "    fi",
        "}",
    ),
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


#: ``sed -E`` programs dropping ``project.license-files`` from a pyproject.toml.
#:
#: PEP 639 only allows that key next to an SPDX *string* expression, so it
#: cannot stay behind once the license is rewritten to the legacy table form
#: (:data:`_INSTALL_PREFIX_OVERRIDE` describes the sibling workaround for the
#: same reason). Enforced by meson-python, which aborts with ``"project
#: .license-files" must not be used when "project.license" is not a SPDX license
#: expression``. Two programs, order significant: the first drops a value whose
#: array closes on the same line, the second an array that opens on one line and
#: closes on a later one. Scoped to ``[project]`` so another table's identically
#: named key is left alone.
_LICENSE_FILES_DROP = (
    (
        r"/^[[:space:]]*\[project\]/,/^[[:space:]]*\[/ "
        r"{ /^[[:space:]]*license-files[[:space:]]*=.*\][[:space:]]*$/d }"
    ),
    (
        r"/^[[:space:]]*\[project\]/,/^[[:space:]]*\[/ "
        r"{ /^[[:space:]]*license-files[[:space:]]*=[[:space:]]*\[/,/\]/d }"
    ),
)


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


def _comment_block(text: str, width: int = 74) -> list[str]:
    """Render ``text`` as a wrapped ``# ...`` BitBake comment block.

    Comments are the only multi-line construct the recipe may carry (the
    structural checks reject continuations), and a hand-wrapped comment block
    is unreadable the moment the text changes: the words drift across the
    fragments. Wrapping one logical string keeps the sentence intact.
    """
    return [f"# {line}" for line in textwrap.wrap(text, width=width)]


def _bitbake_deps(names: list[str], prefix: str) -> list[str]:
    """Render dependency names as ``<prefix><canonical name>``, deduplicated.

    ``prefix`` is the literal package prefix (e.g. ``python3-``) — never a
    ``${...}`` reference, see the module docstring. Non-Python tool recipes
    poky ships bare (``meson``/``ninja``/``cmake``/``patchelf``) render
    unprefixed so the native-variant rename still resolves (``meson`` ->
    ``meson-native``), matching poky's own recipes.
    """
    seen: dict[str, str] = {}
    for n in names:
        canonical = canonical_name(n)
        var = (
            canonical
            if canonical in _OE_BARE_TOOL_PROVIDERS
            else f"{prefix}{canonical}"
        )
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

    Every class named here except ``python_pep517`` inherits ``setuptools3-base``
    and therefore exports the *target* toolchain (``CC``/``LDSHARED``,
    ``STAGING_INCDIR``/``STAGING_LIBDIR``, target ``python3`` on the sysroot),
    so a package with C extensions is cross-compiled for the image's CPU. That
    is why the class choice is also a cross-build decision; the generic
    ``python_pep517`` runs the PEP 517 frontend under ``nativepython3`` instead,
    which is fine for pure Python and conditional for compiled code (the backend
    has to pick up the exported toolchain), so :func:`render_recipe` warns when
    the sdist ships native code.
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


# Native-sysroot packages a *provisioned* build tool needs at a version newer
# than the series ships, beyond what its own ``[build-system].requires`` states.
#
# ``build_version_gaps`` reads the floors a *consumer* declares, which is blind
# to a companion that a build tool imports at runtime without pinning it. The
# concrete case: meson-python 0.18.0 onwards overrides
# ``StandardMetadata.from_pyproject`` and forwards a third argument,
# ``metadata_version``. ``pyproject-metadata`` only accepts that argument from
# 0.8.0 (0.7.1, the copy Scarthgap's native sysroot ships, has the two-argument
# form), and meson-python lists ``pyproject-metadata`` **unversioned** in its
# ``requires``, so no floor is visible to derive. Provisioning meson-python
# without its companion therefore emitted a recipe that dies in ``do_compile``
# with ``TypeError: StandardMetadata.from_pyproject() takes from 2 to 3
# positional arguments but 4 were given``.
#
# Key: canonical name of the provisioned tool; value: ``(from_version,
# requirement)`` pairs - the companion requirement that applies from that
# version of the tool onwards. The last matching entry wins, so a tool can
# tighten its companion over time by listing successive versions.
_TOOL_COMPANION_REQUIREMENTS: dict[str, tuple[tuple[Version, str], ...]] = {
    "meson-python": ((Version("0.18.0"), "pyproject-metadata>=0.8.0"),),
}


def _requirement_to_dependency(requirement: str) -> Dependency | None:
    """Parse a PEP 508 requirement into a BUILD :class:`Dependency`."""
    try:
        req = Requirement(requirement)
    except InvalidRequirement:
        return None
    return Dependency(
        name=req.name,
        kind=DepKind.BUILD,
        requirement=requirement,
        specifier=str(req.specifier),
    )


def tool_companion_gaps(
    meta: PackageMetadata, yocto: YoctoVersion
) -> list[Dependency]:
    """Native-sysroot companions too old for a build tool py2bbr provisions.

    :func:`build_version_gaps` answers "does this *consumer* demand a build tool
    newer than the sysroot ships?". This answers the next question: once py2bbr
    provisions that tool, are the native packages it *imports* too old to run?
    The answer comes from :data:`_TOOL_COMPANION_REQUIREMENTS` rather than from
    the tool's own metadata, because the tools that break this way declare the
    companion unversioned (see that table's comment). A companion the sysroot
    does not ship at all counts as a gap: provisioning it is the only way to
    make the tool's recipe build.
    """
    bundled = _BUNDLED_TOOL_VERSIONS.get(yocto.version, {})
    try:
        version = Version(meta.version)
    except InvalidVersion:
        return []
    gaps: list[Dependency] = []
    for since, requirement in _TOOL_COMPANION_REQUIREMENTS.get(
        meta.canonical_name, ()
    ):
        if version < since:
            continue
        dep = _requirement_to_dependency(requirement)
        if dep is None:
            continue
        needed = _minimum_version(dep.specifier)
        have = bundled.get(canonical_name(dep.name))
        if needed is None:
            continue
        if have is None or needed > Version(have):
            gaps.append(dep)
    return gaps


def sysroot_tool_version(yocto: YoctoVersion, name: str) -> str | None:
    """Version of a build tool the target series' native sysroot ships.

    ``None`` for a tool no series table lists. The caller uses it to tell a
    provisioning that *adds* a provider (the distro has no recipe) from one that
    *shadows* an existing ``RPROVIDER`` (the distro ships one): tasks are keyed
    by recipe name, not version, so the swap invalidates the sstate manifests
    the previous version left behind, and a signature-locked cache
    (``SIGGEN_LOCKEDSIGS``) keeps the old setscene stamps, so bitbake never
    rewrites them. The build then fails in an unrelated recipe with "The sstate
    manifest for task '<recipe>-native:populate_sysroot' could not be found".
    """
    return _BUNDLED_TOOL_VERSIONS.get(yocto.version, {}).get(
        canonical_name(name)
    )


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


def _applies_to_target(dep: Dependency, yocto: YoctoVersion) -> bool:
    """Whether a ``[build-system].requires`` entry can apply on the target.

    A pin guarded by an environment marker is not a floor the build has to
    satisfy: PEP 518 has the *build frontend* evaluate markers against the build
    environment, and that environment is the native sysroot - Linux, on the
    series' own Python. meson-python 0.22.0 pins
    ``packaging>=24.2; sys_platform == "ios"`` next to
    ``packaging>=23.2; sys_platform != "ios"``; treating the iOS pin as a floor
    provisions a recipe for a platform the target never runs. A dependency that
    cannot apply is skipped, exactly as :func:`machine_gated_deps` leaves the
    real answer to bitbake.
    """
    marker = dep.marker
    if not marker:
        return True
    # Imported here: py2bbr.metadata imports py2bbr.targets, which imports this
    # module, so a module-level import would be circular.
    from py2bbr.metadata import marker_env

    try:
        return bool(Marker(marker).evaluate(marker_env(yocto)))
    except (InvalidMarker, UndefinedEnvironmentName):
        # An unresolvable marker must not silently drop a real floor: treat the
        # entry as unconditional and let the build report it.
        return True


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

    The four PEP 517 bootstrap tools (flit-core, build, installer,
    pyproject-hooks) are exempt too: poky ships them at signature-locked
    versions, consumers' ``[build-system]`` bounds are relaxed in
    ``render_recipe``, and provisioning a newer copy shadows poky's own locked
    recipe - PetaLinux then reports SIGGEN_LOCKEDSIGS mismatch warnings for
    the native variant (blessed 1.50.0's ``flit_core>=3.11,<4`` vs Scarthgap's
    3.9.0). The sysroot copies are authoritative for the build.

    Marker-gated entries are skipped when they cannot apply on the target (see
    :func:`_applies_to_target`).
    """
    bundled = _BUNDLED_TOOL_VERSIONS.get(yocto.version, {})
    gaps: list[Dependency] = []
    for dep in meta.build_deps:
        name = canonical_name(dep.name)
        if (
            name in ("setuptools", "wheel")
            or name in _BOOTSTRAP_TOOL_SHAPES
            or name not in bundled
            or not _applies_to_target(dep, yocto)
        ):
            continue
        needed = _minimum_version(dep.specifier)
        if needed is not None and needed > Version(bundled[name]):
            gaps.append(dep)
    return gaps


def effective_build_class(
    meta: PackageMetadata, build_class: str | None = None
) -> str:
    """The class the recipe inherits: the override, the bootstrap knot shape,
    or the one detected from the declared backend.

    Shared with ``py2bbr.targets`` so the build-for-target report judges the
    class that is actually rendered, not a re-derived guess.
    """
    if build_class:
        return build_class
    if meta.canonical_name in _BOOTSTRAP_TOOL_SHAPES:
        return "python_flit_core"
    return pick_build_class(meta.build_backend)[0]


def _wheel_license_unpack(license_file: str) -> str:
    """Recipe lines exposing ``license_file`` from inside the vendored wheel.

    ``do_populate_lic`` searches ``${S}`` - which ``find_license_files`` derives
    from ``LIC_FILES_CHKSUM`` - and a wheel recipe has no source tree, so the
    license text only exists *inside* the vendored zip. Unpacking that one
    member first lets the standard checksum check run unchanged. ``unzip``'s
    own pattern matching covers both PEP 639
    (``<dist-info>/licenses/<name>``) and the older flat ``<dist-info>/<name>``
    layout. Only emitted for a license the target does not already ship (see
    :func:`_recipe_header`).

    A function rather than a template because bitbake's ``${VAR}`` syntax is
    not ``str.format``'s placeholder syntax.
    """
    return (
        "# The license text lives inside the vendored wheel: do_populate_lic\n"
        "# searches ${S} and a wheel recipe has no source tree to search.\n"
        "# Unpack that one member so the standard checksum check finds it.\n"
        'do_populate_lic[prefuncs] += "unpack_wheel_license"\n'
        "\n"
        "unpack_wheel_license() {\n"
        "    install -d ${S}\n"
        "    unzip -jo ${WORKDIR}/*.whl "
        f'"*dist-info/licenses/{license_file}" "*dist-info/{license_file}" '
        "-d ${S}\n"
        "}\n"
    )


def _recipe_header(
    meta: PackageMetadata, warnings: list[str], *, vendored_wheel: bool = False
) -> list[str]:
    """The recipe's leading metadata block, shared by every render shape.

    ``do_populate_lic`` needs ``LIC_FILES_CHKSUM`` and PetaLinux rejects a
    recipe without ``LICENSE``, so both are always emitted. A vendored wheel
    is the awkward case: it has no source tree, so a path into the artifact
    cannot be checksummed. The license text the *target* already ships (the
    common-licenses fallback) is preferred, since that resolves on any Yocto;
    the in-wheel path is only used for an SPDX id outside that table, and then
    :func:`_wheel_license_unpack` has to make it resolvable.
    """
    wheel_license: str | None = None
    if vendored_wheel and not _common_licenses_checksum(meta.license or ""):
        wheel_license = meta.license_file or None
        if not wheel_license:
            warnings.append(
                f"{meta.canonical_name} {meta.version} publishes a wheel whose "
                "license is not a known SPDX id, so do_populate_lic has no file "
                "to checksum; add LIC_FILES_CHKSUM manually."
            )
    lines = [f'SUMMARY = "{_q(meta.summary or meta.name)}"']
    if meta.description and meta.description != meta.summary:
        lines.append(f'DESCRIPTION = "{_q(meta.description)}"')
    if meta.homepage:
        lines.append(f'HOMEPAGE = "{_q(meta.homepage)}"')
    if meta.license:
        lines.append(f'LICENSE = "{_yocto_license_expr(_q(meta.license))}"')
        fallback = _common_licenses_checksum(meta.license)
        if vendored_wheel and (fallback or not wheel_license):
            # Prefer the common-license copy: it resolves on every Yocto and
            # needs no ${S} content. Point into the wheel only when there is
            # no such copy (see _wheel_license_unpack).
            if fallback:
                lines.append(f'LIC_FILES_CHKSUM = "{fallback}"')
            else:
                warnings.append(
                    f"{meta.canonical_name} {meta.version}: no license file to "
                    "checksum; add LIC_FILES_CHKSUM manually."
                )
        elif meta.license_file and meta.license_md5:
            lines.append(
                f'LIC_FILES_CHKSUM = "file://{meta.license_file};md5={meta.license_md5}"'
            )
        elif fallback:
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
    if wheel_license:
        lines.append(_wheel_license_unpack(wheel_license).rstrip())
        lines.append("")
    return lines


#: ``do_install`` for a recipe whose only artifact is a wheel.
#:
#: A wheel is upstream's own build product, not source: there is nothing to
#: unpack, configure or compile, and no build backend to drive. The recipe
#: installs the wheel itself, with pypa installer - the same tool
#: :func:`python_pep517_do_install` uses, so the payload, the console scripts
#: and the ``.dist-info`` land exactly where a built-from-source recipe puts
#: them. The emitted comment explains each variable; the two that are easy to get
#: wrong are ``nativepython3`` (not ``${PYTHON}``, which in a poky target recipe
#: is the *native* interpreter, and whose target counterpart is a target-arch
#: binary that cannot run on the build host) and the class-conditional
#: ``WHEEL_INSTALL_PYTHON`` (the shebang has to name the interpreter that will
#: actually run the script).
_WHEEL_INSTALL = """\
# The interpreter recorded in the console scripts' shebang. A target wheel
# records the target's own python3; the -native/-nativesdk variants record the
# build host's, because that is what will run them. Same split the pypi class
# makes through PEP517_INSTALL_PYTHON.
WHEEL_INSTALL_PYTHON = "python3"
WHEEL_INSTALL_PYTHON:class-native = "nativepython3"
WHEEL_INSTALL_PYTHON:class-nativesdk = "nativepython3"

# Upstream publishes no source distribution for this package, only a wheel, so
# there is nothing to build: the recipe installs the artifact as shipped, with
# pypa installer - the same tool python_pep517_do_install uses, so the payload,
# the console scripts and the .dist-info land where a built recipe puts them.
# nativepython3 runs the installer because the target interpreter is a
# target-arch binary that cannot execute on the build host; the wheel's own
# prebuilt extension modules are simply unpacked, not loaded. --prefix for the
# same reason as every other generated recipe (see _INSTALL_PREFIX_OVERRIDE):
# installer derives its scheme from the running interpreter's sysconfig, whose
# base is an absolute sysroot path it would otherwise join onto ${D}.
do_install() {
    nativepython3 -m installer --interpreter "${USRBINPATH}/env ${WHEEL_INSTALL_PYTHON}" --prefix ${prefix} --destdir=${D} ${WORKDIR}/*.whl
}
"""


def _render_wheel_recipe(
    meta: PackageMetadata, yocto: YoctoVersion
) -> tuple[str, list[str]]:
    """Render a recipe that installs a vendored wheel.

    Chosen when the index offers no sdist and the wheel's Python tag serves the
    target (:func:`py2bbr.metadata._wheel_rejection_reason` rejects the rest).
    None of the build machinery applies - no ``inherit pypi`` (its fetch URL
    would request a ``.tar.gz`` that does not exist, or a different artifact
    than the recorded sha256), no ``S``/``do_unpack``, no build backend, no
    build-time ``-native`` deps - so this is a separate shape rather than a
    branch through :func:`render_recipe`.
    """
    warnings: list[str] = []
    lines = _recipe_header(meta, warnings, vendored_wheel=True)
    lines.append(f'SRC_URI = "file://{_q(meta.source_filename)}"')
    if meta.source_sha256:
        lines.append(f'SRC_URI[sha256sum] = "{meta.source_sha256}"')
    if meta.requires_python:
        lines.append(f"# Upstream requires Python {meta.requires_python}")
    lines.append("")

    # ``inherit pypi`` is deliberately absent: its fetch URL is derived from
    # BPN/PV and always points at a .tar.gz, which is not this artifact.
    # ``setuptools3-base`` supplies PYTHON_SITEPACKAGES_DIR and the FILES
    # default that keeps do_package from reporting "installed but not shipped".
    lines.append("inherit python3targetconfig setuptools3-base")
    lines.append("")
    install_deps = ["python3-installer-native"]
    if "unpack_wheel_license()" in "\n".join(lines):
        # _wheel_license_unpack shells out to unzip, which is only on the build
        # host through unzip-native.
        install_deps.append("unzip-native")
    lines.append(f'DEPENDS += "{" ".join(install_deps)}"')
    lines.append("")
    if meta.runtime_deps:
        deps = " ".join(
            _bitbake_deps(
                [d.name for d in meta.runtime_deps], f"{yocto.python_pn}-"
            )
        )
        lines.append(f'RDEPENDS:${{PN}} += "{deps}"')
        lines.append("")
    if meta.machine_deps:
        lines.append(
            "# Machine-gated dependencies (Requires-Dist platform_machine "
            "markers):"
        )
        by_token: dict[str, list[str]] = {}
        for dep in meta.machine_deps:
            for machine in dep.machines:
                for token in machine_override_tokens(machine):
                    by_token.setdefault(token, []).append(dep.name)
        for token, names in by_token.items():
            deps = " ".join(_bitbake_deps(names, f"{yocto.python_pn}-"))
            lines.append(f'RDEPENDS:${{PN}}:append:{token} += "{deps}"')
        lines.append("")

    lines.append(_WHEEL_INSTALL.rstrip())
    lines.append("")
    lines.append('BBCLASSEXTEND = "native nativesdk"')
    lines.append("")
    lines.append(
        f"# Generated by py2bbr for {meta.canonical_name} {meta.version}"
    )
    lines.append(
        f"# Target: {yocto.label} | source: {meta.source_filename} "
        f"({meta.source_kind})"
    )
    lines.append(f"# Runtime: {yocto.runtime_label}")
    if meta.dropped_deps:
        lines.append(
            "# Dropped dependencies (optional extras / non-target markers):"
        )
        for dep in meta.dropped_deps:
            reason = f" - {dep.reason}" if dep.reason else ""
            lines.append(f"#   {dep.name}{reason}")
    lines.append("")

    warnings.append(
        f"{meta.canonical_name} {meta.version} publishes no source "
        "distribution; the recipe vendors the wheel from the layer and installs "
        "it with pypa installer instead of building it. A wheel carries "
        "prebuilt extension modules for one exact Python ABI and CPU, so this "
        f"only works while the target stays {yocto.runtime_label} and the "
        "wheel's tags match; publish an sdist if the target ever moves."
    )
    return "\n".join(lines), warnings


def render_recipe(
    meta: PackageMetadata,
    yocto: YoctoVersion,
    build_class: str | None = None,
) -> tuple[str, list[str]]:
    """
    Render the ``.bb`` content.

    Returns ``(recipe_text, warnings)``.

    A vendored wheel takes a separate shape (:func:`_render_wheel_recipe`): the
    artifact is a build product, so there is no source to drive a PEP 517
    backend and none of the class/DEPENDS machinery below applies.
    """
    if meta.vendored and meta.source_kind is SourceKind.WHEEL:
        text, warnings = _render_wheel_recipe(meta, yocto)
        if build_class is not None:
            # The override names a class that would build the package, and
            # there is nothing to build. Say so rather than emitting a recipe
            # that ignores the request (or crashing trying to find an unpack
            # directory in a zip).
            warnings.append(
                f"build class {build_class!r} was requested for "
                f"{meta.canonical_name} {meta.version}, but the only artifact "
                "is a wheel: there is no source to build, so the recipe "
                "installs the wheel as shipped. Build an sdist to use a build "
                "class."
            )
        return text, warnings

    warnings: list[str] = []

    warn_backend = pick_build_class(meta.build_backend)[1]
    bootstrap_lines = (
        None
        if build_class is not None
        else _BOOTSTRAP_TOOL_SHAPES.get(meta.canonical_name)
    )
    build_class = effective_build_class(meta, build_class)
    if bootstrap_lines is not None:
        # PEP 517 bootstrap knot: inherit poky's python_flit_core class, whose
        # class-native overrides build/install these tools by hand (a generic
        # python_pep517 recipe would re-close the native dependency loop).
        warn_backend = False
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

    # Cross-build note. Compiled packages must be built for the *target* CPU
    # while the layer is built by the (usually x86_64) build host, so record
    # which mechanism gets the target toolchain into the build: the class
    # inheriting setuptools3-base (target CC/LDSHARED + target STAGING_* +
    # target python3 on the sysroot), meson's cross file, or - for the generic
    # PEP 517 class - the backend honouring that exported environment.
    cross_lines: list[str] = []
    qa_lines: list[str] = []
    if meta.has_native_code:
        # A package that compiles C sources often bundles the intermediate
        # static libraries its own build produced into the wheel, because the
        # PEP 517 classes install whatever the backend emitted (pypa installer
        # has no notion of split packages). Yocto's package_qa then fails
        # do_package_qa with "non -staticdev package contains static .a
        # library" (numpy 2.5.3 ships libnpymath.a / libnpyrandom.a under
        # site-packages). poky's own recipe dodges it with a hand-written
        # FILES:${PN}-staticdev listing those exact paths; a generated recipe
        # cannot know them, since they are an artefact of the build rather than
        # of the metadata. Skip the check instead of guessing the paths.
        qa_lines.append('INSANE_SKIP:${PN} += "staticdev"')
        evidence = ", ".join(meta.native_code_evidence[:2]) or "native code"
        if build_class == "python_mesonpy":
            note = (
                f"Cross-build: this sdist ships compiled code ({evidence}). "
                f"{build_class} runs meson with the target cross file, so the "
                "extension modules are compiled for the target CPU even though "
                "the layer is built on the (usually x86_64) build host. The "
                "pypa installer step runs natively and is architecture "
                "independent."
            )
        elif build_class in CROSS_CAPABLE_CLASSES:
            note = (
                f"Cross-build: this sdist ships compiled code ({evidence}). "
                f"{build_class} inherits setuptools3-base, which exports the "
                "target toolchain (CC/LDSHARED, STAGING_INCDIR/STAGING_LIBDIR) "
                "and DEPENDS the target python3, so extension modules are "
                "compiled for the target CPU. Build tools stay -native (host "
                "binaries) because the PEP 517 frontend has to run on the "
                "build host."
            )
        else:
            note = (
                f"Cross-build: this sdist ships compiled code ({evidence}), but "
                f"the generic {build_class} class drives the PEP 517 frontend "
                "with nativepython3. It still cross-builds when the backend "
                "uses the toolchain setuptools3-base exports (CC/LDSHARED, "
                "STAGING_*); a backend that resolves its own compiler would "
                "produce build-host binaries. Verify the installed .so files "
                "on the target."
            )
            warnings.append(
                f"{meta.canonical_name} ships compiled code "
                f"({', '.join(meta.native_code_evidence[:2])}) and is built with "
                f"'{build_class}', whose PEP 517 frontend runs under "
                f"nativepython3. The recipe pairs it with setuptools3-base, which "
                f"exports the target toolchain, so the build cross-compiles as long "
                f"as the backend uses that environment; confirm the target .so files "
                f"if the build fails to link."
            )
        cross_lines = _comment_block(note)
        if meta.host_assumptions:
            warnings.append(
                "build config passes x86-specific CPU/ISA flags ("
                + ", ".join(meta.host_assumptions[:2])
                + "): the compile for ARM targets (aarch64 / armv7a) may fail "
                "or silently lose an optimization; check the build log on the "
                "target."
            )

    lines: list[str] = _recipe_header(meta, warnings)

    if meta.vendored:
        # S6: the sdist is not reachable at the pypi class' public URL (e.g.
        # a package only on a private index). Ship it in the layer (file://
        # SRC_URI resolves through FILESPATH) and derive S from its archive
        # name; without inherit pypi there is no BPN-derived fetch URL or S.
        # (A vendored *wheel* never reaches here - it renders as
        # _render_wheel_recipe, which has no S at all.)
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

    # The generic python_pep517 class inherits nothing on its own (poky's
    # wrapper classes python_flit_core / python_setuptools_build_meta /
    # python_mesonpy / setuptools3 all inherit setuptools3-base instead), so
    # pair it with setuptools3-base: that class defines PYTHON_SITEPACKAGES_DIR
    # (via python3native <- python3-dir) and ships the install payload with
    # FILES:${PN} += "${PYTHON_SITEPACKAGES_DIR}". Without it do_package fails
    # with "installed but not shipped" for every site-packages file (xmod /
    # inquirer / blessed / editor under PetaLinux).
    inherit_classes = (
        "python_pep517 setuptools3-base"
        if build_class == "python_pep517"
        else build_class
    )
    if meta.vendored:
        lines.append(f"inherit {inherit_classes}")
    else:
        lines.append(f"inherit pypi {inherit_classes}")
        pypi_name = _pypi_package_name(meta.source_filename, meta.version)
        if pypi_name and pypi_name != meta.canonical_name:
            # The pypi class derives PYPI_PACKAGE from BPN (e.g. ``async-pyserial``),
            # but PyPI publishes the sdist under the real package name (often
            # ``async_pyserial``). Pinning the real name keeps both the fetch URL
            # and ``S = ${WORKDIR}/${PYPI_PACKAGE}-${PV}`` on the actual artifact.
            lines.append(f'PYPI_PACKAGE = "{pypi_name}"')
    lines.append("")

    if cross_lines:
        lines.extend(cross_lines)
        lines.append("")

    if qa_lines:
        lines.extend(qa_lines)
        lines.append("")

    # Build-time deps: the build class already provides setuptools + wheel as
    # ``-native``. Add any *extra* PEP 517 build requirements as ``-native``
    # variants (e.g. python3-setuptools-scm-native, python3-hatchling-native).
    # External dev tooling (uv-build) is excluded: it has no -native provider
    # in the target Yocto, and the do_configure rewrite below points the build
    # at the always-present setuptools.build_meta instead.
    #
    # Bootstrap knots (flit-core/build/installer/pyproject-hooks) instead emit
    # poky's own loop-breaking lines verbatim: the python_flit_core class
    # provides python3-flit-core-native itself and the class-native variants
    # build/install by hand, so no extra_* DEPENDS is rendered at all.
    if bootstrap_lines is not None:
        lines.extend(bootstrap_lines)
        lines.append("")
    else:
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
            deps.extend(tools.split())
        # A bare-tool build dep (e.g. meson) already maps to meson-native, so
        # it can duplicate the backend's tool deps; keep each name once.
        deps = list(dict.fromkeys(deps))
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

    # Machine-gated runtime deps (``foo; platform_machine == "aarch64"``) must
    # be selected by bitbake on the real target, not on the host that runs
    # bitbake: baking the build host's answer in would ship the wrong
    # dependency set for a cross build. The override suffix is a token poky's
    # ``meta/conf/machine/include/*/arch-*.inc`` files add to MACHINEOVERRIDES
    # (``x86``, ``x86_64``, ``armv7a``, ``aarch64``, ``riscv64``, ...), so the
    # same recipe stays correct on every machine of the family.
    if meta.machine_deps:
        lines.append(
            "# Machine-gated dependencies (Requires-Dist platform_machine "
            "markers):"
        )
        by_token: dict[str, list[str]] = {}
        for dep in meta.machine_deps:
            for machine in dep.machines:
                for token in machine_override_tokens(machine):
                    by_token.setdefault(token, []).append(dep.name)
        for token, names in by_token.items():
            deps = " ".join(_bitbake_deps(names, f"{yocto.python_pn}-"))
            lines.append(f'RDEPENDS:${{PN}}:append:{token} += "{deps}"')
        lines.append("")

    sed_programs: list[str] = []
    # Set when a PEP 639 string license is rewritten to the legacy table form,
    # which forces ``project.license-files`` out (see _LICENSE_FILES_DROP).
    license_rewritten = False
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
        license_rewritten = True
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
        license_rewritten = bool(quoted_license)
    if license_rewritten:
        # PEP 639 makes ``project.license-files`` valid *only* alongside an SPDX
        # string expression, so the table form and that key cannot coexist.
        # Leaving the key behind aborts the build: meson-python 0.22.0 under
        # PetaLinux failed its do_compile with ``"project.license-files" must not
        # be used when "project.license" is not a SPDX license expression``.
        # Drop the key rather than keep the license: the license text still
        # reaches the image through LIC_FILES_CHKSUM. Two programs, run in
        # order - a value closed on its own line, then an array opened on one
        # line and closed on another (the first program has already removed the
        # single-line form, so the range cannot swallow a following key).
        sed_programs.extend(_LICENSE_FILES_DROP)
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

    # Fixups the distro's own recipe for this tool applied, which a generated
    # replacement would otherwise drop (see _TOOL_INSTALL_ADAPTATIONS).
    if adaptations := _TOOL_INSTALL_ADAPTATIONS.get(meta.canonical_name):
        lines.extend(adaptations)
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
    lines.append(f"# Runtime: {yocto.runtime_label}")
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

    A vendored recipe also copies its artifact into ``out_dir/files/`` (the
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
                f"{meta.canonical_name} vendors its artifact; vendor_source "
                "path is required to copy it next to the recipe"
            )
        files_dir = out_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vendor_source, files_dir / meta.source_filename)
    return path, warnings
