"""Valve KeyValues parsing, against the shapes Steam actually writes."""

from dcs_linux.vdf import dig, parse

LIBRARY_FOLDERS = """
"libraryfolders"
{
    "0"
    {
        "path"        "/home/pilot/.local/share/Steam"
        "label"        ""
        "apps"
        {
            "223750"        "162963086263"
        }
    }
    "1"
    {
        "path"        "/mnt/games/SteamLibrary"
        "label"        ""
    }
}
"""


class TestParse:
    def test_nested_blocks_become_nested_dicts(self) -> None:
        parsed = parse(LIBRARY_FOLDERS)
        folders = parsed["libraryfolders"]
        assert isinstance(folders, dict)
        assert dig(parsed, "libraryfolders", "1", "path") == "/mnt/games/SteamLibrary"

    def test_keys_are_case_insensitive(self) -> None:
        """Steam capitalises the same key differently in different files."""
        parsed = parse('"InstallConfigStore" { "Software" { "Valve" "steam" } }')
        assert dig(parsed, "installconfigstore", "software", "valve") == "steam"

    def test_comments_are_ignored(self) -> None:
        parsed = parse('// a comment\n"root"\n{\n "path" "/games" // trailing\n}\n')
        assert dig(parsed, "root", "path") == "/games"

    def test_escapes_are_decoded(self) -> None:
        parsed = parse(r'"root" { "path" "/mnt/My\\Disk" "name" "say \"hi\"" }')
        assert dig(parsed, "root", "path") == "/mnt/My\\Disk"
        assert dig(parsed, "root", "name") == 'say "hi"'

    def test_empty_text(self) -> None:
        assert parse("") == {}

    def test_unbalanced_braces_do_not_raise(self) -> None:
        """A half-written config is a fact about the machine, not a crash."""
        assert dig(parse('"root" { "path" "/games"'), "root", "path") == "/games"


class TestDig:
    PARSED = parse(LIBRARY_FOLDERS)

    def test_missing_key_is_none(self) -> None:
        assert dig(self.PARSED, "libraryfolders", "9", "path") is None

    def test_descending_into_a_string_is_none(self) -> None:
        assert dig(self.PARSED, "libraryfolders", "0", "path", "deeper") is None

    def test_a_block_is_not_a_string_value(self) -> None:
        assert dig(self.PARSED, "libraryfolders") is None
