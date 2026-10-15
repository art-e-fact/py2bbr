import email
import email.message
import hashlib
import logging
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import InvalidVersion, Version

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

# Map PEP 561 "License :: OSI Approved :: X" to SPDX ids.
# Did not find a package to do this, likely because messy.
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

# Keyword hints used to get a license from `License:` text.
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

# Common licence file names
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
    """
    pip options forwarded to `pip download`.
    """

    index_url: str | None = None
    extra_index_urls: list[str] = field(default_factory=list)
    trusted_hosts: list[str] = field(default_factory=list)
    # Extra raw arguments appended verbatim (e.g. ``--proxy``, ``--retries``).
    pip_args: list[str] = field(default_factory=list)
    # Timeout for pip network operations (0 = pip default).
    timeout: int = 0  # seconds

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

    def index_flags(self) -> list[str]:
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
    path: Path
    kind: SourceKind
    sha256: str
    filename: str
    # True: sdist reachable on PyPi
    # False: Process as vendored file (includes wheels)
    # None: Check deactivated
    public_reachable: bool | None = None


def _pip_executable() -> list[str]:
    import importlib.util

    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip"]
    for candidate in ("pip3", "pip"):
        path = shutil.which(candidate)
        if path:
            return [path]
    return [sys.executable, "-m", "pip"]


def _pip_python_version(yocto: YoctoVersion | None) -> str | None:
    """
    Get and normalise the Python version for the target Yocto.

    This logic is likely to move to src/py2bbr/model.py in the
    YoctoVersion class.
    """
    target = getattr(yocto, "python_version", "") or ""
    parts = target.split(".")
    if len(parts) < 2 or parts[0] != "3" or not parts[1].isdigit():
        return None
    return f"3.{int(parts[1])}"


_MANYLINUX_FIRST_MINOR = 5  # glibc 2.5, the oldest manylinux1 baseline
_MANYLINUX_LAST_MINOR = 45  # past every glibc released so far

_LEGACY_FAMILIES = (
    "manylinux1",
    "manylinux2010",
    "manylinux2014",
    "musllinux_1_1",
    "musllinux_1_2",
    "linux",
)


def _pip_platforms(yocto: YoctoVersion | None) -> list[str]:
    """
    The `--platform` tags a wheel download is restricted to.

    Arch families like manylinux, etc.
    """
    target = getattr(yocto, "platform_machine", "") or ""
    host = platform.machine().strip().lower()
    if not target or not host or target == host:
        return []

    families = [
        f"manylinux_2_{minor}"
        for minor in range(_MANYLINUX_FIRST_MINOR, _MANYLINUX_LAST_MINOR + 1)
    ]
    families.extend(_LEGACY_FAMILIES)
    return [f"{family}_{target}" for family in families]


def _target_label(yocto: YoctoVersion | None) -> str:
    """Name the target a constraint came from, e.g. `Yocto 5.0 (scarthgap)`."""
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
        cmd += ["--python-version", python_version]
    if platform_tags:
        for tag in platform_tags:
            cmd += ["--platform", tag]
    if no_build_isolation:
        cmd.append("--no-build-isolation")
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


# Base URL the `pypi` bitbake class gets sdists from.
_PYPI_FILES_BASE = "https://files.pythonhosted.org/packages/source"


