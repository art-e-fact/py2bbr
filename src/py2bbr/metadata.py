"""Fetch and parse authoritative package metadata.

Current approach
----------------
1. ``pip download`` the package into an isolated temp dir, honouring the user's
   pip index flags (``--index-url`` / ``--extra-index-url`` / ``--trusted-host``
   / arbitrary ``--pip-arg``). This works against PyPI *and* private indexes.
2. Prefer the **sdist** (what ``inherit pypi`` fetches, so its sha256 is the
   correct ``SRC_URI[sha256sum]``); fall back to a **wheel** if sdist is absent.
   That fallback is pinned to the *target* runtime (Python version, and CPU
   architecture when it differs from the build host's) so pip cannot answer with
   an artifact built for the machine py2bbr happens to run on.
3. Parse ``PKG-INFO`` / ``METADATA`` with the stdlib ``email`` parser.
4. Read ``pyproject.toml`` ``[build-system]`` for the backend + build deps.
5. Locate the license file and compute its MD5 (for ``LIC_FILES_CHKSUM``).
6. Classify ``Requires-Dist`` into runtime / build / dropped using ``packaging``
   marker evaluation against the target runtime (Python version and CPU).
7. Inspect the sdist for code that has to be *compiled* and for build config
   that hard-codes an x86 assumption, so the generated recipe can be judged
   per target architecture (see ``py2bbr.targets``).
"""

from __future__ import annotations

import email
import email.message
import hashlib
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import InvalidWheelFilename, parse_wheel_filename

from py2bbr.errors import MetadataError, MetadataNotFoundError
from py2bbr.model import (
    DEFAULT_YOCTO_VERSION,
    Dependency,
    DepKind,
    MachineDep,
    PackageMetadata,
    SourceKind,
    YoctoVersion,
)
from py2bbr.targets import KNOWN_TARGETS

logger = logging.getLogger(__name__)

# Map PEP 561 "License :: OSI Approved :: X" classifier tails to SPDX ids.
_CLASSIFIER_TO_SPDX: dict[str, str] = {
    "MIT License": "MIT",
    "Apache Software License": "Apache-2.0",
    "BSD License": "BSD-3-Clause",
    "GPL v2 (GPLv2)": "GPL-2.0-only",
    "GPL v3 (GPLv3)": "GPL-3.0-only",
    "LGPL": "LGPL-2.1-only",
    "LGPL v2": "LGPL-2.0-only",
    "LGPL v3": "LGPL-3.0-only",
    "ISC License": "ISC",
    "Python Software Foundation License": "PSF-2.0",
    "zlib/libpng License": "Zlib",
    "MPL 2.0": "MPL-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Eclipse Public License 1.0": "EPL-1.0",
    "EUPL 1.1": "EUPL-1.1",
}

# Keyword hints used to sniff a license from free-form ``License:`` text.
_LICENSE_TEXT_HINTS: list[tuple[str, str]] = [
    ("apache license", "Apache-2.0"),
    ("apache-2.0", "Apache-2.0"),
    ("apache 2.0", "Apache-2.0"),
    ("mit license", "MIT"),
    (r"\bmit\b", "MIT"),
    ("bsd 3-clause", "BSD-3-Clause"),
    ("new bsd", "BSD-3-Clause"),
    ("bsd license", "BSD-3-Clause"),
    ("gpl v3", "GPL-3.0-only"),
    ("gplv3", "GPL-3.0-only"),
    ("gnu general public license v3", "GPL-3.0-only"),
    ("gpl v2", "GPL-2.0-only"),
    ("gplv2", "GPL-2.0-only"),
    ("gnu general public license v2", "GPL-2.0-only"),
    ("lgpl", "LGPL-3.0-only"),
    ("isc license", "ISC"),
    (r"\bisc\b", "ISC"),
    ("mozilla public license", "MPL-2.0"),
    ("mplv2", "MPL-2.0"),
    ("mpl v2", "MPL-2.0"),
    ("eclipse public license", "EPL-2.0"),
]

_LICENSE_FILE_NAMES = (
    "LICENSE",
    "LICENSE.txt",
    "LICENSE.md",
    "LICENSE.rst",
    "LICENSE.MIT",
    "LICENSE.APACHE",
    "COPYING",
    "COPYING.txt",
    "COPYING.md",
    "LICENCE",
    "LICENCE.txt",
    "COPYING.LESSER",
)


@dataclass
class PipOptions:
    """pip index options forwarded to ``pip download``."""

    index_url: str | None = None
    extra_index_urls: list[str] = field(default_factory=list)
    trusted_hosts: list[str] = field(default_factory=list)
    #: Extra raw arguments appended verbatim (e.g. ``--proxy``, ``--retries``).
    pip_args: list[str] = field(default_factory=list)
    #: Timeout in seconds for pip network operations (0 = pip default).
    timeout: int = 0

    def build_args(self) -> list[str]:
        args: list[str] = []
        if self.index_url:
            args += ["--index-url", self.index_url]
        for url in self.extra_index_urls:
            args += ["--extra-index-url", url]
        for host in self.trusted_hosts:
            args += ["--trusted-host", host]
        if self.timeout:
            args += ["--timeout", str(self.timeout)]
        args += list(self.pip_args)
        return args

    def describe(self) -> list[str]:
        """Human-readable index flags, for reproducibility reporting."""
        out: list[str] = []
        if self.index_url:
            out.append(f"--index-url={self.index_url}")
        for url in self.extra_index_urls:
            out.append(f"--extra-index-url={url}")
        for host in self.trusted_hosts:
            out.append(f"--trusted-host={host}")
        return out


@dataclass
class FetchedSource:
    """A downloaded upstream artifact and its computed facts."""

    path: Path
    kind: SourceKind
    sha256: str
    filename: str
    #: None = not checked; otherwise whether the pypi class' fetch URL for this
    #: sdist responds on public PyPI (False -> the recipe must vendor instead).
    public_reachable: bool | None = None


def _pip_executable() -> list[str]:
    """
    Find a working pip: prefer the running interpreter's, else a system one.

    ``uv``-managed environments do not always ship ``pip`` in the venv, so fall
    back to a ``pip``/``pip3`` on PATH (``pip download`` only needs to fetch).
    """
    import importlib.util

    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip"]
    for candidate in ("pip3", "pip"):
        path = shutil.which(candidate)
        if path:
            return [path]
    return [sys.executable, "-m", "pip"]


