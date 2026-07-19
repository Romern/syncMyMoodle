import tomllib
from pathlib import Path

from syncmymoodle.constants import YT_DLP_TESTED_VERSION


def test_yt_dlp_requirement_matches_tested_baseline():
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]

    assert f"yt-dlp[default]>={YT_DLP_TESTED_VERSION}" in project["dependencies"]
