# py2bbr

Python to Bitbake Recipe.

Convert a Python package into a Yocto Project *Bitbake recipe* typically
for use in a Yocto layer, including PetaLinux projects.

### Outline and status

`py2bbr` fetches a package from any PyPI-compatible index (honouring pip flags
such as `--extra-index-url`, so private indexes work), derives the Bitbake
variables from it, and generates a `python3-<name>.bb` recipe---optionally
wrapped into a full Yocto layer. It also tries to deal with the full dependency
tree, including Python packages and OE layers/recipes. It defaults to the
Yocto 5.0 series (Scarthgap) and validates the generated recipe to the extent
the local toolchain allows.

There is experimental support for `kas`, but very little testing so far.

### Rationale

Writing recipe by hand is difficult and repetitive, meaning hunting down license
file, computing `LIC_FILES_CHKSUM`, mapping `Requires-Dist` to `RDEPENDS`, and
picking the right `inherit` classes. `py2bbr` is an attempt at doing all that
mechanically, directly from the target package and its formal dependencies.


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

Tests note: Some tests need GNU `sed` available in the environment. On BSD
systems, these tests are skipped. They can be run by installing the GNU
version, either named `sed` or `gsed`.


## Usage

```console
# Generate a recipe + layer (+ experimental kas build file) for `pyserial` (Scarthgap by default)
py2bbr pyserial

# Version pinning (top package only), Yocto selection
py2bbr "requests==2.34.2" --yocto-version 5.0

# Name the target runtime and/or arch (defaults: this interpreter, this machine)
py2bbr pyvizionsdk --python-version 3.12 --target-arch aarch64

# Alternative or private index
py2bbr mytool \
    --extra-index-url https://pypi.mycompany.com/+simple \
    --trusted-host pypi.mycompany.com
```

Note: By default `py2bbr` bundles dependencies following 2 rules:

1. Stricly follow version requirements (`numpy==1.2.3`, or `numpy>=1.2.3`)
2. If there is no version upper bound, automatically reserve the major version (`numpy>=1.2.3` becomes `numpy>=1.2.3,<2`)

The second rule can be deactivated with `--no-major-bump-cap`.

### Output