def _pip_python_version(yocto: YoctoVersion | None) -> str | None:
    """The ``<major>.<minor>`` to hand pip as ``--python-version``, or None.

    pip only understands a major.minor spec. ``YoctoVersion.python_version`` is
    major.minor by contract, but a patch component must not silently disable the
    pin (that failure mode is invisible: the download just resolves for the
    wrong interpreter again).
    """
    target = getattr(yocto, "python_version", "") or ""
    parts = target.split(".")
    if len(parts) < 2 or parts[0] != "3" or not parts[1].isdigit():
        return None
    return f"3.{int(parts[1])}"


#: glibc versions a ``manylinux_X_Y`` tag exists for. pip resolves a
#: ``--platform`` value as a *literal* platform tag (it does not expand
#: ``manylinux_2_17_aarch64`` into the manylinux family the way it derives the
#: build host's own tags), so the family has to be spelled out for the target.
#: The range is deliberately generous: a tag for a glibc that has not shipped
#: yet costs nothing, since no wheel can carry it, while one that is missing
#: would reject a perfectly good artifact. (glibc is still on its 2.x series;
#: a 3.0 would need the major in here too.)
_MANYLINUX_FIRST_MINOR = 5  # glibc 2.5, the oldest manylinux1 baseline
_MANYLINUX_LAST_MINOR = 45  # past every glibc released so far

#: The pre-PEP 600 manylinux aliases, the two musl releases, and the plain
#: linux tag a project uses when it wheels against a hand-built toolchain.
#: None of these exist for every architecture, which is harmless: pip only
#: needs a tag some wheel could carry.
_LEGACY_FAMILIES = (
    "manylinux1",
    "manylinux2010",
    "manylinux2014",
    "musllinux_1_1",
    "musllinux_1_2",
    "linux",
)


def _pip_platforms(yocto: YoctoVersion | None) -> list[str]:
    """The ``--platform`` tags a wheel download is restricted to.

    An empty list means "let pip decide", which is the right answer whenever
    the target is the machine py2bbr runs on: pip then applies this host's own
    tag set - the manylinux and musllinux baselines of the *host's* libc, this
    interpreter's ABI, whatever else the host happens to support - and
    enumerating a guessed family could only narrow that.

    A *different* target architecture is the case pip cannot answer by itself:
    its native tags describe the build host, so it hands over the host's
    artifact (pyvizionsdk publishes a different version per architecture, so
    the wrong wheel is the wrong *version*). The pin is then the only thing
    that makes pip fetch the target's, and py2bbr goes on to record that exact
    artifact's sha256 in ``SRC_URI[sha256sum]``.

    The result mirrors what pip would accept on a machine of that CPU: every
    manylinux glibc baseline, the legacy aliases, musllinux, and the bare
    ``linux_<arch>``.
    """
    target = getattr(yocto, "platform_machine", "") or ""
    host = platform.machine().strip().lower()
    # An empty host architecture means "unknown", not "x86_64": pip's native
    # tags stay in charge there, since they are the only ones that can be right
    # about a machine that cannot say what it is. Naming the target explicitly
    # is what opts into a pin.
    if not target or not host or target == host:
        return []
    families = [
        f"manylinux_2_{minor}"
        for minor in range(_MANYLINUX_FIRST_MINOR, _MANYLINUX_LAST_MINOR + 1)
    ]
    families.extend(_LEGACY_FAMILIES)
    return [f"{family}_{target}" for family in families]


def _target_label(yocto: YoctoVersion | None) -> str:
    """Name the target a constraint came from, e.g. ``Yocto 5.0 (scarthgap)``.

    Without it these messages say "the target", and a reader has no way to tell
    which of several targets in one layer the verdict applies to.
    """
    version = getattr(yocto, "version", "") or ""
    codename = getattr(yocto, "codename", "") or ""
    if version and codename and version != codename:
        return f"Yocto {version} ({codename})"
    return f"Yocto {version}" if version else "the Yocto target"


def _run_pip_download(
    requirement: str,
    dest: Path,
    pip_opts: PipOptions,
    binary_mode: str,
    no_build_isolation: bool = False,
    python_version: str | None = None,
    platform_tags: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = (
        _pip_executable()
        + [
            "download",
            requirement,
            "--no-deps",
            "--no-cache-dir",
            "--disable-pip-version-check",
            binary_mode,
            "-d",
            str(dest),
        ]
        + pip_opts.build_args()
    )
    if python_version:
        # Pin the interpreter the artifact has to run on, or pip resolves
        # against the interpreter py2bbr itself happens to run under. That is a
        # property of the machine running py2bbr, not of the target: a CPython
        # 3.12 layer generated from a 3.14 host would be handed a cp314 wheel
        # (and skip packages gated on ``Requires-Python >= 3.13``), so the
        # generated layer would only work on the host that made it.
        cmd += ["--python-version", python_version]
    if platform_tags:
        # Same reasoning for the CPU: pip's platform tags describe the build
        # host, so an x86_64 host is offered the x86_64 artifact for a target
        # that runs aarch64. Only set for a target that *is* the build host's
        # architecture away (see :func:`_pip_platforms`); pip requires
        # ``--only-binary``/``--no-deps`` alongside it, both of which the
        # caller passes. One comma-separated value: pip's ``--platform`` takes
        # either form, and a single argument keeps the command readable in a
        # debug log.
        cmd += ["--platform", ",".join(platform_tags)]
    if no_build_isolation:
        # Build the sdist's metadata with the environment py2bbr runs in rather
        # than letting pip provision an isolated one from the index.
        cmd.append("--no-build-isolation")
    # pip's isolated metadata build unpacks the sdist into $TMPDIR, which may
    # be an inode-starved tmpfs (ENOSPC mid-extraction); route pip's scratch
    # onto the workdir filesystem (dest as parent), where it is deleted with dest.
    scratch = dest / "pip-scratch"
    scratch.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["TMPDIR"] = str(scratch)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


# Base URL the ``pypi`` bitbake class fetches sdists from (scarthgap
# pypi.bbclass: ``PYPI_SRC_URI = .../packages/source/<first>/<pkg>/<pkg>-<pv>.tar.gz``).
_PYPI_FILES_BASE = "https://files.pythonhosted.org/packages/source"


def _public_pypi_sdist_url(filename: str) -> str | None:
    """The exact URL ``inherit pypi`` would fetch for an sdist, or None.

    The class always requests ``.tar.gz`` (``PYPI_PACKAGE_EXT`` default) at
    ``packages/source/<first-char>/<name>/<name>-<version>.tar.gz``; re-deriving
    that URL lets us predict a ``do_fetch`` 404 for packages that only exist on
    a private index (the pyvizionsdk failure). Wheels and undecodable names
    have no derivable public sdist URL.
    """
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".zip"):
        if filename.endswith(ext):
            stem = filename[: -len(ext)]
            break
    else:
        return None
    # Versions never contain '-' (PEP 440), so the LAST '-' is the name/version
    # split: dashed names (`typed-ast`) must survive it intact.
    name, sep, version = stem.rpartition("-")
    if not sep or not name or not version:
        return None
    return f"{_PYPI_FILES_BASE}/{name[0]}/{name}/{name}-{version}.tar.gz"


