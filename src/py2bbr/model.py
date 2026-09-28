"""Core data model shared across py2bbr modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

#: Default Yocto series py2bbr targets when ``--yocto-version`` is omitted.
DEFAULT_YOCTO_VERSION = "5.0"


class SourceKind(StrEnum):
    """Kind of upstream artifact the recipe fetches."""

    SDIST = "sdist"
    WHEEL = "wheel"


class DepKind(StrEnum):
    """Classification of a ``Requires-Dist`` entry."""

    RUNTIME = "runtime"
    BUILD = "build"
    DROPPED = "dropped"


@dataclass(frozen=True)
class YoctoVersion:
    """A concrete Yocto Project series and the build data derived from it.

    The mapping is owned by py2bbr (kas is data-driven and does not ship a
    release->branch table). ``poky_branch`` is the poky git branch and is also
    the value used in the generated ``kas`` build file.
    """

    version: str
    codename: str
    poky_branch: str
    python_version: str
    #: Layer that provides the ``python3-*`` recipes we depend on.
    meta_python: str = "meta-python"
    #: Base URL of the poky repository (overridable for mirrors).
    poky_url: str = "https://git.yoctoproject.org/poky"
    #: URL of the meta-python layer.
    meta_python_url: str = "https://git.openembedded.org/meta-python"
    #: Branch of the meta-python layer matching this series.
    meta_python_branch: str = ""

    @property
    def python_pn(self) -> str:
        """The Bitbake Python package prefix, e.g. ``python3``."""
        return "python3" if self.python_version.startswith("3") else "python"

    @property
    def label(self) -> str:
        return f"Yocto {self.version} ({self.codename})"


@dataclass(frozen=True)
class Dependency:
    """A single dependency derived from a ``Requires-Dist`` entry."""

    name: str
    kind: DepKind
    #: Original PEP 508 requirement string (for debugging / reporting).
    requirement: str = ""
    #: The version specifier, e.g. ``>=8.0.0``.
    specifier: str = ""
    #: The environment marker guarding this entry, e.g. ``sys_platform == "ios"``.
    #: Empty for an unconditional dependency. Set for ``Requires-Dist`` and
    #: ``[build-system].requires`` entries that carry a marker, so callers can
    #: tell a floor that always applies from one gated on an environment the
    #: target may never have.
    marker: str = ""
    #: Reason this dependency was dropped (only for :attr:`DepKind.DROPPED`).
    reason: str = ""

    @property
    def bitbake_name(self) -> str:
        """The runtime Bitbake package this maps to (without the python prefix).

        e.g. ``mypy-extensions``. The recipe renders it as a single-line,
        quoted ``python3-<name>`` value (never ``${PYTHON_PN}-<name>``).
        """
        return self.name


#: ``platform.machine()`` -> the ``MACHINEOVERRIDES`` tokens poky guarantees
#: for that architecture (see ``meta/conf/machine/include/*/arch-*.inc``:
#: ``arch-x86.inc`` adds ``x86``/``x86_64``, ``arch-armv7a.inc`` ``armv7a``,
#: ``arch-arm64.inc`` ``aarch64``). They are the override suffixes a recipe may
#: use in ``RDEPENDS:${PN}:append:<token>``, so a recipe stays correct on every
#: machine of the family instead of only on the machine py2bbr ran on.
MACHINE_OVERRIDE_TOKENS: dict[str, tuple[str, ...]] = {
    "aarch64": ("aarch64",),
    "aarch64_be": ("aarch64",),
    "amd64": ("x86_64",),
    "arm64": ("aarch64",),
    "armv7a": ("armv7a",),
    "armv7l": ("armv7a",),
    "armv8l": ("armv7a", "armv8a"),
    "i386": ("x86",),
    "i486": ("x86",),
    "i586": ("x86",),
    "i686": ("x86",),
    "riscv64": ("riscv64",),
    "x86_64": ("x86_64",),
}


def machine_override_tokens(machine: str) -> tuple[str, ...]:
    """``MACHINEOVERRIDES`` tokens for a ``platform_machine`` value."""
    return MACHINE_OVERRIDE_TOKENS.get(machine.lower(), (machine.lower(),))


@dataclass(frozen=True)
class MachineDep:
    """A runtime dependency gated on the target's ``platform_machine``.

    ``Requires-Dist`` markers such as ``foo; platform_machine == "x86_64"``
    must not be baked into the recipe's unconditional RDEPENDS: the layer is
    cross-built (x86_64 host) for a different target CPU, and baking the
    *host* machine's answer in ships the wrong dependency set (and drops the
    one the target actually needs). Such deps are rendered as
    ``RDEPENDS:${PN}:append:<MACHINEOVERRIDES token>`` instead, so the
    selection happens inside bitbake on the real target.
    """

    name: str
    #: Original PEP 508 requirement string.
    requirement: str = ""
    #: The marker as written upstream, e.g. ``platform_machine == "aarch64"``.
    marker: str = ""
    #: ``platform_machine`` values on which the dependency applies.
    machines: tuple[str, ...] = ()


@dataclass
class PackageMetadata:
    """Authoritative metadata for one Python package version."""

    name: str
    canonical_name: str
    version: str
    summary: str = ""
    description: str = ""
    homepage: str = ""
    #: SPDX license expression (e.g. ``MIT``, ``Apache-2.0``).
    license: str = ""
    #: The raw license text / expression as found in the metadata.
    license_raw: str = ""
    #: License file name (relative to the source root), e.g. ``LICENSE``.
    license_file: str | None = None
    #: MD5 hex digest of the license file (for ``LIC_FILES_CHKSUM``).
    license_md5: str | None = None
    requires_python: str = ""
    #: ``Project-URL`` entries as ``(label, url)``.
    project_urls: list[tuple[str, str]] = field(default_factory=list)

    # --- source artifact -------------------------------------------------
    source_kind: SourceKind = SourceKind.SDIST
    source_filename: str = ""
    source_sha256: str | None = None
    #: True when the sdist is not reachable on public PyPI at the URL the
    #: ``pypi`` class fetches, so the recipe vendors it (file:// SRC_URI).
    vendored: bool = False
    #: PEP 517 ``[build-system].build-backend`` (e.g. ``setuptools.build_meta``).
    build_backend: str = ""
    #: Every ``Requires-Dist`` entry as published, before marker evaluation
    #: (the target compatibility report re-evaluates them per architecture).
    requires_dist: list[str] = field(default_factory=list)

    # --- build-for-target shape -------------------------------------------
    #: True when the sdist ships code that must be *compiled* (C/C++/Cython/
    #: Rust/Go/Fortran/... sources, or an explicit ``ext_modules``). Such a
    #: package only works on a foreign architecture if the recipe drives the
    #: target toolchain; a backend that runs under ``nativepython3`` produces
    #: host binaries otherwise.
    has_native_code: bool = False
    #: Human-readable evidence for :attr:`has_native_code` (paths / build
    #: requirements), empty for a pure-Python sdist.
    native_code_evidence: list[str] = field(default_factory=list)
    #: Build-config lines that hard-code an x86 assumption (``-march=native``,
    #: ``avx2``, ``x86_64``), i.e. code that may not compile for ARM.
    host_assumptions: list[str] = field(default_factory=list)

    # --- dependencies ----------------------------------------------------
    runtime_deps: list[Dependency] = field(default_factory=list)
    build_deps: list[Dependency] = field(default_factory=list)
    dropped_deps: list[Dependency] = field(default_factory=list)
    #: Runtime deps whose ``platform_machine`` marker makes them target
    #: specific; rendered as machine-conditional RDEPENDS.
    machine_deps: list[MachineDep] = field(default_factory=list)

    # --- provenance ------------------------------------------------------
    #: The index/base URL the package was resolved from (best effort).
    resolved_from: str = ""
    #: Index flags that were used (for reproducibility reporting).
    index_flags: list[str] = field(default_factory=list)
