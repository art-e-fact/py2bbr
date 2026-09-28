"""
Target architecture matrix and build-for-target compatibility report.

py2bbr renders recipes for a **target** image while the layer is built by an
x86_64 build host, so two different architectures are in play:

* ``-native`` packages (cython, meson, pypa/build, ...) are *tools* and must
  be host binaries: the PEP 517 frontend that drives the build (``pypa/build``
  under ``nativepython3``) has to run on the machine doing the building.
* the package itself must be compiled for the *target* CPU, otherwise the
  image carries x86_64 extension modules that the target cannot load.

Only some of poky's python build classes get the second part right. The
cross-capable ones all inherit ``setuptools3-base``, which exports the target
toolchain into the build environment (``LDSHARED = ${CCLD} -shared``,
``STAGING_INCDIR`` / ``STAGING_LIBDIR``, ``python3`` on the target sysroot) and
DEPENDS the target interpreter:

============================  ==========================================
class                         how extension modules get built
============================  ==========================================
``setuptools3``               ``python3-native setup.py bdist_wheel``
                              with the exported target CC/LDSHARED -> target
``python_setuptools_build_meta`` same, via the PEP 517 frontend
``python_mesonpy``            meson cross build (cross file) -> target
``python_pep517``             generic ``nativepython3 -m build``; a backend
                              that shells out to the *exported* target
                              compiler still cross-builds, one that resolves
                              the compiler itself (or a prebuilt wheel) does
                              not
============================  ==========================================

:func:`evaluate` turns that knowledge plus the package's own metadata into a
per-target verdict, so py2bbr can say out loud "this recipe will not work on
aarch64" instead of leaving it to the first cross build. What it can judge is
deliberately limited to facts it can read from the sdist / metadata: Python
version gates, prebuilt platform wheels, toolchain availability, backend
cross-compatibility and x86-specific build flags. A ``RISK`` verdict means
"check this", an ``INCOMPATIBLE`` verdict means "this cannot work".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidWheelFilename, parse_wheel_filename

from py2bbr.model import (
    PackageMetadata,
    YoctoVersion,
)
from py2bbr.recipe import (
    CROSS_CAPABLE_CLASSES,
    HOST_ONLY_CLASSES,
    canonical_name,
)


@dataclass(frozen=True)
class Target:
    """One CPU family py2bbr reports on."""

    key: str
    #: Short human label, e.g. ``cortex-a72 (32-bit ARM)``.
    label: str
    #: ``platform.machine()`` value as Python reports it on this target.
    platform_machine: str
    #: Tokens poky puts in ``MACHINEOVERRIDES`` for this architecture (see
    #: ``meta/conf/machine/include/*/arch-*.inc``): ``x86`` / ``x86_64`` /
    #: ``armv7a`` / ``aarch64`` / ``riscv64`` / ... They are the override
    #: suffixes a recipe may use in ``RDEPENDS:${PN}:append:<token>``.
    machine_overrides: tuple[str, ...]
    #: Example Yocto machine names, for the report.
    machines: str = ""


#: Major targets we report on. ARM Cortex-A cores get one entry each because
#: they differ in TUNE (``cortexa53`` / ``cortexa57`` / ``cortexa72``), and the
#: 32-bit / 64-bit split changes the ``MACHINEOVERRIDES`` token as well.
KNOWN_TARGETS: tuple[Target, ...] = (
    Target(
        key="x86",
        label="x86 (32-bit)",
        platform_machine="i686",
        machine_overrides=("x86",),
        machines="qemux86",
    ),
    Target(
        key="x86_64",
        label="x86_64",
        platform_machine="x86_64",
        machine_overrides=("x86_64",),
        machines="qemux86-64",
    ),
    Target(
        key="cortex-a53",
        label="Cortex-A53 (32-bit ARM)",
        platform_machine="armv7l",
        machine_overrides=("armv7a", "cortexa53"),
        machines="qemuarm (TUNE=cortex-a53)",
    ),
    Target(
        key="cortex-a57",
        label="Cortex-A57 (32-bit ARM)",
        platform_machine="armv7l",
        machine_overrides=("armv7a", "cortexa57"),
        machines="qemuarm (TUNE=cortex-a57)",
    ),
    Target(
        key="cortex-a72",
        label="Cortex-A72 (32-bit ARM)",
        platform_machine="armv7l",
        machine_overrides=("armv7a", "cortexa72"),
        machines="qemuarm (TUNE=cortex-a72)",
    ),
    Target(
        key="cortex-a53-64",
        label="Cortex-A53 (64-bit ARM)",
        platform_machine="aarch64",
        machine_overrides=("aarch64", "cortexa53"),
        machines="qemuarm64 (TUNE=cortex-a53)",
    ),
    Target(
        key="cortex-a72-64",
        label="Cortex-A72 (64-bit ARM)",
        platform_machine="aarch64",
        machine_overrides=("aarch64", "cortexa72"),
        machines="qemuarm64 (TUNE=cortex-a72)",
    ),
    Target(
        key="zynqmp",
        label="Zynq UltraScale+ MPSoC",
        platform_machine="aarch64",
        machine_overrides=("aarch64", "cortexa53"),
        machines="zynqmp (zcu102/zcu104)",
    ),
    Target(
        key="versal",
        label="Versal / Versal AI",
        platform_machine="aarch64",
        machine_overrides=("aarch64", "cortexa53"),
        machines="versal, versal-ai (vekarma)",
    ),
    Target(
        key="riscv64",
        label="RISC-V 64-bit",
        platform_machine="riscv64",
        machine_overrides=("riscv64",),
        machines="qemuriscv64",
    ),
)


def target_by_key(key: str) -> Target | None:
    """Look up a target by key or by ``platform_machine`` value."""
    for t in KNOWN_TARGETS:
        if key == t.key or key == t.platform_machine:
            return t
    return None


#: Backends that cannot produce target binaries without extra infrastructure
#: (a Rust toolchain poky does not ship, or a host-oriented driver).
_HOST_ONLY_BACKENDS = {
    "maturin": "maturin builds for the running interpreter; cross builds need "
    "a Rust cross toolchain and a target runner",
    "setuptools-rust": "setuptools-rust drives cargo for the host; cross "
    "builds need a Rust cross toolchain poky does not ship",
    "setuptools_rust": "setuptools-rust drives cargo for the host; cross "
    "builds need a Rust cross toolchain poky does not ship",
    "cffi-rust": "cffi-rust compiles Rust for the host",
}

#: Build requirements that imply a Rust toolchain for the recipe's DEPENDS.
_RUST_BUILD_DEPS = frozenset(
    {"cffi-rust", "maturin", "setuptools-rust", "setuptools_rust"}
)


class Verdict(StrEnum):
    """Per-target build compatibility."""

    #: Nothing target specific found: the package builds (or is pure Python)
    #: everywhere in the matrix.
    OK = "OK"
    #: Builds for the target, but something needs verifying by hand.
    RISK = "RISK"
    #: Cannot work on this target.
    INCOMPATIBLE = "INCOMPATIBLE"


@dataclass
class TargetVerdict:
    """One target's verdict plus the reasons specific to that target."""

    target: Target
    verdict: Verdict = Verdict.OK
    notes: list[str] = field(default_factory=list)

    def add(self, verdict: Verdict, note: str) -> None:
        """Record ``note``; ``INCOMPATIBLE`` outranks the softer verdicts."""
        self.notes.append(note)
        self.mark(verdict)

    def mark(self, verdict: Verdict) -> None:
        """Raise the verdict without a per-target note (shared reason)."""
        if verdict is Verdict.INCOMPATIBLE:
            self.verdict = Verdict.INCOMPATIBLE
        elif (
            verdict is Verdict.RISK and self.verdict is not Verdict.INCOMPATIBLE
        ):
            self.verdict = Verdict.RISK