def _sdist_reachable(url: str, timeout: float = 5.0) -> bool:
    """Whether the public PyPI sdist URL answers a HEAD request with success.

    Any transport error (DNS, TLS, 4xx/5xx, timeout) means the pypi class'
    ``do_fetch`` would fail the same way, so it counts as unreachable.
    """
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _warn_sdist_unavailable(
    requirement: str, proc: subprocess.CompletedProcess[str]
) -> None:
    """Log why the sdist attempt produced no artifact, before wheel fallback.

    pip downloads an sdist by preparing its PEP 517 metadata in an isolated
    build env, which can fail for environmental reasons (e.g. ``ENOSPC`` on a
    full ``/tmp``) even though the sdist exists on the index. A wheel-based
    recipe then silently loses ``[build-system]`` detection (backend,
    build deps, bounds-relax), so the pip diagnostics must not be dropped.
    """
    tail = "\n".join(
        f"  {line}"
        for line in (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
    )
    logger.warning(
        "%s: sdist fetch failed; falling back to wheel (recipe loses "
        "[build-system] detection). pip said:\n%s",
        requirement,
        tail or "  (no pip diagnostics)",
    )


def _wheel_tags(filename: str) -> set[tuple[str, str]] | None:
    """The ``(interpreter, abi)`` tag pairs a wheel filename claims.

    ``None`` when the name is not a parsable wheel filename, so callers can
    let the build report the real problem instead of guessing here.
    """
    try:
        _name, _version, _build, tags = parse_wheel_filename(filename)
    except (InvalidWheelFilename, ValueError):
        return None
    return {(tag.interpreter, tag.abi) for tag in tags}


def _tag_serves(
    target_major: str, target_minor: str, tag: str, abi: str
) -> bool:
    """Whether one wheel tag runs on CPython ``target_major.target_minor``.

    The stable ABI (``abi3``) is the case that needs care: a ``cp38-abi3`` wheel
    is compiled once against the limited API and installs on every later CPython
    3, so it is *not* version-locked to the 3.8 it was named after. Rejecting it
    would be wrong. A version-specific ABI (``cp312``) is locked.
    """
    if tag in (f"py{target_major}", f"py{target_major}{target_minor}"):
        return True
    if tag == f"cp{target_major}{target_minor}":
        return True
    if abi != "abi3":
        return False
    if tag == f"cp{target_major}":
        return True
    # cpXY-abi3: usable from the minor it names onwards.
    if not tag.startswith(f"cp{target_major}"):
        return False
    built = tag[len(f"cp{target_major}") :]
    return built.isdigit() and int(built) <= int(target_minor)


def _wheel_rejection_reason(
    filename: str, yocto: YoctoVersion | None
) -> str | None:
    """Why a wheel cannot serve the target interpreter, or None if it can.

    A wheel is a *build product* for one exact ABI and platform, not source:
    there is nothing left for bitbake to build. It is still the right artifact
    when a package publishes no sdist, because the target runs the same Python
    the wheel was compiled against. Those tags live in the file name
    (``pyvizionsdk-26.9.1-cp38-abi3-manylinux_2_17_x86_64.whl`` is CPython
    3.8 onwards), so the mismatch is decidable here - rather than surfacing
    much later as a hard load error inside the target's Python.

    The *platform* tag is deliberately not rejected: it is a *choosing* problem
    rather than a parsing one. A wheel built for a foreign CPU is answered by
    the download itself - pip is pinned to the target architecture when it
    differs from the build host's (see :func:`_pip_platforms`) - and whatever
    still arrives is judged per target by the build-for-target report (see
    ``py2bbr.targets``), which knows the whole architecture matrix rather than
    only the one arch py2bbr was told about.
    """
    target = getattr(yocto, "python_version", "") or ""
    if not target.startswith("3."):
        return None  # not a CPython 3 target: nothing to compare against
    tags = _wheel_tags(filename)
    if tags is None:
        return None  # unparsable name: let the build report it
    major, _, minor = target.partition(".")
    if any(_tag_serves(major, minor, tag, abi) for tag, abi in tags):
        return None
    offered = ", ".join(sorted(f"{t}-{a}" for t, a in tags)) or "(none)"
    return (
        f"{filename} is built for {offered}, but "
        f"{_target_label(yocto)} runs CPython {target}. "
        "A wheel ships prebuilt extension modules for one exact ABI and cannot "
        "be rebuilt for another interpreter, so no recipe can install it on the "
        "target."
    )


def fetch_source(
    name: str,
    version: str | None,
    pip_opts: PipOptions,
    workdir: Path,
    keep: bool = False,
    *,
    requirement: str | None = None,
    check_public: bool = False,
    yocto: YoctoVersion | None = None,
) -> FetchedSource:
    """Download the package (sdist preferred) into ``workdir``.

    ``requirement``, when given, is used verbatim as the ``pip download``
    requirement instead of ``name``/``version``. This is how the dependency
    closure resolver passes the raw requirement (with its bounds, e.g.
    ``tomli>=1.0.2``) through to pip so the upstream bounds are honoured when
    picking versions.

    With ``check_public=True``, ``public_reachable`` records whether the
    ``pypi`` class' fetch URL for the artifact answers on public PyPI
    (S6/vendoring: packages only on a private index make do_fetch 404, so the
    recipe must ship the sdist in the layer instead).

    Each call downloads into its own fresh subdirectory of
    ``workdir/download``: the closure resolver fetches many packages into the
    same ``workdir``, and ``_pick_artifact`` must only ever see the artifacts
    of *this* requirement (a shared dir would resolve every member to the
    alphabetically-first artifact already present, and a reused per-package
    dir would keep picking a stale version from an earlier run).

    Raises :class:`MetadataNotFoundError` if no artifact is available, and as a
    backstop for a wheel whose ABI tag cannot run on the target (``yocto``
    enables that check; the wheel download itself is already restricted to the
    target's interpreter).
    """
    requirement = requirement or (f"{name}=={version}" if version else name)
    parent = workdir / "download"
    parent.mkdir(parents=True, exist_ok=True)
    dest = Path(tempfile.mkdtemp(prefix=f"{_canonical(name)}-", dir=parent))

    # Prefer sdist so the sha256 matches what `inherit pypi` fetches. Both
    # attempts demand an sdist explicitly: a wheel surfacing here means the
    # sdist is not obtainable, which is what the fallback below (and its
    # warning) exists to report.
    proc = _run_pip_download(requirement, dest, pip_opts, "--no-binary=:all:")
    artifact = _pick_artifact(dest, prefer_sdist=True, sdists_only=True)
    if artifact is None:
        # Retry without pip's build isolation. Building the sdist's metadata
        # in isolation means pip first downloads the declared build backend
        # from the index, which an index that carries only the package itself
        # (or one reached through a narrow credential) cannot serve - and the
        # download then fails even though the sdist is right there. py2bbr's own
        # environment can build the metadata, so the second attempt drops the
        # isolation and gets the sdist instead of falling through to a wheel.
        logger.debug(
            "%s: sdist download failed, retrying without build isolation",
            requirement,
        )
        # ``proc`` deliberately keeps the *first* attempt's output: that is the
        # root cause the retry works around, and it is the one whose diagnostic
        # is specific (a transient tmpfs failure reads very differently from
        # "no sdist published"). The retry is the mitigation, not the news.
        _run_pip_download(
            requirement,
            dest,
            pip_opts,
            "--no-binary=:all:",
            no_build_isolation=True,
        )
        artifact = _pick_artifact(dest, prefer_sdist=True, sdists_only=True)
    if artifact is None:
        _warn_sdist_unavailable(requirement, proc)
        # No sdist: fall back to a wheel, which bitbake installs instead of
        # building. Pin the *target's* runtime - interpreter and CPU - so pip
        # cannot answer with an artifact built for the machine py2bbr runs on.
        pinned = _pip_python_version(yocto)
        pinned_platform = _pip_platforms(yocto)
        proc = _run_pip_download(
            requirement,
            dest,
            pip_opts,
            "--only-binary=:all:",
            python_version=pinned,
            platform_tags=pinned_platform,
        )
        artifact = _pick_artifact(dest, prefer_sdist=False)
    if artifact is None:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        # pip filters wheels by interpreter (and, when pinned, platform) tag,
        # so an index that carries only artifacts for *other* targets reads as
        # "no versions at all". Without the pins named, that is
        # indistinguishable from a typo in the requirement or a wrong index URL.
        restrictions = []
        served_for = []
        if pinned:
            restrictions.append(f"--python-version {pinned}")
            served_for.append(f"CPython {pinned}")
        if pinned_platform:
            arch = getattr(yocto, "platform_machine", "") or "the target's"
            # The full platform list is a wall of ~45 manylinux baselines; name
            # the CPU it is built around, which is what a reader has to check.
            restrictions.append(f"--platform '*_{arch}'")
            served_for.append(f"{arch} wheels")
        hint = ""
        if restrictions:
            hint = (
                f"\n  The wheel download was restricted to "
                f"{' and '.join(served_for)}, the runtime "
                f"{_target_label(yocto)} runs, so this also covers an index that "
                f"serves wheels for other targets only. Check what it offers: "
                f"`pip download {requirement} --only-binary=:all: "
                f"{' '.join(restrictions)} --no-deps <your index flags>`"
            )
        raise MetadataNotFoundError(
            f"could not download {requirement!r} from the configured index(es) "
            f"{' '.join(pip_opts.describe()) or '(default PyPI)'}.\n"
            + "\n".join(f"  {line}" for line in tail)
            + hint
        )
    # Backstop for the case pip's own target-interpreter filter cannot cover:
    # a wheel it considers compatible still has to name an ABI the target runs.
    # Report that now, not as a target-side load failure after a long cross
    # build.
    if artifact.suffix == ".whl" and (
        reason := _wheel_rejection_reason(artifact.name, yocto)
    ):
        raise MetadataNotFoundError(
            f"{_canonical(name)} {requirement}: the index serves no artifact "
            f"the target can use. {reason}\n"
            f"  The download was already restricted to CPython "
            f"{getattr(yocto, 'python_version', '') or '?'} (the version "
            f"{_target_label(yocto)} runs), so this means the upstream project "
            f"publishes neither an sdist nor a wheel for it, or this index is "
            f"missing those files.\n"
            f"  Build and publish an sdist upstream "
            f"(`python -m build --sdist {name}`), or point --index-url at "
            f"an index that serves one. A recipe cannot cross-build a wheel: "
            f"it is a finished artifact, not source."
        )

    public_reachable: bool | None = None
    if check_public:
        url = _public_pypi_sdist_url(artifact.name)
        public_reachable = _sdist_reachable(url) if url is not None else False

    sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    kind = _kind_of(artifact)
    logger.info(
        "fetched %s (%s, %d bytes)",
        artifact.name,
        kind.value,
        artifact.stat().st_size,
    )
    return FetchedSource(
        artifact, kind, sha256, artifact.name, public_reachable
    )


def _pick_artifact(
    dest: Path, prefer_sdist: bool, sdists_only: bool = False
) -> Path | None:
    # ``Path.suffix`` only returns the last suffix (``.gz``), so match the full
    # sdist extension via ``name.endswith``.
    sdists = [
        p
        for p in dest.iterdir()
        if p.name.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".zip"))
    ]
    wheels = [p for p in dest.iterdir() if p.suffix == ".whl"]
    if prefer_sdist and sdists:
        return min(sdists)
    if sdists_only:
        # A ``--no-binary=:all:`` attempt that produced only a wheel did *not*
        # succeed: a wheel carries no pyproject.toml, so accepting it here
        # would skip the fallback and the warning about the lost
        # [build-system] detection.
        return None
    if wheels:
        return min(wheels)
    if sdists:
        return min(sdists)
    return None


