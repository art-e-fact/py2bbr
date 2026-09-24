"""Fetch and parse authoritative package metadata.

Current approach
----------------
1. ``pip download`` the package into an isolated temp dir, honouring the user's
   pip index flags (``--index-url`` / ``--extra-index-url`` / ``--trusted-host``
   / arbitrary ``--pip-arg``). This works against PyPI *and* private indexes.
2. Prefer the **sdist** (what ``inherit pypi`` fetches, so its sha256 is the
   correct ``SRC_URI[sha256sum]``); fall back to a **wheel** if sdist is absent.
3. Parse ``PKG-INFO`` / ``METADATA`` with the stdlib ``email`` parser.
4. Read ``pyproject.toml`` ``[build-system]`` for the backend + build deps.
5. Locate the license file and compute its MD5 (for ``LIC_FILES_CHKSUM``).
6. Classify ``Requires-Dist`` into runtime / build / dropped using ``packaging``
   marker evaluation against the target Yocto Python version.
"""

from __future__ import annotations

import email
import email.message
import hashlib
import logging
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
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement

from py2bbr.errors import MetadataError, MetadataNotFoundError
from py2bbr.model import (
    DEFAULT_YOCTO_VERSION,
    Dependency,
    DepKind,
    PackageMetadata,
    SourceKind,
    YoctoVersion,
)

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


def _run_pip_download(
    requirement: str, dest: Path, pip_opts: PipOptions, binary_mode: str
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
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
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


def fetch_source(
    name: str,
    version: str | None,
    pip_opts: PipOptions,
    workdir: Path,
    keep: bool = False,
    *,
    requirement: str | None = None,
    check_public: bool = False,
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

    Raises :class:`MetadataNotFoundError` if no artifact is available.
    """
    requirement = requirement or (f"{name}=={version}" if version else name)
    parent = workdir / "download"
    parent.mkdir(parents=True, exist_ok=True)
    dest = Path(tempfile.mkdtemp(prefix=f"{_canonical(name)}-", dir=parent))

    # Prefer sdist so the sha256 matches what `inherit pypi` fetches.
    proc = _run_pip_download(requirement, dest, pip_opts, "--no-binary=:all:")
    artifact = _pick_artifact(dest, prefer_sdist=True)
    if artifact is None:
        # Fall back to wheel (sdist-only metadata not available).
        _run_pip_download(requirement, dest, pip_opts, "--only-binary=:all:")
        artifact = _pick_artifact(dest, prefer_sdist=False)
    if artifact is None:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        raise MetadataNotFoundError(
            f"could not download {requirement!r} from the configured index(es) "
            f"{' '.join(pip_opts.describe()) or '(default PyPI)'}.\n"
            + "\n".join(f"  {line}" for line in tail)
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


def _pick_artifact(dest: Path, prefer_sdist: bool) -> Path | None:
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
        base = name.rsplit("/", 1)[-1]
        return base == "PKG-INFO" or base == "METADATA"

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


def _marker_env(yocto: YoctoVersion | None) -> dict:
    """
    A PEP 508 evaluation environment approximating the Yocto target.

    We target a CPython on Linux; the Python version comes from the Yocto series
    so that version-gated dependencies (``tomli; python_version < "3.11"``) are
    classified correctly for the default Scarthgap/3.12 target.
    """
    env = dict(default_environment())
    env.update(
        {
            "sys_platform": "linux",
            "os_name": "linux",
            "platform_system": "Linux",
            "platform_machine": "x86_64",
            "implementation_name": "cpython",
            "extra": "",
        }
    )
    if yocto is not None:
        env["python_version"] = yocto.python_version
        env["python_full_version"] = yocto.python_version + ".0"
    return env


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

    runtime, _build_from_rd, dropped = classify_requires_dist(
        requires_dist, yocto
    )
    build_deps = _build_deps_from_requires(build_requires)

    # Only sdists are fetched by the pypi class, so only they can hit its URL
    # 404; a wheel's recipe keeps its own fetch semantics.
    vendored = (
        source.kind is SourceKind.SDIST and source.public_reachable is False
    )
    if vendored:
        logger.info(
            "%s: not on public PyPI - recipe vendors the sdist",
            source.filename,
        )
    elif source.public_reachable is True:
        logger.debug("%s: reachable on public PyPI", source.filename)
    logger.debug(
        "%s==%s: license=%s backend=%s runtime_deps=%d build_deps=%d",
        meta_name,
        meta_version,
        license_spdx or "-",
        backend or "setup.py",
        len(runtime),
        len(build_deps),
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
        runtime_deps=runtime,
        build_deps=build_deps,
        dropped_deps=dropped,
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
) -> tuple[list[PackageMetadata], list[str]]:
    """Resolve the transitive **runtime** closure of ``top``.

    BFS over ``runtime_deps``; each member is fetched through ``builder`` (the
    network seam; tests inject a fake) with its raw requirements (bounds
    intact, e.g. ``tomli>=1.0.2``) passed through verbatim so upstream
    bounds are honoured when picking versions.

    Returns ``(closure, warnings)`` with ``closure[0]`` == ``top`` (top
    recipe stays the kas target; bitbake follows RDEPENDS transitively).
    Members are deduplicated by canonical name (cycle-safe); ``skip`` is a set
    of canonical names to drop (the ``--skip-dep`` escape hatch for
    distro-provided packages, e.g. ``numpy``); ``max_depth`` limits recursion
    depth (``None`` = unlimited).
    """
    from collections import deque

    visited: set[str] = {_canonical(top.name)}
    skipped: set[str] = set()
    closure: list[PackageMetadata] = [top]
    queue: deque[tuple[PackageMetadata, int]] = deque([(top, 0)])
    warnings: list[str] = []

    while queue:
        meta, depth = queue.popleft()
        if max_depth is not None and depth >= max_depth:
            continue
        for dep in meta.runtime_deps:
            canonical = _canonical(dep.name)
            if canonical in visited:
                continue
            if canonical in skip:
                skipped.add(canonical)
                continue
            visited.add(canonical)
            member, _source = builder(dep)
            closure.append(member)
            queue.append((member, depth + 1))

    for canonical in sorted(skip):
        if canonical not in visited and canonical not in skipped:
            warnings.append(
                f"--skip-dep {canonical}: not in the runtime closure"
            )
    return closure, warnings


def default_yocto() -> YoctoVersion:
    from py2bbr import yocto  # local import to avoid a cycle

    return yocto.resolve_yocto_version(DEFAULT_YOCTO_VERSION)
