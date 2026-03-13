"""Integration smoke tests for the package scaffold."""

from importlib.metadata import version

from renamr import __version__


def test_package_exposes_version() -> None:
    assert __version__ == version("renamr")
