"""
Yocto series handling, layer generation and ``kas`` build-file emission.
"""

import logging
import platform
import re
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from py2bbr.errors import TargetRuntimeError, YoctoVersionError
from py2bbr.model import PackageMetadata, YoctoVersion
from py2bbr.recipe import bitbake_pn, canonical_name, recipe_filename

logger = logging.getLogger(__name__)

# Known Yocto Project series.
_YOCTO_SERIES: dict[str, YoctoVersion] = {
    "6.0": YoctoVersion(
        "6.0",
        "wayne",
        "wayne",
        "3.13",
        meta_python_branch="wayne",
    ),
    "5.0": YoctoVersion(
        "5.0",
        "scarthgap",
        "scarthgap",
        "3.12",
        meta_python_branch="scarthgap",
    ),
    # "4.3": YoctoVersion(
    #     "4.3",
    #     "styhead",
    #     "styhead",
    #     "3.11",
    #     meta_python_branch="styhead",
    # ),
    # "4.1": YoctoVersion(
    #     "4.1",
    #     "mickledore",
    #     "mickledore",
    #     "3.10",
    #     meta_python_branch="mickledore",
    # ),
    # "3.1": YoctoVersion(
    #     "3.1",
    #     "dunfell",
    #     "dunfell",
    #     "3.9",
    #     meta_python_branch="dunfell",
    # ),
}

# Index by codename too, so ``--yocto-version scarthgap`` works.
#
# Note: No conflict as codenames are not numbers, while versions
#       must be numbers.
for _v in list(_YOCTO_SERIES.values()):
    _YOCTO_SERIES.setdefault(_v.codename, _v)

# Default kas file format version (within the schema's 1..23 range).
DEFAULT_KAS_VERSION = 5


def known_versions() -> list[YoctoVersion]:
    """
    The known series, newest first.

    Only presented per version numbers. Codenames are not used to index know
    versions here.
    """
    seen: dict[str, YoctoVersion] = {}
    for v in _YOCTO_SERIES.values():
        seen.setdefault(v.version, v)
    return sorted(
        seen.values(), key=lambda v: _version_key(v.version), reverse=True
    )


def _version_key(version: str) -> tuple[int, ...]:
    parts = []
    for chunk in version.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            break
    return tuple(parts) or (0,)


def resolve_yocto_version(spec: str | None) -> YoctoVersion:
    """
    Resolve a ``--yocto-version`` value to a `YoctoVersion`.

    Accepts a version (``5.0``, ``5``, ``5.0.0``) or a codename (``scarthgap``).

    Defaults to the 5.0 series (Scarthgap).
    """
    if spec is None:
        spec = "5.0"

    key = spec.strip().lower()
    if key in _YOCTO_SERIES:
        return _YOCTO_SERIES[key]

    # Both "5" and "5.0" are accepted and resolved.
    if key.isdigit() or key[0:1].isdigit():
        candidate = f"{key}.0"
        if candidate in _YOCTO_SERIES:
            return _YOCTO_SERIES[candidate]

    available = ", ".join(
        f"{v.version} ({v.codename})" for v in known_versions()
    )
    raise YoctoVersionError(
        f"unknown Yocto version {spec!r}. Known versions: {available}"
    )


def current_python_version() -> str:
    """``major.minor`` of the interpreter py2bbr itself runs under.

    The default for ``--python-version``: the artifacts on PyPI a developer
    machine resolves are the ones its own interpreter matches, so that is the
    least surprising answer. A target that differs (an older PetaLinux on
    Python 3.12, say) has to be named explicitly.
    """
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def current_platform_machine() -> str:
    """``platform.machine()`` of the build host, lower-cased.

    Falls back to ``x86_64`` - the build architecture py2bbr assumes
    everywhere else - if the platform does not report one, so the
    ``--target-arch`` default is never a value that has to be rejected. A host
    that cannot say what it is is a host whose target gets named explicitly,
    which is the situation anyway.
    """
    return platform.machine().strip().lower() or "x86_64"