def _public_pypi_sdist_url(filename: str) -> str | None:
    """The exact URL ``inherit pypi`` would fetch for an sdist, or None."""
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
    """Check if a URL answers a HEAD request successfully."""
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _warn_sdist_unavailable(
    requirement: str, proc: subprocess.CompletedProcess[str]
) -> None:
    """Log why the sdist attempt produced no artifact."""
    tail = "\n".join(
        f"  {line}"
        for line in (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
    )
    logger.warning(
        "%s: sdist fetch failed; falling back to wheel (the wheel carries "
        "no [build-system] itself; the project's repository is consulted next "
        "when the metadata names one). pip said:\n%s",
        requirement,
        tail or "  (no pip diagnostics)",
    )


def _wheel_tags(filename: str) -> set[tuple[str, str]] | None:
    """The `(interpreter, abi)` tag pairs a wheel filename claims."""
    try:
        _name, _version, _build, tags = parse_wheel_filename(filename)
    except (InvalidWheelFilename, ValueError):
        return None
    return {(tag.interpreter, tag.abi) for tag in tags}


def _tag_serves(
    target_major: str, target_minor: str, tag: str, abi: str
) -> bool:
    """Whether one wheel tag runs on CPython `target_major.target_minor`."""
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
    """Why a wheel cannot serve the target interpreter, or None if it can."""
    target = getattr(yocto, "python_version", "") or ""
    if not target.startswith("3."):
        return None  # not a CPython 3 target
    tags = _wheel_tags(filename)
    if tags is None:
        return None  # unparsable name
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
    """Download the package (sdist preferred) into `workdir`."""
    requirement = requirement or (f"{name}=={version}" if version else name)
    parent = workdir / "download"
    parent.mkdir(parents=True, exist_ok=True)
    dest = Path(tempfile.mkdtemp(prefix=f"{_canonical(name)}-", dir=parent))

    proc = _run_pip_download(requirement, dest, pip_opts, "--no-binary=:all:")
    artifact = _pick_artifact(dest, sdists_only=True)
    if artifact is None:
        logger.debug(
            "%s: sdist download failed, retrying without build isolation",
            requirement,
        )
        _run_pip_download(
            requirement,
            dest,
            pip_opts,
            "--no-binary=:all:",
            no_build_isolation=True,
        )
        artifact = _pick_artifact(dest, sdists_only=True)
    if artifact is None:
        # No sdist available, report and move on
        _warn_sdist_unavailable(requirement, proc)
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
        artifact = _pick_artifact(dest)
    if artifact is None:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        restrictions = []
        served_for = []
        if pinned:
            restrictions.append(f"--python-version {pinned}")
            served_for.append(f"CPython {pinned}")
        if pinned_platform:
            arch = getattr(yocto, "platform_machine", "") or "the target's"
            served_for.append(f"{arch} wheels")
        hint = ""
        if restrictions:
            verdict = (
                f" If this command finds a wheel, the index carries nothing "
                f"for {arch}."
                if pinned_platform
                else ""
            )
            hint = (
                f"\n  The wheel download was restricted to "
                f"{' and '.join(served_for)}, the runtime "
                f"{_target_label(yocto)} runs, so this also covers an index that "
                f"serves wheels for other targets only.{verdict} Check it with: "
                f"`pip download {requirement} --only-binary=:all: "
                f"{' '.join(restrictions)} --no-deps <your index flags>`"
            )
        raise MetadataNotFoundError(
            f"could not download {requirement!r} from the configured index(es) "
            f"{' '.join(pip_opts.index_flags()) or '(default PyPI)'}.\n"
            + "\n".join(f"  {line}" for line in tail)
            + hint
        )
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


def _pick_artifact(dest: Path, sdists_only: bool = False) -> Path | None:
    """
    Choose a package distribution artifact based on priorities.

    Note that we default to prefer sdist distributions, as the most
    complete sources for py2bbr's purpose.
    """
    # `Path.suffix` only returns the last suffix, so using `name.endswith`.
    sdists = [
        p
        for p in dest.iterdir()
        if p.name.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".zip"))
    ]

    # Most frequent and desired exit
    if sdists:
        return min(sdists)

    # Gate to decide whether we continue
    if sdists_only:
        return None

    # Wheels if any
    wheels = [p for p in dest.iterdir() if p.suffix == ".whl"]
    if wheels:
        return min(wheels)

    return None


def _kind_of(path: Path) -> SourceKind:
    return SourceKind.WHEEL if path.suffix == ".whl" else SourceKind.SDIST


def _read_text_from_archive(path: Path, predicate) -> tuple[str, bytes] | None:
    """Return `(member_name, data)` of the first archive member matching."""
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
    """The sole value of `key`, or `""` when absent or repeated."""
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
    """Return `(spdx_id, raw)` for the package license."""
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
    """Locate the license file and return `(name_relative_to_root, md5)`."""
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


