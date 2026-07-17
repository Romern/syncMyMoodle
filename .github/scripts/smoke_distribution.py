"""Smoke-test an installed syncMyMoodle distribution."""

from __future__ import annotations

import importlib
import os
import pkgutil
import subprocess
import sys
import tomllib
from importlib import metadata, resources
from pathlib import Path

from packaging.version import Version


def run(*command: str) -> str:
    """Run an entry point and return its standard output."""
    return subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout


def require_version(label: str, raw_version: str, expected: Version) -> None:
    actual = Version(raw_version)
    assert actual == expected, f"{label}: {actual} != {expected}"


def require_cli_version(label: str, output: str, expected: Version) -> None:
    lines = output.splitlines()
    prefix = "syncmymoodle "
    assert len(lines) == 1 and lines[0].startswith(prefix), (label, output)
    require_version(label, lines[0].removeprefix(prefix), expected)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} EXPECTED_VERSION")

    expected_version = Version(sys.argv[1])
    actual_version = metadata.version("syncMyMoodle")
    require_version("installed metadata", actual_version, expected_version)

    package = importlib.import_module("syncmymoodle")
    for module in pkgutil.iter_modules(package.__path__, "syncmymoodle."):
        importlib.import_module(module.name)

    executable = Path(sys.executable).parent / (
        "syncmymoodle.exe" if os.name == "nt" else "syncmymoodle"
    )
    assert executable.is_file(), executable

    require_cli_version(
        "console entry point",
        run(str(executable), "--version"),
        expected_version,
    )
    require_cli_version(
        "module entry point",
        run(sys.executable, "-m", "syncmymoodle", "--version"),
        expected_version,
    )
    assert run(str(executable), "--help")
    assert run(sys.executable, "-m", "syncmymoodle", "--help")

    example = run(str(executable), "config", "example")
    tomllib.loads(example)

    package_files = resources.files("syncmymoodle")
    bundled_example = package_files.joinpath("config.toml.example")
    certificate = package_files.joinpath("certs", "HARICA-GEANT-TLS-R1.pem")
    assert bundled_example.is_file()
    tomllib.loads(bundled_example.read_text(encoding="utf-8"))
    assert certificate.is_file()
    assert "BEGIN CERTIFICATE" in certificate.read_text(encoding="ascii")


if __name__ == "__main__":
    main()
