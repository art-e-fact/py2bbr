from dataclasses import dataclass, field
from enum import StrEnum

DEFAULT_YOCTO_VERSION = "5.0"


class SourceKind(StrEnum):
    """Kind of file the recipe fetches."""

    SDIST = "sdist"
    WHEEL = "wheel"


class DepKind(StrEnum):
    """Classification of a ``Requires-Dist`` entry."""

    RUNTIME = "runtime"
    BUILD = "build"
    DROPPED = "dropped"


@dataclass(frozen=True)
class YoctoVersion:
    """
    Information on a Yocto Project series and the build data derived from it.
    """

    version: str
    codename: str
    poky_branch: str
    python_version: str

    # `platform_machine` value as Python reports it *on the target* (the CPU
    # the image runs on), e.g. `aarch64`. Not part of the output: it
    # defaults to the build host architecture.
    platform_machine: str = "x86_64"

    # Layer that provides the ``python3-*`` recipes we depend on.
    meta_python: str = "meta-python"
    # URL of the meta-python layer.
    meta_python_url: str = "https://git.openembedded.org/meta-python"
    # Branch of the meta-python layer matching this series.
    meta_python_branch: str = ""

    # Base URL of the poky repository.
    poky_url: str = "https://git.yoctoproject.org/poky"

    # def __post_init__(self):
    #     # Check for valid Python version and normalise it
    #     parts = self.python_version.split(".")
    #     if len(parts) < 2 or parts[0] != "3" or not parts[1].isdigit():
    #         # raise VersionError(f"Invalid Python version: {self.python_version}. Expecting a semver for Python 3 or later.")
    #         return None
    #     # Yes, sorry. A frozen dataclass does not allow any
    #     # change to attributes, even in post inits.
    #     # See:
    #     # - https://docs.python.org/3/library/dataclasses.html#frozen-instances
    #     # - https://stackoverflow.com/questions/53756788/how-to-set-the-value-of-dataclass-field-in-post-init-when-frozen-true#comment130807314_54119384
    #     object.__setattr__(self, "python_version", f"3.{int(parts[1])}")

    @property
    def python_pn(self) -> str:
        """
        The Bitbake Python package prefix, e.g. `python3`.

        Note we focus and test only with `python3`.
        """
        return "python3" if self.python_version.startswith("3") else "python"

    @property
    def label(self) -> str:
        return f"Yocto {self.version} ({self.codename})"

    @property
    def runtime_label(self) -> str:
        """
        Formatted runtime name the layer targets, e.g. ``Python 3.12 on aarch64``.

        Recorded in the recipe and the layer README for transparency and
        debugging.
        """
        return f"Python {self.python_version} on {self.platform_machine}"


@dataclass(frozen=True)
class Dependency:
    """
    A single dependency derived from an `Requires-Dist` entry.
    """

    name: str
    kind: DepKind

    # Original requirement string (for debugging / reporting).
    requirement: str = ""

    # The version specifier, e.g. `>=8.0.0,<8.2.0`.
    specifier: str = ""

    # The environment marker guarding this entry, e.g. ``sys_platform == "ios"``.
    marker: str = ""

    # Reason this dependency was dropped (only for `DepKind.DROPPED`).
    reason: str = ""

    @property
    def bitbake_name(self) -> str:
        """
        The runtime Bitbake package this maps to (without the python prefix).

        Returns `mypackage`, not `python3-mypackage` as in the recipe.
        """
        return self.name


# Override suffixes a recipe may use in ``RDEPENDS:${PN}:append:<token>``,
# so a recipe stays correct on every machine of the family instead of only
# on the machine py2bbr ran on.
# See Poky `meta/conf/machine/include/*/arch-*.inc`
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
    return MACHINE_OVERRIDE_TOKENS.get(machine.lower(), (machine.lower(),))