def _kind_of(path: Path) -> SourceKind:
    return SourceKind.WHEEL if path.suffix == ".whl" else SourceKind.SDIST


def _read_text_from_archive(path: Path, predicate) -> tuple[str, bytes] | None:
    """Return ``(member_name, data)`` of the first archive member matching."""
    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if predicate(info.filename):
                    return info.filename, zf.read(info.filename)
        return None
    # tar family
    mode = "r:*"
    try:
        with tarfile.open(path, mode) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                if predicate(member.name):
                    fobj = tf.extractfile(member)
                    if fobj is not None:
                        return member.name, fobj.read()
    except tarfile.TarError:
        return None
    return None


def parse_metadata_file(source: FetchedSource) -> email.message.Message:
    """Parse the core metadata (PKG-INFO or METADATA) of a source artifact."""

    def is_metadata(name: str) -> bool:
        # Root-level only (zero or one directory prefix), the same convention
        # ``read_build_system`` / ``find_license_file`` use: an sdist can
        # vendor other packages' dist-info (flit_core 3.12 vendors a
        # ``flit_core/vendor/tomli-1.2.3.dist-info/METADATA`` that sorts ahead
        # of the package's own root ``PKG-INFO`` inside the archive), and
        # the archive member order must not decide which package's metadata
        # this source artifact describes.
        depth = name.count("/") - (1 if name.startswith("./") else 0)
        base = name.rsplit("/", 1)[-1]
        return (base == "PKG-INFO" or base == "METADATA") and depth <= 1

    found = _read_text_from_archive(source.path, is_metadata)
    if found is None:
        raise MetadataError(
            f"no PKG-INFO/METADATA found in {source.filename!r}"
        )
    _, data = found
    return email.message_from_string(data.decode("utf-8", errors="replace"))


