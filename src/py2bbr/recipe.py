"""
Render a Bitbake recipe (``python3-<name>.bb``) from package metadata.
"""

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


# List of build backends with no python3-*-native provider in Yocto/PetaLinux,
# Maintained and used to avoid errors like:
#
#     "Nothing PROVIDES 'python3-uv-build-native'"
#
# The values here must be distribution names as would appear in DEPENDS.
_EXTERNAL_BUILD_BACKENDS = frozenset({"uv-build"})


# Non-Python build tool dependencies.
_BUILD_BACKEND_TOOL_DEPS: dict[str, str] = {
    "mesonpy": "meson-native ninja-native patchelf-native",
    "meson-python": "meson-native ninja-native patchelf-native",
    "scikit-build-core": "cmake-native ninja-native",
}


# Non-Python tool dependencies without the ``python3-`` prefix.
# Maintained and used to avoid errors like: "Nothing RPROVIDES".
_OE_BARE_TOOL_PROVIDERS = frozenset({"meson", "ninja", "cmake", "patchelf"})


# Build classes that compile extension modules against the target toolchain.
CROSS_CAPABLE_CLASSES = frozenset(
    {
        "python_flit_core",
        "python_mesonpy",
        "python_setuptools_build_meta",
        "setuptools3",
    }
)

# Build classes that run under the host interpreter. Used for pure-Python
# packages.
HOST_ONLY_CLASSES = frozenset({"python_pep517"})


# Ready to use recipe snippets, per build tool.
#
# Tool names (keys here) must be the formal distribution name.
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


# Recipe commands to mirror what Poky does.
#
# Necessary as recipes that override Poky's completely mask what Poky would do
# (Bitbake replaces, no inheritance or overlay-like application). So here are
# plugs to fullfil what Poky would have done and end up with working layers.
#
# Tool names (keys here) must be the formal distribution name.
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


# Fix for installed Python package to be "found".
#
# Basically adds a common prefix flag the install command.
#
# Avoids errors like "installed but not shipped" at build time.
_INSTALL_PREFIX_OVERRIDE = """\
# Work around PetaLinux's python_pep517_do_install calling pypa installer
# without --prefix: Wheels end up under
# ${D}/output/.../recipe-sysroot-native/... and do_package fails with
# "installed but not shipped". A --prefix keeps every path relative to ${D}.
python_pep517_do_install() {
    nativepython3 -m installer ${INSTALL_WHEEL_COMPILE_BYTECODE} --interpreter "${USRBINPATH}/env ${PEP517_INSTALL_PYTHON}" --prefix ${prefix} --destdir=${D} ${PEP517_WHEEL_PATH}/*.whl
}
"""


# Cleanup regex for licence files.
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
    """Translate an SPDX license expression to Yocto's ``&|()`` operator syntax."""
    expr = expr.replace(" AND ", " & ")
    expr = expr.replace(" OR ", " | ")
    return expr


def _comment_block(text: str, width: int = 74) -> list[str]:
    """Render `text` as a wrapped `# ...` BitBake comment block."""
    return [f"# {line}" for line in textwrap.wrap(text, width=width)]


def _bitbake_deps(names: list[str], prefix: str) -> list[str]:
    """Render dependency names as `<prefix><canonical name>`, deduplicated."""
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
    """The upstream package name an sdist filename encodes, if derivable."""
    for ext in ("tar.gz", "zip"):
        suffix = f"-{version}.{ext}"
        if source_filename.endswith(suffix):
            return source_filename[: -len(suffix)]
    return None


def _sdist_stem(source_filename: str) -> str:
    """
    The directory an sdist gets in, e.g.
    `pyvizionsdk-26.9.1.tar.gz` -> `pyvizionsdk-26.9.1`.
    """
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".zip"):
        if source_filename.endswith(ext):
            return source_filename[: -len(ext)]
    raise ValueError(
        f"cannot derive the unpack directory of {source_filename!r}: "
        "not a known sdist archive extension"
    )


