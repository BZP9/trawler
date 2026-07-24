#!/usr/bin/env python3
"""Split SKILL.md into individual ~/.claude/skills/<name>/SKILL.md files.

Each '## skill: <name>' block becomes its own folder (matching the format
Claude Code expects). Non-skill sections (e.g. Schema quick-ref) are ignored.

Run directly or via the post-push git hook (scripts/setup_hooks.sh).
"""
import pathlib
import re
import sys


def sync(skill_file: pathlib.Path, dest: pathlib.Path) -> int:
    src = skill_file.read_text()
    dest.mkdir(parents=True, exist_ok=True)

    # split on any H2 boundary, process only skill blocks
    blocks = re.split(r"^(?=## )", src, flags=re.MULTILINE)
    synced = 0
    for block in blocks:
        m = re.match(r"^## skill: (.+)", block)
        if not m:
            continue
        name = m.group(1).strip()

        trigger = re.search(r"^\*\*Trigger\*\*: (.+)", block, re.MULTILINE)
        description = trigger.group(1).strip() if trigger else name

        frontmatter = f"---\nname: {name}\ndescription: {description}\n---\n\n"

        skill_dir = dest / name
        skill_dir.mkdir(exist_ok=True)
        out = skill_dir / "SKILL.md"
        out.write_text(frontmatter + block.rstrip() + "\n")
        print(f"  synced → {out}")
        synced += 1

    return synced


if __name__ == "__main__":
    repo = pathlib.Path(__file__).parent.parent
    skill_file = repo / "SKILL.md"
    dest = pathlib.Path.home() / ".claude" / "skills"

    if not skill_file.exists():
        print(f"[sync_skills] SKILL.md not found at {skill_file}", file=sys.stderr)
        sys.exit(1)

    n = sync(skill_file, dest)
    print(f"[sync_skills] {n} skills synced to {dest}")
