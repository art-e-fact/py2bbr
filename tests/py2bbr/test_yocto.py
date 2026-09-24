"""Tests for py2bbr.yocto layer rendering (no network required).

Regression: the generated layer.conf must load in a bitbake environment that
has poky core but NOT meta-python. bitbake (>= the Scarthgap line) refuses to
load a layer whose LAYERDEPENDS names a collection that is not enabled, and it
only honours ``LAYERSERIES_COMPAT_<c>`` (the old ``LAYERCOMPATIBLE_<c>`` is
dead). Since ``pypi``/``setuptools3``/``python_pep517``/
``python_setuptools_build_meta`` all live in poky core in Scarthgap, the layer
may only hard-depend on ``core``.
"""

from __future__ import annotations

import sys

import pytest
import yaml

from py2bbr.errors import YoctoVersionError
from py2bbr.yocto import (
    _version_key,
    default_layer_name,
    known_versions,
    layer_collection,
    render_kas_file,
    render_layer,
    resolve_yocto_version,
    validate_kas_file,
    write_layer,
)


def _layer_conf(meta, yocto) -> str:
    return render_layer(meta, yocto, "meta-py2bbr")["conf/layer.conf"]


def _line(conf: str, prefix: str) -> str:
    hits = [l for l in conf.splitlines() if l.startswith(prefix)]
    assert len(hits) == 1, f"expected exactly one {prefix!r} line: {conf!r}"
    return hits[0]


class TestLayerConf:
    def test_bbfiles_covers_recipe_tree(self, meta, yocto):
        # Without a BBFILES entry bitbake never scans the layer's recipe
        # directory ("No bb files ... matched BBFILE_PATTERN_<c>"), so the
        # recipes are invisible even though the layer itself loads.
        conf = _layer_conf(meta, yocto)
        assert 'BBFILES += "${LAYERDIR}/recipes-*/*/*.bb"' in conf

    def test_depends_on_core_only(self, meta, yocto):
        # A hard dep on the meta-python "python" collection makes bitbake drop
        # the whole layer in poky-only environments ("Nothing PROVIDES ...").
        conf = _layer_conf(meta, yocto)
        assert _line(conf, "LAYERDEPENDS_py2bbr") == (
            'LAYERDEPENDS_py2bbr = "core"'
        )

    def test_uses_modern_layerseries_compat(self, meta, yocto):
        conf = _layer_conf(meta, yocto)
        assert _line(conf, "LAYERSERIES_COMPAT_py2bbr") == (
            'LAYERSERIES_COMPAT_py2bbr = "scarthgap"'
        )
        assert "LAYERCOMPATIBLE" not in conf

    def test_wayne_series_compat(self, meta):
        conf = _layer_conf(meta, resolve_yocto_version("6.0"))
        assert _line(conf, "LAYERSERIES_COMPAT_py2bbr") == (
            'LAYERSERIES_COMPAT_py2bbr = "wayne"'
        )
        assert _line(conf, "LAYERDEPENDS_py2bbr") == (
            'LAYERDEPENDS_py2bbr = "core"'
        )


class TestUniqueLayerName:
    def test_default_layer_name_from_canonical(self):
        assert default_layer_name("async_pyserial") == "meta-async-pyserial"
        assert default_layer_name("async-pyserial") == "meta-async-pyserial"
        assert default_layer_name("Django") == "meta-django"
        assert default_layer_name("zope.interface") == "meta-zope-interface"

    def test_layer_collection_unique_per_package(self):
        assert layer_collection("meta-async-pyserial") == "async_pyserial"
        assert layer_collection("meta-django") == "django"
        assert layer_collection("meta-zope-interface") == "zope_interface"

    def test_render_layer_uses_package_collection(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            name="async-pyserial", canonical_name="async-pyserial"
        )
        conf = render_layer(meta, yocto, "meta-async-pyserial")[
            "conf/layer.conf"
        ]
        assert _line(conf, "BBFILE_COLLECTIONS") == (
            'BBFILE_COLLECTIONS += "async_pyserial"'
        )
        assert "BBFILE_PATTERN_async_pyserial" in conf
        assert _line(conf, "LAYERDEPENDS_async_pyserial") == (
            'LAYERDEPENDS_async_pyserial = "core"'
        )

    def test_explicit_layer_name_override_still_works(self, meta, yocto):
        conf = render_layer(meta, yocto, "meta-custom")["conf/layer.conf"]
        assert _line(conf, "BBFILE_COLLECTIONS") == (
            'BBFILE_COLLECTIONS += "custom"'
        )

    def test_layer_collection_fallback(self):
        assert layer_collection("") == "py2bbr"


