from pathlib import Path

from dcs_linux.desktop import ICON_URL, Desktop, ShortcutStatus, create_shortcut, detect_desktop
from dcs_linux.installs import DcsInstall, Launcher
from tests.fakes import FakeFileFetcher, FakeSystem, FakeWriter

ICON = b"\xff\xd8\xffDCS icon"


def test_kde_is_detected_from_the_current_desktop_session() -> None:
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "KDE"})

    assert detect_desktop(system) is Desktop.KDE


def test_gnome_is_detected_in_a_composite_current_desktop_session() -> None:
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"})

    assert detect_desktop(system) is Desktop.GNOME


def test_an_unsupported_desktop_is_not_guessed() -> None:
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "sway"})

    assert detect_desktop(system) is None


def test_the_current_desktop_outranks_a_stale_session_name() -> None:
    system = FakeSystem(env={"XDG_CURRENT_DESKTOP": "sway", "DESKTOP_SESSION": "gnome"})

    assert detect_desktop(system) is None


def test_a_shortcut_launches_the_stable_install_and_is_executable() -> None:
    system = FakeSystem()
    writer = FakeWriter(system)
    fetcher = FakeFileFetcher(data=ICON)
    install = DcsInstall(game=Path("/mnt/games/DCS World"), launcher=Launcher.DCS_LINUX)

    result = create_shortcut(system, writer, fetcher, Desktop.KDE, install)

    assert result.status is ShortcutStatus.CREATED
    assert result.path is not None
    assert system.read_text(result.path) == (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=DCS World\n"
        "Comment=Launch DCS World through dcs-linux\n"
        f"Exec=dcs-linux launch --install {install.install_id}\n"
        f"Icon={system.home()}/.local/share/icons/dcs-world.jpg\n"
        "Terminal=false\n"
        "Categories=Game;\n"
    )
    assert system.read_bytes(system.home() / ".local/share/icons/dcs-world.jpg") == ICON
    assert fetcher.urls == [ICON_URL]
    assert result.path in system.executable_bits


def test_creating_the_same_shortcut_twice_does_not_duplicate_it() -> None:
    system = FakeSystem()
    writer = FakeWriter(system)
    fetcher = FakeFileFetcher(data=ICON)
    install = DcsInstall(game=Path("/mnt/games/DCS World"), launcher=Launcher.DCS_LINUX)

    first = create_shortcut(system, writer, fetcher, Desktop.GNOME, install)
    second = create_shortcut(system, writer, fetcher, Desktop.GNOME, install)

    assert first.status is ShortcutStatus.CREATED
    assert second.status is ShortcutStatus.EXISTS
    assert first.path == second.path
    assert len([path for path in system.files if path.suffix == ".desktop"]) == 1
    assert fetcher.urls == [ICON_URL]


def test_a_shortcut_write_failure_is_an_outcome_not_an_exception() -> None:
    class FailingWriter(FakeWriter):
        def write_bytes(self, path: Path, data: bytes) -> None:
            raise OSError("disk is read-only")

    system = FakeSystem()
    install = DcsInstall(game=Path("/mnt/games/DCS World"), launcher=Launcher.DCS_LINUX)

    result = create_shortcut(
        system,
        FailingWriter(system),
        FakeFileFetcher(data=ICON),
        Desktop.KDE,
        install,
    )

    assert result.status is ShortcutStatus.FAILED
    assert "disk is read-only" in result.detail


def test_an_icon_download_failure_creates_no_shortcut() -> None:
    system = FakeSystem()
    install = DcsInstall(game=Path("/mnt/games/DCS World"), launcher=Launcher.DCS_LINUX)

    result = create_shortcut(
        system,
        FakeWriter(system),
        FakeFileFetcher(failure="network unavailable"),
        Desktop.KDE,
        install,
    )

    assert result.status is ShortcutStatus.FAILED
    assert "network unavailable" in result.detail
    assert not system.files


def test_a_non_jpeg_icon_response_is_refused() -> None:
    system = FakeSystem()
    install = DcsInstall(game=Path("/mnt/games/DCS World"), launcher=Launcher.DCS_LINUX)

    result = create_shortcut(
        system,
        FakeWriter(system),
        FakeFileFetcher(data=b"<html>not found</html>"),
        Desktop.GNOME,
        install,
    )

    assert result.status is ShortcutStatus.FAILED
    assert "not a JPEG" in result.detail
    assert not system.files