def _first(msg: email.message.Message, key: str) -> str:
    """The sole value of ``key``, or ``""`` when absent or repeated.

    A repeated header (e.g. two ``License`` lines after a bad merge) is
    ambiguous, so treat it as missing rather than guess.
    """
    values = msg.get_all(key) or []
    if len(values) != 1:
        return ""
    val = values[0]
    return val.strip() if isinstance(val, str) else ""


def _project_urls(msg: email.message.Message) -> list[tuple[str, str]]:
    urls: list[tuple[str, str]] = []
    for raw in msg.get_all("Project-URL", []) or []:
        label, _, url = raw.partition(",")
        label = label.strip()
        url = url.strip()
        if url:
            urls.append((label or "Homepage", url))
    return urls


def _homepage(
    msg: email.message.Message, project_urls: list[tuple[str, str]]
) -> str:
    hp = _first(msg, "Home-page")
    if hp:
        return hp
    for label, url in project_urls:
        if label.lower() in (
            "homepage",
            "home",
            "source",
            "repository",
            "repo",
        ):
            return url
    return project_urls[0][1] if project_urls else ""


def detect_license(msg: email.message.Message) -> tuple[str, str]:
    """
    Return ``(spdx_id, raw)`` for the package license.

    Prefers the PEP 639 ``License-Expression`` field, then the OSI ``Classifier``,
    then keyword-sniffing of the free-form ``License:`` text.
    """
    expression = _first(msg, "License-Expression")
    if expression:
        return expression, expression

    for classifier in msg.get_all("Classifier", []) or []:
        if classifier.startswith("License :: OSI Approved :: "):
            tail = classifier[len("License :: OSI Approved :: ") :].strip()
            spdx = _CLASSIFIER_TO_SPDX.get(tail)
            if spdx:
                return spdx, classifier

    raw = _first(msg, "License")
    if raw:
        lowered = raw.lower()
        for hint, spdx in _LICENSE_TEXT_HINTS:
            if re.search(hint, lowered):
                return spdx, raw
    return "", _first(msg, "License")


def find_license_file(source: FetchedSource) -> tuple[str, str] | None:
    """
    Locate the license file and return ``(name_relative_to_root, md5)``.

    Only top-level (sdist) / any (wheel) license files are considered. The name
    is relative to the unpacked source root so it can be used directly in
    ``LIC_FILES_CHKSUM = "file://<name>;md5=<md5>"``.
    """
    names = {n.upper() for n in _LICENSE_FILE_NAMES}

    def is_license(member: str) -> bool:
        parts = member.split("/")
        if source.kind is SourceKind.WHEEL:
            base = parts[-1].upper()
            return base.startswith(("LICENSE", "LICENCE", "COPYING"))
        # sdist: only top-level (exactly one directory prefix) license files
        if len(parts) != 2:
            return False
        return parts[1].upper() in names

    found = _read_text_from_archive(source.path, is_license)
    if found is None:
        return None
    member, data = found
    md5 = hashlib.md5(data).hexdigest()
    if source.kind is SourceKind.WHEEL:
        name = member.rsplit("/", 1)[-1]
    else:
        name = member.split("/", 1)[1]
    return name, md5


def _all_matching(path: Path, predicate) -> list[tuple[str, bytes]]:
    """Return every archive member whose name matches ``predicate``."""
    out: list[tuple[str, bytes]] = []
    if path.suffix in (".whl", ".zip"):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if predicate(info.filename):
                    out.append((info.filename, zf.read(info.filename)))
        return out
    try:
        with tarfile.open(path, "r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                if predicate(member.name):
                    fobj = tf.extractfile(member)
                    if fobj is not None:
                        out.append((member.name, fobj.read()))
    except tarfile.TarError:
        pass
    return out


def _parse_toml(data: bytes) -> dict | None:
    try:
        result = tomllib.loads(data.decode("utf-8", errors="replace"))
    except tomllib.TOMLDecodeError:
        return None
    return result if isinstance(result, dict) else None


def read_build_system(source: FetchedSource) -> tuple[str, list[str]]:
    """
    Return ``(build_backend, build_requires)`` from the package's build config.

    The *top-level* ``pyproject.toml`` (exactly one path separator) is
    authoritative. An sdist can bundle many nested ``pyproject.toml`` files
    (test fixtures, docs), so we never fall back to those when a top-level file
    exists - a top-level file without ``[build-system]`` means the implicit
    setuptools backend.
    """
    cands = _all_matching(source.path, lambda n: n.endswith("pyproject.toml"))
    if not cands:
        return "", []
    top = sorted(
        (c for c in cands if c[0].count("/") == 1), key=lambda nv: nv[0]
    )
    if top:
        for _name, data in top:
            parsed = _parse_toml(data)
            if parsed is None:
                continue
            bs = parsed.get("build-system", {}) or {}
            if bs:
                return (
                    bs.get("build-backend", ""),
                    [str(r) for r in bs.get("requires", []) or []],
                )
            return (
                "",
                [],
            )  # authoritative top-level: implicit setuptools backend
    # No top-level file: use the shallowest nested one that declares a backend.
    rest = sorted(
        (c for c in cands if c[0].count("/") != 1),
        key=lambda nv: (nv[0].count("/"), nv[0]),
    )
    for _name, data in rest:
        parsed = _parse_toml(data)
        if parsed is None:
            continue
        bs = parsed.get("build-system", {}) or {}
        if bs:
            return (
                bs.get("build-backend", ""),
                [str(r) for r in bs.get("requires", []) or []],
            )
    return "", []


def marker_env(yocto: YoctoVersion | None) -> dict:
    """
    A PEP 508 evaluation environment approximating the Yocto target.

    We target a CPython on Linux; the Python version and the CPU come from the
    resolved target runtime (the Yocto series, overridable with
    ``--python-version`` / ``--target-arch``) so that version- and
    architecture-gated dependencies (``tomli; python_version < "3.11"``) are
    classified correctly for the image rather than for the machine py2bbr runs
    on.

    ``platform_machine`` is the *target's* architecture and therefore still only
    an approximation: the layer is cross-built, so the real answer belongs to
    bitbake on the real target. Machine-gated requirements are split out (see
    :func:`machine_gated_deps`) and rendered per ``MACHINEOVERRIDES`` token
    rather than resolved against this value.
    """
    env = dict(default_environment())
    env.update(
        {
            "sys_platform": "linux",
            "os_name": "linux",
            "platform_system": "Linux",
            "platform_machine": (
                getattr(yocto, "platform_machine", "") or "x86_64"
            ),
            "implementation_name": "cpython",
            "extra": "",
        }
    )
    if yocto is not None:
        env["python_version"] = yocto.python_version
        env["python_full_version"] = yocto.python_version + ".0"
    return env


#: Backwards-compatible private alias (same module).
_marker_env = marker_env


# --------------------------------------------------------------------------- #
# build-for-target inspection (native code / x86 assumptions)
# --------------------------------------------------------------------------- #

#: Suffix -> language label. A member with one of these suffixes (outside the
#: test/doc trees, see :func:`_is_ignored_member`) means the sdist ships code
#: that has to be compiled for the target, so the recipe's build class must
#: drive the target toolchain. Headers (``.h``) are not evidence on their own:
#: every pure-Python sdist that vendors a C snippet for a build hook would
#: otherwise look like a compiled package.
_NATIVE_SUFFIX_KINDS: dict[str, str] = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".m": "Objective-C",
    ".mm": "Objective-C++",
    ".pyx": "Cython",
    ".pxd": "Cython",
    ".pxi": "Cython",
    ".rs": "Rust",
    ".go": "Go",
    ".f": "Fortran",
    ".f90": "Fortran",
    ".f95": "Fortran",
    ".cu": "CUDA",
}