def read_build_system(source: FetchedSource) -> tuple[str, list[str]] | None:
    """
    Return `(build_backend, build_requires)` from the package's build config,
    or `None` when the artifact has no `pyproject.toml`.
    """
    cands = _all_matching(source.path, lambda n: n.endswith("pyproject.toml"))
    if not cands:
        return None
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
            )
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


_VCS_RAW_URLS: dict[str, str] = {
    "github.com": "https://raw.githubusercontent.com/{path}/HEAD/{name}",
    "gitlab.com": "https://gitlab.com/{path}/-/raw/HEAD/{name}",
    "bitbucket.org": "https://bitbucket.org/{path}/raw/HEAD/{name}",
}

_VCS_URL_LABELS = frozenset(
    {
        "repository",
        "source",
        "source code",
        "code",
        "github",
        "gitlab",
        "vcs",
        "git",
        "repo",
        "bugs",
    }
)

_VCS_SUBPAGE_MARKERS = (
    "/-/tree/",
    "/-/blob/",
    "/-/src/",
    "/-/issues/",
    "/-/merge_requests/",
    "/-/commits/",
    "/-/releases/",
    "/tree/",
    "/blob/",
    "/src/",
    "/issues/",
    "/pull/",
    "/commits/",
)


def _vcs_repo_candidates(
    project_urls: list[tuple[str, str]], homepage: str = ""
) -> list[tuple[str, str]]:
    """``(host, "owner/repo")`` for each repository URL, best first.

    Deduplicated, and in the order the labels in :data:`_VCS_URL_LABELS` are
    trusted, so a ``Project-URL: Source`` entry wins over a homepage that only
    looks like a repository.
    """
    labelled: list[tuple[int, str]] = []
    order = {label: rank for rank, label in enumerate(_VCS_URL_LABELS)}
    for label, url in project_urls:
        normalised = label.strip().lower()
        if normalised in order:
            labelled.append((order[normalised], url))
    if homepage:
        labelled.append((len(order) + 1, homepage))
    labelled.sort(key=lambda item: item[0])

    out: list[tuple[str, str]] = []
    for _rank, url in labelled:
        repo = _vcs_repo(url)
        if repo is not None and repo not in out:
            out.append(repo)
    return out


def _vcs_repo(url: str) -> tuple[str, str] | None:
    """`(host, "owner/repo")` for a repository URL, or None if unrecognised."""
    url = url.strip()
    url = url.removeprefix("git+")
    # ``#egg=``/``#subdirectory=`` fragments and any trailing path/query.
    url = url.split("#", 1)[0].split("?", 1)[0]
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1].split(":")[0].lower()
    if not host:
        return None
    match = next(
        (
            known
            for known in _VCS_RAW_URLS
            if host == known or host.endswith("." + known)
        ),
        None,
    )
    if match is None:
        return None
    path = parts.path.strip("/")
    path = path.split("@", 1)[0]
    path = path.removesuffix(".git")
    for marker in _VCS_SUBPAGE_MARKERS:
        index = path.find(marker)
        if index != -1:
            path = path[:index]
    segments = [s for s in path.split("/") if s]
    if len(segments) < 2:
        return None
    return match, "/".join(segments)