@dataclass
class TargetReport:
    """The full build-for-target verdict for one package."""

    verdicts: list[TargetVerdict] = field(default_factory=list)
    #: Findings that hold for every target (printed once).
    notes: list[str] = field(default_factory=list)
    #: The build class the report was computed for.
    build_class: str = ""
    #: Architecture that builds the layer (the x86_64/-native side).
    build_host: str = "x86_64"
    #: True when the sdist ships no compiled code.
    pure_python: bool = True
    #: True when ``build_class`` compiles against the target toolchain.
    cross_builds: bool = True

    @property
    def ok(self) -> bool:
        """True when no target is flagged at all."""
        return all(v.verdict is Verdict.OK for v in self.verdicts)

    @property
    def buildable(self) -> bool:
        """True when nothing is INCOMPATIBLE (RISKs are allowed)."""
        return not any(v.verdict is Verdict.INCOMPATIBLE for v in self.verdicts)

    def find(self, key: str) -> TargetVerdict | None:
        for v in self.verdicts:
            if v.target.key == key:
                return v
        return None

    def render(self) -> list[str]:
        """Human-readable: shared findings once, then one line per target."""
        out = [
            (
                f"Build for target (build class {self.build_class}, "
                f"layer built on {self.build_host}):"
            )
        ]
        for note in self.notes:
            out.append(f"  - {note}")
        width = max((len(v.target.label) for v in self.verdicts), default=0)
        for v in self.verdicts:
            out.append(f"  {v.target.label:<{width}}  {v.verdict.value}")
            for note in v.notes:
                out.append(f"      - {note}")
        return out