#: Spellings of one architecture that differ between the command line,
#: ``platform.machine()`` and wheel platform tags. Normalising to the value PEP
#: 508 markers use on the target is what makes ``--target-arch arm64`` evaluate
#: ``platform_machine == "aarch64"`` the way the target's own Python would.
#: Key: a non-canonical spelling; value: the canonical ``platform_machine``.
_ARCH_ALIASES: dict[str, str] = {
    "amd64": "x86_64",
    "x64": "x86_64",
    "x86-64": "x86_64",
    "arm64": "aarch64",
    "arm64-v8a": "aarch64",
    "armv8": "aarch64",
    "armv7": "armv7l",
    "armv7hf": "armv7l",
    "armhf": "armv7l",
    "arm32": "armv7l",
    "i386": "i686",
    "i486": "i686",
    "i586": "i686",
    "x86": "i686",
    "riscv": "riscv64",
}


def normalize_python_version(spec: str) -> str:
    """Reduce a ``--python-version`` value to ``major.minor``.

    ``3.12``, ``3.12.4`` and ``312`` all name 3.12. A patch component is
    dropped on purpose: ``python_full_version`` is derived by appending
    ``.0`` to this value, and bitbake/pypi name runtimes by major.minor.
    """
    raw = (spec or "").strip()
    if not raw:
        raise TargetRuntimeError(
            "empty --python-version; expected a version like 3.12"
        )
    candidate = raw
    if candidate.isdigit() and len(candidate) > 2:
        # pip's dotted-or-not form: "312" -> "3.12".
        candidate = f"{candidate[0]}.{candidate[1:]}"
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)*", candidate)
    if not match:
        raise TargetRuntimeError(
            f"invalid --python-version {spec!r}; expected a version like 3.12"
        )
    return f"{int(match.group(1))}.{int(match.group(2))}"


def normalize_platform_machine(spec: str) -> str:
    """Canonicalise a ``--target-arch`` value into a ``platform_machine``."""
    raw = (spec or "").strip().lower()
    if not raw:
        raise TargetRuntimeError(
            "empty --target-arch; expected an architecture like aarch64"
        )
    machine = _ARCH_ALIASES.get(raw, raw)
    if not re.fullmatch(r"[a-z0-9_]+", machine):
        raise TargetRuntimeError(
            f"invalid --target-arch {spec!r}; expected an architecture like "
            "aarch64 (as platform.machine() reports it)"
        )
    return machine


def resolve_target_runtime(
    yocto: YoctoVersion,
    python_version: str | None = None,
    platform_machine: str | None = None,
) -> YoctoVersion:
    """Return ``yocto`` with the target runtime the run asked for.

    Both axes default to what py2bbr runs on (the machine doing the generating),
    which is a guess about the image; ``--python-version`` / ``--target-arch``
    replace it. The result is a plain :class:`YoctoVersion` copy, so every
    downstream consumer - marker evaluation, the wheel the recipe installs, the
    classifier pruning, the build-for-target report - sees one consistent target
    without any of them having to know which flags were passed.

    Raises :class:`~py2bbr.errors.TargetRuntimeError` on a value that is not a
    version / architecture: silently falling back to the series' answer would
    generate a layer for the wrong target, which is the failure this flag
    exists to prevent.
    """
    changes: dict[str, str] = {}
    if python_version is not None:
        changes["python_version"] = normalize_python_version(python_version)
    if platform_machine is not None:
        changes["platform_machine"] = normalize_platform_machine(
            platform_machine
        )
    return replace(yocto, **changes) if changes else yocto


def layer_collection(layer_name: str) -> str:
    """Derive a safe ``BBFILE_COLLECTIONS`` name from a layer directory name."""
    name = re.sub(r"^[mM]eta[-_]?", "", layer_name)
    name = re.sub(r"[^a-zA-Z0-9]+", "_", name).lower().strip("_")
    return name or "py2bbr"


def default_layer_name(package_name: str) -> str:
    """The default layer directory name for a package: ``meta-<canonical>``.

    e.g. ``async_pyserial`` -> ``meta-async-pyserial``. One layer per package
    (per PEP 503 canonical name), so generated layers never collide.
    """
    return f"meta-{canonical_name(package_name)}"


