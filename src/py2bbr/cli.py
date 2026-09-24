"""
Command-line interface for py2bbr.

Usage examples (see README.md for more information):

    py2bbr black
    py2bbr "requests==2.34.2" --yocto-version 5.0
    py2bbr mytool --extra-index-url https://pypi.mycompany.com/simple \\
                 --trusted-host pypi.mycompany.com --check build
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

from py2bbr import __version__
from py2bbr.check import DEFAULT_LEVEL, STAGES, run_checks
from py2bbr.errors import Py2BbrError
from py2bbr.metadata import PipOptions, build_metadata
from py2bbr.model import DEFAULT_YOCTO_VERSION
from py2bbr.recipe import bitbake_pn, recipe_filename, render_recipe_to_path
from py2bbr.yocto import (
    DEFAULT_KAS_VERSION,
    default_layer_name,
    render_kas_file,
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

    yocto = p.add_argument_group("yocto")
    yocto.add_argument(
        "--yocto-version",
        default=DEFAULT_YOCTO_VERSION,
        help="Yocto version (e.g. 5.0) or codename (e.g. scarthgap).",
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


def _configure_logging(args) -> None:
    """Route py2bbr stage progress to stderr: INFO by default, -q->WARNING, -v->DEBUG.

    The level is set on the ``py2bbr`` logger (its ``py2bbr.*`` children
    inherit it), so unrelated libraries are untouched. The stderr handler is
    attached only when none exists yet, keeping the call idempotent across
    repeated ``main()`` invocations (tests call it many times in one process).
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
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("[py2bbr] %(levelname)s %(message)s")
        )
        lg.addHandler(handler)


def _print_report(
    args, meta, recipe_path, layer_dir, kas_file, warnings, results
):
    if not args.quiet:
        print(f"Package      : {meta.name} {meta.version}")
        print(
            f"Target       : {resolve_yocto_version(args.yocto_version).label}"
        )
        print(f"License      : {meta.license or '(none detected)'}")
        print(f"Homepage     : {meta.homepage or '-'}")
        print(f"Build backend: {meta.build_backend or '(setuptools/setup.py)'}")
        print(f"Runtime deps : {len(meta.runtime_deps)}")
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args)

    try:
        name, version = _parse_package_spec(args.package)
        yocto = resolve_yocto_version(args.yocto_version)
        logger.debug("yocto target: %s (%s)", yocto.version, yocto.codename)
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
            )
        logger.info("closure: %d package(s) resolved", len(closure))

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
        _print_report(
            args,
            meta,
            recipe_path,
            layer_dir if not args.no_layer else None,
            kas_file,
            warnings,
            results,
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


if __name__ == "__main__":
    sys.exit(main())
