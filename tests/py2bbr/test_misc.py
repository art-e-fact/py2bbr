import io
import tarfile

import pytest

import py2bbr
from py2bbr import recipe
from py2bbr.errors import SubprocessError
from py2bbr.metadata import FetchedSource, parse_metadata_file
from py2bbr.model import Dependency, DepKind, SourceKind


class TestSubprocessError:
    def test_fields_and_str(self):
        e = SubprocessError(["bitbake", "-p"], 1, "boom")
        assert e.command == ["bitbake", "-p"]
        assert e.returncode == 1
        assert e.detail == "boom"
        assert "exit 1" in str(e)
        assert "bitbake -p" in str(e)
        assert "boom" in str(e)

    def test_no_detail(self):
        e = SubprocessError(["x"], 2)
        assert " :: " not in str(e)


class TestModel:
    def test_dependency_bitbake_name(self):
        d = Dependency(name="mypy-extensions", kind=DepKind.RUNTIME)
        assert d.bitbake_name == "mypy-extensions"


class TestPackage:
    def test_dir_matches_all(self):
        assert py2bbr.__dir__() == py2bbr.__all__


class TestRecipeEdges:
    def test_render_recipe_to_path(self, meta, yocto, tmp_path):
        path, _ = recipe.render_recipe_to_path(meta, yocto, tmp_path / "out")
        assert path == tmp_path / "out" / "python3-black_26.5.1.bb"
        assert path.is_file()

    def test_no_license_warning(self, make_meta_fn, yocto):
        meta = make_meta_fn(license="", license_raw="")
        _text, warnings = recipe.render_recipe(meta, yocto)
        assert "no license detected" in " ".join(warnings)

    def test_license_without_file_warns(self, make_meta_fn, yocto):
        # Only *unknown* SPDX ids warn: known ids now fall back to the
        # common-licenses idiom (see TestLicenseRendering).
        meta = make_meta_fn(
            license="AGPL-3.0-only", license_file=None, license_md5=None
        )
        text, warnings = recipe.render_recipe(meta, yocto)
        assert "LIC_FILES_CHKSUM" in " ".join(warnings)
        assert "LIC_FILES_CHKSUM =" not in text

    def test_description_line(self, make_meta_fn, yocto):
        meta = make_meta_fn(description="Different description text.")
        text, _ = recipe.render_recipe(meta, yocto)
        assert 'DESCRIPTION = "Different description text."' in text

    def test_pypi_package_name_no_match(self, make_meta_fn, yocto):
        meta = make_meta_fn(source_filename="async_pyserial-0.2.4.tar.gz")
        text, _ = recipe.render_recipe(meta, yocto)
        assert "PYPI_PACKAGE" not in text

    def test_vendored_render_recipe_to_path_copies_source(
        self, make_meta_fn, yocto, tmp_path
    ):
        src = tmp_path / "pyvizionsdk-26.9.1.tar.gz"
        src.write_bytes(b"sdist-bytes")
        meta = make_meta_fn(
            name="pyvizionsdk",
            canonical_name="pyvizionsdk",
            version="26.9.1",
            source_filename="pyvizionsdk-26.9.1.tar.gz",
            vendored=True,
        )
        _path, _warnings = recipe.render_recipe_to_path(
            meta, yocto, tmp_path / "out", vendor_source=src
        )
        copied = tmp_path / "out" / "files" / "pyvizionsdk-26.9.1.tar.gz"
        assert copied.read_bytes() == b"sdist-bytes"

    def test_vendored_render_recipe_to_path_requires_source(
        self, make_meta_fn, yocto, tmp_path
    ):
        meta = make_meta_fn(
            name="pyvizionsdk",
            canonical_name="pyvizionsdk",
            version="26.9.1",
            source_filename="pyvizionsdk-26.9.1.tar.gz",
            vendored=True,
        )
        with pytest.raises(ValueError, match="vendor_source"):
            recipe.render_recipe_to_path(meta, yocto, tmp_path / "out")


class TestParseMetadataFileRootLevel:
    def _source(self, tmp_path) -> FetchedSource:
        tar = tmp_path / "pkg-1.0.0.tar.gz"
        with tarfile.open(tar, "w:gz") as tf:
            info = tarfile.TarInfo(
                "pkg-1.0.0/pkg/vendor/evil-1.2.3.dist-info/METADATA"
            )
            data = (
                b"Metadata-Version: 2.1\nName: evil\nVersion: 1.2.3\n"
                b"Summary: vendored\n"
            )
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
            info = tarfile.TarInfo("pkg-1.0.0/PKG-INFO")
            root = (
                b"Metadata-Version: 2.1\nName: the-real-pkg\nVersion: 1.0.0\n"
                b"Summary: root\n"
            )
            info.size = len(root)
            tf.addfile(info, io.BytesIO(root))
        return FetchedSource(tar, SourceKind.SDIST, "sha", tar.name)

    def test_root_pkg_info_wins_over_vendored_metadata(self, tmp_path):
        msg = parse_metadata_file(self._source(tmp_path))
        assert msg["Name"] == "the-real-pkg"
        assert msg["Version"] == "1.0.0"

    def test_archive_order_does_not_matter(self, tmp_path):
        tar = tmp_path / "pkg-2.0.0.tar.gz"
        with tarfile.open(tar, "w:gz") as tf:
            root = (
                b"Metadata-Version: 2.1\nName: the-real-pkg\nVersion: 2.0.0\n"
                b"Summary: root\n"
            )
            info = tarfile.TarInfo("pkg-2.0.0/PKG-INFO")
            info.size = len(root)
            tf.addfile(info, io.BytesIO(root))
            vendored = (
                b"Metadata-Version: 2.1\nName: evil\nVersion: 9.9.9\n"
                b"Summary: vendored\n"
            )
            info = tarfile.TarInfo(
                "pkg-2.0.0/pkg/vendor/evil-9.9.9.dist-info/METADATA"
            )
            info.size = len(vendored)
            tf.addfile(info, io.BytesIO(vendored))
        msg = parse_metadata_file(
            FetchedSource(tar, SourceKind.SDIST, "sha", tar.name)
        )
        assert msg["Name"] == "the-real-pkg"
        assert msg["Version"] == "2.0.0"
