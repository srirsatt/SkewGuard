"""The manual CLI demo uses real committed releases without touching user repos."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_demo_builds_clean_safe_and_breaking_releases_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "shop-demo"
    command = [
        sys.executable,
        str(ROOT / "scripts/make_demo_repo.py"),
        str(destination),
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, check=True, timeout=30
    )
    assert "Refs: base" in result.stdout

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(destination), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        ).stdout

    assert "price = models.IntegerField" in git("show", "base:catalog/models.py")
    assert "notes = models.TextField(null=True)" in git(
        "show", "safe:catalog/models.py"
    )
    assert "amount = models.IntegerField" in git("show", "breaking:catalog/models.py")
    assert "amount" in git("show", "breaking:catalog/views.py")
    assert git("status", "--porcelain") == ""
    before = git("rev-parse", "HEAD")
    again = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert again.returncode == 2
    assert "already exists" in again.stderr
    assert git("rev-parse", "HEAD") == before
