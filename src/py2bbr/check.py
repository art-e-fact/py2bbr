"""Capability-gated recipe validation ladder.

Stages, cheapest first:

* ``structural``  - pure-Python checks on the generated ``.bb`` (always runs).
* ``parse``       - ``bitbake -p <pn>`` (parse-only; needs bitbake + a
                    sourced BUILDDIR).
* ``checklayer``  - ``yocto-check-layer <layer>`` (needs the tool + BUILDDIR;
                    runs ``bitbake world`` and is expensive).
* ``build``       - ``kas build -c <kasfile>`` (needs kas + network; full build).

``--check LEVEL`` selects the ceiling; every stage up to it that is not runnable
in the current environment is reported as ``SKIPPED`` with the reason, so the
output always shows exactly what was validated and what was not.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from py2bbr.model import PackageMetadata
from py2bbr.recipe import bitbake_pn

#: Ordered stages, cheapest first. ``--check`` names the ceiling.
STAGES = ("structural", "parse", "checklayer", "build")

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
    stage: str
    status: Status
    detail: str = ""
    #: Individual sub-checks (for the structural stage).
    items: list[tuple[str, Status, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (Status.PASS, Status.WARN, Status.SKIP)


def _bitbake_available() -> tuple[bool, str]:
    bitbake = shutil.which("bitbake")
    if not bitbake:
        return False, "bitbake not found on PATH"
    builddir = os.environ.get("BUILDDIR")
    if not builddir:
        return False, "BUILDDIR not set (source oe-init-build-env first)"
    return True, ""


def _checklayer_available() -> tuple[bool, str]:
    tool = shutil.which("yocto-check-layer")
    if not tool:
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


# --------------------------------------------------------------------------- #
# structural
# --------------------------------------------------------------------------- #
def _check_required(text: str, pattern: str, what: str, required: bool):
    found = re.search(pattern, text, re.MULTILINE) is not None
    status = (
        Status.PASS if found else (Status.FAIL if required else Status.WARN)
    )
    return (what, status, "present" if found else "missing")


def _balanced_quotes(text: str) -> tuple[bool, str]:
    bad = []
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if line.count('"') % 2 != 0:
            bad.append(i)
    if bad:
        return False, f"unbalanced double-quotes on lines {bad}"
    return True, ""


def _balanced_braces(text: str) -> tuple[bool, str]:
    # Count ${ ... } openers vs closers (rough; ignores escaped braces).
    opens = text.count("${")
    closes = text.count("}")
    if opens > closes:
        return False, f"more '{{' ({opens}) than '}}' ({closes})"
    return True, ""


def _comment_or_blank(line: str) -> bool:
    return not line.strip() or line.lstrip().startswith("#")


def _timestamp_residue(text: str) -> tuple[bool, str]:
    # Signature of the PetaLinux corruption: a shell timestamp appended to a
    # joined continuation line (``... '00:00``). Never valid in a recipe.
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
    # A value token starting with '-' means the ``${PYTHON_PN}-`` prefix was
    # emptied by expansion (``DEPENDS += -pybind11-native``). Legit dep names
    # never start with '-'.
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


def _balanced_single_quotes(text: str) -> tuple[bool, str]:
    bad = []
    for i, line in enumerate(text.splitlines(), 1):
        if _comment_or_blank(line):
            continue
        if line.count("'") % 2 != 0:
            bad.append(i)
    if bad:
        return False, f"unbalanced single-quotes on lines {bad}"
    return True, ""


def structural_check(recipe_path: Path) -> CheckResult:
    """Cheap, dependency-free checks on a generated recipe."""
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
        _check_required(text, r"^inherit\s+pypi", "inherit pypi", required=True)
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

    ok_q, msg_q = _balanced_quotes(text)
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
# external stages
# --------------------------------------------------------------------------- #
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


def bitbake_parse(
    meta: PackageMetadata, builddir: str, timeout: int
) -> CheckResult:
    pn = bitbake_pn(meta.name)
    # '-p'/'--parse-only' is bitbake's real parse-only mode. '-c parse' would
    # select a do_parse task that does not exist and can never succeed.
    code, out = _run_cmd(["bitbake", "-p", pn], cwd=builddir, timeout=timeout)
    if code == 0:
        return CheckResult("parse", Status.PASS, f"bitbake parsed {pn}")
    tail = "\n".join(out.splitlines()[-15:])
    return CheckResult(
        "parse", Status.FAIL, f"bitbake parse failed ({pn}):\n{tail}"
    )


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
# orchestration
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
    """Run the validation ladder up to (and including) ``level``."""
    if level not in STAGES:
        level = DEFAULT_LEVEL
    ceiling = STAGES.index(level)
    builddir = os.environ.get("BUILDDIR", "")

    results: list[CheckResult] = []
    for stage in STAGES[: ceiling + 1]:
        if stage == "structural":
            results.append(structural_check(recipe_path))
        elif stage == "parse":
            avail, reason = _bitbake_available()
            if not avail:
                results.append(CheckResult("parse", Status.SKIP, reason))
            else:
                results.append(bitbake_parse(meta, builddir, timeout))
        elif stage == "checklayer":
            if layer_dir is None:
                results.append(
                    CheckResult(
                        "checklayer",
                        Status.SKIP,
                        "no layer directory generated",
                    )
                )
                continue
            avail, reason = _checklayer_available()
            if not avail:
                results.append(CheckResult("checklayer", Status.SKIP, reason))
            else:
                results.append(checklayer(layer_dir, builddir, timeout))
        elif stage == "build":
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
    return results