# MD5 digests of the license texts the Yocto series has.
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
    """Space-joined `file://${COMMON_LICENSE_DIR}/<id>;md5=<md5>` entries."""
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
    Return `(build_class, needs_warning)` for the detected backend.
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
    """Whether the recipe must relax `build-system.requires` version bounds."""
    if build_class not in (
        "python_pep517",
        "python_setuptools_build_meta",
        "python_mesonpy",
    ):
        return False
    return any(_has_version_bound(d.requirement) for d in build_deps)


def _newer_minor_pattern(minor: int) -> str:
    """Two-digit regex for Python minor versions strictly greater than `minor`."""
    hi, lo = divmod(minor, 10)
    if lo == 9:
        return rf"[{hi + 1}-9][0-9]"
    return rf"{hi}[{lo + 1}-9]|[{hi + 1}-9][0-9]"


def _classifier_drop_pattern(target_python: str) -> str | None:
    """
    Remove classifiers for Python releases newer than the target.

    Needed to avoid build rejections.
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


# Build-backend / tool versions shipped in each Yocto series.
# Key: Canonical (PEP 503) distribution name.
_BUNDLED_TOOL_VERSIONS: dict[str, dict[str, str]] = {
    "6.0": {
        "build": "1.4.0",
        "cython": "3.2.4",
        "flit-core": "3.12.0",
        "hatchling": "1.29.0",
        "installer": "0.7.0",
        "meson": "1.10.2",
        "meson-python": "0.19.0",
        "ninja": "1.13.2",
        "packaging": "26.0",
        "poetry-core": "2.3.1",
        "pyproject-hooks": "1.2.0",
        "pyproject-metadata": "0.11.0",
        "setuptools": "82.0.1",
        "wheel": "0.46.3",
    },
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
    "4.3": {
        "build": "1.0.3",
        "cython": "0.29.36",
        "flit-core": "3.9.0",
        "hatchling": "1.18.0",
        "installer": "0.7.0",
        "meson": "1.2.2",
        "meson-python": "0.14.0",
        "ninja": "1.11.1",
        "packaging": "23.1",
        "poetry-core": "1.7.0",
        "pyproject-hooks": "1.0.0",
        "pyproject-metadata": "0.7.1",
        "setuptools": "68.2.2",
        "wheel": "0.41.2",
    },
    "4.1": {
        "cython": "0.29.32",
        "flit-core": "3.7.1",
        "hatchling": "1.9.0",
        "installer": "0.5.1",
        "meson": "0.63.3",
        "ninja": "1.11.1",
        "packaging": "21.3",
        "poetry-core": "1.0.8",
        "setuptools": "65.0.2",
        "wheel": "0.37.1",
    },
    "3.1": {
        "cython": "0.29.14",
        "meson": "0.53.2",
        "ninja": "1.10.0",
        "packaging": "20.3",
        "setuptools": "45.2.0",
        "wheel": "0.33.6",
    },
}


# Packages a build tool needs and possibly not provided in a given Yocto
# release.
#
# Useful when a recipe requires a tool version later the one provided by the
# Yocto release.
#
# Key: Canonical (PEP 503) distribution name.
# Value: (version, requirement) is the requirement for the given version.
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
    """List missing or too-old dependencies for a target build tool."""
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
    """Version of a build tool the target series provides."""
    return _BUNDLED_TOOL_VERSIONS.get(yocto.version, {}).get(
        canonical_name(name)
    )


def _minimum_version(specifier: str) -> Version | None:
    """Lowest version a PEP 508 specifier set demands, or `None`."""
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
    """Checks whether a `dep` entry can apply on the target."""
    marker = dep.marker
    if not marker:
        return True

    # TODO Import here to avoid cycles.
    from py2bbr.metadata import marker_env
    try:
        return bool(Marker(marker).evaluate(marker_env(yocto)))
    except (InvalidMarker, UndefinedEnvironmentName):
        return True


def build_version_gaps(
    meta: PackageMetadata, yocto: YoctoVersion
) -> list[Dependency]:
    """
    List required build dependencies the Yocto series does not provide,
    or provides a version that is too old.
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
    """The class the recipe inherits."""
    if build_class:
        return build_class
    if meta.canonical_name in _BOOTSTRAP_TOOL_SHAPES:
        return "python_flit_core"
    return pick_build_class(meta.build_backend)[0]


