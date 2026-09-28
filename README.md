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
├── meta-pyserial/
│   ├── README
│   ├── conf/layer.conf
│   └── recipes-devtools/python/python3-pyserial_3.5.bb
└── py2bbr.kas.yml
```

The layer is named after the package (`meta-<package>`, e.g.
`meta-async-pyserial` for `async_pyserial`) so every generated layer is unique;
override with `--layer-name`.

The `kas` build file (`py2bbr.kas.yml`) sets up poky at the matching branch,
includes the generated layer, and targets the recipe — so `kas build -c
py2bbr.kas.yml` compiles it in a clean environment. meta-python is only added
when deps are left to it (`--no-deps`); a self-contained layer (the default)
carries a recipe for every dependency, so meta-python would only duplicate
providers.

## Key options

| Flag | Meaning |
| --- | --- |
| `--yocto-version` | Yocto version or codename (`5.0`, `scarthgap`, …). Default `5.0`. |
| `--index-url`, `--extra-index-url`, `--trusted-host` | pip index options, forwarded to `pip download`. |
| `--pip-arg` | Append any extra raw argument to `pip download` (repeatable). |
| `--check {structural,parse,checklayer,build}` | Validation ceiling (default `parse`). |
| `--build-class` | Override the inherited build class (default auto → `setuptools3`). |
| `--layer-name` | Override the layer directory name (default: `meta-<package>`, e.g. `meta-async-pyserial`). |
| `--no-layer`, `--no-kas` | Emit only the recipe / skip the kas file. |
| `--keep-sources` | Keep downloaded artifacts for debugging. |
| `--with-deps` | Resolve the transitive runtime-dependency closure and render one recipe per member in the same layer (default; the kas target stays the top recipe, bitbake follows RDEPENDS). |
| `--no-deps` | Disable the closure (default is on): emit only the top recipe and leave its RDEPENDS to other layers (e.g. meta-python). |
| `--max-depth N` | Cap the closure depth (default: unlimited). |
| `--skip-dep NAME` | Exclude a dependency from the closure (repeatable; canonical names). |
| `--targets LIST` | Restrict the build-for-target report to these target keys (comma-separated, e.g. `zynqmp,versal`); an unknown key is reported and ignored. Default: the whole matrix. |
| `--jobs N` | Fetch closure dependencies in parallel (default `4`; `1` = sequential). Each in-flight fetch shows its own live progress bar on stderr (TTY only). The top package and its direct deps keep their order; deeper members always come after the member that discovered them, but members found by two concurrent parents can interleave at the same depth (order has no effect on the generated layer, since bitbake follows `RDEPENDS`). |
| `-q`, `-v` | Logging: INFO stage progress lines on stderr (default); `-q` restricts to warnings/errors (and disables the live fetch progress bars); `-v` adds DEBUG detail (per-package fetch/parse summaries, public-PyPI reachability). |

## Building for the target (cross builds)

A layer is built by the **build host** (usually x86_64) and the image runs on the
**target** CPU. Two architectures are in play, and py2bbr keeps them apart:

* the build **tools** (`-native` recipes: cython, meson, ninja, patchelf,
  pypa/build) are *host* binaries — the PEP 517 frontend that drives the build
  has to run on the machine doing the building;
* the **package** must be compiled for the target, otherwise the image carries
  x86_64 extension modules the target cannot load.

py2bbr makes that explicit in two places.

**1. A cross-build comment in the recipe.** When the sdist ships compiled code,
the recipe states which mechanism gets the target toolchain into the build, and
why the build tools stay `-native`:

```
# Cross-build: this sdist ships compiled code (C source (numpy-2.5.3/numpy/_core/
# check_longdouble.c), ...). python_mesonpy runs meson with the target cross
# file, so the extension modules are compiled for the target CPU even though the
# layer is built on the (usually x86_64) build host. ...
```

**2. A build-for-target report** for x86, x86_64, the ARM Cortex targets
(32-bit and 64-bit), Zynq UltraScale+ MPSoC, Versal / Versal AI and RISC-V 64.
Findings that hold for every target are printed once, then one line per target:

```
Build for target (build class python_mesonpy, layer built on x86_64):
  - extension modules (C source (...), C++ source (...)) are compiled for the
    target: python_mesonpy exports the target toolchain (CC/LDSHARED,
    STAGING_INCDIR/STAGING_LIBDIR) and DEPENDS the target python3
  x86 (32-bit)             OK
  ...
  Versal / Versal AI       OK
