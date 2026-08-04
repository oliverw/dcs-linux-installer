"""The rest of the registry: the two IC-risky fixes and the shader-cache clear.

These are the patches that can cost a user their multiplayer access, so the
tests care less about "does it edit the file" than about the two ways it can
go wrong quietly: writing something that leaves the install broken, and
leaving an install modified with nobody saying so.
"""

from __future__ import annotations

from pathlib import Path

from dcs_linux.checks import INTEGRITY_CHECK, Status, check_integrity
from dcs_linux.patches import (
    MFD_TEXTURE_PATCH,
    VOICE_CHAT_PATCH,
    Outcome,
    Patch,
    PatchStatus,
    apply_patch,
    clear_shader_cache,
    find_mfd_textures,
    revert_patch,
    states,
)
from dcs_linux.patchstate import load
from dcs_linux.probes import Environment, probe_patches
from tests.environments import LAYOUT, OWN_INSTALL, PATHS, healthy_environment
from tests.fakes import FakeSystem, FakeWriter
from tests.test_patches import DCS_VERSION, STORE, install_files

OPTIONS_DB = PATHS.game / "MissionEditor" / "modules" / "optionsDb.lua"

OPTIONS_DB_WITH_VOICE_CHAT = """local Db = {}
Db.plugins = {}
Db.plugins["voice_chat"] = require("VoiceChat")
Db.plugins["sound"] = require("Sound")
return Db
"""

TEXTURES = PATHS.game / "Mods" / "aircraft" / "AH-64D" / "Textures" / "AH-64D_cockpit"
MFD_TEXTURE = TEXTURES / "MFD_LCD_AH64_LEFT.dds"
SIGHT_TEXTURE = TEXTURES / "TEDAC_LCD_AH64.dds"
OTHER_TEXTURE = TEXTURES / "cockpit_panel.dds"

ORIGINAL_TEXTURE = b"DDS \x00compressed"
CONVERTED_TEXTURE = b"DDS \x00uncompressed"

FXO = PATHS.prefix_saved_games / "DCS" / "fxo"
METASHADERS = PATHS.prefix_saved_games / "DCS" / "metashaders2"
DURABLE_FXO = PATHS.saved_games / "DCS" / "fxo" if PATHS.saved_games else Path("/nowhere")


def apply(system: FakeSystem, writer: FakeWriter, patch: Patch) -> Outcome:
    return apply_patch(system, writer, STORE, patch, PATHS, DCS_VERSION)


class TestVoiceChat:
    """IC-risky: `optionsDb.lua` is a game file, and DCS hashes it."""

    def machine(self, contents: str = OPTIONS_DB_WITH_VOICE_CHAT) -> FakeSystem:
        return FakeSystem(files={str(OPTIONS_DB): contents})

    def test_the_voice_chat_line_is_commented_out_and_the_rest_left_alone(self) -> None:
        system = self.machine()

        outcome = apply(system, FakeWriter(system), VOICE_CHAT_PATCH)

        assert outcome.ok and outcome.changed
        patched = system.read_text(OPTIONS_DB) or ""
        assert '-- Db.plugins["voice_chat"] = require("VoiceChat")' in patched
        assert 'Db.plugins["sound"] = require("Sound")' in patched

    def test_the_edit_is_recorded_so_drift_and_revert_work_like_any_other_patch(self) -> None:
        system = self.machine()
        writer = FakeWriter(system)
        apply(system, writer, VOICE_CHAT_PATCH)

        record = load(system, STORE)["voice-chat"]
        assert [file.path for file in record.files] == [OPTIONS_DB]
        assert states(system, STORE, (VOICE_CHAT_PATCH,))[0].status is PatchStatus.APPLIED

    def test_revert_restores_the_file_byte_for_byte(self) -> None:
        """Anything less would leave the install failing integrity checks."""
        system = self.machine()
        writer = FakeWriter(system)
        before = install_files(system)

        apply(system, writer, VOICE_CHAT_PATCH)
        revert_patch(system, writer, STORE, VOICE_CHAT_PATCH)

        assert install_files(system) == before

    def test_a_version_with_no_voice_chat_entry_is_a_refusal_not_a_no_op(self) -> None:
        """On current DCS versions this fix is not needed (ADR-0004)."""
        system = self.machine("local Db = {}\nreturn Db\n")
        before = dict(system.files)

        outcome = apply(system, FakeWriter(system), VOICE_CHAT_PATCH)

        assert not outcome.ok
        assert "not needed" in outcome.detail
        assert system.files == before

    def test_a_multi_line_entry_is_refused_rather_than_half_commented(self) -> None:
        """`-- ` on a line that opens a table would leave the file unloadable."""
        system = self.machine('Db.plugins["voice_chat"] = {\n  enabled = true,\n}\n')
        before = dict(system.files)

        outcome = apply(system, FakeWriter(system), VOICE_CHAT_PATCH)

        assert not outcome.ok
        assert "unloadable" in outcome.detail
        assert system.files == before

    def test_a_table_opening_on_the_next_line_is_refused_too(self) -> None:
        """The same multi-line entry, spelled over one more line."""
        system = self.machine('Db.plugins["voice_chat"] =\n{\n  enabled = true,\n}\n')
        before = dict(system.files)

        outcome = apply(system, FakeWriter(system), VOICE_CHAT_PATCH)

        assert not outcome.ok
        assert "unloadable" in outcome.detail
        assert system.files == before

    def test_an_entry_sharing_a_line_with_others_is_refused(self) -> None:
        """Commenting the line out would silently disable `sound` as well."""
        system = self.machine('gui = { ["voice_chat"] = true, ["sound"] = true }\n')
        before = dict(system.files)

        outcome = apply(system, FakeWriter(system), VOICE_CHAT_PATCH)

        assert not outcome.ok
        assert system.files == before

    def test_a_missing_options_db_writes_nothing(self) -> None:
        system = FakeSystem()
        outcome = apply(system, FakeWriter(system), VOICE_CHAT_PATCH)
        assert not outcome.ok
        assert system.files == {}

    def test_an_already_commented_line_is_not_commented_twice(self) -> None:
        system = self.machine('-- Db.plugins["voice_chat"] = require("VoiceChat")\n')
        outcome = apply(system, FakeWriter(system), VOICE_CHAT_PATCH)
        assert not outcome.ok


