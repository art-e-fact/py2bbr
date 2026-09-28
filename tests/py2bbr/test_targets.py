"""Tests for py2bbr.targets: the target matrix and build-for-target report.

The layer is cross-built (x86_64 host, target CPU from the board), so the
report has to separate two questions that look alike: "does the build class
compile against the *target* toolchain" (a property of the class) and "does
this package work on *this* CPU" (a property of the target). Verdicts are
therefore ``OK`` / ``RISK`` (builds, verify) / ``INCOMPATIBLE`` (cannot work).
"""

from __future__ import annotations

import pytest

from py2bbr.model import (
    Dependency,
    DepKind,
    MachineDep,
    SourceKind,
    machine_override_tokens,
)
from py2bbr.targets import (
    KNOWN_TARGETS,
    Verdict,
    evaluate,
    target_by_key,
)
from py2bbr.targets import (
    _wheel_platform as wheel_platform,
)


class TestMatrix:
    def test_covers_the_requested_targets(self):
        keys = [t.key for t in KNOWN_TARGETS]
        assert "x86" in keys
        assert "x86_64" in keys
        assert {"cortex-a53", "cortex-a57", "cortex-a72"} <= set(keys)
        assert {"cortex-a53-64", "cortex-a72-64"} <= set(keys)
        assert "zynqmp" in keys
        assert "versal" in keys
        assert "riscv64" in keys

    def test_32bit_and_64bit_arm_report_the_python_machine(self):
        a53 = target_by_key("cortex-a53")
        a53_64 = target_by_key("cortex-a53-64")
        assert a53.platform_machine == "armv7l"
        assert a53_64.platform_machine == "aarch64"
        # 32-bit ARM and 64-bit ARM need different override tokens.
        assert "armv7a" in a53.machine_overrides
        assert "aarch64" in a53_64.machine_overrides

    def test_x86_uses_the_32bit_token(self):
        assert target_by_key("x86").machine_overrides == ("x86",)
        assert target_by_key("x86_64").machine_overrides == ("x86_64",)

    def test_lookup_by_platform_machine(self):
        assert target_by_key("aarch64").key == "cortex-a53-64"

    def test_lookup_miss(self):
        assert target_by_key("sparc64") is None

    def test_override_tokens_known_and_fallback(self):
        assert machine_override_tokens("aarch64") == ("aarch64",)
        assert machine_override_tokens("armv7l") == ("armv7a",)
        assert machine_override_tokens("AMD64") == ("x86_64",)
        # Unknown machine: keep the raw name as the token so a recipe is still
        # correct (the machine name is always in MACHINEOVERRIDES).
        assert machine_override_tokens("Nios-III") == ("nios-iii",)


class TestWheelPlatform:
    def test_any_wheel_is_arch_neutral(self):
        assert wheel_platform("black-26.5.1-py3-none-any.whl") == ("", "")

    def test_manylinux_x86_64(self):
        arch, platform = wheel_platform(
            "numpy-2.5.3-cp312-cp312-manylinux_2_17_x86_64.whl"
        )
        assert arch == "x86_64"
        assert "manylinux" in platform

    def test_macos_wheel_pins_a_foreign_platform(self):
        arch, platform = wheel_platform(
            "foo-1.0-cp312-cp312-macosx_11_0_arm64.whl"
        )
        assert (arch, platform) == ("", "macosx_11_0_arm64")

    def test_unparseable_filename(self):
        assert wheel_platform("not-a-wheel.txt") == ("", "")


class TestPurePython:
    def test_ok_everywhere(self, make_meta_fn, yocto):
        report = evaluate(make_meta_fn(), "python_pep517", yocto)
        assert report.pure_python is True
        assert report.ok is True
        assert report.buildable is True
        assert len(report.verdicts) == len(KNOWN_TARGETS)
        assert report.cross_builds is False
        assert any("pure-Python sdist" in n for n in report.notes)

    def test_summary_render_mentions_class(self, make_meta_fn, yocto):
        lines = evaluate(make_meta_fn(), "python_pep517", yocto).render()
        assert "build class python_pep517" in lines[0]
        assert "x86_64" in lines[0]


