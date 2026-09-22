"""Small edge tests: errors, model, package, recipe corners."""

from __future__ import annotations

import py2bbr
from py2bbr import recipe
from py2bbr.errors import SubprocessError
from py2bbr.model import Dependency, DepKind


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
        meta = make_meta_fn(license="MIT", license_file=None, license_md5=None)
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
