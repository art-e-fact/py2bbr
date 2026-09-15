# py2bbr

Convert a Python package into a Yocto Project **Bitbake recipe** — with a
ready-to-build **kas** build file and a capability-gated validation ladder.

`py2bbr` fetches a package's authoritative metadata from any PyPI-compatible
index (honouring pip flags such as `--extra-index-url`, so private indexes work),
derives the Bitbake variables from it, and emits a `python3-<name>.bb` recipe —
optionally wrapped in a full Yocto layer. It defaults to the **Yocto 5.0 series
(Scarthgap)** and validates the generated recipe to the extent the local toolchain
allows.

## Why

Writing a `python3-*.bb` recipe by hand means hunting down the license file,
computing `LIC_FILES_CHKSUM`, mapping `Requires-Dist` (with extras and environment
markers) to `RDEPENDS`, and picking the right `inherit` classes. `py2bbr` does that
mechanically from the package's own metadata.

## Install

Hopefully from PyPi soonish. For now, please install from the repository.

## Dev Install

Developed with Python **3.11+** and [uv](https://docs.astral.sh/uv/).

```console
uv sync --extra dev
uv run py2bbr --help

# Tests
uv run pytest
```

## Usage

```console
# Generate a recipe + layer + kas build file for `pyserial` (Scarthgap by default)
$ py2bbr pyserial

# Pin a version, pick a Yocto series
$ py2bbr "requests==2.34.2" --yocto-version 5.0

# Reach a package that is not on PyPI (private index)
$ py2bbr mytool \
    --extra-index-url https://pypi.mycompany.com/simple \
    --trusted-host pypi.mycompany.com

# Go all the way to a real build (requires kas + network)
$ py2bbr pyserial --check build
```

### Output

```
.
├── meta-py2bbr/
│   ├── README
│   ├── conf/layer.conf
│   └── recipes-devtools/python/python3-pyserial_3.5.bb
└── py2bbr.kas.yml
```

The `kas` build file (`py2bbr.kas.yml`) sets up poky + meta-python at the matching
branch, includes the generated layer, and targets the recipe — so `kas build -c
py2bbr.kas.yml` compiles it in a clean environment.

## Key options

| Flag | Meaning |
| --- | --- |
| `--yocto-version` | Yocto version or codename (`5.0`, `scarthgap`, …). Default `5.0`. |
| `--index-url`, `--extra-index-url`, `--trusted-host` | pip index options, forwarded to `pip download`. |
| `--pip-arg` | Append any extra raw argument to `pip download` (repeatable). |
| `--check {structural,parse,checklayer,build}` | Validation ceiling (default `parse`). |
| `--build-class` | Override the inherited build class (default auto → `setuptools3`). |
| `--no-layer`, `--no-kas` | Emit only the recipe / skip the kas file. |
| `--keep-sources` | Keep downloaded artifacts for debugging. |

## Checks

`--check LEVEL` runs the ladder up to and including `LEVEL`. Stages that are not
runnable in the current environment are reported as `SKIPPED` with the reason, so the
output always states exactly what was validated:

| Stage | What it does | Requires |
| --- | --- | --- |
| `structural` | Pure-Python checks on the `.bb` (required vars, balanced quotes/braces). | nothing |
| `parse` | `bitbake -p <pn>` (parse-only). | `bitbake` + a sourced `BUILDDIR` |
| `checklayer` | `yocto-check-layer <layer>` (runs `bitbake world`; expensive). | `yocto-check-layer` + `BUILDDIR` |
| `build` | `kas build -c <kasfile>` (full build). | `kas` + network |

## Supported Yocto series

`py2bbr` ships a mapping of Yocto version → codename / poky branch / target Python.
Currently: **6.0 (Wayne)**, **5.0 (Scarthgap, default)**, 4.3 (Styhead), 4.1
(Mickledore), 3.1 (Dunfell). Add entries in `src/py2bbr/yocto.py` if you need more.

Tests are currently conducted on 5.0 (the default), and the others are "experimental"
for now using LLM generated code.

## How it works

1. `pip download` the package (sdist preferred, wheel fallback) honouring your index
   flags, into an isolated dir.
2. Parse `PKG-INFO`/`METADATA` (stdlib `email`), read `pyproject.toml`
   `[build-system]` for the backend + build deps, locate the license file and compute
   its MD5, and compute the sdist SHA-256.
3. Classify `Requires-Dist` with [`packaging`](https://pypi.org/project/packaging/):
   optional extras and non-target markers are dropped, the rest become
   `RDEPENDS:${PN} += "${PYTHON_PN}-<dep>"`.
4. Render the recipe (canonical `inherit pypi setuptools3` pattern), a layer, and a
   `kas` build file (validated against the official kas JSON schema).
5. Run the validation ladder.

## License

MIT — see [LICENSE](LICENSE).
