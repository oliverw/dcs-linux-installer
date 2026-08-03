"""Guards on the release pipeline (issue #4).

These assert the delivery decisions, not the code: the version comes from the
git tag, publishing is triggered by a tag, and it uses PyPI trusted publishing
rather than a stored token. Each is easy to break silently, and a PyPI upload
is irreversible.
"""

import tomllib
from pathlib import Path
from typing import Any

import check_version
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

# CI config is not shipped to users, so the workflow guards only run in a clone.
needs_workflow = pytest.mark.skipif(
    not RELEASE_WORKFLOW.exists(), reason="repo-only guard: release.yml is not in the sdist"
)


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


@pytest.fixture(scope="module")
def release_steps(release_workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return list(release_workflow["jobs"]["publish"]["steps"])


def test_version_is_derived_from_the_git_tag(pyproject: dict[str, Any]) -> None:
    assert "version" in pyproject["project"].get("dynamic", [])
    assert "version" not in pyproject["project"]
    assert pyproject["tool"]["hatch"]["version"]["source"] == "vcs"
    assert any("hatch-vcs" in req for req in pyproject["build-system"]["requires"])


def test_the_sdist_ships_everything_its_own_tests_need(pyproject: dict[str, Any]) -> None:
    included = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert {"src", "tests", "scripts"} <= set(included)


@needs_workflow
def test_release_is_triggered_by_pushing_a_version_tag(
    release_workflow: dict[str, Any],
) -> None:
    assert release_workflow["on"]["push"]["tags"] == ["v*"]


@needs_workflow
def test_release_uses_trusted_publishing_and_no_stored_token(
    release_workflow: dict[str, Any],
) -> None:
    body = RELEASE_WORKFLOW.read_text()
    assert "secrets." not in body
    assert "PYPI_API_TOKEN" not in body
    assert release_workflow["jobs"]["publish"]["permissions"]["id-token"] == "write"


@needs_workflow
def test_release_grants_no_write_scope_beyond_the_oidc_token(
    release_workflow: dict[str, Any],
) -> None:
    permissions = release_workflow["jobs"]["publish"]["permissions"]
    # An explicit block sets every unlisted scope to none, so checkout needs
    # contents: read spelled out -- otherwise this breaks if the repo goes private.
    assert permissions["contents"] == "read"
    assert set(permissions) == {"contents", "id-token"}


@needs_workflow
def test_release_checks_the_tag_and_runs_the_suite_before_publishing(
    release_steps: list[dict[str, Any]],
) -> None:
    commands = [step.get("run", "") for step in release_steps]
    publish = next(i for i, run in enumerate(commands) if "uv publish" in run)
    gates = ["uv run pytest", "check_version.py", "uvx --from ./dist/*.whl"]
    for gate in gates:
        index = next(i for i, run in enumerate(commands) if gate in run)
        assert index < publish, f"{gate!r} must run before the upload"


def _dist(tmp_path: Path, version: str) -> Path:
    (tmp_path / f"dcs_linux_installer-{version}-py3-none-any.whl").touch()
    (tmp_path / f"dcs_linux_installer-{version}.tar.gz").touch()
    return tmp_path


def test_check_version_accepts_artefacts_matching_the_tag(tmp_path: Path) -> None:
    assert check_version.main("refs/tags/v1.2.3", _dist(tmp_path, "1.2.3")) == 0


def test_check_version_accepts_a_pre_release_tag_hatch_vcs_normalised(tmp_path: Path) -> None:
    # hatch-vcs turns the tag v1.2.3-rc1 into the version 1.2.3rc1.
    assert check_version.main("refs/tags/v1.2.3-rc1", _dist(tmp_path, "1.2.3rc1")) == 0


def test_check_version_rejects_artefacts_that_disagree_with_the_tag(tmp_path: Path) -> None:
    # What a shallow clone produces: no tags, so hatch-vcs falls back to a dev version.
    assert check_version.main("refs/tags/v1.2.3", _dist(tmp_path, "0.1.dev35+g09624a2")) == 1


def test_check_version_rejects_an_empty_dist_directory(tmp_path: Path) -> None:
    assert check_version.main("refs/tags/v1.2.3", tmp_path) == 1


def test_check_version_rejects_a_missing_tag(tmp_path: Path) -> None:
    assert check_version.main("", _dist(tmp_path, "1.2.3")) == 1


def test_readme_documents_both_uvx_and_uv_tool_install() -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    assert "uvx --from dcs-linux-installer dcs-linux" in readme
    assert "uv tool install dcs-linux-installer" in readme
