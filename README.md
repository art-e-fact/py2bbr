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
uv sync --dev
uv run py2bbr --help

# Tests
uv run pytest
```

## Usage

```console
# Generate a recipe + layer + kas build file for `pyserial` (Scarthgap by default)
py2bbr pyserial

# Pin a version, pick a Yocto series
py2bbr "requests==2.34.2" --yocto-version 5.0

# Reach a package that is not on PyPI (private index)
py2bbr mytool \
    --extra-index-url https://pypi.mycompany.com/simple \
    --trusted-host pypi.mycompany.com

# Go all the way to a real build (requires kas + network)
py2bbr pyserial --check build
```

### Output

Typically under a [Yocto Poky](https://github.com/yoctoproject/poky) `build` folder:

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
Currently: **6.0 (Wayne)** and **5.0 (Scarthgap, default)** are supported. We expect
4.3 (Styhead), 4.1 (Mickledore) and 3.1 (Dunfell) to work too, but untested (so
commented out in code for now). Add entries in `src/py2bbr/yocto.py` to start
supporting more.

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

## Examples

### Add async-pyserial to a PetaLinux project

A concrete use case for py2bbr is to create layer/recipes to add to projects like managed
by PetaLinux. Here is an example to add the `async-pyserial` package available on PyPi to
a PetaLinux 2024.2 project (choice led by one of our current projects---nothing special
to it and should work on other versions by double checking their documentations).

We assume:

* A PetaLinux 2024.2 project created under the `pl-project` folder. The PetaLinux
  distribution must be recent enough to accept Python packages by default, or configured
  to accept them (typically a `petalinux-config` command).
* A layer/recipe for `async-pyserial` create by py2bbr: `uv run py2bbr async-pyserial`,
  run under the `mylayer` folder. Currently it creates a `mylayer/build/meta-py2bbr`
  folder and related.
* `py2bbr` has been called to target the same Yocto version as PetaLinux. Here both use
  Yocto 5.0 series, Scarthgap.

The steps are as follows:

1. Copy the layer into the project (documented location, UG1144 "Adding Layers"):

```
cp -r mylayer/build/meta-py2bbr pl-project/project-spec/
```

2. Register the layer

```
cd pl-project
petalinux-config   # → Yocto Settings → User Layers
# enter: ${PROOT}/project-spec/meta-py2bbr
# save and exit
Verify the layer now appears in <proj>/build/conf/bblayers.conf.
```

3. Add the package to the rootfs:

```
echo 'CONFIG_python3-async-pyserial' >> pl-project/project-spec/meta-user/conf/user-rootfsconfig
petalinux-config -c rootfs   # → user packages → enable python3-async-pyserial
```

4. Rebuild:

```
petalinux-build
```

5. Verify on the target (e.g. in QEMU, or directly on device:

```
python3 -c "import async_pyserial"
```

Note on minimal PetaLinux: Some installations of PetaLinux do not include `python3`, reportedly `petalinux-image-minimal`. If Python is reported missing:
- Add `python3` to the `IMAGE_INSTALL:append` line (in interactive configuration or under pl-project/project-spec/configs`.
- Switch to the full image (petalinux-build targeting petalinux-image-full), which includes `python3` and `pip`.

## License

MIT — see [LICENSE](LICENSE).