def _fetch_bytes(url: str, timeout: float) -> bytes | None:
    """GET `url` and return the body, or None on any failure."""
    request = urllib.request.Request(url, headers={"User-Agent": "py2bbr"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                return None
            return response.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None


def build_system_from_vcs(
    project_urls: list[tuple[str, str]],
    homepage: str = "",
    filename: str = "pyproject.toml",
    timeout: float = 5.0,
) -> tuple[str, list[str], str] | None:
    """
    The project's declared `[build-system]`, read from its own repository.

    Returns `(build_backend, build_requires, provenance)`, or None when no
    candidate repository has a project `filename`.
    """
    for host, path in _vcs_repo_candidates(project_urls, homepage):
        url = _VCS_RAW_URLS[host].format(path=path, name=filename)
        data = _fetch_bytes(url, timeout)
        if data is None:
            logger.debug("no %s at %s", filename, url)
            continue
        parsed = _parse_toml(data)
        if parsed is None:
            logger.debug("unparsable %s at %s", filename, url)
            continue
        build_system = parsed.get("build-system", {}) or {}
        if not build_system:
            logger.debug("no [build-system] in %s at %s", filename, url)
            continue
        return (
            build_system.get("build-backend", ""),
            [str(r) for r in build_system.get("requires", []) or []],
            f"{host}/{path}@{filename}",
        )
    return None


def marker_env(yocto: YoctoVersion | None) -> dict:
    """An evaluation environment approximating the Yocto target."""
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

# `[build-system].requires` entries that imply compiled code even when the
# sdist carries no source of that language (a generated one, typically).
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
    """Whether a member lives in a test/doc/example tree or is a test file."""
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


def wheel_ships_prebuilt_binaries(filename: str) -> bool:
    """Whether a wheel tag informs on prebuilt/architecture-specific code."""
    try:
        _name, _version, _build, tags = parse_wheel_filename(filename)
    except (InvalidWheelFilename, ValueError):
        return False
    return any(tag.platform != "any" for tag in tags)


_TOOLCHAIN_LIB_NAMES = frozenset(
    {
        "libc.so.6",
        "libm.so.6",
        "libpthread.so.0",
        "librt.so.1",
        "libdl.so.2",
        "libutil.so.1",
        "libresolv.so.2",
        "libgcc_s.so.1",
        "libstdc++.so.6",
        "libc.musl-aarch64.so.1",
        "libc.musl-x86_64.so.1",
    }
)


def _is_toolchain_soname(soname: str) -> bool:
    """Whether ``soname`` is a toolchain runtime piece, never an external
    system library to surface (see :data:`_TOOLCHAIN_LIB_NAMES`)."""
    if soname in _TOOLCHAIN_LIB_NAMES:
        return True
    # The dynamic linker's name embeds the ISA (ld-linux-aarch64.so.1 /
    # ld-musl-x86_64.so.1), so match it by shape instead of enumerating it.
    return soname.startswith(("ld-linux-", "ld-musl-")) and soname.endswith(
        ".so.1"
    )


_ELF_MAGIC = b"\x7fELF"
_PT_LOAD = 1
_PT_DYNAMIC = 2
_DT_NULL = 0
_DT_NEEDED = 1
_DT_STRTAB = 5
_DT_STRSZ = 10
_DT_SONAME = 14
# `.gnu.version_d` table
_DT_VERDEF = 0x6FFFFFFC
_DT_VERDEFNUM = 0x6FFFFFFD
# Long enough for any real .dynstr entry
_MAX_DYNSTR = 512
# Sanity caps on the ``.gnu.version_d`` walk
_MAX_VERDEF = 256


def _elf64_dynamic_tags(
    data: bytes,
) -> tuple[str | None, list[str], list[str]]:
    """
    Read `(DT_SONAME, DT_NEEDED, symbol versions)` out of an ELF64
    little-endian blob.
    """
    if len(data) < 0x40 or data[:4] != _ELF_MAGIC:
        return (None, [], [])
    if data[4] != 2 or data[5] != 1:  # ELF64, little-endian
        return (None, [], [])
    (e_phoff,) = struct.unpack_from("<Q", data, 0x20)
    (e_phentsize,) = struct.unpack_from("<H", data, 0x36)
    (e_phnum,) = struct.unpack_from("<H", data, 0x38)
    if not e_phentsize or not e_phnum:
        return (None, [], [])
    loads: list[tuple[int, int, int]] = []  # (vaddr, filesz, offset)
    dyn: list[tuple[int, int]] = []  # (offset, filesz)
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if off + 56 > len(data):
            break
        (p_type,) = struct.unpack_from("<I", data, off)
        (p_offset,) = struct.unpack_from("<Q", data, off + 8)
        (p_vaddr,) = struct.unpack_from("<Q", data, off + 16)
        (p_filesz,) = struct.unpack_from("<Q", data, off + 32)
        if p_type == _PT_LOAD:
            loads.append((p_vaddr, p_filesz, p_offset))
        elif p_type == _PT_DYNAMIC:
            dyn.append((p_offset, p_filesz))

    def file_offset_of(vaddr: int) -> int:
        for base, filesz, offset in loads:
            if filesz and base <= vaddr < base + filesz:
                return offset + (vaddr - base)
        return vaddr  # unusual layout: treat it as an offset already

    needed: list[int] = []
    soname: int | None = None
    strtab: int | None = None
    strsz = 0
    verdef: int | None = None  # file offset of .gnu.version_d
    verdefnum = 0
    for p_offset, p_filesz in dyn:
        end = min(p_offset + p_filesz, len(data))
        pos = p_offset
        while pos + 16 <= end:
            (d_tag,) = struct.unpack_from("<q", data, pos)
            (d_val,) = struct.unpack_from("<Q", data, pos + 8)
            if d_tag == _DT_NULL:
                break
            elif d_tag == _DT_NEEDED:
                needed.append(d_val)
            elif d_tag == _DT_STRTAB:
                strtab = file_offset_of(d_val)
            elif d_tag == _DT_STRSZ:
                strsz = d_val
            elif d_tag == _DT_SONAME:
                soname = d_val
            elif d_tag == _DT_VERDEF:
                verdef = file_offset_of(d_val)
            elif d_tag == _DT_VERDEFNUM:
                verdefnum = d_val
            pos += 16
    if strtab is None:
        return (None, [], [])
    names = [
        name
        for idx in needed
        if (name := _elf_strtab_lookup(data, strtab, strsz, idx))
    ]
    soname_name = (
        _elf_strtab_lookup(data, strtab, strsz, soname)
        if soname is not None
        else None
    )
    versions = (
        _elf64_verdef_names(data, strtab, strsz, verdef, verdefnum)
        if verdef is not None
        else []
    )
    return (soname_name, names, versions)


def _elf64_verdef_names(
    data: bytes, strtab: int, strsz: int, verdef: int, verdefnum: int
) -> list[str]:
    """
    The symbol-version names a dynamic ELF64 defines, from
    `.gnu.version_d`.
    """
    if verdefnum < 1 or verdefnum > _MAX_VERDEF:
        return []
    versions: list[str] = []
    pos = verdef
    for _ in range(min(verdefnum, _MAX_VERDEF)):
        if pos + 20 > len(data):
            break
        (vd_aux, vd_next) = struct.unpack_from("<II", data, pos + 12)
        if pos + vd_aux + 8 <= len(data):
            (vda_name,) = struct.unpack_from("<I", data, pos + vd_aux)
            name = _elf_strtab_lookup(data, strtab, strsz, vda_name)
            if name and ".so" not in name:
                versions.append(name)
        if not vd_next:
            break
        pos += vd_next
    return versions


def _elf_strtab_lookup(data: bytes, strtab: int, strsz: int, idx: int) -> str:
    if idx >= strsz or strtab + idx >= len(data):
        return ""
    start = strtab + idx
    end = data.find(b"\x00", start, start + _MAX_DYNSTR)
    if end == -1:
        return ""
    return data[start:end].decode("utf-8", "replace")


def wheel_external_needed_libs(filename: str | os.PathLike[str]) -> list[str]:
    """
    Shared libraries the wheel ELF binaries need, but the wheel does not
    bundle itself and the C toolchain is not assumed to provide.
    """
    needed: set[str] = set()
    provided: set[str] = set()
    so_member = re.compile(r"\.so(?:\.|$)")
    try:
        with zipfile.ZipFile(filename) as zf:
            for info in zf.infolist():
                if info.is_dir() or not so_member.search(info.filename):
                    continue
                try:
                    payload = zf.read(info)
                except (OSError, zipfile.BadZipFile):
                    continue
                soname, deps, _versions = _elf64_dynamic_tags(payload)
                if soname is None and not deps:
                    # A non-ELF member whose name happens to carry ".so"
                    # (a text file, a symlink target) - not an error.
                    continue
                if soname:
                    provided.add(soname)
                needed.update(deps)
    except (OSError, zipfile.BadZipFile):
        return []
    return sorted(
        lib
        for lib in needed
        if lib not in provided and not _is_toolchain_soname(lib)
    )


def wheel_bundled_lib_versions(
    filename: str | os.PathLike[str],
) -> dict[str, list[str]]:
    """
    The symbol versions each wheel-bundled shared library defines, keyed
    by `DT_SONAME`.
    """
    so_member = re.compile(r"\.so(?:\.|$)")
    versions_by_soname: dict[str, list[str]] = {}
    try:
        with zipfile.ZipFile(filename) as zf:
            for info in zf.infolist():
                if info.is_dir() or not so_member.search(info.filename):
                    continue
                try:
                    payload = zf.read(info)
                except (OSError, zipfile.BadZipFile):
                    continue
                soname, _deps, versions = _elf64_dynamic_tags(payload)
                if not soname or not versions:
                    continue
                versions_by_soname.setdefault(soname, [])
                for version in versions:
                    if version not in versions_by_soname[soname]:
                        versions_by_soname[soname].append(version)
    except (OSError, zipfile.BadZipFile):
        return {}
    return {
        soname: sorted(versions)
        for soname, versions in sorted(versions_by_soname.items())
    }


def detect_native_code(
    source: FetchedSource, build_requires: list[str] = ()
) -> tuple[bool, list[str]]:
    """Whether the artifact ships code that must be compiled, plus detail."""

    # TODO Rename to detect_need_to_compile ?

    found: dict[str, str] = {}
    for name in _member_names(source.path):
        kind = _NATIVE_SUFFIX_KINDS.get(PurePosixPath(name).suffix.lower())
        if kind is None or _is_ignored_member(name):
            continue
        found.setdefault(kind, name)
    evidence = [
        f"{kind} source ({path})" for kind, path in sorted(found.items())
    ]

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
    """Top-level build config lines that pin the build to an x86 host."""
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
    """Split machine-specific requirements from the arch-independent ones."""
    base = marker_env(yocto)
    # Several matrix entries share one platform_machine (aarch64 covers
    # ZynqMP, Versal and the generic Cortex-A targets), so dedupe while keeping
    # the matrix order.
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


def apply_major_cap(raw: str) -> tuple[str, str]:
    """Cap `raw` at its lower bound's major series."""
    try:
        req = Requirement(raw)
    except InvalidRequirement:
        return raw, ""

    # Find floor
    floor: Version | None = None
    for item in req.specifier:
        op = item.operator
        if op in ("==", "===", "<", "<=", "~="):
            # Already pinned or up-bounded.
            return raw, str(req.specifier)
        if op in (">=", ">"):
            try:
                candidate = Version(item.version)
            except InvalidVersion:
                return raw, str(req.specifier)
            if floor is None or candidate > floor:
                floor = candidate
    if floor is None or floor.major == 0:
        return raw, str(req.specifier)

    specifier = f"{req.specifier},<{floor.major + 1}"
    parts = [req.name]
    if req.extras:
        parts.append(f"[{','.join(sorted(req.extras))}]")
    parts.append(specifier)
    if req.marker is not None:
        parts.append(f"; {req.marker}")

    return "".join(parts), specifier


def classify_requires_dist(
    requires_dist: list[str],
    yocto: YoctoVersion | None,
    *,
    cap_major_bump: bool = True,
) -> tuple[list[Dependency], list[Dependency], list[Dependency]]:
    """
    Split `Requires-Dist` into (runtime, build, dropped).

    Drop rules:
    * Anything gated on an `extra ==` marker (as optional)
    * Incompatible with the Yocto target (e.g. Windows-only, lower Python)

    Unless `cap_major_bump=False`, every runtime requirement carries the
    same-major ceiling from `apply_major_cap`: ``numpy>=1.21.0`` is held
    and resolved as `numpy>=1.21.0,<2` (newest 1.x, never 2.x).
    """
    env = marker_env(yocto)
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
                satisfied = True  # Keep for now
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

        capped = apply_major_cap(raw) if cap_major_bump else None
        runtime.append(
            Dependency(
                name=name,
                kind=DepKind.RUNTIME,
                requirement=capped[0] if capped else raw,
                specifier=capped[1] if capped else str(req.specifier),
            )
        )

    return runtime, [], dropped


def _build_deps_from_requires(
    requires: list[str], *, cap_major_bump: bool = True
) -> list[Dependency]:
    out: list[Dependency] = []
    for raw in requires:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            continue
        if cap_major_bump:
            requirement, specifier = apply_major_cap(raw)
        else:
            requirement, specifier = raw, str(req.specifier)
        out.append(
            Dependency(
                name=req.name,
                kind=DepKind.BUILD,
                requirement=requirement,
                specifier=specifier,
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
    cap_major_bump: bool = True,
) -> tuple[PackageMetadata, FetchedSource]:
    """Build a `PackageMetadata` from target requirements."""
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

    declared = read_build_system(source)
    build_backend_source = ""
    if declared is None and source.kind is SourceKind.WHEEL:
        # A wheel has no build information. Let's try to check any
        # public information on available VCSs.
        found = build_system_from_vcs(project_urls, homepage)
        if found is not None:
            backend, build_requires, build_backend_source = found
            logger.info(
                "%s %s: no build config in the artifact; read [build-system] "
                "from %s (the project's default branch, so this describes the "
                "project today, not necessarily this release)",
                meta_name,
                meta_version,
                build_backend_source,
            )
        else:
            backend, build_requires = "", []
    else:
        backend, build_requires = declared if declared is not None else ("", [])
    lic = find_license_file(source)
    license_file, license_md5 = lic if lic else (None, None)

    # Machine-gated requirements are resolved by bitbake on the real target,
    # not here (the host that runs bitbake is not the target).
    machine_deps, plain_requires = machine_gated_deps(requires_dist, yocto)
    runtime, _build_from_rd, dropped = classify_requires_dist(
        plain_requires, yocto, cap_major_bump=cap_major_bump
    )
    build_deps = _build_deps_from_requires(
        build_requires, cap_major_bump=cap_major_bump
    )
    has_native_code, native_evidence = detect_native_code(
        source, build_requires
    )
    host_assumptions = detect_host_assumptions(source)

    # vendored means to package directly into the layer/recipe.
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
        build_backend_source=build_backend_source,
        ships_prebuilt_binaries=(
            source.kind is SourceKind.WHEEL
            and wheel_ships_prebuilt_binaries(source.filename)
        ),
        external_needed_libs=(
            wheel_external_needed_libs(source.path)
            if source.kind is SourceKind.WHEEL
            else []
        ),
        bundled_lib_versions=(
            wheel_bundled_lib_versions(source.path)
            if source.kind is SourceKind.WHEEL
            else {}
        ),
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
        index_flags=pip_opts.index_flags(),
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
    """Resolve the transitive **runtime** closure of ``top``."""
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
        # Parallel and greedy BFS
        seq = 0
        discovered: list[Future] = []  # discovery order

        with ThreadPoolExecutor(max_workers=workers) as pool:
            # future -> (submission sequence, member depth)
            pending: dict[Future, tuple[int, int]] = {}

            def _submit(dep: Dependency, own_depth: int) -> None:
                nonlocal seq
                seq += 1
                fut = pool.submit(builder, dep)
                pending[fut] = (seq, own_depth)
                discovered.append(fut)

            for _, dep, depth in _discover(top, 0):
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
    from py2bbr import yocto

    return yocto.resolve_yocto_version(DEFAULT_YOCTO_VERSION)
