#!/usr/bin/env bash
# One-time setup: installs git post-push hook that auto-syncs SKILL.md
# on every push. Run once per clone.
#
# Usage: bash scripts/setup_hooks.sh

set -euo pipefail

REPO="$(git rev-parse --show-toplevel)"
HOOK="$REPO/.git/hooks/post-push"

cat > "$HOOK" << 'EOF'
#!/usr/bin/env bash
python3 "$(git rev-parse --show-toplevel)/scripts/sync_skills.py"
EOF

chmod +x "$HOOK"
echo "[setup_hooks] post-push hook installed at $HOOK"
echo "[setup_hooks] SKILL.md will auto-sync to ~/.claude/skills/ on every push"
