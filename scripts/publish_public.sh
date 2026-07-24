#!/usr/bin/env bash
# Publish a clean, single-commit snapshot of `main` to the public mirror repo.
#
# Only ever touches the LOCAL `public` branch in this (private) repo — never
# `main`, never any other branch, never pushes `public` to this repo's own
# origin. `public` is disposable scratch: reset (deleted + recreated as an
# orphan) on every run, so it never accumulates history either.
#
# Why a snapshot, not `git push`ing real history: the private repo's history
# may contain things (old author info, internal notes, wrong turns) that
# were never meant to be public. A fresh single-commit tree side-steps
# needing to audit 100+ commits before every publish.
#
# Usage: scripts/publish_public.sh [public-repo-url]
#   Default target: https://github.com/BZP9/trawler.git
#   Override once:  scripts/publish_public.sh git@github.com:you/other.git
#   Override always: export TRAWLER_PUBLIC_REMOTE=...
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PUBLIC_REMOTE="${1:-${TRAWLER_PUBLIC_REMOTE:-https://github.com/BZP9/trawler.git}}"
ORIG_BRANCH="$(git branch --show-current)"

[ -z "$(git status --porcelain)" ] || {
  echo "working tree has uncommitted changes — commit or stash before publishing" >&2
  exit 1
}

echo "[publish] snapshotting main -> local 'public' branch (reset each run)"
git checkout -q main
git branch -D public >/dev/null 2>&1 || true
git checkout -q --orphan public
git commit -q -m "Trawler: row-by-row inference control plane

Batch LLM generation and embedding pipelines with structured hooks,
categorized errors, Postgres-backed run logs, and a portable offload
loop for compute on machines without direct DB access."

echo "[publish] public -> $PUBLIC_REMOTE (main, force)"
git push --force "$PUBLIC_REMOTE" public:main

echo "[publish] back to $ORIG_BRANCH; 'public' left in place for inspection, never pushed to origin"
git checkout -q "$ORIG_BRANCH"
