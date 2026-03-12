"""Integration smoke tests for the package scaffold."""

from file_renamer import __version__


def test_package_exposes_version() -> None:
    assert __version__ == "0.1.0"