def _layer_conf(collection: str, codename: str) -> str:
    # BBFILES: bitbake only scans files matching BBFILES globs, so a layer
    # that never adds its recipe tree is invisible ("No bb files ... matched
    # BBFILE_PATTERN_<c>") even though the layer itself loads.
    # LAYERDEPENDS: only hard-depend on "core". bitbake refuses to load a
    # layer whose LAYERDEPENDS names a collection not enabled in BBLAYERS and
    # silently drops every recipe in it ("Nothing PROVIDES ..."). The classes
    # py2bbr emits (pypi, setuptools3, python_pep517,
    # python_setuptools_build_meta) all live in poky core for the supported
    # series, so meta-python ("python") must not be a hard layer dependency;
    # recipe-level needs of it fail per-recipe with a precise error instead.
    # LAYERSERIES_COMPAT: the modern series-compatibility variable; the old
    # LAYERCOMPATIBLE is no longer read by bitbake.
    return (
        f'BBPATH .= ":${{LAYERDIR}}"\n'
        f'BBFILES += "${{LAYERDIR}}/recipes-*/*/*.bb"\n'
        f"\n"
        f'BBFILE_COLLECTIONS += "{collection}"\n'
        f'BBFILE_PATTERN_{collection} := "^${{LAYERDIR}}/"\n'
        f'BBFILE_PRIORITY_{collection} = "5"\n'
        f'LAYERDEPENDS_{collection} = "core"\n'
        f'LAYERSERIES_COMPAT_{collection} = "{codename}"\n'
    )


def _layer_readme(meta: PackageMetadata, yocto: YoctoVersion) -> str:
    return (
        f"# py2bbr-generated layer\n"
        f"\n"
        f"Generated by py2bbr. This layer provides a Bitbake recipe for the\n"
        f"Python package `{meta.name}` ({meta.version}) targeting\n"
        f"{yocto.label}, {yocto.runtime_label}.\n"
        f"\n"
        f"Recipe: `recipes-devtools/python/{recipe_filename(bitbake_pn(meta.name, yocto.python_pn), meta.version)}`\n"
    )


def render_layer(
    meta: PackageMetadata,
    yocto: YoctoVersion,
    layer_name: str,
    build_class: str | None = None,
    extra_metas: list[PackageMetadata] | None = None,
) -> dict[str, str]:
    """Render a full Yocto layer as ``{relative_path: content}``.

    ``extra_metas`` (the other members of the runtime-dependency closure, in
    BFS order, ``extra_metas[0]`` excluded — ``meta`` is the top recipe) each
    render their own recipe into the *same* layer. One recipe per member;
    ``RDEPENDS:${PN}`` on each member pulls the next recipe transitively
    (bitbake follows RDEPENDS), so the kas target stays ``meta``'s PN.
    """
    from py2bbr.recipe import render_recipe

    collection = layer_collection(layer_name)
    pn = bitbake_pn(meta.name, yocto.python_pn)
    recipe_path = f"recipes-devtools/python/{recipe_filename(pn, meta.version)}"
    recipe_text, _warnings = render_recipe(meta, yocto, build_class=build_class)

    files = {
        "conf/layer.conf": _layer_conf(collection, yocto.codename),
        "README": _layer_readme(meta, yocto),
        recipe_path: recipe_text,
    }

    for member in extra_metas or ():
        member_pn = bitbake_pn(member.name, yocto.python_pn)
        member_path = (
            "recipes-devtools/python/"
            f"{recipe_filename(member_pn, member.version)}"
        )
        member_text, _warnings = render_recipe(
            member, yocto, build_class=build_class
        )
        files[member_path] = member_text

    return files