#: Directories whose contents are never part of the installed/built package.
_IGNORED_DIR_NAMES = frozenset(
    {
        "bench",
        "benchmarks",
        "ci",
        "doc",
        "docs",
        "example",
        "examples",
        "test",
        "test_data",
        "testing",
        "tests",
    }
)

#: ``[build-system].requires`` entries that imply compiled code even when the
#: sdist carries no source of that language (a generated one, typically).
_NATIVE_BUILD_REQS = frozenset(
    {
        "cffi",
        "cffi-rust",
        "cython",
        "maturin",
        "pybind11",
        "pyo3",
        "setuptools-rust",
    }
)

#: Build-config tokens that pin the build to an x86 CPU. Deliberately limited
#: to *flag-shaped* tokens: a bare ``x86_64``/``amd64``/``i686`` string in a
#: ``pyproject.toml`` is nearly always a cibuildwheel image name or a guarded
#: ``platform_machine`` branch, not a hard-coded build, and flagging those
#: buries the real hazard. A guarded machine test is fine; ``-march=native`` or
#: ``-mavx2`` reaching the ARM compiler is not. Their presence does not prove
#: the package is x86-only, so they only raise a RISK for non-x86 targets - with
#: the file named, so the user can check it.
_HOST_ISA_TOKENS = (
    "-march=",
    "-mtune=",
    "-mcpu=",
    "--cpu=",
    "-mavx",
    "-msse",
    "-mfma",
    "-mno-sse",
    "-mno-avx",
    "avx2",
    "avx512",
    "sse4_2",
    "sse4.2",
)

_HOST_ISA_FILES = ("setup.py", "pyproject.toml", "setup.cfg", "meson.build")

#: Cap on evidence strings so a huge vendored C tree cannot flood the report.
_MAX_EVIDENCE = 4


def _member_names(path: Path) -> list[str]:
    """Every member name in the artifact (names only, no extraction)."""
    if path.suffix in (".whl", ".zip"):
        try:
            with zipfile.ZipFile(path) as zf:
                return zf.namelist()
        except (OSError, zipfile.BadZipFile):
            return []
    try:
        with tarfile.open(path, "r:*") as tf:
            return tf.getnames()
    except (OSError, tarfile.TarError):
        return []


def _is_ignored_member(name: str) -> bool:
    """Whether a member lives in a test/doc/example tree or is a test file.

    sdists routinely ship the CPython C-API test suite (``Lib/test/``) or a
    Cython parser fixture; neither is compiled into the wheel.
    """
    parts = PurePosixPath(name).parts
    if len(parts) < 2:
        return True
    for part in parts[1:-1]:
        if part.lower() in _IGNORED_DIR_NAMES:
            return True
    stem = parts[-1].rsplit(".", 1)[0].lower()
    return stem.startswith(("test_", "conftest")) or stem.endswith(
        ("_test", "_tests")
    )


def detect_native_code(
    source: FetchedSource, build_requires: list[str] = ()
) -> tuple[bool, list[str]]:
    """Whether the artifact ships code that must be compiled, plus evidence.

    Evidence strings name the language and an example path (or the build
    requirement that pulls in a compiler). A pure-Python sdist yields
    ``(False, [])``; the recipe then needs no target toolchain and the target
    report has nothing to warn about.
    """
    found: dict[str, str] = {}
    for name in _member_names(source.path):
        kind = _NATIVE_SUFFIX_KINDS.get(PurePosixPath(name).suffix.lower())
        if kind is None or _is_ignored_member(name):
            continue
        found.setdefault(kind, name)
    evidence = [
        f"{kind} source ({path})" for kind, path in sorted(found.items())
    ]

    # ``[build-system].requires`` entries arrive as full PEP 508 requirements
    # (``cython>=3.1``), so compare the parsed project name, not the raw string.
    for req in build_requires:
        try:
            name = _canonical(Requirement(req).name)
        except InvalidRequirement:
            continue
        if name in _NATIVE_BUILD_REQS:
            evidence.append(f"build requires {name}")

    if not evidence:
        for _name, data in _all_matching(
            source.path,
            lambda n: n.count("/") == 1 and n.endswith("setup.py"),
        ):
            if b"ext_modules" in data or b"Extension(" in data:
                evidence.append("setup.py declares ext_modules")
                break

    evidence = evidence[:_MAX_EVIDENCE]
    return bool(evidence), evidence


def detect_host_assumptions(source: FetchedSource) -> list[str]:
    """Top-level build config lines that pin the build to an x86 host.

    ``"<file>: <tokens>"`` per file, e.g. ``"setup.py: -march=, -mavx"``. Only
    consulted for non-x86 targets, where such a flag either fails the compile
    or silently disables an optimization.
    """
    out: list[str] = []
    for name, data in _all_matching(
        source.path,
        lambda n: (
            n.count("/") == 1 and PurePosixPath(n).name in _HOST_ISA_FILES
        ),
    ):
        text = data.decode("utf-8", errors="replace").lower()
        hits = [token for token in _HOST_ISA_TOKENS if token in text]
        if not hits:
            continue
        out.append(f"{PurePosixPath(name).name}: {', '.join(hits[:3])}")
    return out[:_MAX_EVIDENCE]


