"""Build number and changelog, read from CHANGELOG.md, which ships inside the package.

The running build is the newest entry in the changelog. Its commit comes from ``_commit.txt``,
which ``git archive`` fills in (see .gitattributes); in a checkout we ask git instead.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path

HERE = Path(__file__).parent
HEADING = re.compile(r"^## Build (\d+)\s*·\s*(\d{4}-\d{2}-\d{2})(?:\s*·\s*([0-9a-f]{7,40}))?\s*$")


@dataclass(frozen=True)
class Release:
    build: int
    date: str
    commit: str | None
    notes: str


def parse_changelog(text: str) -> list[Release]:
    """Every ``## Build N · date [· commit]`` entry, newest first."""
    out: list[Release] = []
    head: re.Match | None = None
    lines: list[str] = []

    def close() -> None:
        if head:
            out.append(Release(int(head[1]), head[2], head[3], "\n".join(lines).strip()))

    for line in text.splitlines():
        if line.startswith("## "):
            close()
            head, lines = HEADING.match(line), []
        elif head:
            lines.append(line)
    close()
    return sorted(out, key=lambda r: r.build, reverse=True)


@cache
def releases() -> list[Release]:
    return parse_changelog((HERE / "CHANGELOG.md").read_text())


def current() -> Release:
    return releases()[0]


def find(build: int) -> Release | None:
    return next((r for r in releases() if r.build == build), None)


@cache
def commit() -> str | None:
    """The commit this code was built from: stamped by ``git archive``, else from git, else None."""
    try:
        stamped = (HERE / "_commit.txt").read_text().strip()
    except OSError:
        stamped = ""
    if stamped and not stamped.startswith("$Format"):
        return stamped
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=HERE, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None
