import re
import tomllib
from pathlib import Path

import remi

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def test_version_is_semver_without_labels():
    assert SEMVER.match(remi.__version__), remi.__version__


def test_major_version_is_zero_until_first_completion():
    assert remi.__version__.split(".")[0] == "0"


def test_pyproject_reads_version_from_package():
    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" in data["project"]["dynamic"]
    assert data["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "remi.__version__"


def test_changelog_mentions_current_version_or_unreleased():
    text = (Path(__file__).parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]" in text or f"## [{remi.__version__}]" in text


def test_user_agent():
    assert remi.user_agent() == f"Remi/{remi.__version__}"
