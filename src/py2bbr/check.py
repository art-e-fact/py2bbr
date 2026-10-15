import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from py2bbr.model import PackageMetadata
from py2bbr.recipe import bitbake_pn

logger = logging.getLogger(__name__)

# Ordered levels, cheapest first.
LEVELS = ("structural", "parse", "checklayer", "build")

DEFAULT_LEVEL = "parse"
_DEFAULT_TIMEOUT = 1800


class Status(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIPPED"
    ERROR = "ERROR"


@dataclass
class CheckResult:
    level: str
    status: Status
    detail: str = ""
    #: Individual sub-checks (for the structural level).
    items: list[tuple[str, Status, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (Status.PASS, Status.WARN, Status.SKIP)


def _bitbake_available() -> tuple[bool, str]:
    """
    Note we consider bitbake usable only if also with BUILDDIR set.
    """
    if not shutil.which("bitbake"):
        return False, "bitbake not found on PATH"
    if not os.environ.get("BUILDDIR"):
        return False, "BUILDDIR not set (source oe-init-build-env first)"
    return True, ""


def _checklayer_available() -> tuple[bool, str]:
    """
    Note we consider yocto-check-layer usable only if also with BUILDDIR set.
    """
    if not shutil.which("yocto-check-layer"):
        return False, "yocto-check-layer not found on PATH"
    if not os.environ.get("BUILDDIR"):
        return False, "BUILDDIR not set (source oe-init-build-env first)"
    return True, ""


def _kas_available(kas_file: Path | None) -> tuple[bool, str]:
    if not shutil.which("kas"):
        return False, "kas not found on PATH"
    if not kas_file or not Path(kas_file).exists():
        return False, "no kas build file available"
    return True, ""


def _check_required(text: str, pattern: str, what: str, required: bool):
    found = re.search(pattern, text, re.MULTILINE) is not None
    status = (
        Status.PASS if found else (Status.FAIL if required else Status.WARN)
    )
    return (what, status, "present" if found else "missing")


def _balanced_double_quotes(text: str) -> tuple[bool, str]:
    """
    Simple heuristic that does not work with escaped quotes.
    """
    bad = []
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if line.count('"') % 2 != 0:
            bad.append(i)
    if bad:
        return False, f"unbalanced double-quotes on lines {bad}"
    return True, ""


def _balanced_single_quotes(text: str) -> tuple[bool, str]:
    """
    Simple heuristic that does not work with escaped quotes.
    """
    bad = []
    for i, line in enumerate(text.splitlines(), 1):
        if _comment_or_blank(line):
            continue
        if line.count("'") % 2 != 0:
            bad.append(i)
    if bad:
        return False, f"unbalanced single-quotes on lines {bad}"
    return True, ""


def _balanced_braces(text: str) -> tuple[bool, str]:
    """
    Simple heuristic that does not work with escaped braces.

    TODO: Check on equality instead?
    """
    opens = text.count("${")
    closes = text.count("}")
    if opens > closes:
        return False, f"more '{{' ({opens}) than '}}' ({closes})"
    return True, ""


def _comment_or_blank(line: str) -> bool:
    return not line.strip() or line.lstrip().startswith("#")


def _timestamp_residue(text: str) -> tuple[bool, str]:
    """
    TODO This is a bandaid against timestamps showing in PetaLinux builds
    There must be a better way.
    """
    bad = [
        i
        for i, line in enumerate(text.splitlines(), 1)
        if not _comment_or_blank(line)
        and re.search(r"'\d{1,2}:\d{2}\s*$", line)
    ]
    if bad:
        return False, f"timestamp residue on lines {bad}"
    return True, ""


def _dep_token_prefix(text: str) -> tuple[bool, str]:
    """
    TODO This is a bandaid against wrong dep names rejecteda (correctly) by
    PetaLinux builds. There must be a better way.
    """
    bad = []
    for i, line in enumerate(text.splitlines(), 1):
        if not re.match(r"^(DEPENDS|RDEPENDS)\b", line.lstrip()):
            continue
        parts = line.split("+=", 1)
        if len(parts) != 2:
            continue
        for tok in parts[1].strip().strip('"').split():
            if tok.startswith("-"):
                bad.append(f"{i} ({tok})")
    if bad:
        return False, "dep token starting with '-': " + ", ".join(bad)
    return True, ""


def _run_cmd(
    cmd: list[str],
    cwd: str | None,
    timeout: int,
    extra_env: dict | None = None,
) -> tuple[int, str]:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except (OSError, FileNotFoundError) as exc:
        return 127, f"failed to execute: {exc}"
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode, out.strip()


# --------------------------------------------------------------------------- #
# Level: structural
# --------------------------------------------------------------------------- #
def structural_check(recipe_path: Path) -> CheckResult:
    """
    Cheap checks on a generated recipe.
    """
    items: list[tuple[str, Status, str]] = []
    try:
        text = Path(recipe_path).read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult(
            "structural", Status.ERROR, f"cannot read recipe: {exc}"
        )

    if not text.strip():
        return CheckResult("structural", Status.FAIL, "recipe file is empty")

    items.append(
        _check_required(text, r"^SUMMARY\s*=", "SUMMARY", required=True)
    )
    items.append(
        _check_required(text, r"^LICENSE\s*=", "LICENSE", required=True)
    )
    items.append(
        _check_required(
            text, r"^LIC_FILES_CHKSUM\s*=", "LIC_FILES_CHKSUM", required=False
        )
    )
    items.append(
        _check_required(text, r"^HOMEPAGE\s*=", "HOMEPAGE", required=False)
    )
    items.append(
        _check_required(
            text, r"^SRC_URI\[sha256sum\]", "SRC_URI[sha256sum]", required=False
        )
    )
    items.append(
        _check_required(text, r"^RDEPENDS:", "RDEPENDS", required=False)
    )

    vendored = bool(re.search(r'^SRC_URI\s*=\s*"file://', text, re.MULTILINE))
    if vendored:
        # When no sdist and only wheel --> vendor path (install binary only)
        installs_wheel = bool(
            re.search(r"^do_install\s*\(\)", text, re.MULTILINE)
        )
        if installs_wheel:
            items.append(
                _check_required(
                    text,
                    r"^DEPENDS.*python3-installer",
                    "python3-installer (wheel install)",
                    required=True,
                )
            )
        else:
            items.append(
                _check_required(
                    text,
                    r'^S\s*=\s*"\$\{WORKDIR\}/',
                    "S (vendored)",
                    required=True,
                )
            )
    else:
        # PyPi path (not vendored)
        items.append(
            _check_required(
                text, r"^inherit\s+pypi", "inherit pypi", required=True
            )
        )

    ok_q, msg_q = _balanced_double_quotes(text)
    items.append(
        ("balanced quotes", Status.PASS if ok_q else Status.FAIL, msg_q)
    )
    ok_b, msg_b = _balanced_braces(text)
    items.append(
        ("balanced braces", Status.PASS if ok_b else Status.WARN, msg_b)
    )
    ok_ts, msg_ts = _timestamp_residue(text)
    items.append(
        ("timestamp residue", Status.PASS if ok_ts else Status.FAIL, msg_ts)
    )
    ok_dt, msg_dt = _dep_token_prefix(text)
    items.append(
        ("dep token prefix", Status.PASS if ok_dt else Status.FAIL, msg_dt)
    )
    ok_sq, msg_sq = _balanced_single_quotes(text)
    items.append(
        (
            "balanced single-quotes",
            Status.PASS if ok_sq else Status.WARN,
            msg_sq,
        )
    )

    failed = [i for i in items if i[1] is Status.FAIL]
    status = (
        Status.FAIL
        # Here using the fact that [] is "falsy"
        if failed
        else (
            Status.WARN
            if any(i[1] is Status.WARN for i in items)
            else Status.PASS
        )
    )
    summary = f"{len(items) - len(failed)}/{len(items)} checks passed"
    if failed:
        summary += f"; failed: {[i[0] for i in failed]}"
    return CheckResult("structural", status, summary, items=items)


# --------------------------------------------------------------------------- #
# Level: parse
# --------------------------------------------------------------------------- #
def bitbake_parse(
    meta: PackageMetadata, builddir: str, timeout: int
) -> CheckResult:
    pn = bitbake_pn(meta.name)
    # Parse checking with -p/--parse-only
    code, out = _run_cmd(["bitbake", "-p", pn], cwd=builddir, timeout=timeout)
    if code == 0:
        return CheckResult("parse", Status.PASS, f"bitbake parsed {pn}")
    tail = "\n".join(out.splitlines()[-15:])
    return CheckResult(
        "parse", Status.FAIL, f"bitbake parse failed ({pn}):\n{tail}"
    )


# --------------------------------------------------------------------------- #
# Level: checklayer
# --------------------------------------------------------------------------- #
def checklayer(layer_dir: Path, builddir: str, timeout: int) -> CheckResult:
    layer_dir = Path(layer_dir)
    code, out = _run_cmd(
        ["yocto-check-layer", str(layer_dir)], cwd=builddir, timeout=timeout
    )
    tail = "\n".join(out.splitlines()[-25:])
    if code == 0:
        return CheckResult(
            "checklayer",
            Status.PASS,
            "yocto-check-layer passed",
        )
    if code == 2:
        return CheckResult(
            "checklayer", Status.FAIL, "yocto-check-layer failed:\n" + tail
        )
    return CheckResult(
        "checklayer", Status.ERROR, f"yocto-check-layer exited {code}:\n{tail}"
    )


# --------------------------------------------------------------------------- #
# Level: build
# --------------------------------------------------------------------------- #
def kas_build(kas_file: Path, workdir: Path, timeout: int) -> CheckResult:
    kas_file = Path(kas_file)
    code, out = _run_cmd(
        ["kas", "build", "-c", str(kas_file), "-w", str(workdir)],
        cwd=str(workdir),
        timeout=timeout,
    )
    tail = "\n".join(out.splitlines()[-25:])
    if code == 0:
        return CheckResult("build", Status.PASS, "kas build succeeded")
    return CheckResult(
        "build", Status.FAIL, f"kas build failed (exit {code}):\n{tail}"
    )


# --------------------------------------------------------------------------- #
# Levels runner
# --------------------------------------------------------------------------- #
def run_checks(
    meta: PackageMetadata,
    recipe_path: Path,
    layer_dir: Path | None,
    kas_file: Path | None,
    level: str = DEFAULT_LEVEL,
    workdir: Path | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> list[CheckResult]:
    """
    Run the validation levels up to and including `level`.
    """
    if level not in LEVELS:
        logger.warning(
            "level %s unknown, running with %s ...", level, DEFAULT_LEVEL
        )
        level = DEFAULT_LEVEL
    ceiling = LEVELS.index(level)
    builddir = os.environ.get("BUILDDIR", "")

    results: list[CheckResult] = []
    for level in LEVELS[: ceiling + 1]:
        logger.info("check %s: running ...", level)
        before = len(results)
        if level == "structural":
            results.append(structural_check(recipe_path))
        elif level == "parse":
            avail, reason = _bitbake_available()
            if not avail:
                results.append(CheckResult("parse", Status.SKIP, reason))
            else:
                results.append(bitbake_parse(meta, builddir, timeout))
        elif level == "checklayer":
            if layer_dir is None:
                results.append(
                    CheckResult(
                        "checklayer",
                        Status.SKIP,
                        "no layer directory generated",
                    )
                )
            else:
                avail, reason = _checklayer_available()
                if not avail:
                    results.append(
                        CheckResult("checklayer", Status.SKIP, reason)
                    )
                else:
                    results.append(checklayer(layer_dir, builddir, timeout))
        elif level == "build":
            wd = workdir or (
                Path(kas_file).parent / "kas-workdir"
                if kas_file
                else Path.cwd() / "kas-workdir"
            )
            wd.mkdir(parents=True, exist_ok=True)
            avail, reason = _kas_available(kas_file)
            if not avail:
                results.append(CheckResult("build", Status.SKIP, reason))
            else:
                results.append(kas_build(kas_file, wd, timeout))
        for r in results[before:]:
            logger.info("check %s: %s - %s", r.level, r.status.value, r.detail)
    return results