class TestMfdTextures:
    """IC-risky, and it needs a tool that is not installed by default."""

    def machine(self, **overrides: object) -> FakeSystem:
        defaults: dict[str, object] = {
            "blobs": {
                str(MFD_TEXTURE): ORIGINAL_TEXTURE,
                str(SIGHT_TEXTURE): ORIGINAL_TEXTURE,
                str(OTHER_TEXTURE): ORIGINAL_TEXTURE,
            },
            "executables": {"magick": "/usr/bin/magick"},
            "binary_commands": {
                f"magick {path} -define dds:compression=none dds:-": CONVERTED_TEXTURE
                for path in (MFD_TEXTURE, SIGHT_TEXTURE)
            },
        }
        return FakeSystem(**{**defaults, **overrides})  # type: ignore[arg-type]

    def test_only_the_mfd_and_sight_textures_are_touched(self) -> None:
        system = self.machine()

        outcome = apply(system, FakeWriter(system), MFD_TEXTURE_PATCH)

        assert outcome.ok and outcome.changed
        assert system.read_bytes(MFD_TEXTURE) == CONVERTED_TEXTURE
        assert system.read_bytes(SIGHT_TEXTURE) == CONVERTED_TEXTURE
        assert system.read_bytes(OTHER_TEXTURE) == ORIGINAL_TEXTURE

    def test_revert_puts_the_shipped_textures_back(self) -> None:
        system = self.machine()
        writer = FakeWriter(system)
        before = install_files(system)

        apply(system, writer, MFD_TEXTURE_PATCH)
        revert_patch(system, writer, STORE, MFD_TEXTURE_PATCH)

        assert install_files(system) == before

    def test_a_missing_imagemagick_says_how_to_install_it(self) -> None:
        system = self.machine(executables={}, binary_commands={})
        before = dict(system.files)

        outcome = apply(system, FakeWriter(system), MFD_TEXTURE_PATCH)

        assert not outcome.ok
        assert "ImageMagick" in outcome.detail
        assert system.files == before

    def test_imagemagick_6_is_accepted_under_its_own_name(self) -> None:
        system = self.machine(
            executables={"convert": "/usr/bin/convert"},
            binary_commands={
                f"convert {path} -define dds:compression=none dds:-": CONVERTED_TEXTURE
                for path in (MFD_TEXTURE, SIGHT_TEXTURE)
            },
        )

        outcome = apply(system, FakeWriter(system), MFD_TEXTURE_PATCH)

        assert outcome.ok
        assert system.read_bytes(MFD_TEXTURE) == CONVERTED_TEXTURE

    def test_a_conversion_that_fails_writes_nothing_at_all(self) -> None:
        """Half-converted textures are the one outcome worse than none."""
        system = self.machine(
            binary_commands={
                f"magick {MFD_TEXTURE} -define dds:compression=none dds:-": CONVERTED_TEXTURE
            }
        )
        before = dict(system.files)

        outcome = apply(system, FakeWriter(system), MFD_TEXTURE_PATCH)

        assert not outcome.ok
        assert system.files == before

    def test_an_install_with_no_loose_textures_is_told_why_plainly(self) -> None:
        """The honest reason, not "your install is fine": DCS ships these
        textures inside .zip archives, which this patch does not open."""
        system = FakeSystem(executables={"magick": "/usr/bin/magick"})

        outcome = apply(system, FakeWriter(system), MFD_TEXTURE_PATCH)

        assert not outcome.ok
        assert ".zip archives" in outcome.detail

    def test_the_search_is_bounded_and_finds_textures_by_name(self) -> None:
        system = self.machine()
        assert find_mfd_textures(system, PATHS) == (MFD_TEXTURE, SIGHT_TEXTURE)


