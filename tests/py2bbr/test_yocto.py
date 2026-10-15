import platform
import sys

import pytest
import yaml

from py2bbr.errors import TargetRuntimeError, YoctoVersionError
from py2bbr.recipe import bitbake_pn, recipe_filename
from py2bbr.yocto import (
    _version_key,
    current_platform_machine,
    current_python_version,
    default_layer_name,
    known_versions,
    layer_collection,
    normalize_platform_machine,
    normalize_python_version,
    render_kas_file,
    render_layer,
    resolve_target_runtime,
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
        conf = _layer_conf(meta, yocto)
        assert 'BBFILES += "${LAYERDIR}/recipes-*/*/*.bb"' in conf

    def test_depends_on_core_only(self, meta, yocto):
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

    def test_wrynose_series_compat(self, meta):
        conf = _layer_conf(meta, resolve_yocto_version("6.0"))
        assert _line(conf, "LAYERSERIES_COMPAT_py2bbr") == (
            'LAYERSERIES_COMPAT_py2bbr = "wrynose"'
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
        assert resolve_yocto_version("wrynose").version == "6.0"

    def test_invalid(self):
        with pytest.raises(YoctoVersionError, match="unknown Yocto version"):
            resolve_yocto_version("9.9")

    def test_known_versions_newest_first(self):
        assert [v.version for v in known_versions()] == [
            "6.0",
            "5.0",
        ]

    def test_version_key(self):
        assert _version_key("6.0") == (6, 0)
        assert _version_key("weird") == (0,)


class TestResolveTargetRuntime:
    def test_defaults_to_the_running_interpreter(self):
        assert current_python_version() == (
            f"{sys.version_info.major}.{sys.version_info.minor}"
        )
        assert current_platform_machine() == (
            platform.machine().lower() or "x86_64"
        )

    def test_overrides_both_axes(self):
        y = resolve_target_runtime(
            resolve_yocto_version("5.0"),
            python_version="3.11",
            platform_machine="aarch64",
        )
        assert (y.python_version, y.platform_machine) == ("3.11", "aarch64")
        assert y.runtime_label == "Python 3.11 on aarch64"
        assert (y.version, y.codename) == ("5.0", "scarthgap")
        assert y.python_pn == "python3"

    def test_no_override_returns_the_series_itself(self):
        y = resolve_yocto_version("5.0")
        assert resolve_target_runtime(y) is y

    def test_aliases_are_canonicalised(self):
        y = resolve_target_runtime(
            resolve_yocto_version("5.0"), platform_machine="ARM64"
        )
        assert y.platform_machine == "aarch64"
        assert (
            resolve_target_runtime(
                resolve_yocto_version("5.0"), platform_machine="amd64"
            ).platform_machine
            == "x86_64"
        )
        assert (
            resolve_target_runtime(
                resolve_yocto_version("5.0"), platform_machine="armv7"
            ).platform_machine
            == "armv7l"
        )

    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("3.12", "3.12"),
            ("3.12.4", "3.12"),
            ("312", "3.12"),
            (" 3.9 ", "3.9"),
        ],
    )
    def test_python_version_is_reduced_to_major_minor(self, spec, expected):
        assert normalize_python_version(spec) == expected

    @pytest.mark.parametrize("spec", ["", "3", "3.x", "python", "3-12"])
    def test_unparsable_python_version_is_rejected(self, spec):
        with pytest.raises(TargetRuntimeError, match="--python-version"):
            normalize_python_version(spec)

    @pytest.mark.parametrize("spec", ["", "a b", "aarch64/64", "--help"])
    def test_unparsable_arch_is_rejected(self, spec):
        with pytest.raises(TargetRuntimeError, match="--target-arch"):
            normalize_platform_machine(spec)


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

    def test_write_layer_copies_vendored_sources(
        self, make_meta_fn, yocto, tmp_path
    ):
        src = tmp_path / "pyvizionsdk-26.9.1.tar.gz"
        src.write_bytes(b"fake-sdist")
        meta = make_meta_fn(
            name="pyvizionsdk",
            canonical_name="pyvizionsdk",
            version="26.9.1",
            source_filename="pyvizionsdk-26.9.1.tar.gz",
            vendored=True,
        )
        recipe_name = recipe_filename(
            bitbake_pn("pyvizionsdk", yocto.python_pn), "26.9.1"
        )
        layer_dir = tmp_path / "meta-pyvizionsdk"
        write_layer(meta, yocto, layer_dir, vendor_sources={recipe_name: src})
        vendored = (
            layer_dir
            / "recipes-devtools"
            / "python"
            / "files"
            / "pyvizionsdk-26.9.1.tar.gz"
        )
        assert vendored.read_bytes() == b"fake-sdist"

    def test_write_layer_vendored_missing_source_raises(
        self, make_meta_fn, yocto, tmp_path
    ):
        meta = make_meta_fn(
            name="pyvizionsdk",
            canonical_name="pyvizionsdk",
            version="26.9.1",
            source_filename="pyvizionsdk-26.9.1.tar.gz",
            vendored=True,
        )
        layer_dir = tmp_path / "meta-pyvizionsdk"
        with pytest.raises(ValueError, match="vendor_sources"):
            write_layer(meta, yocto, layer_dir, vendor_sources={})


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

    def test_validate_reports_a_broken_validator(self, monkeypatch):
        import jsonschema

        def boom(_self, _config):
            raise jsonschema.SchemaError("broken schema")

        monkeypatch.setattr(
            jsonschema.Draft7Validator, "iter_errors", boom, raising=True
        )
        assert validate_kas_file({"header": {"version": 5}}) == [
            "kas schema validation raised: broken schema"
        ]

    def test_validate_fallback(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "jsonschema", None)
        assert validate_kas_file({}) == ["kas config missing required 'header'"]
        assert validate_kas_file({"header": {}}) == [
            "kas config header missing 'version'"
        ]
        assert validate_kas_file({"header": {"version": 5}}) == []

    def test_self_contained_excludes_meta_python(self, meta, yocto, tmp_path):
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