def write_layer(
    meta: PackageMetadata,
    yocto: YoctoVersion,
    layer_dir: Path,
    build_class: str | None = None,
    extra_metas: list[PackageMetadata] | None = None,
    vendor_sources: dict[str, Path] | None = None,
) -> Path:
    """Write the layer tree under ``layer_dir``; return the top recipe path.

    ``vendor_sources`` maps recipe file names (e.g. ``python3-x_1.0.bb``) to
    the artifact each vendored recipe must ship; each is copied into the
    layer's ``recipes-devtools/python/files/`` so the ``file://`` SRC_URI
    resolves through FILESPATH. A vendored recipe without its source is an
    error: the generated recipe would reference a file the layer does not
    carry.
    """
    layer_dir = Path(layer_dir)
    files = render_layer(
        meta,
        yocto,
        layer_dir.name,
        build_class=build_class,
        extra_metas=extra_metas,
    )
    vendored_recipes = {
        recipe_filename(bitbake_pn(m.name, yocto.python_pn), m.version)
        for m in [meta, *(extra_metas or [])]
        if m.vendored
    }
    missing = vendored_recipes - set(vendor_sources or {})
    if missing:
        raise ValueError(
            "vendored recipe(s) lack their sdist in vendor_sources: "
            + ", ".join(sorted(missing))
        )
    recipe_path = None
    for rel, content in files.items():
        dest = layer_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        if rel.endswith(".bb") and (
            recipe_path is None
            or rel.startswith(
                f"recipes-devtools/python/{recipe_filename(bitbake_pn(meta.name, yocto.python_pn), meta.version)}"
            )
        ):
            recipe_path = dest
    for src in (vendor_sources or {}).values():
        files_dir = layer_dir / "recipes-devtools" / "python" / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, files_dir / Path(src).name)
    if vendor_sources:
        logger.info(
            "vendored %d sdist(s) into %s",
            len(vendor_sources),
            layer_dir / "recipes-devtools" / "python" / "files",
        )
    logger.info(
        "wrote layer %s (%d .bb recipe(s))",
        layer_dir,
        sum(1 for rel in files if rel.endswith(".bb")),
    )
    return layer_dir if recipe_path is None else recipe_path


def render_kas_file(
    meta: PackageMetadata,
    yocto: YoctoVersion,
    layer_dir: Path,
    machine: str = "qemux86-64",
    kas_version: int = DEFAULT_KAS_VERSION,
    extra_targets: list[str] | None = None,
    include_meta_python: bool = True,
) -> str:
    """
    Render a ``kas`` build file (YAML) that builds the generated recipe.

    The file references poky at the matching branch and includes the generated
    layer as a local (non-URL) repo. ``include_meta_python`` adds the
    meta-python layer (needed only when deps are left to it, i.e. ``--no-deps``;
    a self-contained layer must exclude it or its recipes would duplicate the
    generated member recipes).
    """
    import yaml

    from py2bbr.recipe import bitbake_pn

    layer_dir = Path(layer_dir).resolve()
    pn = bitbake_pn(meta.name, yocto.python_pn)
    targets = list(dict.fromkeys([pn] + list(extra_targets or [])))

    repos: dict[str, dict] = {
        "poky": {
            "url": yocto.poky_url,
            "branch": yocto.poky_branch,
        },
        layer_dir.name: {
            "url": None,
            "path": str(layer_dir),
        },
    }
    if include_meta_python:
        repos["meta-python"] = {
            "url": yocto.meta_python_url,
            "branch": yocto.meta_python_branch or yocto.codename,
        }

    config = {
        "header": {"version": kas_version},
        "build_system": "openembedded",
        "machine": machine,
        "target": targets,
        "repos": repos,
    }
    return yaml.safe_dump(config, sort_keys=False, default_flow_style=False)


def validate_kas_file(config: dict) -> list[str]:
    """
    Validate a kas config dict against the official kas JSON schema.

    Returns a list of error strings (empty if valid). Falls back to a minimal
    structural check if ``kas``/``jsonschema`` are unavailable.
    """
    try:
        import jsonschema  # type: ignore
        from kas.configschema import CONFIGSCHEMA  # type: ignore
    except Exception:  # noqa: BLE001
        # Minimal fallback: the schema requires a header with a version.
        if not isinstance(config, dict) or "header" not in config:
            return ["kas config missing required 'header'"]
        if "version" not in config.get("header", {}):
            return ["kas config header missing 'version'"]
        return []

    try:
        validator = jsonschema.Draft7Validator(CONFIGSCHEMA)
        errors = sorted(
            validator.iter_errors(config), key=lambda e: list(e.path)
        )
        return [
            f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
            for err in errors
        ]
    except Exception as exc:  # noqa: BLE001
        return [f"kas schema validation raised: {exc}"]