def machine_gated_deps(
    requires_dist: list[str], yocto: YoctoVersion | None
) -> tuple[list[MachineDep], list[str]]:
    """Split machine-specific requirements from the arch-independent ones.

    ``foo; platform_machine == "aarch64"`` must not be resolved here: the layer
    is cross-built, so the answer depends on the *target*, not on the host that
    runs bitbake. Such entries are returned as :class:`MachineDep` (with the
    target list they apply to) and the recipe renders them as machine-conditional
    ``RDEPENDS:${PN}:append:<token>``; everything else is returned unchanged for
    the normal marker classification.

    A requirement no known target satisfies is dropped entirely (e.g.
    ``win32``-only machine values cannot appear in a POSIX ``platform_machine``).
    """
    base = _marker_env(yocto)
    # Several matrix entries share one platform_machine (aarch64 covers
    # ZynqMP, Versal and the generic Cortex-A targets), so dedupe while keeping
    # the matrix order: the recipe groups the deps per MACHINEOVERRIDES token.
    machines_all = tuple(
        dict.fromkeys(t.platform_machine for t in KNOWN_TARGETS)
    )
    gated: list[MachineDep] = []
    plain: list[str] = []
    for raw in requires_dist:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            plain.append(raw)
            continue
        marker = req.marker
        if marker is None or "platform_machine" not in str(marker):
            plain.append(raw)
            continue
        machines = tuple(
            m
            for m in machines_all
            if marker.evaluate({**base, "platform_machine": m})
        )
        if not machines:
            continue
        gated.append(
            MachineDep(
                name=req.name,
                requirement=raw,
                marker=str(marker),
                machines=machines,
            )
        )
    return gated, plain


def classify_requires_dist(
    requires_dist: list[str], yocto: YoctoVersion | None
) -> tuple[list[Dependency], list[Dependency], list[Dependency]]:
    """
    Split ``Requires-Dist`` into (runtime, build, dropped).

    Rules:
    * anything gated on an ``extra ==`` marker is optional -> dropped
    * otherwise the marker is evaluated against the Yocto target; if it does not
      hold on this target (e.g. Windows-only, or a lower Python) -> dropped
    * unconditional (or target-satisfied) deps -> runtime
    """
    env = _marker_env(yocto)
    runtime: list[Dependency] = []
    dropped: list[Dependency] = []

    for raw in requires_dist:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            dropped.append(
                Dependency(
                    name=raw,
                    kind=DepKind.DROPPED,
                    requirement=raw,
                    reason="unparseable requirement",
                )
            )
            continue

        name = req.name
        marker = req.marker
        if marker is not None and "extra" in str(marker):
            dropped.append(
                Dependency(
                    name=name,
                    kind=DepKind.DROPPED,
                    requirement=raw,
                    specifier=str(req.specifier),
                    reason=f"optional extra ({marker})",
                )
            )
            continue

        if marker is not None:
            try:
                satisfied = marker.evaluate(env)
            except:  # noqa: E722
                satisfied = True  # be conservative: keep if we cannot evaluate
            if not satisfied:
                dropped.append(
                    Dependency(
                        name=name,
                        kind=DepKind.DROPPED,
                        requirement=raw,
                        specifier=str(req.specifier),
                        reason=f"marker not satisfied on target ({marker})",
                    )
                )
                continue

        runtime.append(
            Dependency(
                name=name,
                kind=DepKind.RUNTIME,
                requirement=raw,
                specifier=str(req.specifier),
            )
        )

    return runtime, [], dropped


def _build_deps_from_requires(requires: list[str]) -> list[Dependency]:
    out: list[Dependency] = []
    for raw in requires:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            continue
        out.append(
            Dependency(
                name=req.name,
                kind=DepKind.BUILD,
                requirement=raw,
                specifier=str(req.specifier),
                marker=str(req.marker) if req.marker is not None else "",
            )
        )
    return out


def build_metadata(
    name: str,
    version: str | None,
    pip_opts: PipOptions,
    workdir: Path,
    yocto: YoctoVersion | None = None,
    keep: bool = False,
    *,
    requirement: str | None = None,
    check_public: bool = False,
) -> tuple[PackageMetadata, FetchedSource]:
    """Fetch + parse a package into a fully-populated :class:`PackageMetadata`.

    ``check_public`` forwards to :func:`fetch_source`; an sdist that is not
    reachable at the ``pypi`` class' public URL marks the metadata ``vendored``
    so the recipe ships the sdist in the layer instead of inheriting ``pypi``.
    """
    source = fetch_source(
        name,
        version,
        pip_opts,
        workdir,
        keep=keep,
        requirement=requirement,
        check_public=check_public,
        yocto=yocto,
    )
    msg = parse_metadata_file(source)

    meta_name = _first(msg, "Name") or name
    meta_version = _first(msg, "Version") or (version or "")
    summary = _first(msg, "Summary")
    description = _first(msg, "Description") or summary
    project_urls = _project_urls(msg)
    homepage = _homepage(msg, project_urls)
    license_spdx, license_raw = detect_license(msg)
    requires_python = _first(msg, "Requires-Python")
    requires_dist = list(msg.get_all("Requires-Dist", []) or [])

    backend, build_requires = read_build_system(source)
    lic = find_license_file(source)
    license_file, license_md5 = lic if lic else (None, None)

    # Machine-gated requirements are resolved by bitbake on the real target,
    # not here (the host that runs bitbake is not the target).
    machine_deps, plain_requires = machine_gated_deps(requires_dist, yocto)
    runtime, _build_from_rd, dropped = classify_requires_dist(
        plain_requires, yocto
    )
    build_deps = _build_deps_from_requires(build_requires)
    has_native_code, native_evidence = detect_native_code(
        source, build_requires
    )
    host_assumptions = detect_host_assumptions(source)

    # The recipe must fetch *exactly* the artifact whose sha256 is recorded. The
    # pypi class can only do that for an sdist it can reach at its derived
    # public URL; anything else has to be vendored into the layer as a file://
    # SRC_URI. That covers two cases:
    #  - an sdist on a private index (its derived URL 404s), and
    #  - a wheel, which the pypi class never fetches - it would request the
    #    .tar.gz instead, so do_fetch either 404s (no sdist) or dies on
    #    "sha256 mismatch" (one exists) while the recipe claims a different
    #    artifact than the one it was hashed from.
    # None means the public URL was not checked, which is equally not a proof
    # that it resolves, so only an explicit True leaves the fetch to pypi.
    vendored = source.public_reachable is not True
    if vendored:
        logger.info(
            "%s: not fetchable by the pypi class - recipe vendors the %s",
            source.filename,
            "sdist" if source.kind is SourceKind.SDIST else "wheel",
        )
    elif source.public_reachable is True:
        logger.debug("%s: reachable on public PyPI", source.filename)
    logger.debug(
        "%s==%s: license=%s backend=%s runtime_deps=%d build_deps=%d "
        "native_code=%s machine_deps=%d",
        meta_name,
        meta_version,
        license_spdx or "-",
        backend or "setup.py",
        len(runtime),
        len(build_deps),
        "yes" if has_native_code else "no",
        len(machine_deps),
    )

    meta = PackageMetadata(
        name=meta_name,
        canonical_name=_canonical(meta_name),
        version=meta_version,
        summary=summary,
        description=description,
        homepage=homepage,
        license=license_spdx,
        license_raw=license_raw,
        license_file=license_file,
        license_md5=license_md5,
        requires_python=requires_python,
        project_urls=project_urls,
        source_kind=source.kind,
        source_filename=source.filename,
        source_sha256=source.sha256,
        vendored=vendored,
        build_backend=backend,
        requires_dist=list(requires_dist),
        has_native_code=has_native_code,
        native_code_evidence=native_evidence,
        host_assumptions=host_assumptions,
        runtime_deps=runtime,
        build_deps=build_deps,
        dropped_deps=dropped,
        machine_deps=machine_deps,
        resolved_from=(
            pip_opts.index_url
            or (
                pip_opts.extra_index_urls[0]
                if pip_opts.extra_index_urls
                else "https://pypi.org"
            )
        ),
        index_flags=pip_opts.describe(),
    )
    return meta, source


