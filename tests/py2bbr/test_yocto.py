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

from py2bbr.yocto import render_layer, resolve_yocto_version


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
