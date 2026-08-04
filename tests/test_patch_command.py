"""`dcs-linux patch` end to end, with the machine replaced by a fixture."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from dcs_linux import cli, patches
from dcs_linux.installs import select
from dcs_linux.patches import SEGOE_FONT_NAMES, SEGOE_FONT_PATCH, Patch
from dcs_linux.probes import Environment, probe_patches
from tests.environments import OWN_INSTALL, PATHS, healthy_environment
from tests.fakes import FakeSystem, FakeWriter
from tests.test_check_command import STEAM_INSTALL
from tests.test_patches import DEJAVU, FONT_BYTES, machine, plan_nothing

runner = CliRunner()


def use(monkeypatch: pytest.MonkeyPatch, system: FakeSystem, **overrides: object) -> FakeSystem:
    """Point the CLI at a fixture machine, patch state and all."""
    base = healthy_environment(**overrides)

    def fake_probe(_: object, identifier: str | None = None) -> Environment:
        targeted = select(base.installs, identifier) if identifier else base.targeted
        return replace(
            base,
            targeted=targeted,
            patches=probe_patches(system, base.layout, targeted),
        )

    monkeypatch.setattr(cli, "probe", fake_probe)
    monkeypatch.setattr(cli, "RealSystem", lambda: system)
    monkeypatch.setattr(cli, "RealWriter", lambda: FakeWriter(system))
    return system


class TestList:
    def test_bare_patch_lists_without_changing_anything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        system = use(monkeypatch, machine())
        before = dict(system.files)

        result = runner.invoke(cli.app, ["--no-color", "patch"])

        assert result.exit_code == 0
        assert "segoe-fonts" in result.stdout
        assert "not applied" in result.stdout
        assert system.files == before

    def test_the_documented_list_flag_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use(monkeypatch, machine())
        result = runner.invoke(cli.app, ["--no-color", "patch", "--list"])
        assert result.exit_code == 0
        assert "segoe-fonts" in result.stdout

    def test_json_reports_the_status_and_the_ic_risk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use(monkeypatch, machine())
        result = runner.invoke(cli.app, ["--json", "patch", "--list"])

        payload = json.loads(result.stdout)
        assert payload == {
            "command": "patch",
            "action": "list",
            "patches": [
                {
                    "id": "segoe-fonts",
                    "summary": payload["patches"][0]["summary"],
                    "ic_risk": False,
                    "status": "not-applied",
                    "detail": "not applied",
                }
            ],
        }

    def test_applied_shows_up_as_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use(monkeypatch, machine())
        runner.invoke(cli.app, ["patch", "apply"])

        result = runner.invoke(cli.app, ["--json", "patch", "--list"])
        assert json.loads(result.stdout)["patches"][0]["status"] == "applied"


class TestApply:
    def test_the_fonts_land_in_the_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        system = use(monkeypatch, machine())

        result = runner.invoke(cli.app, ["--no-color", "patch", "apply"])

        assert result.exit_code == 0
        for name in SEGOE_FONT_NAMES:
            assert system.read_bytes(PATHS.fonts / name) == FONT_BYTES

    def test_applying_twice_says_so_and_still_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use(monkeypatch, machine())
        runner.invoke(cli.app, ["patch", "apply"])

        result = runner.invoke(cli.app, ["--no-color", "patch", "apply"])

        assert result.exit_code == 0
        assert "already applied" in result.stdout

    def test_a_patch_that_cannot_match_fails_without_writing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        system = use(monkeypatch, machine(blobs={}))
        before = dict(system.files)

        result = runner.invoke(cli.app, ["--no-color", "patch", "apply"])

        assert result.exit_code == 1
        assert "dejavu" in result.stdout
        assert system.files == before

    def test_an_unknown_patch_name_is_a_usage_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use(monkeypatch, machine())
        result = runner.invoke(cli.app, ["patch", "apply", "no-such-patch"])
        assert result.exit_code == 2

    def test_refuses_to_guess_between_several_installs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Writing to the wrong install is not something a user would notice."""
        system = use(
            monkeypatch,
            machine(),
            installs=(OWN_INSTALL, STEAM_INSTALL),
            targeted=None,
        )
        before = dict(system.files)

        result = runner.invoke(cli.app, ["patch", "apply"])

        assert result.exit_code == 2
        assert system.files == before

    def test_an_install_can_be_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        system = use(
            monkeypatch,
            machine(),
            installs=(OWN_INSTALL, STEAM_INSTALL),
            targeted=None,
        )

        result = runner.invoke(cli.app, ["patch", "apply", "--install", OWN_INSTALL.install_id])

        assert result.exit_code == 0
        assert system.read_bytes(PATHS.fonts / "segoeui.ttf") == FONT_BYTES

    def test_a_risky_patch_is_neither_swept_up_nor_applied_unasked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0004: applying a hashed-file edit silently costs multiplayer."""
        risky = Patch(id="risky", summary="edits a hashed file", ic_risk=True, plan=plan_nothing)
        monkeypatch.setattr(cli, "REGISTRY", (SEGOE_FONT_PATCH, risky))
        monkeypatch.setattr(patches, "REGISTRY", (SEGOE_FONT_PATCH, risky))
        system = use(monkeypatch, machine())

        swept = runner.invoke(cli.app, ["--json", "patch", "apply"])
        assert [p["id"] for p in json.loads(swept.stdout)["patches"]] == ["segoe-fonts"]

        named = runner.invoke(cli.app, ["--no-color", "patch", "apply", "risky"])
        assert named.exit_code == 2
        assert "integrity" in named.output
        assert system.read_bytes(PATHS.fonts / "segoeui.ttf") == FONT_BYTES

    def test_json_reports_what_changed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use(monkeypatch, machine())
        result = runner.invoke(cli.app, ["--json", "patch", "apply"])

        payload = json.loads(result.stdout)
        assert payload["command"] == "patch"
        assert payload["action"] == "apply"
        assert payload["patches"][0]["id"] == "segoe-fonts"
        assert payload["patches"][0]["ok"] is True
        assert payload["patches"][0]["changed"] is True


class TestRevert:
    def test_the_install_goes_back_to_how_it_was(self, monkeypatch: pytest.MonkeyPatch) -> None:
        system = use(monkeypatch, machine())
        runner.invoke(cli.app, ["patch", "apply"])

        result = runner.invoke(cli.app, ["--no-color", "patch", "revert"])

        assert result.exit_code == 0
        assert system.read_bytes(PATHS.fonts / "segoeui.ttf") is None
        assert system.read_bytes(DEJAVU) == FONT_BYTES

    def test_reverting_what_was_never_applied_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use(monkeypatch, machine())
        result = runner.invoke(cli.app, ["--no-color", "patch", "revert"])
        assert result.exit_code == 0
        assert "not applied" in result.stdout