def _wheel_license_unpack(license_file: str) -> str:
    """Recipe lines exposing `license_file` in a wheel."""
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
    """The recipe leading metadata block, shared by every render."""
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
            # sdist ships no license file and do_populate_lic fails without
            # for missing LIC_FILES_CHKSUM. So point at the license text the
            # target ships in meta/files/common-licenses.
            lines.append(f'LIC_FILES_CHKSUM = "{fallback}"')
        else:
            warnings.append(
                "no license file found in the source tree; "
                "add LIC_FILES_CHKSUM manually (license is declared via metadata only)."
            )
    else:
        # PetaLinux errors on recipes without a LICENSE field
        # Follow the Yocto convention for an unknown/proprietary licence.
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


# `do_install` for a recipe whose only artifact is a wheel.
_WHEEL_INSTALL = """\
# The interpreter recorded in the console scripts' shebang.
WHEEL_INSTALL_PYTHON = "python3"
WHEEL_INSTALL_PYTHON:class-native = "nativepython3"
WHEEL_INSTALL_PYTHON:class-nativesdk = "nativepython3"

# Only a wheel is available, so just install (nothing to build).
do_install() {
    nativepython3 -m installer --interpreter "${USRBINPATH}/env ${WHEEL_INSTALL_PYTHON}" --prefix ${prefix} --destdir=${D} ${WORKDIR}/*.whl
}
"""

# QA skip for a vendored wheel's prebuilt binaries.
_WHEEL_QA_SKIP = """\
# The wheel contains prebuilt binaries that are already stripped.
INSANE_SKIP:${PN} = "already-stripped file-rdeps"
"""

# Mapping from a shared-library `DT_SONAME` to the OE package that
# typically provides it (scarthgap OE-core package splits).
#
# Used only for reporting, not making the recipe.
_OE_LIB_PACKAGE_HINTS = {
    "libgobject-2.0.so.0": "glib-2.0",
    "libglib-2.0.so.0": "glib-2.0",
    "libgio-2.0.so.0": "glib-2.0",
    "libgmodule-2.0.so.0": "glib-2.0",
    "libgstreamer-1.0.so.0": "gstreamer1.0",
    "libgstapp-1.0.so.0": "gstreamer1.0-plugins-base",
    "libgstallocators-1.0.so.0": "gstreamer1.0-plugins-base",
    "libgstvideo-1.0.so.0": "gstreamer1.0-plugins-base",
    "libgsttag-1.0.so.0": "gstreamer1.0-plugins-base",
    "libgstpbutils-1.0.so.0": "gstreamer1.0-plugins-base",
    "libudev.so.1": "udev",
    "libz.so.1": "zlib",
    "libpcre2-8.so.0": "libpcre2",
    "libssl.so.3": "openssl",
    "libcrypto.so.3": "openssl",
    "libcurl.so.4": "curl",
}


def _oe_lib_package_hint(soname: str) -> str:
    """Typical OE package for `soname`, or `""`"""
    return _OE_LIB_PACKAGE_HINTS.get(soname, "")