#: ``platform_machine`` values that are x86, i.e. targets an x86-tuned build
#: (AVX/SSE flags) is at home on.
_X86_MACHINES = frozenset({"amd64", "i386", "i486", "i586", "i686", "x86_64"})


def _wheel_platform(filename: str) -> tuple[str, str]:
    """``(arch, platform)`` a wheel filename pins, both empty if arch-agnostic.

    ``py3-none-any`` is architecture independent. Anything else ships binaries
    built for one platform: ``cp312-cp312-manylinux_2_17_aarch64.whl`` pins both
    the OS and the CPU family, ``...-macosx_11_0_arm64.whl`` pins an OS this
    image never runs.
    """
    try:
        _name, _version, _build, tags = parse_wheel_filename(filename)
    except (InvalidWheelFilename, ValueError):
        return "", ""
    platforms = {t.platform for t in tags}
    if platforms == {"any"}:
        return "", ""
    # Several tags of one wheel can name different platforms (compatibility
    # tags); report the lowest so the message names one platform, not a set.
    platform = min(platforms)
    for family in (
        "x86_64",
        "aarch64",
        "i686",
        "i386",
        "armv7l",
        "riscv64",
    ):
        if family in platform:
            return family, platform
    return "", platform


def _python_verdict(meta: PackageMetadata, yocto: YoctoVersion) -> str:
    """Reason the target Python cannot build the package, or ``""``."""
    if not meta.requires_python:
        return ""
    try:
        spec = SpecifierSet(meta.requires_python)
    except InvalidSpecifier:
        return ""
    if spec.contains(yocto.python_version, prereleases=True):
        return ""
    return (
        f"upstream requires Python {meta.requires_python}, but {yocto.label} "
        f"ships Python {yocto.python_version}"
    )


def _rust_reason(meta: PackageMetadata) -> str:
    """Reason a Rust toolchain blocks the build, or ``""``."""
    names = {canonical_name(d.name) for d in meta.build_deps} | {
        canonical_name(meta.build_backend or "")
    }
    for name in sorted(names & _RUST_BUILD_DEPS):
        return _HOST_ONLY_BACKENDS.get(name, "needs a Rust toolchain")
    return ""


