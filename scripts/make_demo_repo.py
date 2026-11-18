"""Create disposable shop releases for manually exercising SkewGuard."""

import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=SkewGuard Demo",
            "-c",
            "user.email=demo@example.invalid",
            "-C",
            str(repo),
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def make_demo_repo(destination: Path) -> Path:
    """Create base, safe, and breaking refs, refusing to overwrite any destination."""
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    shutil.copytree(
        ROOT / "benchmark/apps/shop",
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".skewguard-venv"),
    )
    git(destination, "init", "--initial-branch=main")
    git(destination, "add", ".")
    git(destination, "commit", "-m", "Create baseline shop")
    git(destination, "tag", "base")

    migration = destination / "catalog/migrations/0002_change.py"
    model = destination / "catalog/models.py"
    git(destination, "checkout", "-b", "safe")
    shutil.copyfile(ROOT / "benchmark/cases/shop_add_notes.py", migration)
    model.write_text(
        model.read_text(encoding="utf-8").replace(
            "    class Meta:",
            "    notes = models.TextField(null=True)\n\n    class Meta:",
            1,
        ),
        encoding="utf-8",
    )
    git(destination, "add", ".")
    git(destination, "commit", "-m", "Add nullable product notes")

    git(destination, "checkout", "-b", "breaking", "base")
    shutil.copyfile(ROOT / "benchmark/cases/shop_rename_price.py", migration)
    for path in (model, destination / "catalog/views.py"):
        path.write_text(
            path.read_text(encoding="utf-8").replace("price", "amount"),
            encoding="utf-8",
        )
    git(destination, "add", ".")
    git(destination, "commit", "-m", "Rename product price to amount")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "destination", type=Path, help="A new temporary repository path."
    )
    arguments = parser.parse_args()
    try:
        repo = make_demo_repo(arguments.destination)
    except (OSError, subprocess.SubprocessError) as error:
        parser.exit(2, f"Error: {error}\n")
    print(f"Demo repository: {repo}")
    print("Refs: base (old release), safe (nullable field), breaking (renamed field)")


if __name__ == "__main__":
    main()