```

| Verdict | Meaning |
| --- | --- |
| `OK` | Nothing to check. |
| `RISK` | It builds, but something has to be verified (e.g. native code driven by a class whose PEP 517 frontend runs under `nativepython3`, or a build config that passes x86-specific CPU flags). |
| `INCOMPATIBLE` | It cannot work: the target Python is excluded by `Requires-Python`, the only source is a prebuilt wheel for another platform/architecture, or the build needs a Rust toolchain poky does not ship. |

`Requires-Dist` entries gated on `platform_machine` are **not** evaluated
against the build host's answer (that would bake x86_64 into an ARM image).
They are split out and rendered per architecture, using the MACHINEOVERRIDES
tokens poky guarantees (`x86`, `x86_64`, `armv7a`, `aarch64`, `riscv64`):

```
# Machine-gated dependencies (Requires-Dist platform_machine markers):
RDEPENDS:${PN}:append:aarch64 += "python3-sysv-ipc"
RDEPENDS:${PN}:append:x86_64 += "python3-sysv-ipc"
```

The report only claims what it can read from the sdist and the metadata. It
does not run a cross build, so a `RISK` stays a `RISK` — use `--check parse` /
`--check build` for that.

## Checks

`--check LEVEL` runs the ladder up to and including `LEVEL`. Stages that are not
runnable in the current environment are reported as `SKIPPED` with the reason, so the
output always states exactly what was validated:

| Stage | What it does | Requires |
| --- | --- | --- |
| `structural` | Pure-Python checks on the `.bb` (required vars, balanced quotes/braces, corruption signatures: timestamp residue, `-`-prefixed dep tokens, single-quote balance). | nothing |
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
   single-line, double-quoted `RDEPENDS:${PN} += "python3-<dep>"` values (literal
   `python3-` prefix, no `\` continuations — see the PetaLinux note below).
   Entries gated on `platform_machine` are kept out of that unconditional line
   and rendered per `MACHINEOVERRIDES` token, so the build host's architecture
   never decides the target's dependency set.
4. Inspect the source for compiled code (`.c`/`.cpp`/`.pyx`/`.rs`/… outside
   test and doc trees, `ext_modules` in `setup.py`, native build requirements)
   and for x86-specific CPU flags in the build config.
5. Render the recipe (canonical `inherit pypi setuptools3` pattern), a
   `meta-<package>` layer named after the package's canonical name, and a `kas`
   build file (validated against the official kas JSON schema).
6. Report the build-for-target verdict (see the cross-build section above).
7. Run the validation ladder.

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
petalinux-config   # → Yocto Settings → User Layers
# enter: ${PROOT}/project-spec/meta-async-pyserial
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

### Why recipes are single-line quoted

`py2bbr` renders `DEPENDS` / `RDEPENDS` as single-line, double-quoted values
with a literal `python3-` prefix, and pins `PYPI_PACKAGE` when the sdist file
name differs from the canonical package name (e.g. `async_pyserial` vs
`async-pyserial`, which makes both `do_fetch` and `S = ${WORKDIR}/${PYPI_PACKAGE}-${PV}`
point at the actual artifact). Older py2bbr output used multi-line `\`
continuations with ``${PYTHON_PN}`` references — a construct some PetaLinux
releases corrupt in a line-oriented post-processing step (shell-style
continuation joining with the variable unset), producing unparsable lines like
`DEPENDS += -pybind11-native '00:00`. The single-line quoted shape survives such
passes intact, and `py2bbr --check structural` reports the known corruption
signatures (timestamp residue, `-`-prefixed dep tokens, unbalanced single
quotes) if they ever reappear.

PEP 517 builds get one more guard: when the recipe uses a PEP 517 build class
(`python_pep517` / `python_setuptools_build_meta`), `py2bbr` appends a
`do_configure` fragment that strips version bounds inside `build-system.requires`.
PetaLinux 2024.2 / Scarthgap ships `setuptools 69.1.1` and `wheel 0.42.0` in the
native sysroot, while the build class' `do_compile` runs
`python3 -m build --no-isolation`, whose dependency check compares the source's
`requires` bounds against those native versions — so e.g. `async-pyserial`
0.2.4 (which demands `setuptools>=71.0.1` and `wheel>=0.43.0`) aborted with
`ERROR Missing dependencies`. The native sysroot versions are authoritative
for the build, so only the `[build-system]` bounds are relaxed; `[project]`
dependency bounds are left intact.

