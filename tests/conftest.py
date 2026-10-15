import pytest

from py2bbr.model import Dependency, DepKind, PackageMetadata, SourceKind


def make_meta(**overrides) -> PackageMetadata:
    """Build a realistic yet simple `PackageMetadata` for tests (no I/O)."""
    base: dict = {
        "name": "black",
        "canonical_name": "black",
        "version": "26.5.1",
        "summary": "The uncompromising code formatter.",
        "description": "The uncompromising code formatter.",
        "homepage": "https://github.com/psf/black",
        "license": "MIT",
        "license_raw": "MIT",
        "license_file": "LICENSE",
        "license_md5": "d3465a2a183908c9cb95bf490bd1e7ab",
        "requires_python": ">=3.10",
        "project_urls": [("Repository", "https://github.com/psf/black")],
        "source_kind": SourceKind.SDIST,
        "source_filename": "black-26.5.1.tar.gz",
        "source_sha256": "dd321f668053961824bcc1be1cc1df748b2d7e4fa28086b08331e577b0100a73",
        "build_backend": "hatchling.build",
        "runtime_deps": [
            Dependency(
                name="click", kind=DepKind.RUNTIME, requirement="click>=8.0.0"
            ),
            Dependency(
                name="packaging",
                kind=DepKind.RUNTIME,
                requirement="packaging>=22.0",
            ),
        ],
        "build_deps": [
            Dependency(
                name="hatchling",
                kind=DepKind.BUILD,
                requirement="hatchling>=1.27.0",
            ),
        ],
        "dropped_deps": [
            Dependency(
                name="tomli",
                kind=DepKind.DROPPED,
                requirement='tomli>=1.1.0; python_version < "3.11"',
                reason='marker not satisfied on target (python_version < "3.11")',
            ),
        ],
    }
    base.update(overrides)
    return PackageMetadata(**base)


@pytest.fixture
def meta() -> PackageMetadata:
    return make_meta()


@pytest.fixture
def make_meta_fn():
    return make_meta


@pytest.fixture
def yocto():
    from py2bbr.yocto import resolve_yocto_version

    return resolve_yocto_version("5.0")