Typically under a [Yocto Poky](https://github.com/yoctoproject/poky) `build` folder:

```
.
├── meta-pyserial/
│   ├── README
│   ├── conf/layer.conf
│   └── recipes-devtools/python/python3-pyserial_3.5.bb
└── py2bbr.kas.yml
```

The layer is named after the package (`meta-<package>`, e.g.
`meta-async-pyserial` for `async_pyserial`) so every generated layer is unique;
override with `--layer-name`.


## Key options

| Flag | Meaning |
| --- | --- |
| `--build-class` | Override the inherited build class (default auto is `setuptools3`). |
| `--check {structural,parse,checklayer,build}` | Validation levels (default `parse`). |
| `--index-url`, `--extra-index-url`, `--trusted-host` | Subset of pip index options forwarded to `pip download`. |
| `--jobs N` | Fetch dependencies in parallel (default `4`; `1` is sequential). |
| `--keep-sources` | Keep downloaded files for debugging. |
| `--layer-name` | Override the layer directory name (default: `meta-<package>`, e.g. `meta-async-pyserial`). |
| `--max-depth N` | Cap the dependency tree walk depth (default: unlimited). |
| `--no-deps` | Disable dependency closure (default is on): Generate only the top recipe and leave its RDEPENDS to other layers (e.g. meta-python). |
| `--no-layer`, `--no-kas` | Generate only the recipe and skip the kas file. |
| `--no-major-bump-cap` | Do not cap dependency majors. By default every, dependency with a lower bound only (`numpy>=1.21.0`) is resolved as `numpy>=1.21.0,<2`---the latest release of the major series (1.x), not a later major (2.x) as it could break builds (typically to avoid numpy 2.x breaking OpenCV, etc). Strong bounds (`~=`, `<`, `<=`, `==`) are untouched. |
| `--pip-arg` | Append any extra raw argument to `pip download` (repeatable). |
| `--python-version` | Python version the target image runs (`3.12`). Default: The interpreter py2bbr itself runs under. |
| `-q`/`--quiet`, `-v`/`--verbose` | Logging: INFO stage progress lines on stderr (default); `-q` restricts to warnings/errors (and disables the live fetch progress bars); `-v` adds DEBUG detail (per-package fetch/parse summaries, public-PyPI reachability). |
| `--skip-dep NAME` | Exclude a dependency from the closure (repeatable; canonical names). |
| `--target-arch`, `--arch` | CPU the target image runs, as `platform.machine()` reports it (`aarch64`, `armv7l`, `x86_64`, `riscv64`). Default: This machine arch. |
| `--targets LIST` | Restrict the final report to these target keys (comma-separated, e.g. `zynqmp,versal`); an unknown key is reported and ignored. Default: the whole list. |
| `--with-deps` | Walk the dependency tree and render one recipe per member in the same layer (default; bitbake follows RDEPENDS). |
| `--yocto-version` | Yocto version or codename (like `5.0`, `scarthgap`). Default `5.0`. |


## Generation outline

Here is an overview of what `py2bbr` does:

1. `pip download` the target package (sdist preferred, wheel fallback).
   Any wheel fallback is pinned to the target runtime and architecture.
2. Parse `PKG-INFO`/`METADATA`, read `pyproject.toml` for the build system
   and dependencies, license file and digest checking.
3. Classify `Requires-Dist` with [`packaging`](https://pypi.org/project/packaging/).
4. Inspect the source for compiled code (`.c`/`.cpp`/`.pyx`/`.rs`/...,
   `ext_modules` in `setup.py`, native build requirements), etc.
5. Render the recipe (with `inherit pypi setuptools3` pattern), a
   `meta-<package>` layer named after the package name, and a `kas`
   build file.
6. Run the validation levels.
7. Report.


## Target runtimes

`py2bbr` generates layer and recipe by default for the executing runtime and
architecture. It can generate for a different Python runtime and/or architecture
using its respective optional arguments:

```bash
# An older PetaLinux image running Python 3.12 on a 64-bit ARM CPU
py2bbr pyvizionsdk --python-version 3.12 --target-arch aarch64
```

| | Default | Override with |
| --- | --- | --- |
| Python version | the interpreter `py2bbr` itself runs under | `--python-version 3.12` (`3.12.4` and `312` are currently accepted but reduced to `3.12`) |
| CPU architecture | this machine (`platform.machine()`) | `--target-arch aarch64` (`--arch`; `arm64` --> `aarch64`, `amd64` --> `x86_64`, `armv7` --> `armv7l`) |

The resolved values are reported (`Runtime : Python 3.12 on aarch64`) and
recorded in the recipe.


## Checks

Checks attempt to validate as much of the generated layer and recipe as possible.
Checks are organised in levels of different values and costs, with different
tool requirements (some need `bitbake` available, etc). They are selected with
`--check LEVEL`, where all checks up to `LEVEL` get run.  Stages that are not
runnable in the current environment are reported as `SKIPPED`.

| Level | What it does | Requires |
| --- | --- | --- |
| `structural` | Pure-Python checks on the `.bb` (required vars, balanced quotes/braces, corruption signatures: timestamp residue, `-`-prefixed dep tokens, single-quote balance). | nothing |
| `parse` | `bitbake -p <pn>` (parse-only). | `bitbake` + a sourced `BUILDDIR` |
| `checklayer` | `yocto-check-layer <layer>` (runs an expensive `bitbake world`). | `yocto-check-layer` + `BUILDDIR` |
| `build` | `kas build -c <kasfile>` (full build). | `kas` + network |


## Supported Yocto series

`py2bbr` contains a mapping of Yocto version/codename / Poky branch / target Python.
Currently: **6.0 (Wayne)** and **5.0 (Scarthgap, default)** are supported. We expect
4.3 (Styhead), 4.1 (Mickledore) and 3.1 (Dunfell) to work too, but untested (so
commented out in code for now). Add entries in `src/py2bbr/yocto.py` to start
supporting more.

End-to-end tests are currently run on 5.0 (the default), and the others are "experimental"
for now using LLM generated code.


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
run under the `mylayer` folder. Currently it creates a `mylayer/build/meta-async-pyserial`
folder and related.
* `py2bbr` has been called to target the same Yocto version as PetaLinux. Here both use
  Yocto 5.0 series, Scarthgap.

The steps are as follows:

1. Copy the layer into the project (documented location, UG1144 "Adding Layers"):

```
cp -r mylayer/build/meta-async-pyserial pl-project/project-spec/
```

2. Register the layer

```
cd pl-project
petalinux-config   # Menus: Yocto Settings --> User Layers
# Enter: ${PROOT}/project-spec/meta-async-pyserial
# Save and exit
Verify the layer now appears in <proj>/build/conf/bblayers.conf.
```

3. Add the package to the rootfs:

```
echo 'CONFIG_python3-async-pyserial' >> pl-project/project-spec/meta-user/conf/user-rootfsconfig
petalinux-config -c rootfs   # Menus: user packages --> enable python3-async-pyserial
```

4. Rebuild:

```
petalinux-build
```

5. Verify on the target (e.g. in QEMU, or directly on device:

```
python3 -c "import async_pyserial"
```

## Disclosure

This work has involved manual and generated code, using:

* Local version of Qwen 3.8
* Online version of OpenCode Big Pickle


## License

MIT — see [LICENSE](LICENSE).
