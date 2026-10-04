"""/update: pull the latest code from GitHub, check it compiles, and restart into it."""
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT_S = 120


@dataclass
class UpdateResult:
    message: str
    restart: bool


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[-500:])
    return result.stdout.strip()


def current_version(repo: Path) -> str:
    return git(repo, "log", "-1", "--format=%h %s")


def self_update(repo: Path) -> UpdateResult:
    """Fast-forward to the latest pushed commit. If the new code doesn't compile, go back to the old one."""
    before = git(repo, "rev-parse", "--short", "HEAD")
    git(repo, "fetch", "--quiet", "origin")
    if git(repo, "rev-list", "--count", "HEAD..@{upstream}") == "0":
        return UpdateResult(f"Already up to date: {current_version(repo)}", restart=False)
    changes = git(repo, "log", "--format=%h %s", "HEAD..@{upstream}")
    git(repo, "merge", "--ff-only", "--quiet", "@{upstream}")
    sources = [str(path) for path in sorted((repo / "bridge").glob("*.py"))]
    check = subprocess.run([sys.executable, "-m", "py_compile", *sources], capture_output=True, text=True)
    if check.returncode != 0:
        git(repo, "reset", "--hard", "--quiet", before)
        return UpdateResult(
            f"Update stopped: the new code doesn't compile, so I stayed on {before}.\n{check.stderr[-800:]}",
            restart=False,
        )
    return UpdateResult(f"Updated {before} → {current_version(repo)}\n{changes}\nRestarting, back in ~20s.", restart=True)
