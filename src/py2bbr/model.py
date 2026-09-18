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
    #: Reason this dependency was dropped (only for :attr:`DepKind.DROPPED`).
    reason: str = ""

    @property
    def bitbake_name(self) -> str:
        """The runtime Bitbake package this maps to (without the python prefix).

        e.g. ``mypy-extensions``. The recipe renders it as a single-line,
        quoted ``python3-<name>`` value (never ``${PYTHON_PN}-<name>``).
        """
        return self.name


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
    #: PEP 517 ``[build-system].build-backend`` (e.g. ``setuptools.build_meta``).
    build_backend: str = ""

    # --- dependencies ----------------------------------------------------
    runtime_deps: list[Dependency] = field(default_factory=list)
    build_deps: list[Dependency] = field(default_factory=list)
    dropped_deps: list[Dependency] = field(default_factory=list)

    # --- provenance ------------------------------------------------------
    #: The index/base URL the package was resolved from (best effort).
    resolved_from: str = ""
    #: Index flags that were used (for reproducibility reporting).
    index_flags: list[str] = field(default_factory=list)