class TestNativeCode:
    @pytest.fixture(autouse=True)
    def _maker(self, make_meta_fn):
        self.mk = make_meta_fn

    def _native(self, **kw):
        return self.mk(
            has_native_code=True,
            native_code_evidence=["C source (pkg/src/mod.c)"],
            **kw,
        )

    def test_cross_capable_class_is_ok(self, yocto):
        report = evaluate(self._native(), "setuptools3", yocto)
        assert report.cross_builds is True
        assert report.pure_python is False
        assert report.ok is True
        assert any("compiled for the target" in n for n in report.notes)

    def test_host_only_class_is_a_risk_not_a_failure(self, yocto):
        report = evaluate(self._native(), "python_pep517", yocto)
        assert report.ok is False
        assert report.buildable is True
        assert all(v.verdict is Verdict.RISK for v in report.verdicts)
        assert any("nativepython3" in n for n in report.notes)

    def test_unknown_class_says_so(self, yocto):
        report = evaluate(self._native(), "my_custom_class", yocto)
        assert "not one of the classes" in " ".join(report.notes)

    def test_x86_flags_only_flag_non_x86(self, yocto):
        report = evaluate(
            self._native(host_assumptions=["setup.py: -march=native"]),
            "setuptools3",
            yocto,
        )
        assert report.find("x86_64").verdict is Verdict.OK
        for key in ("cortex-a53", "zynqmp", "versal", "riscv64"):
            verdict = report.find(key)
            assert verdict.verdict is Verdict.RISK
            assert any("-march=native" in n for n in verdict.notes)


class TestGlobalBlocks:
    def test_python_newer_than_target_python(self, make_meta_fn, yocto):
        report = evaluate(
            make_meta_fn(requires_python=">=3.13"), "setuptools3", yocto
        )
        assert report.buildable is False
        assert all(v.verdict is Verdict.INCOMPATIBLE for v in report.verdicts)
        assert any("ships Python 3.12" in n for n in report.notes)

    def test_python_ceiling_excludes_target(self, make_meta_fn, yocto):
        report = evaluate(
            make_meta_fn(requires_python="<3.12"), "setuptools3", yocto
        )
        assert report.buildable is False

    def test_python_ceiling_includes_target(self, make_meta_fn, yocto):
        report = evaluate(
            make_meta_fn(requires_python=">=3.9,<3.13"), "setuptools3", yocto
        )
        assert report.buildable is True

    def test_unparseable_requires_python_is_ignored(self, make_meta_fn, yocto):
        report = evaluate(
            make_meta_fn(requires_python="not a spec"), "setuptools3", yocto
        )
        assert report.buildable is True

    def test_no_requires_python_is_ignored(self, make_meta_fn, yocto):
        report = evaluate(
            make_meta_fn(requires_python=""), "setuptools3", yocto
        )
        assert report.buildable is True

    def test_rust_build_dep_blocks_every_target(self, make_meta_fn, yocto):
        report = evaluate(
            make_meta_fn(
                build_deps=[
                    Dependency(
                        name="maturin",
                        kind=DepKind.BUILD,
                        requirement="maturin>=1.0",
                    )
                ]
            ),
            "python_pep517",
            yocto,
        )
        assert report.buildable is False
        assert any("Rust" in n for n in report.notes)

    def test_rust_backend_name_blocks(self, make_meta_fn, yocto):
        report = evaluate(
            make_meta_fn(build_backend="maturin"), "python_pep517", yocto
        )
        assert report.buildable is False

    def test_cffi_rust_names_are_canonicalized(self, make_meta_fn, yocto):
        report = evaluate(
            make_meta_fn(
                build_deps=[
                    Dependency(
                        name="setuptools_rust",
                        kind=DepKind.BUILD,
                        requirement="setuptools-rust>=1.9",
                    )
                ]
            ),
            "python_setuptools_build_meta",
            yocto,
        )
        assert report.buildable is False
        assert any("cargo" in n for n in report.notes)


class TestPrebuiltWheels:
    @pytest.fixture(autouse=True)
    def _maker(self, make_meta_fn):
        self.mk = make_meta_fn

    def _wheel_meta(self, filename, **kw):
        return self.mk(
            source_kind=SourceKind.WHEEL, source_filename=filename, **kw
        )

    def test_foreign_arch_is_incompatible(self, yocto):
        report = evaluate(
            self._wheel_meta("foo-1.0-cp312-cp312-manylinux_2_17_x86_64.whl"),
            "setuptools3",
            yocto,
        )
        assert report.find("x86_64").verdict is Verdict.RISK
        for key in ("cortex-a53", "cortex-a72-64", "zynqmp", "versal"):
            assert report.find(key).verdict is Verdict.INCOMPATIBLE
        assert report.buildable is False

    def test_matching_arch_is_a_risk_not_a_failure(self, yocto):
        report = evaluate(
            self._wheel_meta("foo-1.0-cp312-cp312-manylinux_2_17_aarch64.whl"),
            "setuptools3",
            yocto,
        )
        assert report.find("zynqmp").verdict is Verdict.RISK
        assert report.find("x86_64").verdict is Verdict.INCOMPATIBLE
        assert report.buildable is False

    def test_non_linux_wheel_is_incompatible_everywhere(self, yocto):
        report = evaluate(
            self._wheel_meta("foo-1.0-cp312-cp312-macosx_11_0_x86_64.whl"),
            "setuptools3",
            yocto,
        )
        assert all(v.verdict is Verdict.INCOMPATIBLE for v in report.verdicts)
        assert any("prebuilt wheel" in n for n in report.notes)

    def test_any_wheel_is_architecture_neutral(self, yocto):
        report = evaluate(
            self._wheel_meta("black-26.5.1-py3-none-any.whl"),
            "python_pep517",
            yocto,
        )
        assert report.buildable is True
        assert report.ok is True


