"""Guards on the release pipeline (issue #4).

These assert the delivery decisions, not the code: the version comes from the
git tag, and publishing uses PyPI trusted publishing rather than a stored
token. Both are easy to break silently and expensive to notice late.
"""

import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


def _load_check_version() -> ModuleType:
    path = REPO_ROOT / "scripts" / "check-version.py"
    spec = importlib.util.spec_from_file_location("check_version", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_version = _load_check_version()


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


@pytest.fixture(scope="module")
def release_workflow() -> dict[str, Any]:
    parsed: dict[Any, Any] = yaml.safe_load(RELEASE_WORKFLOW.read_text())
    # PyYAML reads a bare `on:` key as the boolean True.
    if True in parsed:
        parsed["on"] = parsed.pop(True)
    return parsed


def test_version_is_derived_from_the_git_tag(pyproject: dict[str, Any]) -> None:
    assert "version" in pyproject["project"].get("dynamic", [])
    assert "version" not in pyproject["project"]
    assert pyproject["tool"]["hatch"]["version"]["source"] == "vcs"
    assert any("hatch-vcs" in req for req in pyproject["build-system"]["requires"])


def test_release_is_triggered_by_pushing_a_version_tag(
    release_workflow: dict[str, Any],
) -> None:
    assert release_workflow["on"]["push"]["tags"] == ["v*"]


def test_release_publishes_with_trusted_publishing_and_no_stored_token() -> None:
    body = RELEASE_WORKFLOW.read_text()
    assert "secrets." not in body
    assert "PYPI_API_TOKEN" not in body
    assert "id-token: write" in body


def test_release_refuses_to_publish_when_tag_and_package_disagree() -> None:
    body = RELEASE_WORKFLOW.read_text()
    assert "check-version.py" in body


def _dist(tmp_path: Path, version: str) -> Path:
    (tmp_path / f"dcs_linux_installer-{version}-py3-none-any.whl").touch()
    (tmp_path / f"dcs_linux_installer-{version}.tar.gz").touch()
    return tmp_path


def test_check_version_accepts_artefacts_matching_the_tag(tmp_path: Path) -> None:
    dist = _dist(tmp_path, "1.2.3")
    assert check_version.main("refs/tags/v1.2.3", dist) == 0


def test_check_version_rejects_artefacts_that_disagree_with_the_tag(tmp_path: Path) -> None:
    dist = _dist(tmp_path, "0.1.dev35+g09624a2")
    assert check_version.main("refs/tags/v1.2.3", dist) == 1


def test_check_version_rejects_an_empty_dist_directory(tmp_path: Path) -> None:
    assert check_version.main("refs/tags/v1.2.3", tmp_path) == 1


def test_check_version_rejects_a_missing_tag(tmp_path: Path) -> None:
    assert check_version.main("", _dist(tmp_path, "1.2.3")) == 1


def test_readme_documents_both_uvx_and_uv_tool_install() -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    assert "uvx --from dcs-linux-installer dcs-linux" in readme
    assert "uv tool install dcs-linux-installer" in readme