def evaluate(
    meta: PackageMetadata,
    build_class: str,
    yocto: YoctoVersion,
    *,
    targets: tuple[Target, ...] | None = None,
) -> TargetReport:
    """Judge ``meta`` against every target in the matrix.

    ``build_class`` is the class the recipe inherits, so an explicit
    ``--build-class`` override is reflected in the verdict. Findings that do
    not depend on the target (the Python version gate, a missing Rust
    toolchain, how the build class cross-compiles) are collected once in
    :attr:`TargetReport.notes`; only genuinely per-target findings repeat.
    """
    report = TargetReport(
        build_class=build_class, pure_python=not meta.has_native_code
    )
    targets = targets or KNOWN_TARGETS

    # --- findings that hold for every target ------------------------------
    python_block = _python_verdict(meta, yocto)
    rust_block = _rust_reason(meta)
    wheel_arch, wheel_platform = (
        _wheel_platform(meta.source_filename)
        if meta.source_kind.value == "wheel"
        else ("", "")
    )
    foreign_os = bool(wheel_platform) and "linux" not in wheel_platform
    report.cross_builds = build_class in CROSS_CAPABLE_CLASSES
    host_only = build_class in HOST_ONLY_CLASSES

    if python_block:
        report.notes.append(f"every target: {python_block}")
    if rust_block:
        report.notes.append(
            f"every target: {rust_block}; poky ships no Rust recipe, so the "
            "DEPENDS entry has no provider either"
        )
    if wheel_arch or foreign_os:
        report.notes.append(
            f"source is a prebuilt wheel ({meta.source_filename}): it installs "
            "binaries instead of compiling them, so it only ever matches one "
            "architecture - build from the sdist for a cross target"
        )
    if meta.has_native_code:
        evidence = ", ".join(meta.native_code_evidence[:2]) or "native code"
        if report.cross_builds:
            report.notes.append(
                f"extension modules ({evidence}) are compiled for the target: "
                f"{build_class} exports the target toolchain (CC/LDSHARED, "
                "STAGING_INCDIR/STAGING_LIBDIR) and DEPENDS the target python3"
            )
        else:
            driver = (
                "whose PEP 517 frontend runs under nativepython3: the backend "
                "has to pick up the target toolchain setuptools3-base exports"
                if host_only
                else "which is not one of the classes that exports the target "
                "toolchain"
            )
            report.notes.append(
                f"extension modules ({evidence}) are built by {build_class}, "
                f"{driver}, or the image gets {report.build_host} binaries"
            )
    else:
        report.notes.append(
            f"pure-Python sdist: {build_class} produces host-independent wheels"
        )
    if meta.machine_deps:
        names = ", ".join(sorted(d.name for d in meta.machine_deps))
        report.notes.append(
            f"machine-gated dependencies ({names}) are rendered as "
            "RDEPENDS:${PN}:append:<MACHINEOVERRIDES token>, so bitbake picks "
            "the set that matches the target"
        )

    # --- per-target findings ----------------------------------------------
    for target in targets:
        v = TargetVerdict(target)
        if python_block or rust_block:
            v.mark(Verdict.INCOMPATIBLE)
        if foreign_os:
            v.add(
                Verdict.INCOMPATIBLE,
                f"the wheel is built for {wheel_platform}, not for a Linux "
                "target",
            )
        elif wheel_arch and wheel_arch != target.platform_machine:
            v.add(
                Verdict.INCOMPATIBLE,
                f"prebuilt {wheel_arch} binaries on a "
                f"{target.platform_machine} target",
            )
        elif wheel_arch:
            v.add(
                Verdict.RISK,
                "the prebuilt wheel matches this architecture, so the image "
                "works - but nothing is compiled for the target",
            )
        if (
            meta.has_native_code
            and not report.cross_builds
            and v.verdict is not Verdict.INCOMPATIBLE
        ):
            # A shared reason (missing Rust toolchain, prebuilt wheel of the
            # wrong OS) already decided this target: repeating the "confirm
            # the sysroot" advice on top only buries the verdict.
            v.add(
                Verdict.RISK,
                f"confirm the {target.platform_machine} extension modules in "
                f"the sysroot: {build_class} would otherwise ship "
                f"{report.build_host} binaries",
            )
        if (
            meta.host_assumptions
            and v.verdict is not Verdict.INCOMPATIBLE
            and target.platform_machine not in _X86_MACHINES
        ):
            v.add(
                Verdict.RISK,
                "build config hard-codes x86 ISA flags ("
                + ", ".join(meta.host_assumptions[:2])
                + "): the compile may fail, or an optimization may be "
                "silently dropped on this target",
            )
        needed = [
            d.name
            for d in meta.machine_deps
            if target.platform_machine in d.machines
        ]
        other = [
            d.name
            for d in meta.machine_deps
            if target.platform_machine not in d.machines
        ]
        if needed:
            v.add(Verdict.OK, "needs " + ", ".join(needed))
        if other:
            v.add(
                Verdict.OK,
                "not a dependency here: " + ", ".join(other),
            )

        report.verdicts.append(v)

    return report