class TestShaderCache:
    """Maintenance, not a patch: nothing to back up, nothing to revert."""

    def machine(self) -> FakeSystem:
        return FakeSystem(
            files={
                str(FXO / "0a1b.fxo"): "compiled",
                str(METASHADERS / "deferred.hlsl"): "compiled",
                str(DURABLE_FXO / "0a1b.fxo"): "compiled",
                str(PATHS.game / "bin" / "DCS.exe"): "untouched",
            }
        )

    def test_every_cache_directory_is_deleted(self) -> None:
        system = self.machine()

        cleared = clear_shader_cache(system, FakeWriter(system), PATHS)

        assert set(cleared.directories) == {FXO, METASHADERS, DURABLE_FXO}
        assert system.read_text(FXO / "0a1b.fxo") is None
        assert system.read_text(DURABLE_FXO / "0a1b.fxo") is None

    def test_the_game_directory_is_never_touched(self) -> None:
        """That is the whole reason clearing the cache is integrity-check safe."""
        system = self.machine()

        clear_shader_cache(system, FakeWriter(system), PATHS)

        assert system.read_text(PATHS.game / "bin" / "DCS.exe") == "untouched"

    def test_nothing_is_recorded_because_there_is_nothing_to_revert(self) -> None:
        system = self.machine()

        clear_shader_cache(system, FakeWriter(system), PATHS)

        assert load(system, STORE) == {}

    def test_a_mapped_saved_games_is_cleared_once_not_twice(self) -> None:
        """In-prefix and durable are one directory on a mapped install."""
        system = FakeSystem(
            files={str(DURABLE_FXO / "0a1b.fxo"): "compiled"},
            links={str(PATHS.prefix_saved_games): str(PATHS.saved_games)},
        )

        cleared = clear_shader_cache(system, FakeWriter(system), PATHS)

        assert len(cleared.directories) == 1
        assert system.read_text(DURABLE_FXO / "0a1b.fxo") is None

    def test_clearing_an_empty_cache_is_not_an_error(self) -> None:
        system = FakeSystem()
        cleared = clear_shader_cache(system, FakeWriter(system), PATHS)
        assert cleared.directories == ()
        assert "no shader cache" in cleared.detail


class TestIntegrityCheckRow:
    """`check` must say when the install carries hashed-file modifications."""

    def environment_after_applying(self, system: FakeSystem) -> Environment:
        return healthy_environment(patches=probe_patches(system, LAYOUT, OWN_INSTALL))

    def test_an_unmodified_install_passes(self) -> None:
        result = check_integrity(healthy_environment())
        assert result.name == INTEGRITY_CHECK
        assert result.status is Status.PASS

    def test_a_risky_patch_in_place_warns_and_says_how_to_undo_it(self) -> None:
        system = FakeSystem(files={str(OPTIONS_DB): OPTIONS_DB_WITH_VOICE_CHAT})
        apply(system, FakeWriter(system), VOICE_CHAT_PATCH)

        result = check_integrity(self.environment_after_applying(system))

        assert result.status is Status.WARN
        assert "voice-chat" in result.detail
        assert (
            result.remediation == "dcs-linux patch revert voice-chat   # to play multiplayer again"
        )

    def test_partly_drifted_still_counts_as_modified(self) -> None:
        """Drift is per file, so an update can leave some of our edits in place."""
        system = FakeSystem(files={str(OPTIONS_DB): OPTIONS_DB_WITH_VOICE_CHAT})
        writer = FakeWriter(system)
        apply(system, writer, VOICE_CHAT_PATCH)
        writer.write_bytes(OPTIONS_DB, b"shipped by DCS 2.9.29")

        result = check_integrity(self.environment_after_applying(system))

        assert result.status is Status.WARN

    def test_it_is_a_warning_not_a_blocking_failure(self) -> None:
        """The user chose this; `check` states the cost, it does not veto it."""
        system = FakeSystem(files={str(OPTIONS_DB): OPTIONS_DB_WITH_VOICE_CHAT})
        apply(system, FakeWriter(system), VOICE_CHAT_PATCH)

        result = check_integrity(self.environment_after_applying(system))

        assert not result.is_blocking