def _render_external_libs_comment(sonames: list[str]) -> str:
    """
    Recipe comment naming shared libraries that are not bundled in the recipe.
    """
    lines = [
        (
            "# The wheel's bundled binaries link against these shared "
            "libraries, which are NOT bundled"
        ),
        "# in the wheel and must already be in the target rootfs:",
    ]
    width = max(len(s) for s in sonames)
    for soname in sonames:
        hint = _oe_lib_package_hint(soname)
        if hint:
            lines.append(
                f"#   {soname:<{width}}   typically packaged as {hint}"
            )
        else:
            lines.append(
                f"#   {soname:<{width}}   no OE provider in py2bbr's "
                "knowledge (add the providing package yourself)"
            )
    lines.append(
        "# No RDEPENDS is generated for them - the provider name differs "
        "between distros - so make sure"
    )
    lines.append("# the image provides them when this package is used.")
    return "\n".join(lines)


def _render_bundled_libs_block(bundled: dict[str, list[str]]) -> str:
    """
    Recipe comment + `RPROVIDES` re-exporting the wheel's bundled sidecar
    libraries.
    """
    tokens_by_soname: list[str] = []
    for soname, versions in bundled.items():
        # Consumers' DT_NEEDED records the renamed soname with no version, so
        # rpm requires the bare `soname()(64bit)` token in addition to the
        # versioned symbols; both must be re-provided.
        tokens = [f"{soname}()(64bit)"]
        tokens += [f"{soname}({version})(64bit)" for version in versions]
        tokens_by_soname.append(" ".join(tokens))
    if not tokens_by_soname:
        return ""
    lines: list[str] = []
    lines.append(
        "# The sidecar libraries bundled with this wheel are an auditwheel "
        "rename of"
    )
    lines.append(
        "# system libraries, and rpm needs their versioned symbols under the "
        "renamed"
    )
    lines.append(
        "# soname (e.g. libblkid-e4a77f31.so.1.1.0(BLKID_1.0)(64bit)) while "
        "providing"
    )
    lines.append(
        "# them under neither. The libraries ship inside the wheel, so this "
        "package"
    )
    lines.append(
        "# provides those tokens itself; without this, do_rootfs fails with "
        '"nothing provides" for every versioned symbol they define.'
    )
    lines.append(
        "# One assignment per bundled library keeps every line's quotes "
        "balanced (the"
    )
    lines.append(
        "# structural check py2bbr runs on its own recipes rejects bitbake "
        "line continuations)."
    )
    lines += [f'RPROVIDES:${{PN}} += "{group}"' for group in tokens_by_soname]
    return "\n".join(lines)


def _render_wheel_recipe(
    meta: PackageMetadata, yocto: YoctoVersion
) -> tuple[str, list[str]]:
    """Render a recipe that installs a bundled wheel."""
    warnings: list[str] = []
    lines = _recipe_header(meta, warnings, vendored_wheel=True)
    lines.append(f'SRC_URI = "file://{_q(meta.source_filename)}"')
    if meta.source_sha256:
        lines.append(f'SRC_URI[sha256sum] = "{meta.source_sha256}"')
    if meta.requires_python:
        lines.append(f"# Upstream requires Python {meta.requires_python}")
    lines.append("")

    # `setuptools3-base` supplies PYTHON_SITEPACKAGES_DIR and the FILES default
    # that keeps do_package from reporting "installed but not shipped".
    lines.append("inherit python3targetconfig setuptools3-base")
    lines.append("")
    install_deps = ["python3-installer-native"]
    if "unpack_wheel_license()" in "\n".join(lines):
        # _wheel_license_unpack delegates to unzip, which is only on the build
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
    if meta.ships_prebuilt_binaries:
        lines.append(_WHEEL_QA_SKIP.rstrip())
        lines.append("")
    if meta.external_needed_libs:
        lines.append(_render_external_libs_comment(meta.external_needed_libs))
        lines.append("")
    if meta.bundled_lib_versions:
        lines.append(_render_bundled_libs_block(meta.bundled_lib_versions))
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
    if meta.build_backend_source:
        warnings.append(
            f"{meta.canonical_name} {meta.version}: the wheel carries no build "
            f"config, so [build-system] was read from "
            f"{meta.build_backend_source} - the project's default branch, which "
            "describes the project today and not necessarily this release. If "
            "upstream migrated its backend after the tag, this names the wrong "
            "one; an sdist would settle it."
        )
    if meta.ships_prebuilt_binaries:
        warnings.append(
            f"{meta.canonical_name} {meta.version}: the wheel ships prebuilt "
            "binaries that upstream already stripped, so the recipe waives "
            "bitbake's fatal already-stripped QA check and no -dbg package is "
            "produced. The symbols are not in the artifact; only a from-source "
            "build can restore them."
        )
    if meta.external_needed_libs:
        hinted = sorted(
            {
                pkg
                for soname in meta.external_needed_libs
                if (pkg := _oe_lib_package_hint(soname))
            }
        )
        rootfs_note = (
            f" Typical OE providers are {', '.join(hinted)}." if hinted else ""
        )
        warnings.append(
            f"{meta.canonical_name} {meta.version}: the wheel's bundled "
            "binaries link against shared libraries that are not bundled in "
            "the wheel and must already be in the target rootfs: "
            f"{', '.join(meta.external_needed_libs)}.{rootfs_note} The recipe "
            "cannot name their packages for every distro, so it lists them "
            "instead of guessing them into RDEPENDS; make sure the image "
            "provides them when this package is used."
        )
    return "\n".join(lines), warnings


