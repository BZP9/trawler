#!/usr/bin/env python3
"""Mechanical self-audit for RowInfer. Run: uv run python scripts/audit.py

Checks the contracts in AGENTS.md that a machine can verify. Exit 0 = all
pass (warnings allowed), exit 1 = at least one FAIL. Designed for agents
running an improvement pass (see EVOLVE.md) and for CI.

All checks are DB-free and fast.
"""
from __future__ import annotations
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).parent.parent
SRC = REPO / "src" / "trawler"

FAIL: list[str] = []
WARN: list[str] = []


def check(name: str, problems: list[str], warn_only: bool = False) -> None:
    bucket = WARN if warn_only else FAIL
    tag = "warn" if warn_only else "FAIL"
    if problems:
        print(f"✗ {name}")
        for p in problems:
            print(f"    [{tag}] {p}")
        bucket.extend(problems)
    else:
        print(f"✓ {name}")


# ---------------------------------------------------------------------------
# 1. tests pass
# ---------------------------------------------------------------------------

def check_tests() -> list[str]:
    r = subprocess.run(
        ["uv", "run", "pytest", "tests/", "-q", "--no-header"],
        cwd=REPO, capture_output=True, text=True,
    )
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-10:])
        return [f"pytest failed:\n{tail}"]
    return []


# ---------------------------------------------------------------------------
# 2. no RuntimeError in library code (AGENTS.md: setter errors → ConfigError)
#    Allowlisted: the _pre_flight wrapper in run/base.py.
# ---------------------------------------------------------------------------

_RUNTIME_ALLOW = ("pre-flight failed",)


def check_no_runtime_error() -> list[str]:
    problems = []
    for py in SRC.rglob("*.py"):
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if "raise RuntimeError" in line:
                ctx = line.strip()
                nxt = py.read_text().splitlines()[i:i + 2]
                blob = ctx + " ".join(nxt)
                if any(a in blob for a in _RUNTIME_ALLOW):
                    continue
                problems.append(f"{py.relative_to(REPO)}:{i} {ctx} — use a RowInferError subclass")
    return problems


# ---------------------------------------------------------------------------
# 3. every error class: exported in __all__ + documented in the error tables
# ---------------------------------------------------------------------------

def check_errors_exported_and_documented() -> list[str]:
    text = (SRC / "errors.py").read_text()
    subclasses = re.findall(r"^class (\w+)\(RowInferError\):", text, re.M)
    problems = []
    all_block = (SRC / "__init__.py").read_text()
    skill = (REPO / "SKILL.md").read_text()
    run_readme = (SRC / "run" / "README.md").read_text()
    # base class must be exported; only concrete categories go in doc tables
    if "RowInferError" not in all_block:
        problems.append("RowInferError not exported in trawler/__init__.py")
    for cls in subclasses:
        if cls not in all_block:
            problems.append(f"{cls} not exported in trawler/__init__.py")
        if cls not in skill:
            problems.append(f"{cls} missing from SKILL.md error table")
        if cls not in run_readme:
            problems.append(f"{cls} missing from run/README.md error table")
    return problems


# ---------------------------------------------------------------------------
# 4. every subpackage dir with .py files has __init__.py
# ---------------------------------------------------------------------------

def check_init_files() -> list[str]:
    problems = []
    for d in {p.parent for p in SRC.rglob("*.py")} | {p.parent for p in SRC.rglob("*.sql")}:
        if not (d / "__init__.py").exists():
            problems.append(f"{d.relative_to(REPO)}/ missing __init__.py")
    return problems


# ---------------------------------------------------------------------------
# 5. skill drift: rendered SKILL.md blocks vs ~/.claude/skills/<name>/SKILL.md
#    (warn only — the local skills dir is a per-machine install)
# ---------------------------------------------------------------------------

def check_skill_drift() -> list[str]:
    dest = pathlib.Path.home() / ".claude" / "skills"
    src = (REPO / "SKILL.md").read_text()
    problems = []
    for block in re.split(r"^(?=## )", src, flags=re.M):
        m = re.match(r"^## skill: (.+)", block)
        if not m:
            continue
        name = m.group(1).strip()
        trigger = re.search(r"^\*\*Trigger\*\*: (.+)", block, re.M)
        description = trigger.group(1).strip() if trigger else name
        expected = f"---\nname: {name}\ndescription: {description}\n---\n\n" + block.rstrip() + "\n"
        installed = dest / name / "SKILL.md"
        if not installed.exists():
            problems.append(f"skill {name!r} not installed — run scripts/sync_skills.py")
        elif installed.read_text() != expected:
            problems.append(f"skill {name!r} stale — run scripts/sync_skills.py")
    return problems


# ---------------------------------------------------------------------------
# 6. imports resolve (deps declared in pyproject actually installed)
# ---------------------------------------------------------------------------

def check_imports() -> list[str]:
    r = subprocess.run(
        ["uv", "run", "python", "-c", "import trawler, psycopg, psycopg_pool"],
        cwd=REPO, capture_output=True, text=True,
    )
    return [r.stderr.strip().splitlines()[-1]] if r.returncode != 0 else []


# ---------------------------------------------------------------------------
# 7. untracked source files (easy to forget `git add` by-file)
# ---------------------------------------------------------------------------

def check_untracked() -> list[str]:
    r = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True,
    )
    problems = []
    for line in r.stdout.splitlines():
        if line.startswith("??"):
            path = line[3:].strip()
            if path.startswith(("src/", "tests/", "scripts/")):
                problems.append(f"untracked source file: {path}")
    return problems


if __name__ == "__main__":
    check("tests pass", check_tests())
    check("no raise RuntimeError in library code", check_no_runtime_error())
    check("error classes exported + documented", check_errors_exported_and_documented())
    check("subpackages have __init__.py", check_init_files())
    check("installed skills match SKILL.md", check_skill_drift(), warn_only=True)
    check("dependencies import", check_imports())
    check("no untracked source files", check_untracked(), warn_only=True)

    print()
    if FAIL:
        print(f"[audit] {len(FAIL)} failure(s), {len(WARN)} warning(s)")
        sys.exit(1)
    print(f"[audit] all checks pass ({len(WARN)} warning(s))")
