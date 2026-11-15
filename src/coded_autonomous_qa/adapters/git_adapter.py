"""Read-only git access (subprocess, fixed argv, allow-listed subcommands)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

READ_ONLY_SUBCOMMANDS = frozenset({"diff", "log", "rev-parse", "show", "ls-files", "status", "merge-base"})
_REF = re.compile(r"^[A-Za-z0-9._/~^@{}\-]{1,200}$")


class GitError(Exception):
    pass


class GitAdapter:
    def __init__(self, repo: Path, timeout_s: int = 60) -> None:
        if shutil.which("git") is None:
            raise GitError("git is not installed or not on PATH.")
        self.repo = repo
        self.timeout_s = timeout_s

    def _run(self, *args: str) -> str:
        if not args or args[0] not in READ_ONLY_SUBCOMMANDS:
            raise GitError(f"git {args[0] if args else ''} is not an allowed read-only subcommand.")
        proc = subprocess.run(
            ["git", "-C", str(self.repo), "--no-pager", *args],
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
            check=False,
            env={"GIT_TERMINAL_PROMPT": "0", "PATH": _safe_path(), "HOME": str(Path.home())},
        )
        if proc.returncode != 0:
            raise GitError(proc.stderr.strip() or f"git {' '.join(args)} failed")
        return proc.stdout

    @staticmethod
    def check_ref(ref: str) -> str:
        if not _REF.match(ref) or ref.startswith("-"):
            raise GitError(f"Invalid git ref: {ref!r}")
        return ref

    def toplevel(self) -> Path:
        return Path(self._run("rev-parse", "--show-toplevel").strip())

    def changed_files(self, base: str, head: str | None) -> list[dict[str, Any]]:
        """Files changed between base and head (head=None: working tree incl. staged changes)."""
        base = self.check_ref(base)
        rng = [base, self.check_ref(head)] if head else [base]
        files: dict[str, dict[str, Any]] = {}
        for line in self._run("diff", "--name-status", "-M", *rng).splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0][0]
            if status == "R" and len(parts) >= 3:
                files[parts[2]] = {"path": parts[2], "status": "renamed", "old_path": parts[1]}
            else:
                files[parts[1]] = {
                    "path": parts[1],
                    "status": {"A": "added", "M": "modified", "D": "deleted", "C": "copied", "T": "type_changed"}.get(
                        status, status
                    ),
                }
        for line in self._run("diff", "--numstat", "-M", *rng).splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                path = parts[2]
                if " => " in path:  # rename notation
                    path = re.sub(r"\{[^}]* => ([^}]*)\}", r"\1", path)
                    path = path.split(" => ")[-1]
                entry = files.get(path)
                if entry is not None:
                    entry["added"] = int(parts[0]) if parts[0].isdigit() else 0
                    entry["deleted"] = int(parts[1]) if parts[1].isdigit() else 0
        for entry in files.values():
            entry.setdefault("added", 0)
            entry.setdefault("deleted", 0)
        return list(files.values())

    def commits(self, base: str, head: str, limit: int = 100) -> list[dict[str, str]]:
        out = self._run(
            "log",
            f"--max-count={limit}",
            "--format=%H%x1f%an%x1f%ad%x1f%s",
            "--date=iso-strict",
            f"{self.check_ref(base)}..{self.check_ref(head)}",
        )
        commits = []
        for line in out.splitlines():
            sha, author, date, subject = ([*line.split("\x1f"), "", "", "", ""])[:4]
            commits.append({"sha": sha, "author": author, "date": date, "subject": subject})
        return commits

    def tracked_files(self) -> list[str]:
        return [f for f in self._run("ls-files").splitlines() if f]


def _safe_path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin")