def _canonical(name: str) -> str:
    """PEP 503 canonical form (lowercase, runs of -_. collapsed to -)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def resolve_closure(
    top: PackageMetadata,
    builder: Callable[[Dependency], tuple[PackageMetadata, FetchedSource]],
    *,
    skip: frozenset[str] = frozenset(),
    max_depth: int | None = None,
    workers: int = 1,
) -> tuple[list[PackageMetadata], list[str]]:
    """Resolve the transitive **runtime** closure of ``top``.

    BFS over ``runtime_deps``; each member is fetched through ``builder`` (the
    network seam; tests inject a fake) with its raw requirements (bounds
    intact, e.g. ``tomli>=1.0.2``) passed through verbatim so upstream
    bounds are honoured when picking versions.

    With ``workers > 1`` fetches run in parallel through one shared thread
    pool. A member's dependencies are unknowable until its own fetch has
    completed, so instead of waiting for the whole BFS level to finish (a
    level barrier that serialises deep or uneven dependency chains behind the
    slowest member of each level), each completed fetch immediately hands its
    newly discovered dependencies to the pool. ``builder`` must be
    thread-safe — the fetch path already is: every call downloads into its
    own fresh subdirectory of ``workdir``.

    Closure order: ``closure[0]`` is always ``top`` (the kas target; bitbake
    follows RDEPENDS transitively) and every other member appears after the
    member that discovered it, in discovery order — so chain and
    single-parent graphs match a sequential BFS exactly. With ``workers > 1``
    members discovered by two *concurrent* parents can interleave at the same
    depth; member order has no effect on the generated layer's build
    correctness. Members are deduplicated by canonical name (cycle-safe);
    ``skip`` is a set of canonical names to drop (the ``--skip-dep`` escape
    hatch for distro-provided packages, e.g. ``numpy``); ``max_depth`` limits
    recursion depth (``None`` = unlimited).
    """
    from concurrent.futures import (
        FIRST_COMPLETED,
        Future,
        ThreadPoolExecutor,
        wait,
    )

    visited: set[str] = {_canonical(top.name)}
    skipped: set[str] = set()
    closure: list[PackageMetadata] = [top]
    warnings: list[str] = []

    def _discover(
        meta: PackageMetadata, depth: int
    ) -> list[tuple[str, Dependency, int]]:
        """The unvisited, unskipped deps of ``meta`` at ``depth + 1``."""
        if max_depth is not None and depth >= max_depth:
            return []
        newly: list[tuple[str, Dependency, int]] = []
        for dep in meta.runtime_deps:
            canonical = _canonical(dep.name)
            if canonical in visited:
                continue
            if canonical in skip:
                skipped.add(canonical)
                continue
            visited.add(canonical)
            newly.append((canonical, dep, depth + 1))
        return newly

    if workers <= 1:
        # Sequential FIFO BFS: deterministic, same order every run.
        frontier: list[tuple[PackageMetadata, int]] = [(top, 0)]
        while frontier:
            depth = frontier[0][1]
            batch: list[Dependency] = [
                dep
                for meta, _depth in frontier
                for _c, dep, _d in _discover(meta, _depth)
            ]
            if not batch:
                break
            fetched = [builder(dep) for dep in batch]
            members = [member for member, _source in fetched]
            closure.extend(members)
            frontier = [(member, depth + 1) for member in members]
    else:
        # Greedy parallel: a completed fetch releases its deps into the pool
        # immediately, so a deep chain keeps every worker busy instead of
        # stalling behind the slowest member of each BFS level.
        seq = 0
        discovered: list[Future] = []  # submission order (== discovery order)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            # future -> (submission sequence, the member's own depth)
            pending: dict[Future, tuple[int, int]] = {}

            def _submit(dep: Dependency, own_depth: int) -> None:
                nonlocal seq
                seq += 1
                fut = pool.submit(builder, dep)
                pending[fut] = (seq, own_depth)
                discovered.append(fut)

            for _c, dep, depth in _discover(top, 0):
                _submit(dep, depth)

            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for fut in sorted(done, key=lambda f: pending[f][0]):
                    member, _source = fut.result()
                    own_depth = pending.pop(fut)[1]
                    for _name, dep, depth in _discover(member, own_depth):
                        _submit(dep, depth)

            closure.extend(fut.result()[0] for fut in discovered)

    for canonical in sorted(skip):
        if canonical not in visited and canonical not in skipped:
            warnings.append(
                f"--skip-dep {canonical}: not in the runtime closure"
            )
    return closure, warnings


def default_yocto() -> YoctoVersion:
    from py2bbr import yocto  # local import to avoid a cycle

    return yocto.resolve_yocto_version(DEFAULT_YOCTO_VERSION)
