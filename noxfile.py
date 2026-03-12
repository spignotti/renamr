import shutil
import tomllib
from pathlib import Path

import nox

nox.options.sessions = ["lint", "typecheck", "test"]
nox.options.stop_on_first_error = False

PROJECT_NAME = tomllib.loads(Path("pyproject.toml").read_text())["project"]["name"]


def sync_project(session: nox.Session) -> None:
    """Sync dependencies and reinstall the local package."""
    session.run(
        "uv",
        "sync",
        "--all-groups",
        "--reinstall-package",
        PROJECT_NAME,
        external=True,
    )


def run_uv(session: nox.Session, *args: str) -> None:
    """Run a uv command from within a nox session."""
    sync_project(session)
    session.run("uv", *args, external=True)


@nox.session
def lint(session: nox.Session) -> None:
    """Check code with ruff."""
    run_uv(session, "run", "ruff", "check", "src", "tests")


@nox.session
def format(session: nox.Session) -> None:
    """Format code with ruff."""
    run_uv(session, "run", "ruff", "format", "src", "tests")


@nox.session
def typecheck(session: nox.Session) -> None:
    """Type check with pyright."""
    run_uv(session, "run", "pyright", "src", "tests")


@nox.session
def fix(session: nox.Session) -> None:
    """Auto-fix all issues."""
    run_uv(session, "run", "ruff", "check", "--fix", "src", "tests")
    run_uv(session, "run", "ruff", "format", "src", "tests")


@nox.session
def test(session: nox.Session) -> None:
    """Run all tests."""
    run_uv(session, "run", "pytest", "-v")


@nox.session
def test_unit(session: nox.Session) -> None:
    """Run unit tests only."""
    run_uv(session, "run", "pytest", "tests/unit", "-v")


@nox.session
def coverage(session: nox.Session) -> None:
    """Run tests with coverage."""
    run_uv(session, "run", "pytest", "--cov=src", "--cov-report=term")


@nox.session
def ci(session: nox.Session) -> None:
    """Full CI pipeline."""
    run_uv(session, "run", "ruff", "check", "src", "tests")
    run_uv(session, "run", "ruff", "format", "--check", "src", "tests")
    run_uv(session, "run", "pyright", "src", "tests")
    run_uv(session, "run", "pytest", "-v", "--cov=src")


@nox.session
def clean(session: nox.Session) -> None:
    """Clean build artifacts."""
    for raw_path in [".pytest_cache", ".ruff_cache", ".pyright", "htmlcov", ".coverage"]:
        path = Path(raw_path)
        if path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            session.log(f"Removed: {path}")

    for path in Path(".").rglob("__pycache__"):
        shutil.rmtree(path)
        session.log(f"Removed: {path}")
