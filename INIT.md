# Init Manual — first-time setup (human or agent)

For someone (or an agent) touching Trawler for the very first time: no
Postgres, no `.env`, maybe no remote GPU box yet. Once this is done, day-to-day
work uses `MANUAL.md` (proven workflows) and `SKILL.md` (per-task agent
triggers) — this file only covers getting from nothing to a working setup.

If you're an agent doing this on someone's behalf: run each numbered step,
show the output, and stop to ask before anything destructive (creating a new
Postgres role/DB is fine; touching an existing one is not — see step 1).

## 1. Local machine — Python, uv, Postgres

```sh
# uv (Python package/venv manager this repo uses everywhere — never bare python/pip)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Postgres — any install method is fine, just need a running server + a user
# that can create databases. macOS example:
brew install postgresql@16 && brew services start postgresql@16

cd Trawler
uv sync
```

Check before creating anything: `psql -l` — if a `trawler` database or role
already exists and isn't yours, stop and ask the owner rather than reusing it.

## 2. `.env` — the one file every machine needs

```sh
cp .env.example .env
```

Fill in `TRAWLER_DSN` first (everything else can wait):

```sh
trawler-init --dsn postgresql://localhost:5432          # creates the DB + schemas + seed cfg rows
```

`trawler-init` prints the exact `export TRAWLER_DSN=...` line it used — put
that in `.env`. This step is idempotent; safe to re-run against the same DB.

Then, once (per clone):

```sh
bash scripts/setup_hooks.sh        # auto-syncs SKILL.md -> ~/.claude/skills on push
python3 scripts/sync_skills.py     # load Claude Code skills globally, right now
```

Verify: `uv run pytest -q` should pass with zero setup beyond this.

Everything below is optional — only needed if you (or a coworker) want to run
generation/embedding jobs on a machine that isn't reachable from here over
plain HTTP (a GPU box behind NAT, a friend's desktop, etc.) via the **offload
loop**.

## 3. Wiring a remote GPU box

This box needs: SSH access, `uv`, a model server (llama.cpp / LM Studio /
Ollama) listening on `localhost`, and a `tmux` binary (the job queue runs
inside a tmux session so it survives SSH disconnects).

**3a. SSH — key-based, no password prompts.** The offload scripts shell out
to `ssh $REMOTE_SSH ...` non-interactively; a password prompt will just hang.

```sh
ssh-keygen -t ed25519 -C "trawler"       # skip if you already have a key
ssh-copy-id <user>@<box-host>            # installs your pubkey on the box
ssh <user>@<box-host> echo ok            # must print "ok" with NO prompt
```

If you use an `~/.ssh/config` alias instead of `user@host`, that's fine —
`TRAWLER_REMOTE_<NAME>_SSH` accepts either.

**3b. On the box itself** (once, via that same ssh session):

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv
which tmux || brew install tmux                   # or apt/dnf equivalent
# start whichever model server you're using, bound to loopback, e.g.:
#   llama-server -hf <repo>:<quant> --port 8080
#   (LM Studio: start the local server from its UI/CLI, note the port)
```

You do **not** need to clone Trawler onto the box by hand — the first
`enqueue`/`push --with-repo` rsyncs this repo over and runs `uv sync` there
for you (see `.env.example` comment on `_JOBS`).

**3c. Register the box in `.env`** (yours, gitignored — nothing to share with
a repo):

```sh
# TRAWLER_REMOTES=studio
# TRAWLER_REMOTE_STUDIO_SSH=user@10.0.0.1        # or an ssh-config alias
# TRAWLER_REMOTE_STUDIO_JOBS=~/trawler-jobs       # any writable dir on the box
# TRAWLER_REMOTE_STUDIO_URL=http://localhost:8080/v1   # the model server from 3b, must end /v1 (except Ollama)
# TRAWLER_REMOTE_STUDIO_MODELS=~/models
# TRAWLER_REMOTE_STUDIO_WORKERS=8                 # match the server's concurrency (see MANUAL.md "Concurrency: match the server")
```

Sharing that box with someone else? Steps 3a/3b above happen ONCE — the box
itself doesn't get set up twice. Only step 3c differs: each person adds their
own `.env` block for the same box, with their own distinct `NAME` (any label
works, doesn't need to be a username) and their own `_JOBS` dir. See
`.env.example`'s "Two people, SAME box" block and `MANUAL.md`'s "Two people,
one GPU box" section — a shared/colliding queue-runner name is what causes
two people's queues to step on each other.

**3d. Smoke test:**

```sh
trawler models                 # lists weights + confirms the model server answers
# then a real round-trip:
trawler bundle --prompt <p> --decoder <m> --model-type <mt> --source raw.<t> --pk id --doc-col doc
trawler enqueue <job-id>        # pushes + starts the queue runner in tmux on the box
trawler status                  # should show the job progressing
trawler import <job-id>         # brings results back once done
```

## 4. Diagnosing a failed wire-up (for an agent to run, in order)

| Symptom | Check | Likely cause |
|---|---|---|
| `ssh` hangs or asks for a password | `ssh -v <target> echo ok` | key not installed (redo 3a) — never hardcode a password anywhere |
| `enqueue` fails with "no such file" on the box | `ssh <target> 'which uv'` | uv not installed on the box, or not at `~/.local/bin/uv` |
| `trawler models` shows no running server | `ssh <target> 'curl -s localhost:8080/v1/models'` (or the box's actual port) | model server not started, or bound to a different port than `_URL` |
| `enqueue`/`status` say queue runner is DOWN right after starting it | `ssh <target> 'tmux ls'` | no `tmux` on the box, or wrong absolute path over non-interactive ssh (`/opt/homebrew/bin/tmux` on some macOS installs — see MANUAL.md "The running queue is old code...") |
| Two people's queues stepping on each other | `trawler status`/`trawler queue` prints `runner: UP (NAME COLLISION...)` automatically — no need to ssh in and guess | Two people picked the same `REMOTE_NAME` — pick a different one in your `.env` and re-enqueue, see step 3c |
| `trawler-init` / `uv sync` errors | run each command by hand, don't retry blind | usually a genuinely missing system dependency — read the actual error, don't guess a fix |

Never `ssh` directly into the box to poke at a running job outside these
checks — `trawler status` / `trawler queue` / `trawler jobs` are the
supported interface (see the `trawler-offload` skill).

## What's next

Day-to-day usage (bundling real jobs, priorities, interrupts, troubleshooting
a stalled run) is `MANUAL.md`. Per-task agent skills are in `SKILL.md`. This
file is not meant to be re-read once your `.env` works and `uv run pytest -q`
passes.