Note on minimal PetaLinux: Some installations of PetaLinux do not include `python3`, reportedly `petalinux-image-minimal`. If Python is reported missing:
- Add `python3` to the `IMAGE_INSTALL:append` line (in interactive configuration or under pl-project/project-spec/configs`.
- Switch to the full image (petalinux-build targeting petalinux-image-full), which includes `python3` and `pip`.

### "The sstate manifest for task ... could not be found"

```
ERROR: python3-cython-native-3.3.0-r0 do_prepare_recipe_sysroot: The sstate
manifest for task 'python3-flit-core-native:populate_sysroot' (multilib variant '')
could not be found.
The pkgarchs considered were: x86_64, x86_64_ubuntu-22.04.
But none of these manifests exists:
    .../tmp/sstate-control/manifest-x86_64-python3-flit-core-native.populate_sysroot
```

This is not a recipe problem. `do_prepare_recipe_sysroot` reads
`tmp/sstate-control/manifest-<pkgarch>-<recipe>.populate_sysroot` for every
dependency whose sysroot it installs, and bitbake only (re)writes that manifest
when it actually processes the task — fetches it from sstate or runs it. A
`do_populate_sysroot_setscene` stamp in `tmp/stamps/` means the task counts as
done, so bitbake skips it and never writes the manifest, and the task that
*asks* for the manifest dies instead. Check for the lonely stamp:

```
ls build/tmp/stamps/x86_64-linux/python3-flit-core-native/
```

### Why the manifest goes missing

The usual cause is a **provider swap**: the layer gained (or lost) a recipe that
provides the same `RPROVIDER` as one the distro already ships. `python3-flit-core`
and `python3-meson-python` are the ones that bite, because py2bbr provisions a
newer copy when a `build-system.requires` floor demands one (numpy 2.5.3 wants
`meson-python>=0.18.0`, Scarthgap ships 0.15.0) while poky already ships a
recipe under that name. Tasks are identified by recipe name, not version, so the
swap invalidates the previous version's manifests; a signature-locked cache
(PetaLinux's `SIGGEN_LOCKEDSIGS`, with `SSTATE_EXISTS_CHECK = "none"`) keeps
trusting the old stamps, and the manifest is never recreated. The build log
shows the swap directly:

```
WARNING: The python3-flit-core-native:do_recipe_qa sig is computed to be <new>,
but the sig is locked to <old> in SIGGEN_LOCKEDSIGS
```

It bites hardest when the swap is later undone (the generated recipe removed, or
regenerated at another version): the old version's stamps match again, so
bitbake does nothing at all, and every recipe that needs that sysroot fails —
in this case `python3-cython-native`, which is innocent.

### Recovery

Drop the cached task state for every recipe that took part in a swap, so bitbake
redoes the setscene task and rewrites the manifest (the sstate tarball is usually
still in `build/sstate-cache`, so the restore is local and fast):

```
petalinux-build -c cleansstate python3-flit-core-native python3-meson-python-native
petalinux-build
```

or, equivalently, if the build directory is reachable directly:

```
rm -rf build/tmp/stamps/*/python3-flit-core-native \
       build/tmp/stamps/*/python3-meson-python-native
rm -f  build/tmp/sstate-control/manifest-*python3-flit-core-native* \
       build/tmp/sstate-control/manifest-*python3-meson-python-native*
```

A missing manifest for a bootstrap tool (`flit-core`, `build`, `installer`,
`meson-python`) surfaces as a cascade: every recipe that goes through the
`python_pep517` chain then fails in `do_prepare_recipe_sysroot` long before the
actual problem. When a whole-tree reset is cheaper than chasing recipes,
`petalinux-build -x cleansstate` redoes them all.

### PEP 517 bootstrap tools render with poky's own build loop

Four distributions form the PEP 517 build toolchain itself — `flit-core`, `build`,
`installer`, `pyproject-hooks` — and poky's own recipes for them deliberately
build and install *by hand* inside the recipe, precisely because they cannot
depend on their own toolchain. `python_pep517.bbclass` appends
`python3-build-native python3-installer-native` to `DEPENDS`, and every one of
those four recipes `inherit python_flit_core` (which itself adds
`python3-flit-core-native`), so provisioning them through the generic shape
closes a dependency loop. PetaLinux reported it as hundreds of circular
`create-spdx` dependencies when a newly provisioned `python3-flit-core_3.12.0`
(Scarthgap ships 3.9.0) started shadowing poky's.

`py2bbr` therefore renders these four packages with poky's own `python_flit_core`
shape: `inherit pypi python_flit_core`, the same `DEPENDS:remove:class-native`
of `python3-build-native`/`python3-installer-native`, and the manual
`python_flit_core_do_manual_build` / `python_pep517_do_bootstrap_install`
class-native functions — so provisioning a newer `flit-core` no longer re-opens
the loop. The shape is keyed on the canonical package name (not the build
backend), and an explicit `--build-class` always wins over it.

### Private-index packages are vendored into the layer

Some packages only exist on a private index (e.g. `pyvizionsdk`);
`inherit pypi`'s `do_fetch` then 404s against public PyPI and the build dies.
`py2bbr` checks reachability of the exact URL the `pypi` class would fetch, and
when it is unreachable the recipe ships the sdist **inside the layer** instead:
`SRC_URI = "file://pyvizionsdk-26.9.1.tar.gz"` (resolved through FILESPATH from
`recipes-devtools/python/files/`), a derived `S`, and the same
`SRC_URI[sha256sum]` / `LIC_FILES_CHKSUM` guarantees. The layer stays
self-contained and builds offline.

## License

MIT — see [LICENSE](LICENSE).
