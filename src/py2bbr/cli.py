"""
Command-line interface for py2bbr.

Usage examples (see README.md for more information):

    py2bbr black
    py2bbr "requests==2.34.2" --yocto-version 5.0
    py2bbr pyvizionsdk --python-version 3.12 --target-arch aarch64
    py2bbr mytool --extra-index-url https://pypi.mycompany.com/simple \\
                 --trusted-host pypi.mycompany.com --check build
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import contextmanager
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

from py2bbr import __version__
from py2bbr.check import DEFAULT_LEVEL, STAGES, run_checks
from py2bbr.errors import MetadataNotFoundError, Py2BbrError
from py2bbr.metadata import PipOptions, build_metadata
from py2bbr.model import DEFAULT_YOCTO_VERSION, Dependency
from py2bbr.recipe import (
    bitbake_pn,
    build_version_gaps,
    canonical_name,
    recipe_filename,
    render_recipe_to_path,
    sysroot_tool_version,
    tool_companion_gaps,
)
from py2bbr.yocto import (
    DEFAULT_KAS_VERSION,
    current_platform_machine,
    current_python_version,
    default_layer_name,
    render_kas_file,
    resolve_target_runtime,
    resolve_yocto_version,
    validate_kas_file,
    write_layer,
)

logger = logging.getLogger(__name__)


def _parse_package_spec(spec: str) -> tuple[str, str | None]:
    """Split a positional ``name`` / ``name==version`` into (name, version)."""
    try:
        req = Requirement(spec)
    except InvalidRequirement as exc:
        raise Py2BbrError(f"invalid package spec {spec!r}: {exc}") from exc
    version = None
    for sp in req.specifier:
        if sp.operator in ("==", "==="):
            version = sp.version
            break
    return req.name, version


class _PipArgParser(argparse.ArgumentParser):
    """ArgumentParser that lets ``--pip-arg`` consume option-like values.

    argparse refuses to use a token starting with ``-`` as an option value, so
    ``--pip-arg --retries`` would fail with "expected one argument". Rewrite
    such pairs into the unambiguous ``--pip-arg=--retries`` form first.
    """

    def parse_known_args(self, args=None, namespace=None):
        if args is None:
            args = sys.argv[1:]
        args = list(args)
        merged: list[str] = []
        i = 0
        while i < len(args):
            tok = args[i]
            if (
                tok == "--pip-arg"
                and i + 1 < len(args)
                and args[i + 1].startswith("-")
                and args[i + 1] != "--"
            ):
                merged.append(f"--pip-arg={args[i + 1]}")
                i += 2
            else:
                merged.append(tok)
                i += 1
        return super().parse_known_args(merged, namespace)


def build_parser() -> argparse.ArgumentParser:
    p = _PipArgParser(
        prog="py2bbr",
        description="Convert a Python package into a Yocto Bitbake recipe.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "package",
        help="Package name, optionally pinned: 'black' or 'black==26.5.1'.",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"py2bbr {__version__}",
    )

    out = p.add_argument_group("output")
    out.add_argument(
        "-o",
        "--output",
        default=".",
        help="Output directory for the layer and kas file.",
    )
    out.add_argument(
        "--layer-name",
        default=None,
        help="Name of the generated Yocto layer directory "
        "(default: meta-<package>).",
    )
    out.add_argument(
        "--no-layer",
        action="store_true",
        help="Emit only the .bb recipe file (no conf/layer.conf, no README).",
    )
    out.add_argument(
        "--kas-file",
        default=None,
        help="Path for the generated kas build file (default: <output>/py2bbr.kas.yml).",
    )
    out.add_argument(
        "--no-kas",
        action="store_true",
        help="Do not generate a kas build file.",
    )
    out.add_argument(
        "--keep-sources",
        action="store_true",
        help="Keep the downloaded source artifacts (for debugging).",
    )
    out.add_argument(
        "--with-deps",
        dest="with_deps",
        action="store_true",
        default=True,
        help=(
            "Resolve the transitive runtime dependency closure and render one "
            "recipe per closure member into the same layer (RDEPENDS chains "
            "them; the top recipe stays the kas target). This is the default "
            "so generated layers are self-contained; pass --no-deps to "
            "disable it."
        ),
    )
    out.add_argument(
        "--no-deps",
        dest="with_deps",
        action="store_false",
        help=(
            "Do not resolve the runtime dependency closure; emit only the top "
            "recipe and leave its RDEPENDS to be provided by other layers "
            "(e.g. meta-python)."
        ),
    )
    out.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Limit dependency resolution depth (None = unlimited).",
    )
    out.add_argument(
        "--skip-dep",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Skip this canonical package name (repeatable) when resolving the "
            "dependency closure (escape hatch for distro-provided deps)."
        ),
    )
    out.add_argument(
        "--jobs",
        type=int,
        default=4,
        help=(
            "Number of fetches to run in parallel while resolving the "
            "dependency closure (1 = sequential)."
        ),
    )

    yocto = p.add_argument_group("yocto")
    yocto.add_argument(
        "--yocto-version",
        default=DEFAULT_YOCTO_VERSION,
        help="Yocto version (e.g. 5.0) or codename (e.g. scarthgap).",
    )
    yocto.add_argument(
        "--python-version",
        metavar="X.Y",
        default=current_python_version(),
        help=(
            "Python version the target image runs (e.g. 3.12). Overrides the "
            "series' own version: it pins the artifact the recipe installs "
            "(wheel ABI tag), judges Requires-Python, evaluates the "
            "python_version PEP 508 markers and prunes the classifiers the "
            "target's Python does not know."
        ),
    )
    yocto.add_argument(
        "--target-arch",
        "--arch",
        dest="target_arch",
        metavar="ARCH",
        default=current_platform_machine(),
        help=(
            "CPU architecture the target image runs, as platform.machine() "
            "reports it (e.g. aarch64, armv7l, x86_64). A wheel download is "
            "pinned to it when it differs from this machine's, so the recipe "
            "installs the target's artifact and not the build host's."
        ),
    )
    yocto.add_argument(
        "--build-class",
        default=None,
        help="Bitbake build class to inherit (default: auto, usually setuptools3).",
    )
    yocto.add_argument(
        "--machine",
        default="qemux86-64",
        help="MACHINE for the generated kas build file.",
    )
    yocto.add_argument(
        "--targets",
        default=None,
        help="Comma-separated target keys for the build-for-target report "
        "(default: all known targets: "
        "x86, x86_64, cortex-a53, cortex-a57, cortex-a72, cortex-a53-64, "
        "cortex-a72-64, zynqmp, versal, riscv64).",
    )

    pipg = p.add_argument_group("pip index (forwarded to 'pip download')")
    pipg.add_argument(
        "--index-url", default=None, help="Primary package index URL."
    )
    pipg.add_argument(
        "--extra-index-url",
        action="append",
        default=[],
        help="Additional package index URL (repeatable).",
    )
    pipg.add_argument(
        "--trusted-host",
        action="append",
        default=[],
        help="Trusted host (repeatable).",
    )
    pipg.add_argument(
        "--pip-arg",
        action="append",
        default=[],
        help="Extra raw argument appended to 'pip download' (repeatable).",
    )
    pipg.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="pip network timeout in seconds (0 = pip default).",
    )

    checkg = p.add_argument_group("checks")
    checkg.add_argument(
        "--check",
        choices=list(STAGES),
        default=DEFAULT_LEVEL,
        help="Validation ceiling: structural | parse | checklayer | build.",
    )
    checkg.add_argument(
        "--check-timeout",
        type=int,
        default=1800,
        help="Timeout (s) for each external check stage.",
    )

    p.add_argument(
        "-q", "--quiet", action="store_true", help="Only print errors."
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose output."
    )
    return p


class _RichLogHandler(logging.Handler):
    """Logging handler that prints records through a rich ``Console``.

    When the fetch progress bars are live on the same console, rich prints
    the line *above* the live progress region and redraws the bars underneath.
    Without this, plain stderr lines would be written into the middle of the
    live region and the bars' ``\\r``/cursor-up control sequences would erase
    them (the log output lost its newlines).
    """

    def __init__(self, console):
        super().__init__()
        self._console = console

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._console.print(
                self.format(record),
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)


def _configure_logging(args, console=None) -> None:
    """Route py2bbr stage progress to stderr: INFO by default, -q->WARNING, -v->DEBUG.

    The level is set on the ``py2bbr`` logger (its ``py2bbr.*`` children
    inherit it), so unrelated libraries are untouched. The stderr handler is
    attached only when none exists yet, keeping the call idempotent across
    repeated ``main()`` invocations (tests call it many times in one process).

    ``console`` is the rich Console shared with the fetch progress bars; when
    given, records are routed through it (see :class:`_RichLogHandler`) so
    log lines and the live bars cooperate on one stream.
    """
    level = (
        logging.DEBUG
        if args.verbose
        else logging.WARNING
        if args.quiet
        else logging.INFO
    )
    lg = logging.getLogger("py2bbr")
    lg.setLevel(level)
    if not lg.handlers:
        if console is not None:
            handler: logging.Handler = _RichLogHandler(console)
        else:
            handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("[py2bbr] %(levelname)s %(message)s")
        )
        lg.addHandler(handler)


class _FetchProgress:
    """One live progress bar per in-flight fetch on stderr (TTY only).

    Closure fetches run in parallel (``--jobs``); each gets its own rich task
    row that is removed on completion, so the terminal shows exactly which
    fetches are still running. Disabled when stderr is not a TTY (pipes,
    tests) or under ``-q``; every call site stays unconditional and no-ops.
    """

    def __init__(self, enabled: bool, console=None):
        self._progress = None
        if enabled:
            from rich.progress import (
                BarColumn,
                Progress,
                TextColumn,
                TimeElapsedColumn,
            )

            self._progress = Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TimeElapsedColumn(),
                console=console,
                transient=True,
            )

    def start(self) -> None:
        if self._progress is not None:
            self._progress.start()

    def stop(self) -> None:
        if self._progress is not None:
            self._progress.stop()

    @contextmanager
    def fetch(self, requirement: str):
        if self._progress is None:
            yield
            return
        task_id = self._progress.add_task(f"fetching {requirement}", total=None)
        try:
            yield
        finally:
            self._progress.remove_task(task_id)


def _print_report(
    args,
    meta,
    yocto,
    recipe_path,
    layer_dir,
    kas_file,
    warnings,
    results,
    target_report,
):
    if not args.quiet:
        print(f"Package      : {meta.name} {meta.version}")
        print(f"Target       : {yocto.label}")
        print(f"Runtime      : {yocto.runtime_label}")
        print(f"License      : {meta.license or '(none detected)'}")
        print(f"Homepage     : {meta.homepage or '-'}")
        print(f"Build backend: {meta.build_backend or '(setuptools/setup.py)'}")
        if meta.build_backend_source:
            # Saying *where* the answer came from keeps it from reading as the
            # release's own declaration: this is the project's default branch.
            print(f"               (read from {meta.build_backend_source})")
        print(f"Runtime deps : {len(meta.runtime_deps)}")
        print(f"External libs: {', '.join(meta.external_needed_libs) or '-'}")
        print(
            f"Dropped deps : {len(meta.dropped_deps)}"
            + (
                f" ({', '.join(d.name for d in meta.dropped_deps)})"
                if meta.dropped_deps
                else ""
            )
        )
        print(f"Recipe       : {recipe_path}")
        if layer_dir:
            print(f"Layer        : {layer_dir}")
        if kas_file:
            print(f"Kas file     : {kas_file}")

    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f"  - {w}")

    _print_target_report(target_report)

    print("\nChecks:")
    for r in results:
        icon = {
            "PASS": "✓",
            "WARN": "!",
            "FAIL": "✗",
            "SKIPPED": "·",
            "ERROR": "✗",
        }.get(r.status.value, "?")
        print(f"  [{icon}] {r.stage:<11} {r.status.value:<8} {r.detail}")
        for name, status, detail in r.items:
            print(f"        - {name}: {status.value} {detail}")


def _select_targets(spec: str | None, warnings: list[str]):
    """Resolve ``--targets`` into a :class:`py2bbr.targets.Target` tuple.

    Empty spec means "the whole known matrix". An unknown key is reported and
    ignored rather than failing the run: a typo in a convenience flag should not
    cost the user the recipe they asked for.
    """
    from py2bbr.targets import KNOWN_TARGETS, target_by_key

    if not spec:
        return KNOWN_TARGETS
    keys = [k.strip() for k in spec.split(",") if k.strip()]
    selected = []
    for key in keys:
        target = target_by_key(key)
        if target is None:
            warnings.append(
                f"unknown target '{key}' in --targets; known targets: "
                + ", ".join(t.key for t in KNOWN_TARGETS)
            )
            continue
        if target not in selected:
            selected.append(target)
    return tuple(selected) or KNOWN_TARGETS


def _rendered_build_class(meta, build_class: str | None) -> str:
    """The build class the recipe is actually rendered with.

    Shares :func:`py2bbr.recipe.render_recipe`'s dispatch, so the report cannot
    describe a class the recipe does not use. A vendored wheel renders as a
    wheel install with no build class at all, which is reported as
    ``wheel-install`` (installing a prebuilt artifact is trivially portable -
    the wheel's own tags are what constrain it, and the rejection for those
    already happened at fetch time).
    """
    from py2bbr.model import SourceKind
    from py2bbr.recipe import effective_build_class

    if meta.vendored and meta.source_kind is SourceKind.WHEEL:
        return "wheel-install"
    return effective_build_class(meta, build_class)


def _build_target_report(meta, yocto, args, warnings):
    """Build the build-for-target verdict for the rendered build class.

    The layer is cross-built: build tools are host binaries, the package itself
    must be compiled for the target CPU. The report says which targets the
    generated recipe can serve and what breaks where.
    """
    from py2bbr.targets import evaluate

    targets = _select_targets(getattr(args, "targets", None), warnings)
    return evaluate(
        meta,
        _rendered_build_class(meta, args.build_class),
        yocto,
        targets=targets,
    )


def _shadow_warning(shadowed: list[str]) -> str:
    """Warn that provisioned build tools took over a distro RPROVIDER.

    ``python3-flit-core`` and ``python3-meson-python`` are the ones that bite:
    poky ships both, and py2bbr provisions a newer copy when a
    ``build-system.requires`` floor demands one. Bitbake keys tasks by recipe
    name, not version, so the swap drops the previous version's sstate
    manifests; a signature-locked cache (PetaLinux's ``SIGGEN_LOCKEDSIGS`` with
    ``SSTATE_EXISTS_CHECK = "none"``) keeps honouring the old setscene stamps,
    so the manifests are never rewritten and every recipe needing that sysroot
    fails in ``do_prepare_recipe_sysroot`` - a recipe that has nothing to do
    with the change.
    """
    names = ", ".join(shadowed)
    tasks = " ".join(f"{pn}-native" for pn in shadowed)
    return (
        f"these shadow the recipe(s) the target Yocto already ships ({names}): "
        f"every consumer now resolves the layer's copy of "
        f"{', '.join(f'{pn}-native' for pn in shadowed)}. In a tree that "
        "already built the distro's copy, the swap invalidates its sstate "
        "manifests while a signature-locked cache (PetaLinux "
        "SIGGEN_LOCKEDSIGS) keeps its setscene stamps valid, so the next "
        'build fails in an unrelated recipe with "The sstate manifest for '
        "task '<recipe>-native:populate_sysroot' could not be found\". "
        f"Rebuild those tasks once: petalinux-build -c cleansstate {tasks}"
    )


def _provision_build_tools(
    closure: list,
    gaps: list[Dependency],
    closure_sources: dict[str, Path],
    warnings_extra: list[str],
    progress,
    pip_opts,
    workdir: Path,
    yocto,
    args,
) -> set[str]:
    """Add a recipe for every build tool the target sysroot is too old for.

    Returns the canonical names actually provisioned. The worklist is processed
    to a fixpoint because a provisioned tool is itself built in the native
    sysroot and can drag in further gaps, from two directions:

    * its own ``[build-system].requires`` floors (``build_version_gaps``) - a
      tool built by meson-python needs meson/ninja that the sysroot may be too
      old for, exactly like the original consumer did;
    * the native packages it imports at run time
      (:func:`tool_companion_gaps`) - meson-python 0.18.0+ calls
      ``StandardMetadata.from_pyproject`` with the ``metadata_version`` argument
      that ``pyproject-metadata`` only accepts from 0.8.0, and declares that
      companion unversioned so no floor is visible.

    A single pass would emit a meson-python recipe that still cannot build, so
    the loop re-evaluates each tool it pulls in.
    """
    have = {canonical_name(m.name) for m in closure}
    queued: list[Dependency] = list(gaps)
    provisioned: set[str] = set()
    while queued:
        dep = queued.pop(0)
        cname = canonical_name(dep.name)
        if cname in have:
            continue
        logger.info(
            "provisioning %s (sysroot of %s ships an older build dep)",
            dep.name,
            yocto.label,
        )
        try:
            with progress.fetch(dep.requirement):
                member, msource = build_metadata(
                    dep.name,
                    None,
                    pip_opts,
                    workdir,
                    yocto=yocto,
                    keep=args.keep_sources,
                    requirement=dep.requirement,
                    check_public=True,
                )
        except MetadataNotFoundError as exc:
            warnings_extra.append(f"could not provision {dep.name}: {exc}")
            continue
        closure.append(member)
        closure_sources[member.canonical_name] = msource.path
        have.add(cname)
        provisioned.add(cname)
        queued.extend(build_version_gaps(member, yocto))
        queued.extend(tool_companion_gaps(member, yocto))
    return provisioned


def _print_target_report(report) -> None:
    """Print the target report, compactly when nothing is flagged."""
    if report is None:
        return
    if report.ok:
        kind = (
            "pure Python"
            if report.pure_python
            else "cross-compiled for the target"
        )
        print(
            f"\nBuild for target: OK on {len(report.verdicts)} target(s) "
            f"(build class {report.build_class}, {kind})"
        )
        return
    for line in report.render():
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = None
    if sys.stderr.isatty() and not args.quiet:
        from rich.console import Console

        console = Console(stderr=True)
    _configure_logging(args, console=console)
    progress = _FetchProgress(enabled=console is not None, console=console)
    progress.start()

    try:
        name, version = _parse_package_spec(args.package)
        yocto = resolve_yocto_version(args.yocto_version)
        yocto = resolve_target_runtime(
            yocto,
            python_version=args.python_version,
            platform_machine=args.target_arch,
        )
        logger.debug(
            "yocto target: %s (%s), runtime: %s",
            yocto.version,
            yocto.codename,
            yocto.runtime_label,
        )
        pip_opts = PipOptions(
            index_url=args.index_url,
            extra_index_urls=list(args.extra_index_url),
            trusted_hosts=list(args.trusted_host),
            pip_args=list(args.pip_arg),
            timeout=args.timeout,
        )

        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)

        # 1. Fetch + parse metadata.
        logger.info("fetching %s==%s ...", name, version or "latest")
        workdir = output / ".py2bbr-work"
        with progress.fetch(f"{name}=={version or 'latest'}"):
            meta, source = build_metadata(
                name,
                version,
                pip_opts,
                workdir,
                yocto=yocto,
                keep=args.keep_sources,
                check_public=True,
            )
        # Path of the artifact each closure member was fetched from, keyed by
        # canonical name: vendored members copy their sdist into the layer.
        closure_sources: dict[str, Path] = {meta.canonical_name: source.path}

        # Dependency closure (default on, so the layer is self-contained:
        # every RDEPENDS has a recipe in the same layer). Skipped with
        # --no-layer (no layer to hold member recipes) or --no-deps (deps
        # left to other layers, e.g. meta-python).
        closure = [meta]
        warnings_extra: list[str] = []
        if args.with_deps and not args.no_layer:
            from py2bbr.metadata import resolve_closure

            def _builder(req):
                with progress.fetch(req.requirement):
                    member, msource = build_metadata(
                        req.name,
                        None,
                        pip_opts,
                        workdir,
                        yocto=yocto,
                        keep=args.keep_sources,
                        requirement=req.requirement,
                        check_public=True,
                    )
                closure_sources[member.canonical_name] = msource.path
                return member, msource

            closure, warnings_extra = resolve_closure(
                meta,
                _builder,
                skip=frozenset(args.skip_dep),
                max_depth=args.max_depth,
                workers=max(1, args.jobs),
            )
        logger.info("closure: %d package(s) resolved", len(closure))

        # Build-backend version-gap provisioning: when a build-system bound
        # demands a newer tool than the target series' native sysroot ships
        # (numpy 2.5.3 wants Cython>=3.1.0; Scarthgap has 3.0.8), render a
        # recipe of the newer tool into the layer so the layer builds it
        # itself instead of failing the --no-isolation dependency check.
        gaps = [
            dep
            for member in list(closure)
            for dep in build_version_gaps(member, yocto)
        ]
        if not (args.with_deps and not args.no_layer):
            for dep in gaps:
                warnings_extra.append(
                    f"{dep.name}: build-system.requires demands {dep.requirement}, "
                    "newer than the target Yocto's native sysroot ships; no "
                    "layer is generated to hold a newer recipe (use --with-deps)."
                )
        else:
            provisioned = _provision_build_tools(
                closure,
                gaps,
                closure_sources,
                warnings_extra,
                progress,
                pip_opts,
                workdir,
                yocto,
                args,
            )
            if provisioned:
                warnings_extra.append(
                    "provisioned newer build-backend recipe(s) into the layer "
                    "(the sysroot ships older than build-system.requires "
                    "demands): " + ", ".join(sorted(provisioned))
                )
                shadowed = sorted(
                    pn
                    for pn in (
                        bitbake_pn(name, yocto.python_pn)
                        for name in provisioned
                        if sysroot_tool_version(yocto, name)
                    )
                )
                if shadowed:
                    warnings_extra.append(_shadow_warning(shadowed))

        # 2. Render + write the recipe (and optionally a full layer).
        layer_name = args.layer_name or default_layer_name(meta.canonical_name)
        layer_dir = output / layer_name if not args.no_layer else None
        extra_metas = closure[1:] if len(closure) > 1 else None
        warnings = list(warnings_extra)
        if args.no_layer:
            recipe_path, recipe_warnings = render_recipe_to_path(
                meta,
                yocto,
                output,
                build_class=args.build_class,
                vendor_source=source.path if meta.vendored else None,
            )
            logger.info("wrote recipe %s", recipe_path)
            warnings += recipe_warnings
        else:
            vendor_sources = {
                recipe_filename(
                    bitbake_pn(m.name, yocto.python_pn), m.version
                ): closure_sources[m.canonical_name]
                for m in closure
                if m.vendored
            }
            recipe_path = write_layer(
                meta,
                yocto,
                layer_dir,
                build_class=args.build_class,
                extra_metas=extra_metas,
                vendor_sources=vendor_sources or None,
            )
            from py2bbr.recipe import render_recipe

            _text, recipe_warnings = render_recipe(
                meta, yocto, build_class=args.build_class
            )
            warnings += recipe_warnings

        # 3. Render + validate the kas build file.
        kas_file = None
        kas_errors: list[str] = []
        if not args.no_kas and not args.no_layer:
            kas_file = Path(args.kas_file or (output / "py2bbr.kas.yml"))
            kas_text = render_kas_file(
                meta,
                yocto,
                layer_dir,
                machine=args.machine,
                kas_version=DEFAULT_KAS_VERSION,
                extra_targets=[
                    bitbake_pn(m.name, yocto.python_pn)
                    for m in (closure[1:] if len(closure) > 1 else [])
                ],
                # self-contained layer (default): no meta-python, its recipes
                # would duplicate the generated member recipes; meta-python
                # is only needed when deps are left to it (--no-deps).
                include_meta_python=not args.with_deps,
            )
            kas_file.parent.mkdir(parents=True, exist_ok=True)
            kas_file.write_text(kas_text, encoding="utf-8")
            logger.info("wrote kas file %s", kas_file)
            import yaml

            config = yaml.safe_load(kas_text)
            kas_errors = validate_kas_file(config)
            if kas_errors:
                warnings.append(
                    "generated kas file failed schema validation: "
                    + "; ".join(kas_errors)
                )
            else:
                logger.info("kas build file validated against kas schema")

        # 4. Run the validation ladder.
        logger.info("running validation ladder (check=%s) ...", args.check)
        results = run_checks(
            meta,
            recipe_path,
            layer_dir if not args.no_layer else None,
            kas_file,
            level=args.check,
            workdir=output / ".py2bbr-work" / "kas-build",
            timeout=args.check_timeout,
        )

        # 5. Report.
        target_report = _build_target_report(meta, yocto, args, warnings)
        if not target_report.buildable:
            warnings.append(
                "the generated recipe cannot build for at least one target in "
                "the matrix; see the build-for-target report"
            )
        _print_report(
            args,
            meta,
            yocto,
            recipe_path,
            layer_dir if not args.no_layer else None,
            kas_file,
            warnings,
            results,
            target_report,
        )

        if args.keep_sources:
            print(f"\nSources kept in: {source.path.parent}")

        failed = [r for r in results if r.status.value in ("FAIL", "ERROR")]
        return 1 if failed else 0

    except Py2BbrError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    finally:
        progress.stop()


if __name__ == "__main__":
    sys.exit(main())
