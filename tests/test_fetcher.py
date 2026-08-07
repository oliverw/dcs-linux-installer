from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from dcs_linux.fetcher import FILE_FETCH_TIMEOUT, MAX_FILE_BYTES, RealFileFetcher


class Response:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.data[:limit]


def test_a_small_file_is_downloaded_with_a_bounded_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, int]] = []

    def open_url(url: str, *, timeout: int) -> Response:
        seen.append((url, timeout))
        return Response(b"contents")

    monkeypatch.setattr(urllib.request, "urlopen", open_url)

    result = RealFileFetcher().fetch_file("https://example.test/icon.jpg")

    assert result.data == b"contents"
    assert result.failure is None
    assert seen == [("https://example.test/icon.jpg", FILE_FETCH_TIMEOUT)]


def test_an_oversized_file_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda url, *, timeout: Response(b"x" * (MAX_FILE_BYTES + 1)),  # noqa: ARG005
    )

    result = RealFileFetcher().fetch_file("https://example.test/too-large")

    assert result.data is None
    assert result.failure is not None
    assert "exceeded" in result.failure


def test_a_network_error_is_a_download_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(url: str, *, timeout: int) -> Response:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    result = RealFileFetcher().fetch_file("https://example.test/icon.jpg")

    assert result.data is None
    assert result.failure is not None
    assert "offline" in result.failure
