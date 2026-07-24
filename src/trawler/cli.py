"""trawler — CLI entry point.

Subcommands (the whole offload loop, one front door)
----------------------------------------------------
bundle      Export an offline job directory from Postgres.
push        Rsync a job dir to a named remote (wraps scripts/offload.sh).
enqueue     Push + add to the remote's priority queue (-p N; auto-runs, auto-retries).
run         Start a job directly on a remote (tmux watchdog; prefer enqueue).
interrupt   Interrupt a running/queued job; partial kept, resumable.
status      Job progress + queue health on a remote.
queue       Queue state on a remote: active / waiting / cooling / done / interrupted.
jobs        Control-plane view: offload jobs registered in gen._gen_log.
job-config  Print a job's bundle recipe + a ready-to-copy re-bundle command.
rebundle    Re-ship a prompt: resolve its latest recipe, preview or --go bundle it.
clean       Delete imported job dirs to reclaim disk (dry-run unless --yes).
pull        Rsync job.sqlite back from a remote.
import      Pull (if given a job id) + merge results into Postgres.
run-bundle  Execute a bundle locally without Postgres (used ON the remote).
models      List model files + running servers on a remote.
fetch-model Download a GGUF from HF onto a remote (resumable).

Remote verbs delegate to scripts/offload.sh; `-r <name>` picks the remote
(default: first in TRAWLER_REMOTES). Add new subcommands in
``_register_subcommands``.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


def _repo_root() -> str:
    return os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))


_ALIAS_MARKER = "# trawler: cli alias — auto-added by trawler, do not edit by hand"
_RC_FILES = {"zsh": "~/.zshrc", "bash": "~/.bashrc"}


def _alias_block() -> str:
    root = _repo_root()
    return f"""
{_ALIAS_MARKER}
# Added automatically the first time `trawler` ran on this device, so the
# bare `trawler` command works in any new terminal without `uv run` in
# front of it. Safe to remove if you install trawler another way (e.g.
# `uv tool install`) — trawler will not re-add it once this exact block is
# gone from EVERY line, but will re-add it if only part is deleted.
alias trawler='uv run --project {root} trawler'
"""


def _ensure_shell_alias() -> None:
    """Register a `trawler` alias in the user's shell rc, once per device, idempotently.

    Runs on every invocation, including many concurrent ones during a batch
    job — the flock makes the check-then-append atomic so parallel `trawler`
    processes can't race each other into writing the block twice.
    """
    import fcntl

    shell = os.environ.get("SHELL", "")
    rc_path = _RC_FILES.get("zsh") if "zsh" in shell else _RC_FILES.get("bash") if "bash" in shell else None
    if rc_path is None:
        return  # unrecognized shell — don't guess, don't touch anything
    rc = os.path.expanduser(rc_path)
    try:
        with open(rc, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            existing = f.read()
            block = _alias_block()
            if all(line in existing for line in block.strip().splitlines()):
                return  # every line of the block already present — nothing to do
            f.write(block)
            print(f"registered `trawler` alias in {rc} — restart terminal or `source {rc}` to use it")
    except OSError as e:
        print(f"warning: alias registration failed (non-fatal): {e}")


def _offload(verb: str, argv: list[str]) -> int:
    script = os.path.join(_repo_root(), "scripts", "offload.sh")
    return subprocess.call([script, verb, *argv])


# Extra names for a passthrough verb (share the canonical verb's offload.sh case).
_VERB_ALIASES: dict[str, list[str]] = {}

# Verbs that must refuse to touch a job that's RUNNING on the remote unless
# --force is passed — item 3 of output/psql-status-sync/STATE.md. Prevents
# the class of accident that caused a duplicate queue entry: re-enqueueing
# (or re-pushing job.sqlite over) a job that never actually stopped.
_GUARDED_VERBS = {"enqueue", "push"}


def _remote_job_running(job_id: str, remote: str) -> bool:
    """True if `job_id` is CURRENTLY RUNNING on `remote` — reuses the exact
    check `pull` already does (scripts/offload.sh:83-93): a
    queue/active/<job-id>.task file, OR a live run-bundle process matched on
    the job id (never on $REMOTE_JOBS — see MANUAL.md's tilde-expansion
    trap). Delegates to remote_env.sh's resolve_remote for remote lookup so
    there's exactly one place that knows the .env format. Returns False
    (fail open, matching `pull`'s own best-effort posture) if the remote
    can't be reached at all — the check is a safety NET, not a hard gate;
    callers needing certainty should read the printed warning.
    """
    remote_env = os.path.join(_repo_root(), "scripts", "remote_env.sh")
    probe = (
        f'source "{remote_env}" && resolve_remote "{remote}" >/dev/null 2>&1 && '
        f'ssh "$REMOTE_SSH" "'
        f'[ -f $REMOTE_JOBS/queue/active/{job_id}.task ] && echo yes; '
        f'pgrep -f \\"run-bundle.*{job_id}\\" >/dev/null 2>&1 && echo yes; '
        f'true"'
    )
    try:
        out = subprocess.run(
            ["bash", "-c", probe],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return "yes" in out.stdout.splitlines()


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_bundle(args: argparse.Namespace) -> None:
    from trawler.offload.bundle import bundle

    pk: str | list[str] = args.pk if len(args.pk) > 1 else args.pk[0]
    result = bundle(
        prompt_name=args.prompt,
        decoder_name=args.decoder,
        model_type_name=args.model_type,
        source_table=args.source,
        pk=pk,
        doc_cols=args.doc_col,
        limit=args.limit,
        # Anchor at the trawler repo, not cwd: the shell alias runs from any
        # directory, and push/enqueue/import resolve <repo>/output/jobs.
        out=args.out or os.path.join(_repo_root(), "output", "jobs"),
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print("DRY RUN — no job dir written, no gen._gen_log row, no claims:")
        print(f"  total source rows:   {result['total']}")
        print(f"  already claimed:     {result['claimed']}")
        print(f"  pending (would ship): {result['pending']}")
        print(f"  config: {result['config']}")
    else:
        print(f"Bundle written to: {result}")


def _cmd_run_bundle(args: argparse.Namespace) -> None:
    from trawler.offload.runner import run_bundle

    params: dict = {}
    if args.temperature is not None:
        params["temperature"] = args.temperature
    if args.max_tokens is not None:
        params["max_tokens"] = args.max_tokens
    if args.timeout is not None:
        params["timeout"] = args.timeout
    summary = run_bundle(
        args.job_dir,
        concurrency=args.concurrency,
        retries=args.retries,
        limit=args.limit,
        early_stop=args.early_stop or None,   # 0 → disabled
        params=params,
    )
    print(f"run-bundle summary: {summary}")


def _cmd_import(args: argparse.Namespace) -> None:
    from trawler.offload.importer import import_bundle

    target = args.job
    is_running = False
    if os.path.isdir(target):                       # a job dir path: import as-is
        job_dir = target
    else:                                           # a job id: pull from remote first
        job_dir = os.path.join(_repo_root(), "output", "jobs", target)
        remote = ["-r", args.remote] if args.remote else []
        rc = _offload("pull", [*remote, target])
        if rc != 0:
            sys.exit(rc)
        # Same liveness check `pull` itself just did (offload.sh:83-93) —
        # decides whether an incomplete-coverage import lands as 'running'
        # (still live) or 'partial' (confirmed stopped). Best-effort: an
        # unreachable remote reads as not-running (fail open), matching pull.
        is_running = _remote_job_running(target, args.remote)
    res = import_bundle(job_dir, force=args.force, is_running=is_running)
    print(f"Imported as run {res['run_id']}: "
          f"{res['ok']} ok, {res['failed']} failed → {res['table']}")
    if res["status"] == "running":
        print(f"  job status = running ({res['ok']}/{res['total']} rows imported so far) — "
              f"still active on the remote; re-import later for more rows")
    elif res["status"] == "partial":
        print(f"  job status = partial ({res['ok']}/{res['total']} rows) — "
              f"main task NOT complete, not running; re-enqueue to finish, re-import to add more")
    else:
        print(f"  job status = complete ({res['total']} rows)")


def _cmd_status(args: argparse.Namespace) -> None:
    # No job id and no --all: one-stop overview (remote + queue + local
    # pending jobs), so an operator never has to know status/queue/jobs are
    # three different verbs. `status <JOB_ID>` and `status --all` keep their
    # existing focused, remote-only behavior unchanged.
    if not args.job and not args.all:
        sys.exit(_cmd_status_overview(args))

    script = os.path.join(_repo_root(), "scripts", "remote_status.sh")
    cmd = [script]
    if args.remote:
        cmd += ["-r", args.remote]
    if args.all:
        cmd += ["--all"]
    elif args.job:
        cmd += [args.job]
    sys.exit(subprocess.call(cmd))


def _sync_all_remote_jobs(remote_arg: str | None) -> None:
    """Refresh gen._gen_log for every non-terminal local job, not just the
    newest one on the remote. remote_status.sh only syncs the single job it's
    given (or the newest job dir when called with none) — without this sweep,
    `trawler status`'s overview silently left older still-live/parked jobs
    (e.g. an interrupted one buried behind a newer active job) showing stale
    status until someone happened to run `status <that-job-id>` directly.
    Best-effort per job: one job's ssh/sqlite failure must not block others'.
    """
    import psycopg
    from psycopg.rows import dict_row

    from trawler.dsn import resolve_dsn

    try:
        with psycopg.connect(resolve_dsn(None), row_factory=dict_row) as conn:
            rows = conn.execute(
                "SELECT name, config->>'remote' AS remote FROM gen._gen_log "
                "WHERE config->>'offload' = 'true' "
                "AND status IN ('exported', 'running', 'partial', 'interrupted') "
                "AND status != 'cleaned'"
            ).fetchall()
    except Exception:
        return  # local pending-jobs section will surface the same failure

    script = os.path.join(_repo_root(), "scripts", "remote_status.sh")
    for r in rows:
        remote = remote_arg or r["remote"]
        if not remote:
            continue
        cmd = [script, "-r", remote, r["name"]]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _cmd_status_overview(args: argparse.Namespace) -> int:
    """`trawler status` (no args): remote progress + queue health + local
    pending jobs in one output, three clearly headed sections. Each section
    is independent — a failure in one (no remote configured, DB unreachable)
    prints a note in that section and the others still run; never raises.
    """
    rc = 0

    _sync_all_remote_jobs(args.remote)

    print("== remote (studio) ==")
    remote_script = os.path.join(_repo_root(), "scripts", "remote_status.sh")
    remote_cmd = [remote_script]
    if args.remote:
        remote_cmd += ["-r", args.remote]
    r = subprocess.call(remote_cmd)
    if r != 0:
        rc = r
    print()

    print("== queue ==")
    r = _offload("queue", ["-r", args.remote] if args.remote else [])
    if r != 0:
        rc = r
    print()

    print("== local pending jobs ==")
    _print_local_pending_jobs(all_jobs=False)

    return rc


def _print_local_pending_jobs(all_jobs: bool) -> None:
    """Shared by `trawler jobs` and the `status` overview's local section.
    Never raises — an unreachable DB prints one line and returns so `status`
    stays useful DB-less.

    `all_jobs=False` (default) also excludes jobs `trawler clean` has stamped
    `config->>'stage' = 'cleaned'` — those are ghost rows for job dirs that
    no longer exist locally. `all_jobs=True` (`trawler jobs --all`) still
    shows them, since it's the full-history view.
    """
    import psycopg
    from psycopg.rows import dict_row

    from trawler.dsn import resolve_dsn

    where = "config->>'offload' = 'true'"
    if not all_jobs:
        # pending = not yet done: never run (exported), actively running,
        # partially imported and confirmed stopped (partial), or parked
        # (interrupted). Excludes only 'complete' (and ghost/cleaned rows).
        where += " AND status IN ('exported', 'running', 'partial', 'interrupted')"
        # Exclude ghost rows for jobs `clean` already deleted the dir for.
        # status='cleaned' covers rows cleaned after this change; the stage
        # check is kept for rows cleaned before it (stage-only stamp, old DBs).
        where += " AND status != 'cleaned'"
        where += " AND COALESCE(config->>'stage', '') != 'cleaned'"
    try:
        with psycopg.connect(resolve_dsn(None), row_factory=dict_row) as conn:
            rows = conn.execute(
                f"SELECT name, model, status, n_rows, n_done, n_failed, "
                f"       config->>'remote' AS remote, config->>'stage' AS stage, "
                f"       started_at "
                f"FROM gen._gen_log WHERE {where} ORDER BY started_at DESC LIMIT 50"
            ).fetchall()
    except Exception as e:
        print(f"local DB unreachable: {e}")
        return
    if not rows:
        print("no pending offload jobs" if not all_jobs else "no offload jobs")
        return
    for r in rows:
        remote = r["remote"] or "-"
        print(f"{r['name']}  status={r['status']}  stage={r['stage'] or '-'}  "
              f"remote={remote}  rows={r['n_rows']}  "
              f"done={r['n_done'] or 0}/{r['n_failed'] or 0}f  "
              f"since={r['started_at']:%Y-%m-%d %H:%M}")


def _cmd_offload_passthrough(args: argparse.Namespace) -> None:
    verb_args = list(args.args)
    forced = "--force" in verb_args
    if forced:
        # Consumed by the guard below, never forwarded — offload.sh (bash
        # side) has no --force flag of its own.
        verb_args = [a for a in verb_args if a != "--force"]
    if args.verb in _GUARDED_VERBS and not forced:
        # job id is verb_args[0] (REMAINDER positional — see parser wiring);
        # everything after it (-p N, run-bundle args...) is irrelevant here.
        job_id = verb_args[0] if verb_args and not verb_args[0].startswith("-") else None
        if job_id and _remote_job_running(job_id, args.remote):
            print(
                f"refusing: {job_id} is RUNNING on the remote "
                f"(queue/active task or live run-bundle process found). "
                f"Re-{args.verb}ing a running job risks a duplicate queue "
                f"entry or overwriting its live job.sqlite. "
                f"Pass --force if you're sure.",
                file=sys.stderr,
            )
            sys.exit(1)
    extra = ["-r", args.remote] if args.remote else []
    sys.exit(_offload(args.verb, [*extra, *verb_args]))


def _cmd_jobs(args: argparse.Namespace) -> None:
    _print_local_pending_jobs(all_jobs=args.all)


def _rebundle_line(prompt, decoder, model_type, source, pk, doc_cols) -> str:
    """Ready-to-copy re-bundle command — or an explicit note when the recipe
    is incomplete (legacy _gen_log rows predate pk/doc_cols/prompt in the
    config jsonb). Never emit a broken command a copy-pasting agent would run.
    """
    missing = [name for name, v in [
        ("prompt", prompt), ("decoder", decoder), ("model-type", model_type),
        ("source", source), ("pk", pk), ("doc-col", doc_cols),
    ] if not v]
    if missing:
        return (
            f"(no re-bundle line: recipe incomplete, missing {', '.join(missing)} "
            "— legacy log row; recover from the job dir's job.toml on the "
            "remote, or re-bundle by hand per MANUAL.md)"
        )
    return (
        f"trawler bundle --prompt {prompt} --decoder {decoder} "
        f"--model-type {model_type} --source {source} "
        f"--pk {' '.join(pk)} --doc-col {' '.join(doc_cols)}"
    )


def _resolve_job_recipe(job_id: str) -> dict:
    """Resolve a job's full bundle recipe — shared by `job-config` and `rebundle`.

    Resolution order:
      (a) local output/jobs/<job-id>/job.toml, if present — authoritative
      (b) else gen._gen_log row where config->>'job_id' = <job-id>
      (c) else raises LookupError naming both places checked

    Returns a dict with keys: source ("local"|"gen_log"), local_path, run_id,
    prompt, decoder, model_type, source_table, pk, doc_cols, and (gen_log
    source only) status/stage/remote/n_rows/n_done/n_failed.
    """
    import tomllib

    local_path = os.path.join(_repo_root(), "output", "jobs", job_id, "job.toml")

    if os.path.exists(local_path):
        with open(local_path, "rb") as f:
            data = tomllib.load(f)
        job = data.get("job", {})
        prompt = data.get("prompt", {})
        decoder = data.get("decoder", {})
        model_type = data.get("model_type", {})
        source = data.get("source", {})
        return {
            "source": "local",
            "local_path": local_path,
            "run_id": job.get("run_id"),
            "prompt": prompt.get("name"),
            "decoder": decoder.get("name"),
            "model_type": model_type.get("name"),
            "source_table": source.get("table"),
            "pk": source.get("pk") or [],
            "doc_cols": source.get("doc_cols") or [],
        }

    # Fall back to gen._gen_log.
    import psycopg
    from psycopg.rows import dict_row

    from trawler.dsn import resolve_dsn

    try:
        with psycopg.connect(resolve_dsn(None), row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT run_id, name, model, status, n_rows, n_done, n_failed, "
                "       source_table, system_prompt_content, "
                "       config->>'stage' AS stage, config->>'remote' AS remote, "
                "       config->>'model_type' AS model_type, "
                "       config->'pk' AS pk, config->'doc_cols' AS doc_cols, "
                "       config->>'prompt' AS prompt "
                "FROM gen._gen_log WHERE config->>'job_id' = %s "
                "ORDER BY started_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
    except Exception as exc:
        raise LookupError(
            f"could not resolve recipe for {job_id!r} — "
            f"no local job.toml at {local_path}, and gen._gen_log lookup "
            f"failed: {exc}"
        ) from exc

    if row is None:
        raise LookupError(
            f"could not resolve recipe for {job_id!r} — "
            f"checked local job.toml at {local_path} (not found), and "
            f"gen._gen_log for a row with config->>'job_id' = {job_id!r} "
            "(no match)"
        )

    return {
        "source": "gen_log",
        "local_path": local_path,
        "run_id": row["run_id"],
        "status": row["status"],
        "stage": row["stage"],
        "remote": row["remote"],
        "prompt": row["prompt"],
        "decoder": row["model"],
        "model_type": row["model_type"],
        "source_table": row["source_table"],
        "pk": row["pk"] or [],
        "doc_cols": row["doc_cols"] or [],
        "n_rows": row["n_rows"],
        "n_done": row["n_done"],
        "n_failed": row["n_failed"],
    }


def _cmd_job_config(args: argparse.Namespace) -> None:
    """Print the full bundle recipe for a job id + a ready-to-copy re-bundle line."""
    job_id = args.job

    try:
        recipe = _resolve_job_recipe(job_id)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    pk = recipe["pk"]
    doc_cols = recipe["doc_cols"]

    if recipe["source"] == "local":
        print(f"job-config for {job_id} (source: local {recipe['local_path']})")
        print(f"  run_id:      {recipe['run_id']}")
        print(f"  prompt:      {recipe['prompt']}")
        print(f"  decoder:     {recipe['decoder']}")
        print(f"  model_type:  {recipe['model_type']}")
        print(f"  source:      {recipe['source_table']}")
        print(f"  pk:          {pk}")
        print(f"  doc_cols:    {doc_cols}")
    else:
        print(f"job-config for {job_id} (source: gen._gen_log run_id={recipe['run_id']})")
        print(f"  run_id:      {recipe['run_id']}")
        print(f"  status:      {recipe['status']}")
        print(f"  stage:       {recipe['stage'] or '-'}")
        print(f"  remote:      {recipe['remote'] or '-'}")
        print(f"  prompt:      {recipe['prompt']}")
        print(f"  decoder:     {recipe['decoder']}")
        print(f"  model_type:  {recipe['model_type']}")
        print(f"  source:      {recipe['source_table']}")
        print(f"  pk:          {pk}")
        print(f"  doc_cols:    {doc_cols}")
        print(f"  rows:        n_rows={recipe['n_rows']} n_done={recipe['n_done']} "
              f"n_failed={recipe['n_failed']}")

    print("\n" + _rebundle_line(
        recipe["prompt"], recipe["decoder"], recipe["model_type"],
        recipe["source_table"], pk, doc_cols))


def _find_latest_job_id_for_prompt(prompt: str) -> str | None:
    """Find the most recent job id for a prompt, comparing a DB match
    (config->>'prompt' = <prompt>, fallback: name LIKE '<prompt>-%') against
    local output/jobs/<prompt>-*/job.toml dirs — whichever has the newer
    job-id timestamp suffix wins (local job.toml is authoritative when it's
    at least as new, since it carries the full recipe without a DB round-trip).
    """
    import re as _re

    candidates: list[str] = []

    jobs_root = os.path.join(_repo_root(), "output", "jobs")
    if os.path.isdir(jobs_root):
        prefix = f"{prompt}-"
        for name in os.listdir(jobs_root):
            if name.startswith(prefix) and os.path.isdir(os.path.join(jobs_root, name)):
                candidates.append(name)

    db_job_id = None
    try:
        import psycopg
        from psycopg.rows import dict_row

        from trawler.dsn import resolve_dsn

        with psycopg.connect(resolve_dsn(None), row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT config->>'job_id' AS job_id FROM gen._gen_log "
                "WHERE config->>'prompt' = %s "
                "AND status != 'cleaned' "
                "AND COALESCE(config->>'stage', '') != 'cleaned' "
                "ORDER BY started_at DESC LIMIT 1",
                (prompt,),
            ).fetchone()
            if row is None or not row["job_id"]:
                row = conn.execute(
                    "SELECT name AS job_id FROM gen._gen_log "
                    "WHERE name LIKE %s "
                    "AND status != 'cleaned' "
                    "AND COALESCE(config->>'stage', '') != 'cleaned' "
                    "ORDER BY started_at DESC LIMIT 1",
                    (f"{prompt}-%",),
                ).fetchone()
            if row and row["job_id"]:
                db_job_id = row["job_id"]
    except Exception:
        db_job_id = None  # DB unreachable — fall back to local-only

    if db_job_id:
        candidates.append(db_job_id)

    if not candidates:
        return None

    _TS_RE = _re.compile(r"-(\d{8}T\d{6}Z)$")

    def _ts_key(job_id: str) -> str:
        m = _TS_RE.search(job_id)
        return m.group(1) if m else ""

    candidates = sorted(set(candidates), key=_ts_key)
    return candidates[-1]


def _cmd_rebundle(args: argparse.Namespace) -> None:
    """Find a prompt's most recent job, resolve its recipe exactly like
    job-config, and either print a dry-run preview (default) or actually
    re-bundle it (--go). Never guesses a missing recipe field.
    """
    from trawler.offload.bundle import bundle

    prompt = args.prompt

    job_id = _find_latest_job_id_for_prompt(prompt)
    if job_id is None:
        print(
            f"error: no job found for prompt {prompt!r} — checked "
            f"output/jobs/{prompt}-*/job.toml and gen._gen_log "
            f"(config->>'prompt' = {prompt!r}, and name LIKE '{prompt}-%')",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        recipe = _resolve_job_recipe(job_id)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    missing = [name for name, v in [
        ("prompt", recipe["prompt"]), ("decoder", recipe["decoder"]),
        ("model-type", recipe["model_type"]), ("source", recipe["source_table"]),
        ("pk", recipe["pk"]), ("doc-col", recipe["doc_cols"]),
    ] if not v]
    if missing:
        print(
            f"error: recipe for {job_id!r} is incomplete, missing "
            f"{', '.join(missing)} — legacy gen._gen_log row and no local "
            f"job.toml to fall back on. Recover it by hand per MANUAL.md.",
            file=sys.stderr,
        )
        sys.exit(1)

    pk = recipe["pk"]
    doc_cols = recipe["doc_cols"]
    raw_line = (
        f"trawler bundle --prompt {recipe['prompt']} --decoder {recipe['decoder']} "
        f"--model-type {recipe['model_type']} --source {recipe['source_table']} "
        f"--pk {' '.join(pk)} --doc-col {' '.join(doc_cols)}"
    )

    print(f"rebundle: resolved {prompt!r} to job {job_id!r} (source: {recipe['source']})")
    print(f"  run_id:      {recipe['run_id']}")
    print(f"  decoder:     {recipe['decoder']}")
    print(f"  model_type:  {recipe['model_type']}")
    print(f"  source:      {recipe['source_table']}")
    print(f"  pk:          {pk}")
    print(f"  doc_cols:    {doc_cols}")

    if not args.go:
        result = bundle(
            prompt_name=recipe["prompt"],
            decoder_name=recipe["decoder"],
            model_type_name=recipe["model_type"],
            source_table=recipe["source_table"],
            pk=pk if len(pk) > 1 else pk[0],
            doc_cols=doc_cols,
            limit=args.limit,
            dry_run=True,
        )
        if result["pending"] == 0:
            print(
                "\nnothing to bundle — if new source rows should exist, "
                "refresh the staging table via the task's stage_*.py"
            )
        else:
            print("\nDRY RUN — no job dir written, no gen._gen_log row, no claims:")
            print(f"  total source rows:    {result['total']}")
            print(f"  already claimed:      {result['claimed']}")
            print(f"  pending (would ship): {result['pending']}")
        limit_flag = f" --limit {args.limit}" if args.limit else ""
        print(f"\nto actually bundle: trawler rebundle {prompt} --go{limit_flag}")
        print(f"raw bundle command (fallback/reference): {raw_line}")
        return

    result = bundle(
        prompt_name=recipe["prompt"],
        decoder_name=recipe["decoder"],
        model_type_name=recipe["model_type"],
        source_table=recipe["source_table"],
        pk=pk if len(pk) > 1 else pk[0],
        doc_cols=doc_cols,
        limit=args.limit,
    )
    print(f"\nBundle written to: {result}")
    print(f"enqueue with: trawler enqueue {os.path.basename(result)}")


def _dir_size_h(path: str) -> str:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    for unit in ("B", "K", "M", "G"):
        if total < 1024 or unit == "G":
            return f"{total:.0f}{unit}"
        total /= 1024
    return f"{total:.0f}G"


def _load_job_run_id(job_dir: str) -> str | None:
    """Return the run_id recorded in job.toml, or None if absent/unreadable."""
    try:
        import tomllib
        toml_path = os.path.join(job_dir, "job.toml")
        if not os.path.exists(toml_path):
            return None
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        return data.get("job", {}).get("run_id")
    except Exception:
        return None


def _release_claims(conn, prompt_name: str, run_id: str) -> int:
    """DELETE pending claim rows for run_id from gen.<prompt>. Returns count."""
    import re as _re
    _SAFE = _re.compile(r"^[A-Za-z0-9_-]{1,63}$")
    if not _SAFE.match(prompt_name):
        return 0
    row = conn.execute(
        f'DELETE FROM gen."{prompt_name}" WHERE run_id=%s AND status=%s',
        (run_id, "pending"),
    ).fetchone()
    # psycopg DELETE doesn't return rows; use rowcount via statusmessage
    # Alternative: SELECT COUNT first.  We use the cursor's rowcount trick.
    return 0  # actual count logged via COUNT below; this path is unused


def _count_pending_claims(conn, prompt_name: str, run_id: str) -> int:
    """Return number of pending claims for run_id in gen.<prompt>."""
    import re as _re
    _SAFE = _re.compile(r"^[A-Za-z0-9_-]{1,63}$")
    if not _SAFE.match(prompt_name):
        return 0
    try:
        row = conn.execute(
            f'SELECT COUNT(*) AS n FROM gen."{prompt_name}" '
            "WHERE run_id=%s AND status=%s",
            (run_id, "pending"),
        ).fetchone()
        return row["n"] if row else 0
    except Exception:
        return 0


def _cmd_clean(args: argparse.Namespace) -> None:
    """Delete local (and optionally remote) job dirs to reclaim disk.

    Job dirs persist after import by design (resume + audit). This removes the
    ones that are safe to drop — imported (status='complete') jobs — so
    output/jobs/ doesn't stack up. Dry-run unless --yes.

    Claim release: for each cleaned job, pending placeholder rows written by
    bundle (status='pending', scoped to the job's run_id) are deleted from the
    gen table. Only 'pending' rows are touched — ok/failed rows are never
    deleted. Idempotent. If Postgres is unreachable, dirs are still deleted and
    a warning is printed (claim release is best-effort, not a hard dep).
    """
    import psycopg
    from psycopg.rows import dict_row

    from trawler.dsn import resolve_dsn

    jobs_root = os.path.join(_repo_root(), "output", "jobs")

    # Resolve the target job ids + their remote (for --remote deletion).
    targets: list[tuple[str, str | None]] = []   # (job_id, remote)
    try:
        if args.imported:
            with psycopg.connect(resolve_dsn(None), row_factory=dict_row) as conn:
                rows = conn.execute(
                    "SELECT name, config->>'remote' AS remote FROM gen._gen_log "
                    "WHERE config->>'offload' = 'true' AND status = 'complete' "
                    "ORDER BY started_at"
                ).fetchall()
            targets = [(r["name"], r["remote"]) for r in rows]
        elif args.job:
            with psycopg.connect(resolve_dsn(None), row_factory=dict_row) as conn:
                row = conn.execute(
                    "SELECT status, config->>'remote' AS remote FROM gen._gen_log "
                    "WHERE name = %s AND config->>'offload' = 'true' "
                    "ORDER BY started_at DESC LIMIT 1",
                    (args.job,),
                ).fetchone()
            status = row["status"] if row else None
            if status != "complete" and not args.force:
                print(f"refusing: {args.job} is status={status or 'unknown'} "
                      f"(not imported). Deleting its local dir loses the bundle "
                      f"needed to pull/import. Re-run with --force to override.")
                sys.exit(1)
            targets = [(args.job, row["remote"] if row else None)]
        else:
            print("specify a <job-id> or --imported")
            sys.exit(2)
    except Exception as exc:
        if not args.job and not args.imported:
            raise
        print(f"warning: Postgres unreachable ({exc}); "
              "proceeding with dir deletion only (no claim release)")
        targets = [(args.job, None)] if args.job else []

    # Keep only jobs that actually have something to delete.
    present = [(j, rm) for (j, rm) in targets
               if os.path.isdir(os.path.join(jobs_root, j))
               or (args.remote and rm)]
    if not present:
        print("nothing to clean (no local job dirs"
              + (" or remote copies" if args.remote else "") + ")")
        return

    verb = "deleting" if args.yes else "would delete (dry-run; pass --yes)"
    print(f"{verb}:")
    for job_id, remote in present:
        local = os.path.join(jobs_root, job_id)
        size = f" ({_dir_size_h(local)})" if os.path.isdir(local) else ""
        loc = local if os.path.isdir(local) else "(no local dir)"
        tail = f"  + remote {remote}:{job_id}" if (args.remote and remote) else ""

        # Look up claim info from job.toml.
        run_id = _load_job_run_id(local) if os.path.isdir(local) else None
        # prompt_name is the prefix of job_id (everything before the timestamp).
        # job_id format: <prompt_name>-<timestamp>
        import re as _re
        _m = _re.match(r"^(.+)-\d{8}T\d{6}Z$", job_id)
        prompt_name = _m.group(1) if _m else None

        n_claims = 0
        if run_id and prompt_name:
            try:
                import psycopg as _pg
                from psycopg.rows import dict_row as _dr
                from trawler.dsn import resolve_dsn as _rdsn
                with _pg.connect(_rdsn(None), row_factory=_dr) as _conn:
                    n_claims = _count_pending_claims(_conn, prompt_name, run_id)
            except Exception:
                n_claims = 0  # DB unreachable — best effort

        claim_note = f"  [{n_claims} pending claim(s) would be released]" if n_claims else ""
        print(f"  {job_id}{size}  {loc}{tail}{claim_note}")
        if not args.yes:
            continue

        # Release pending claims first (best-effort; dir deletion proceeds either way).
        if run_id and prompt_name:
            try:
                import psycopg as _pg
                from psycopg.rows import dict_row as _dr
                from trawler.dsn import resolve_dsn as _rdsn
                with _pg.connect(_rdsn(None), row_factory=_dr) as _conn:
                    _conn.execute(
                        f'DELETE FROM gen."{prompt_name}" '
                        "WHERE run_id=%s AND status='pending'",
                        (run_id,),
                    )
                    _conn.commit()
                if n_claims:
                    print(f"    released {n_claims} pending claim(s) for {job_id}")
            except Exception as exc:
                print(f"  ! claim release failed for {job_id}: {exc} "
                      "(dirs still deleted; re-run clean after DB is available)")

        # Stamp the gen._gen_log row status='cleaned' (+ config->>'stage' for
        # back-compat with pre-2026-07-14 filters) so `trawler jobs`/`status`
        # stop showing a ghost row for a dir that no longer exists, and psql
        # itself (raw SELECT status) reflects it too — not just the jsonb
        # config blob. Best-effort: DB unreachable → warn, dirs still deleted.
        try:
            import psycopg as _pg
            from psycopg.rows import dict_row as _dr
            from trawler.dsn import resolve_dsn as _rdsn
            with _pg.connect(_rdsn(None), row_factory=_dr) as _conn:
                _conn.execute(
                    "UPDATE gen._gen_log SET status = 'cleaned', config = config || "
                    "'{\"stage\": \"cleaned\"}'::jsonb "
                    "WHERE config->>'job_id' = %s",
                    (job_id,),
                )
                _conn.commit()
        except Exception as exc:
            print(f"  ! status=cleaned stamp failed for {job_id}: {exc} "
                  "(dirs still deleted; job may still show in `trawler jobs`)")

        if os.path.isdir(local):
            import shutil
            shutil.rmtree(local)
        if args.remote and remote:
            rc = _offload("clean", ["-r", remote, job_id])
            if rc != 0:
                print(f"  ! remote clean failed for {job_id} (rc={rc})")
    if not args.yes:
        print(f"\n{len(present)} job(s) — nothing deleted. Re-run with --yes.")


# ---------------------------------------------------------------------------
# Parser wiring
# ---------------------------------------------------------------------------

def _register_subcommands(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    # ---- bundle ----
    bp = sub.add_parser(
        "bundle",
        help="Export a self-contained offline generation job from Postgres.",
        description=(
            "Snapshot cfg tables + pending source rows into a portable job directory "
            "(job.toml, rows.jsonl, job.sqlite) that can run on a remote machine "
            "without a Postgres connection."
        ),
    )
    bp.add_argument("--prompt", required=True, metavar="NAME",
                    help="cfg.system_prompt name")
    bp.add_argument("--decoder", required=True, metavar="NAME",
                    help="cfg.decoder name")
    bp.add_argument("--model-type", dest="model_type", required=True, metavar="NAME",
                    help="cfg.model_type name")
    bp.add_argument("--source", required=True, metavar="TABLE",
                    help="Source table (schema.table or bare table name)")
    bp.add_argument("--pk", required=True, nargs="+", metavar="COL",
                    help="Primary key column(s) — one or more, space-separated")
    bp.add_argument("--doc-col", dest="doc_col", nargs="+", metavar="COL",
                    default=None,
                    help="Column(s) forming the user message (joined by newline). "
                         "Required for run-bundle to work on the remote.")
    bp.add_argument("--limit", type=int, default=None, metavar="N",
                    help="Cap on pending rows to include (default: all)")
    bp.add_argument("--out", default=None, metavar="DIR",
                    help="Output parent directory (default: <trawler repo>/output/jobs, "
                         "regardless of cwd — push/enqueue/import look there)")
    bp.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="Compute pending/total/claimed row counts + resolved "
                         "config and print them — zero side effects (no job "
                         "dir, no gen._gen_log INSERT, no claim rows)")
    bp.set_defaults(func=_cmd_bundle)

    # ---- run-bundle ----
    rp = sub.add_parser(
        "run-bundle",
        help="Execute a bundled job dir locally — no Postgres needed.",
        description=(
            "Read job.toml + rows.jsonl, call the model endpoint (base_url "
            "resolved from this machine's env), write results to job.sqlite. "
            "Resumable: already-ok rows are skipped."
        ),
    )
    rp.add_argument("job_dir", metavar="JOB_DIR", help="Bundle directory")
    rp.add_argument("--concurrency", type=int, default=1, metavar="N",
                    help="Parallel model calls (default: 1)")
    rp.add_argument("--retries", type=int, default=0, metavar="N",
                    help="Retries on transient endpoint errors (default: 0)")
    rp.add_argument("--limit", type=int, default=None, metavar="N",
                    help="Process at most N pending rows this pass")
    rp.add_argument("--early-stop", dest="early_stop", type=int, default=10,
                    metavar="N",
                    help="Abort after N consecutive failures (default: 10; "
                         "0 disables)")
    rp.add_argument("--temperature", type=float, default=None)
    rp.add_argument("--max-tokens", dest="max_tokens", type=int, default=None)
    rp.add_argument("--timeout", type=int, default=None, metavar="SECONDS")
    rp.set_defaults(func=_cmd_run_bundle)

    # ---- import ----
    ip = sub.add_parser(
        "import",
        help="Pull results from the remote (if given a job id) and merge into Postgres.",
        description=(
            "Given a JOB_ID: rsync job.sqlite back from the remote, then import. "
            "Given a directory path: import it as-is (no pull). Import creates/"
            "extends gen.<prompt>, registers a run (fresh run_id, config records "
            "the job id) and completes the job's log row. Refuses to import the "
            "same job twice unless --force."
        ),
    )
    ip.add_argument("job", metavar="JOB_ID_OR_DIR",
                    help="Job id (pulls from remote first) or a bundle directory path")
    ip.add_argument("-r", "--remote", default="", metavar="NAME",
                    help="Named remote to pull from (default: first in TRAWLER_REMOTES)")
    ip.add_argument("--force", action="store_true",
                    help="Import even if this job_id was imported before")
    ip.set_defaults(func=_cmd_import)

    # ---- remote verbs (thin passthrough to scripts/offload.sh) ----
    for verb, vhelp, usage in [
        ("push", "Rsync a job dir to a remote (never overwrites remote job.sqlite). "
         "Refuses a job that's RUNNING on the remote unless --force.",
         "trawler push <JOB_ID> [--with-repo] [--force]"),
        ("enqueue", "Push + add to the remote's priority queue (auto-run, auto-retry; "
         "-p N: higher prio runs first and preempts lower, default 0). "
         "Refuses a job that's RUNNING on the remote unless --force — prevents "
         "the duplicate-queue-entry accident (job both active and waiting).",
         "trawler enqueue <JOB_ID> [-p N] [--force] [run-bundle args...]"),
        ("run", "Start a job directly in a tmux watchdog (prefer enqueue).",
         "trawler run <JOB_ID> [run-bundle args...]"),
        ("interrupt", "Interrupt a running/queued job (partial kept, resume via re-enqueue).",
         "trawler interrupt <JOB_ID>"),
        ("queue", "Show the remote queue: active / waiting / cooling / done / interrupted. "
         "See `trawler status` for the combined overview (remote + queue + local jobs).",
         "trawler queue"),
        ("pull", "Rsync job.sqlite back from the remote.",
         "trawler pull <JOB_ID>"),
        ("models", "List model files + running model servers on the remote.",
         "trawler models"),
        ("fetch-model", "Resumable GGUF download onto the remote.",
         "trawler fetch-model <hf-repo> <file>"),
    ]:
        aliases = _VERB_ALIASES.get(verb, [])
        vp = sub.add_parser(verb, aliases=aliases, help=vhelp,
                            description=vhelp,
                            usage=f"{usage} [-r NAME]")
        vp.add_argument("-r", "--remote", default="", metavar="NAME",
                        help="Named remote from .env (default: first in TRAWLER_REMOTES)")
        vp.add_argument("args", nargs=argparse.REMAINDER,
                        help="Arguments forwarded to scripts/offload.sh")
        vp.set_defaults(func=_cmd_offload_passthrough, verb=verb)

    # ---- status ----
    sp = sub.add_parser(
        "status",
        help="One-stop overview: remote progress + queue health + local pending jobs "
             "(no args); `status <JOB_ID>` / `status --all` keep the focused remote-only view.",
        description=(
            "With no arguments: prints three sections — '== remote (studio) ==' "
            "(wraps scripts/remote_status.sh), '== queue ==' (wraps "
            "scripts/offload.sh queue), and '== local pending jobs ==' (same as "
            "`trawler jobs`, degrades gracefully if Postgres is unreachable). "
            "Given a JOB_ID or --all, behaves like the old remote-only status."
        ),
    )
    sp.add_argument("job", nargs="?", metavar="JOB_ID",
                    help="Job id to inspect (default: newest on remote)")
    sp.add_argument("-r", "--remote", default="", metavar="NAME",
                    help="Named remote from .env (default: first in TRAWLER_REMOTES)")
    sp.add_argument("--all", action="store_true",
                    help="Show every job on every named remote")
    sp.set_defaults(func=_cmd_status)

    # ---- jobs ----
    jp = sub.add_parser(
        "jobs",
        help="List offload jobs (pending by default; --all for full history). "
             "See `trawler status` for the combined overview (remote + queue + local jobs).",
        description=(
            "Offload jobs are registered in gen._gen_log at bundle time "
            "(status='exported') and completed by import. push/run stamp "
            "remote + stage via scripts/offload.sh. See `trawler status` for "
            "the combined overview (remote + queue + local jobs)."
        ),
    )
    jp.add_argument("--all", action="store_true",
                    help="Include imported jobs, not just pending ones")
    jp.set_defaults(func=_cmd_jobs)

    # ---- job-config ----
    jcp = sub.add_parser(
        "job-config",
        help="Print a job's full bundle recipe + a ready-to-copy re-bundle command.",
        description=(
            "Resolves the bundle recipe (prompt, decoder, model_type, source, "
            "pk, doc_cols) for a job id. Looks first at the local "
            "output/jobs/<job-id>/job.toml (authoritative if present), then "
            "falls back to the gen._gen_log row (config->>'job_id' = <job-id>) "
            "— which still works after `trawler clean` deletes the local dir."
        ),
    )
    jcp.add_argument("job", metavar="JOB_ID", help="Job id to look up")
    jcp.set_defaults(func=_cmd_job_config)

    # ---- rebundle ----
    rbp = sub.add_parser(
        "rebundle",
        help="Re-ship a prompt: find its most recent job, resolve the recipe "
             "exactly like job-config, and preview or re-bundle it.",
        description=(
            "Finds the prompt's most recent job (local output/jobs/<prompt>-*/"
            "job.toml, compared against the newest gen._gen_log row for the "
            "prompt) and resolves its full bundle recipe via the same logic as "
            "`job-config`. Without --go: prints the recipe plus a bundle "
            "--dry-run preview (pending/total/claimed) and the exact commands "
            "to actually ship it. With --go: bundles for real. Refuses to "
            "guess any missing recipe field — exits 1 naming what's missing."
        ),
    )
    rbp.add_argument("prompt", metavar="PROMPT", help="Prompt name to re-bundle")
    rbp.add_argument("--go", action="store_true",
                      help="Actually bundle (default: dry-run preview only)")
    rbp.add_argument("--limit", type=int, default=None, metavar="N",
                      help="Cap on pending rows to include (default: all)")
    rbp.set_defaults(func=_cmd_rebundle)

    # ---- clean ----
    cp = sub.add_parser(
        "clean",
        help="Delete imported job dirs to reclaim disk (dry-run unless --yes).",
        description=(
            "Job dirs (output/jobs/<id>/) persist after import by design. "
            "This removes ones safe to drop — imported (status='complete') jobs — "
            "so they don't stack up. Dry-run by default."
        ),
    )
    cg = cp.add_mutually_exclusive_group()
    cg.add_argument("job", nargs="?", metavar="JOB_ID",
                    help="Clean a single job (refused unless imported, or --force)")
    cg.add_argument("--imported", action="store_true",
                    help="Clean every imported (status='complete') offload job")
    cp.add_argument("--yes", action="store_true",
                    help="Actually delete (without this it only lists)")
    cp.add_argument("--remote", action="store_true",
                    help="Also rm the remote box's copy of each job dir")
    cp.add_argument("--force", action="store_true",
                    help="Allow cleaning a not-yet-imported single job")
    cp.set_defaults(func=_cmd_clean)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trawler",
        description="Trawler — batch LLM inference control plane.",
    )
    sub = parser.add_subparsers(dest="command", title="subcommands")
    sub.required = True
    _register_subcommands(sub)
    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    from trawler.env import load_env
    load_env()                       # .env → os.environ (exported vars win)
    _ensure_shell_alias()
    parser = _build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