class TestMachineGatedDeps:
    def test_notes_which_deps_apply_where(self, make_meta_fn, yocto):
        meta = make_meta_fn(
            machine_deps=[
                MachineDep(
                    name="arm-only",
                    requirement='arm-only; platform_machine == "aarch64"',
                    marker='platform_machine == "aarch64"',
                    machines=("aarch64",),
                ),
                MachineDep(
                    name="x86-only",
                    requirement='x86-only; platform_machine == "x86_64"',
                    marker='platform_machine == "x86_64"',
                    machines=("x86_64",),
                ),
            ]
        )
        report = evaluate(meta, "setuptools3", yocto)
        x86_64 = report.find("x86_64")
        assert x86_64.verdict is Verdict.OK
        assert any("needs x86-only" in n for n in x86_64.notes)
        assert any("not a dependency here: arm-only" in n for n in x86_64.notes)
        arm = report.find("zynqmp")
        assert any("needs arm-only" in n for n in arm.notes)
        assert any("machine-gated dependencies" in n for n in report.notes)


class TestTargetSelection:
    def test_explicit_subset(self, make_meta_fn, yocto):
        subset = (target_by_key("zynqmp"), target_by_key("versal"))
        report = evaluate(make_meta_fn(), "setuptools3", yocto, targets=subset)
        assert [v.target.key for v in report.verdicts] == [
            "zynqmp",
            "versal",
        ]

    def test_empty_uses_the_whole_matrix(self, make_meta_fn, yocto):
        report = evaluate(make_meta_fn(), "setuptools3", yocto, targets=())
        assert len(report.verdicts) == len(KNOWN_TARGETS)

    def test_find_missing_key(self, make_meta_fn, yocto):
        assert (
            evaluate(make_meta_fn(), "setuptools3", yocto).find("nope") is None
        )


class TestVerdictRanking:
    @pytest.mark.parametrize(
        "first,second,expected",
        [
            (Verdict.RISK, Verdict.OK, Verdict.RISK),
            (Verdict.INCOMPATIBLE, Verdict.RISK, Verdict.INCOMPATIBLE),
            (Verdict.RISK, Verdict.INCOMPATIBLE, Verdict.INCOMPATIBLE),
        ],
    )
    def test_incompatible_outranks_everything(self, first, second, expected):
        from py2bbr.targets import TargetVerdict

        v = TargetVerdict(KNOWN_TARGETS[0], first)
        v.add(second, "note")
        assert v.verdict is expected


class TestSharedReasonsSuppressRowNotes:
    """A shared reason already decided the verdict: do not pile notes on top.

    Printing "confirm the aarch64 extension modules" on a row that is
    INCOMPATIBLE for a different reason makes the reader hunt for which of the
    two is the real blocker.
    """

    def test_rust_block_silences_the_per_target_risk(self, make_meta_fn, yocto):
        report = evaluate(
            make_meta_fn(
                has_native_code=True,
                native_code_evidence=["Rust source (pkg/src/lib.rs)"],
                build_deps=[
                    Dependency(
                        name="maturin",
                        kind=DepKind.BUILD,
                        requirement="maturin>=1.0",
                    )
                ],
            ),
            "python_pep517",
            yocto,
        )
        assert all(v.notes == [] for v in report.verdicts)
        assert all(v.verdict is Verdict.INCOMPATIBLE for v in report.verdicts)

    def test_host_only_class_still_asks_for_verification(
        self, make_meta_fn, yocto
    ):
        report = evaluate(
            make_meta_fn(
                has_native_code=True,
                native_code_evidence=["C source (pkg/src/mod.c)"],
            ),
            "python_pep517",
            yocto,
        )
        assert all(v.notes for v in report.verdicts)
        assert all(v.verdict is Verdict.RISK for v in report.verdicts)