def render_recipe(
    meta: PackageMetadata,
    yocto: YoctoVersion,
    build_class: str | None = None,
) -> tuple[str, list[str]]:
    """
    Render the `.bb` content.

    Returns `(recipe_text, warnings)`.

    (bundled wheels take are processed with `_render_wheel_recipe`)
    """
    if meta.vendored and meta.source_kind is SourceKind.WHEEL:
        text, warnings = _render_wheel_recipe(meta, yocto)
        if build_class is not None:
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

    cross_lines: list[str] = []
    qa_lines: list[str] = []
    if meta.has_native_code:
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
        # Record the upstream Python constraint (may differ from bitbake, which
        # enforces its own Python version).
        lines.append(f"# Upstream requires Python {meta.requires_python}")
    lines.append("")

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
            lines.append(f'PYPI_PACKAGE = "{pypi_name}"')
    lines.append("")

    if cross_lines:
        lines.extend(cross_lines)
        lines.append("")

    if qa_lines:
        lines.extend(qa_lines)
        lines.append("")

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
    license_rewritten = False
    if _needs_bounds_relax(build_class, meta.build_deps):
        warnings.append(
            "build-system.requires pins versions that may exceed the target "
            "Yocto's native setuptools/wheel; the recipe relaxes them in "
            "do_configure:append (the native sysroot versions are authoritative)."
        )
        sed_programs.append(
            r"/^[[:space:]]*\[build-system\]/,/^[[:space:]]*\[/ "
            r"s/([A-Za-z0-9_.]+)([[:space:]]*[<>=!~][^\"';]*[0-9][^\"';]*)/\1/g"
        )
    if external_backend:
        sed_programs.append(
            r"/^[[:space:]]*\[build-system\]/,/^[[:space:]]*\[/ "
            r"{ "
            r"s/^[[:space:]]*requires[[:space:]]*=.*/requires = [\"setuptools\", \"wheel\"]/; "
            r"s/^[[:space:]]*build-backend[[:space:]]*=.*/build-backend = \"setuptools.build_meta\"/ "
            r"}"
        )
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

    lines.append(_INSTALL_PREFIX_OVERRIDE.rstrip())
    lines.append("")

    if adaptations := _TOOL_INSTALL_ADAPTATIONS.get(meta.canonical_name):
        lines.extend(adaptations)
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

    return "\n".join(lines), warnings


def render_recipe_to_path(
    meta: PackageMetadata,
    yocto: YoctoVersion,
    out_dir,
    build_class: str | None = None,
    *,
    vendor_source=None,
) -> tuple:
    """Write the recipe into `out_dir` and return `(path, warnings)`."""
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
