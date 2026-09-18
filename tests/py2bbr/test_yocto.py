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

from py2bbr.yocto import (
    default_layer_name,
    layer_collection,
    render_layer,
    resolve_yocto_version,
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