class TestResolveVersion:
    def test_none_defaults_to_50(self):
        assert resolve_yocto_version(None).version == "5.0"

    def test_digit_major(self):
        assert resolve_yocto_version("5").version == "5.0"

    def test_codename(self):
        assert resolve_yocto_version("wayne").version == "6.0"

    def test_invalid(self):
        with pytest.raises(YoctoVersionError, match="unknown Yocto version"):
            resolve_yocto_version("9.9")

    def test_known_versions_newest_first(self):
        assert [v.version for v in known_versions()] == ["6.0", "5.0"]

    def test_version_key(self):
        assert _version_key("6.0") == (6, 0)
        assert _version_key("weird") == (0,)


class TestWriteLayer:
    def test_writes_full_tree(self, meta, yocto, tmp_path):
        layer_dir = tmp_path / "meta-black"
        recipe_path = write_layer(meta, yocto, layer_dir)
        assert (
            recipe_path
            == layer_dir
            / "recipes-devtools"
            / "python"
            / "python3-black_26.5.1.bb"
        )
        assert recipe_path.is_file()
        assert (layer_dir / "conf" / "layer.conf").is_file()
        assert (layer_dir / "README").is_file()


class TestKasFile:
    def test_render(self, meta, yocto, tmp_path):
        layer = tmp_path / "meta-black"
        cfg = yaml.safe_load(
            render_kas_file(
                meta, yocto, layer, extra_targets=["core-image-base"]
            )
        )
        assert cfg["header"]["version"] == 5
        assert cfg["build_system"] == "openembedded"
        assert cfg["machine"] == "qemux86-64"
        assert cfg["target"] == ["python3-black", "core-image-base"]
        assert cfg["repos"]["poky"]["branch"] == "scarthgap"
        assert cfg["repos"]["meta-black"]["path"] == str(layer.resolve())

    def test_validate_valid(self, meta, yocto, tmp_path):
        cfg = yaml.safe_load(
            render_kas_file(meta, yocto, tmp_path / "meta-black")
        )
        assert validate_kas_file(cfg) == []

    def test_validate_invalid(self, meta, yocto, tmp_path):
        cfg = yaml.safe_load(
            render_kas_file(meta, yocto, tmp_path / "meta-black")
        )
        del cfg["header"]
        assert validate_kas_file(cfg)

    def test_validate_fallback(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "jsonschema", None)
        assert validate_kas_file({}) == ["kas config missing required 'header'"]
        assert validate_kas_file({"header": {}}) == [
            "kas config header missing 'version'"
        ]
        assert validate_kas_file({"header": {"version": 5}}) == []

    def test_self_contained_excludes_meta_python(self, meta, yocto, tmp_path):
        # The default self-contained layer carries a recipe for every closure
        # member, so meta-python must not be referenced: its matching recipes
        # (e.g. python3-click) would duplicate the generated member recipes.
        cfg = yaml.safe_load(
            render_kas_file(
                meta,
                yocto,
                tmp_path / "meta-black",
                include_meta_python=False,
            )
        )
        assert "meta-python" not in cfg["repos"]

    def test_no_deps_keeps_meta_python(self, meta, yocto, tmp_path):
        # With --no-deps, RDEPENDS are left to external layers, so the kas
        # build must still reach meta-python to provide them.
        cfg = yaml.safe_load(
            render_kas_file(
                meta,
                yocto,
                tmp_path / "meta-black",
                include_meta_python=True,
            )
        )
        assert "meta-python" in cfg["repos"]


class TestLayerClosureRecipes:
    def test_render_layer_one_recipe_per_member(
        self, meta, make_meta_fn, yocto
    ):
        member = make_meta_fn(
            name="click", canonical_name="click", version="8.1.7"
        )
        files = render_layer(meta, yocto, "meta-black", extra_metas=[member])
        assert "recipes-devtools/python/python3-black_26.5.1.bb" in files
        assert "recipes-devtools/python/python3-click_8.1.7.bb" in files

    def test_write_layer_writes_member_recipes(
        self, meta, make_meta_fn, yocto, tmp_path
    ):
        member = make_meta_fn(
            name="click", canonical_name="click", version="8.1.7"
        )
        layer_dir = tmp_path / "meta-black"
        recipe_path = write_layer(meta, yocto, layer_dir, extra_metas=[member])
        assert recipe_path.name == "python3-black_26.5.1.bb"
        assert (
            layer_dir / "recipes-devtools/python/python3-click_8.1.7.bb"
        ).is_file()
        assert recipe_path.is_file()