@dataclass(frozen=True)
class MachineDep:
    """
    A runtime dependency conditional to `platform_machine` specs.

    `Requires-Dist` such as `mypackage; platform_machine == "x86_64"`
    must not be baked into the recipe's unconditional RDEPENDS (arch mismatch
    when cross-building). These deps are rendered as
    `RDEPENDS:${PN}:append:<MACHINEOVERRIDES token>`, so the arch selection
    happens inside bitbake on the real target.
    """

    name: str

    # Original requirement string.
    requirement: str = ""

    # The original marker, e.g. `platform_machine == "aarch64"`.
    marker: str = ""

    # `platform_machine` values on which the dependency applies.
    machines: tuple[str, ...] = ()


@dataclass
class PackageMetadata:
    """
    Metadata for one Python package version.
    """

    name: str
    canonical_name: str
    version: str
    summary: str = ""
    description: str = ""
    homepage: str = ""

    # Standardised SPDX license.
    license: str = ""

    # Raw license text.
    license_raw: str = ""

    # License file name (relative to the source root), e.g. `LICENSE`.
    license_file: str | None = None

    # MD5 hex digest of the license file (for `LIC_FILES_CHKSUM`).
    license_md5: str | None = None

    requires_python: str = ""

    # `Project-URL` entries as `(label, url)`.
    project_urls: list[tuple[str, str]] = field(default_factory=list)

    # Where `build_backend` came from the package's own `pyproject.toml`,
    # or a label like `github.com/mecompany/myproprietarysdk@pyproject.toml`
    # when a wheel shipped no project file and it was read from the
    # repository instead. Empty means the sdist/wheel did not say and no
    # source could be reached, so the backend is the implicit setuptools
    # default.
    build_backend_source: str = ""

    source_kind: SourceKind = SourceKind.SDIST
    source_filename: str = ""
    source_sha256: str | None = None

    # True when the sdist is not reachable on public PyPI at the URL the
    # `pypi` class fetches, so the recipe vendors it (file:// SRC_URI).
    vendored: bool = False

    # PEP 517 `[build-system].build-backend`.
    build_backend: str = ""

    # Every `Requires-Dist` entry as published, before marker evaluation.
    requires_dist: list[str] = field(default_factory=list)

    # True when the sdist ships code that must be *compiled* (C/C++/Cython/
    # Rust/Go/Fortran/... sources, or an explicit ``ext_modules``).
    has_native_code: bool = False

    # Human-readable evidence for debugging (paths / build requirements).
    # Empty for a pure-Python sdist.
    native_code_evidence: list[str] = field(default_factory=list)

    # Build-config lines that hard-code an x86 assumption (`-march=native`,
    # `avx2`, `x86_64`), i.e. code that may not compile for ARM.
    host_assumptions: list[str] = field(default_factory=list)

    # Essential with many vendor wheels: True when a wheel contains
    # architecture-specific binaries (likely with already stripped ELF
    # objects). Bitbake's `already-stripped` QA check is a fatal error and
    # blocks if the recipe is not explicit.
    ships_prebuilt_binaries: bool = False

    # Sonames (`libgstreamer-1.0.so.0`) that the wheel's ELF binaries link
    # against but that the wheel does not bundle, that is shared libraries
    # that must already exist in the target rootfs.
    external_needed_libs: list[str] = field(default_factory=list)

    # Lib map to retrieve correct names for wheel bundled libraries.
    bundled_lib_versions: dict[str, list[str]] = field(default_factory=dict)

    runtime_deps: list[Dependency] = field(default_factory=list)
    build_deps: list[Dependency] = field(default_factory=list)
    dropped_deps: list[Dependency] = field(default_factory=list)
    # Runtime deps with `platform_machine` marker that makes them arch
    # specific,
    machine_deps: list[MachineDep] = field(default_factory=list)

    # The index/base URL the package was resolved from (best effort).
    resolved_from: str = ""

    # Index flags that were used.
    index_flags: list[str] = field(default_factory=list)
