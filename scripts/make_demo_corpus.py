#!/usr/bin/env python3
"""Build the synthetic corpus the committed frontend fixtures are dumped from.

    python3 scripts/make_demo_corpus.py [--root DIR] [--dump]

`frontend/src/fixtures/*.json` is the sample dashboard: what anyone sees who
opens the built frontend with no server running. It therefore ships in every
wheel and on every GitHub page view, which rules out dumping it from a real
corpus -- a real corpus is a list of the author's clients, employers and
side projects, keyed by absolute path. The fixtures used to be exactly that
and had to be replaced.

So the sample data is generated. Three constraints shaped this script:

**It must go through the real pipeline.** This writes Claude Code and Codex
log files in their on-disk formats and then runs the ordinary
ingest -> derive -> cost -> group path over them, exactly as `cci` would on a
real machine. Nothing writes to the `event`, `span` or `project` tables
directly. That is what keeps the fixtures structurally valid as the schema
moves: if an adapter changes shape, this corpus stops parsing and says so,
where a hand-written JSON blob would silently drift until a reviewer noticed
the dashboard was rendering a fossil.

**It must look like real usage.** `test1`/`test2` over four hours produces a
dashboard that demonstrates nothing: an empty heatmap, a flat cost chart, one
lane in the swimlane, concurrency pinned at 1. The generated corpus runs
~6.5 months across 25 paths in 16 projects, two agents, five models, with
subagent threads that overlap their parents (so concurrency exceeds 1),
sessions that resume after idle gaps (so threads have several spans), and
priced token usage dominated by cache reads (so the cost breakdown has the
shape a real one has).

**It must be reproducible.** Everything is drawn from `random.Random(SEED)`,
so a re-run with the same seed produces byte-identical fixtures and a diff in
`frontend/src/fixtures` means a real change in pipeline behaviour -- which is
the regression value the old committed snapshot had, kept.

The one thing not done through the filesystem is the git probe. Grouping
learns a checkout's remote by running `git remote get-url origin` in it,
which would mean materialising 25 real repositories to have them discovered.
Instead the `project_probe` rows are seeded with the remotes the fictional
checkouts would have reported, and detection runs in its documented hermetic
mode (`probe_fs=False`), so the rule ladder itself -- rule 1 git_remote,
rule 4 path_ancestor, the worktree folding -- runs for real against them.

Nothing here is a real person, repository, organisation or path.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

SEED = 20260920
HOSTNAME = "demo-mbp.local"
HOME = "/Users/demo"

START = datetime(2026, 3, 2, tzinfo=timezone.utc)
END = datetime(2026, 9, 18, tzinfo=timezone.utc)

CLAUDE_VERSION = "2.1.4"
CODEX_VERSION = "0.104.0"

# -- the fictional world ---------------------------------------------------
# (root_path, remote, weight, exists). `weight` is relative share of sessions;
# `exists` False is a checkout that has since been deleted, which is the
# common case for worktrees and which the projects table renders as "gone".
GH = "https://github.com/{}/{}.git"

PROJECTS: list[tuple[str, str | None, int, bool]] = [
    # A flagship service worked on from its main checkout and five worktrees,
    # spread over all three layouts `worktree_shape()` recognises. This is the
    # corpus's "one project, many paths" story: six rows in the paths list
    # that must collapse to one lane in the swimlane. The worktrees that have
    # been deleted report no remote, so folding them in is left to the
    # path-shape rules rather than to rule 1.
    (f"{HOME}/Coding/atlas-chat", GH.format("northwind-labs", "atlas-chat"), 34, True),
    (f"{HOME}/Coding/atlas-chat/.claude/worktrees/streaming", None, 13, True),
    (f"{HOME}/Coding/atlas-chat/.claude/worktrees/retry-budget-spike", None, 9, False),
    (f"{HOME}/.t3/worktrees/atlas-chat/t3code-4b91c2e0", None, 7, False),
    (f"{HOME}/conductor/workspaces/atlas-chat/bergen", None, 5, True),
    (f"{HOME}/Coding/forks/atlas-chat", GH.format("northwind-labs", "atlas-chat"), 3, False),

    # The tool itself, dogfooded from a main checkout and two worktrees.
    (f"{HOME}/Coding/cc-insights", GH.format("driftwood", "cc-insights"), 22, True),
    (f"{HOME}/Coding/cc-insights/.claude/worktrees/fixtures", None, 8, True),
    (f"{HOME}/.t3/worktrees/cc-insights/t3code-9d02f5a1", None, 6, False),

    # A repo whose subdirectory is worked in directly and has no remote of its
    # own: rule 4 (path_ancestor) has to fold it into its parent.
    (f"{HOME}/Coding/slm-finetune", GH.format("northwind-labs", "slm-finetune"), 14, True),
    (f"{HOME}/Coding/slm-finetune/latex", None, 5, True),

    (f"{HOME}/Coding/census-pipeline", GH.format("northwind-labs", "census-pipeline"), 12, True),
    (f"{HOME}/conductor/workspaces/census-pipeline/almeria", None, 4, False),

    (f"{HOME}/Coding/atlas-chat-sdk", GH.format("northwind-labs", "atlas-chat-sdk"), 9, True),
    (f"{HOME}/Coding/skill-planner", GH.format("northwind-labs", "skill-planner"), 11, True),
    (f"{HOME}/Coding/doc-classifier", GH.format("northwind-labs", "doc-classifier"), 8, True),

    (f"{HOME}/Coding/harbor-cli", GH.format("driftwood", "harbor-cli"), 10, True),
    (f"{HOME}/Coding/pinecrest", GH.format("driftwood", "pinecrest"), 7, True),
    (f"{HOME}/Coding/tabula-rasa", GH.format("driftwood", "tabula-rasa"), 6, True),
    (f"{HOME}/Coding/glyphwright", GH.format("driftwood", "glyphwright"), 5, True),
    # One non-GitHub remote, so the forge column is not uniform.
    (f"{HOME}/Coding/seaglass", "git@gitlab.com:driftwood/seaglass.git", 5, True),

    # No remote and nothing to fold into: these stay ungrouped, which the
    # dashboard reports as an explicit "ungrouped" bucket rather than hiding.
    (f"{HOME}/scratch/spike-websockets", None, 4, True),
    (f"{HOME}/scratch/bench-tokenizer", None, 3, True),
    (f"{HOME}/Downloads/repro-1284", None, 2, False),
    (f"{HOME}/Coding/notes", None, 3, True),
]

BRANCHES = ["main", "develop", "feat/streaming", "fix/retry-budget",
            "perf/index", "chore/deps", "feat/export", "main"]

CLAUDE_MODELS = [("claude-opus-5", 38), ("claude-sonnet-5", 37), ("claude-fable-5-1", 25)]
CODEX_MODELS = [("gpt-5.6", 62), ("gpt-5.4", 38)]

# Claude Code records a subagent's configured TYPE.
AGENT_TYPES = ["general-purpose", "Explore", "Plan", "code-reviewer",
               "docs-writer", "test-runner", "workflow-subagent"]
# Codex records a random per-thread NICKNAME instead, which is why the two must
# never be presented in the same column. Codex's scheme is scientist surnames.
CODEX_NICKS = ["Ampere", "Boltzmann", "Curie", "Dirac", "Euler", "Faraday",
               "Gauss", "Hopper", "Ising", "Joule", "Kelvin", "Lovelace",
               "Maxwell", "Noether", "Ohm", "Planck", "Rayleigh", "Shannon"]

TOOLS = ["Read", "Edit", "Bash", "Grep", "Glob", "Write", "WebFetch", "Agent"]


def pick(rng: random.Random, weighted: list[tuple[str, int]]) -> str:
    return rng.choices([v for v, _ in weighted], [w for _, w in weighted])[0]


def iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def slug_for(path: str) -> str:
    """Claude Code's directory name for a project: the path, slashes to dashes."""
    return path.replace("/", "-").replace(".", "-")


class Corpus:
    """Writes the two log trees, then reports what it wrote."""

    def __init__(self, root: Path, rng: random.Random) -> None:
        self.root = root
        self.rng = rng
        self.claude = root / "logs" / "claude" / "projects"
        self.codex = root / "logs" / "codex" / "sessions"
        self.sessions = 0
        self.threads = 0
        self.events = 0

    # -- token usage -------------------------------------------------------
    def usage(self, turn: int, model: str) -> dict:
        """Plausible per-turn usage. Cache reads dominate, as they do in life:
        every turn re-reads the conversation so far, so the number climbs with
        the turn index and ends up the largest line in the cost breakdown."""
        rng = self.rng
        cache_read = min(12_000 + turn * rng.randint(2_000, 9_000), 190_000)
        cache_write = rng.choice([0, 0, 0, rng.randint(1_200, 24_000)])
        block: dict = {
            "input_tokens": rng.randint(2, 90),
            "output_tokens": rng.randint(60, 2_600),
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
        }
        # A minority of writes take the one-hour TTL, which is priced
        # differently; the adapter reads the split out of `cache_creation`.
        if cache_write and rng.random() < 0.28:
            block["cache_creation"] = {
                "ephemeral_1h_input_tokens": cache_write,
                "ephemeral_5m_input_tokens": 0,
            }
        elif cache_write:
            block["cache_creation"] = {
                "ephemeral_1h_input_tokens": 0,
                "ephemeral_5m_input_tokens": cache_write,
            }
        return block

    # -- Claude Code -------------------------------------------------------
    def claude_session(self, cwd: str, start: datetime, bursts: int) -> None:
        rng = self.rng
        sid = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        model = pick(rng, CLAUDE_MODELS)
        branch = rng.choice(BRANCHES)
        directory = self.claude / slug_for(cwd)
        directory.mkdir(parents=True, exist_ok=True)

        lines: list[dict] = []
        subagents: list[tuple[str, list[dict]]] = []
        ts = start
        turn = 0
        prev_uuid: str | None = None

        for burst in range(bursts):
            if burst:
                # Longer than the 300s idle threshold: this is what splits a
                # thread into several spans rather than one long one.
                ts += timedelta(seconds=rng.randint(420, 10_800))
            # Most bursts open with a human turn (attended=1). Some resume
            # mid-agent-turn, which must score 0, so the dashboard's
            # attended/unattended split is not degenerate.
            human_opened = burst == 0 or rng.random() < 0.72
            if human_opened:
                prev_uuid = self._line(
                    lines, ts, sid, cwd, branch, "user", prev_uuid,
                    content="(prompt text is never ingested)")
                ts += timedelta(seconds=rng.randint(3, 25))

            for _ in range(rng.randint(2, 9)):
                turn += 1
                prev_uuid = self._line(
                    lines, ts, sid, cwd, branch, "assistant", prev_uuid,
                    model=model, usage=self.usage(turn, model))
                ts += timedelta(seconds=rng.randint(4, 70))

                if rng.random() < 0.78:
                    tool = rng.choice(TOOLS)
                    call = f"toolu_{rng.getrandbits(48):012x}"
                    prev_uuid = self._line(
                        lines, ts, sid, cwd, branch, "assistant", prev_uuid,
                        model=model, usage=self.usage(turn, model),
                        tool_use=(tool, call))
                    ts += timedelta(seconds=rng.randint(2, 55))
                    prev_uuid = self._line(
                        lines, ts, sid, cwd, branch, "user", prev_uuid,
                        tool_result=call)
                    ts += timedelta(seconds=rng.randint(2, 40))

                    # An Agent call spawns a subagent thread, and the subagent
                    # runs WHILE the parent keeps going -- that overlap is
                    # exactly what the concurrency chart measures.
                    if tool == "Agent":
                        subagents.append(self._claude_subagent(sid, cwd, branch, ts, model))

            prev_uuid = self._line(
                lines, ts, sid, cwd, branch, "assistant", prev_uuid,
                model=model, usage=self.usage(turn + 1, model))
            ts += timedelta(seconds=rng.randint(5, 60))

        (directory / f"{sid}.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in lines))
        self.sessions += 1
        self.threads += 1
        self.events += len(lines)

        for agent_id, agent_lines in subagents:
            nested = directory / sid / "subagents"
            nested.mkdir(parents=True, exist_ok=True)
            (nested / f"agent-{agent_id}.jsonl").write_text(
                "".join(json.dumps(line) + "\n" for line in agent_lines))
            self.threads += 1
            self.events += len(agent_lines)

    def _claude_subagent(self, sid: str, cwd: str, branch: str,
                         start: datetime, model: str) -> tuple[str, list[dict]]:
        rng = self.rng
        agent_id = f"{rng.getrandbits(64):016x}"
        agent_type = rng.choice(AGENT_TYPES)
        lines: list[dict] = []
        ts = start
        prev: str | None = None
        # The opening line is `user`-typed but carries the ORCHESTRATOR's task
        # prompt, not a human's -- `attended` must still score this thread 0.
        prev = self._line(lines, ts, sid, cwd, branch, "user", prev,
                          content="(task prompt)", agent_id=agent_id)
        ts += timedelta(seconds=rng.randint(2, 10))
        for turn in range(rng.randint(3, 14)):
            prev = self._line(lines, ts, sid, cwd, branch, "assistant", prev,
                              model=model, usage=self.usage(turn + 1, model),
                              agent_id=agent_id, agent_type=agent_type)
            ts += timedelta(seconds=rng.randint(4, 50))
            if rng.random() < 0.8:
                call = f"toolu_{rng.getrandbits(48):012x}"
                prev = self._line(lines, ts, sid, cwd, branch, "assistant", prev,
                                  model=model, usage=self.usage(turn + 1, model),
                                  agent_id=agent_id, agent_type=agent_type,
                                  tool_use=(rng.choice(TOOLS[:7]), call))
                ts += timedelta(seconds=rng.randint(2, 45))
                prev = self._line(lines, ts, sid, cwd, branch, "user", prev,
                                  tool_result=call, agent_id=agent_id)
                ts += timedelta(seconds=rng.randint(2, 30))
        return agent_id, lines

    def _line(self, out: list[dict], ts: datetime, sid: str, cwd: str, branch: str,
              kind: str, parent: str | None, *, content: str = "ok",
              model: str | None = None, usage: dict | None = None,
              tool_use: tuple[str, str] | None = None, tool_result: str | None = None,
              agent_id: str | None = None, agent_type: str | None = None) -> str:
        uid = str(uuid.UUID(int=self.rng.getrandbits(128), version=4))
        message: dict
        if tool_use:
            name, call = tool_use
            message = {"id": f"msg_{self.rng.getrandbits(40):010x}", "model": model,
                       "role": "assistant",
                       "content": [{"type": "tool_use", "id": call, "name": name,
                                    "input": {}}]}
        elif tool_result:
            message = {"role": "user",
                       "content": [{"type": "tool_result", "tool_use_id": tool_result,
                                    "content": "ok"}]}
        elif kind == "assistant":
            message = {"id": f"msg_{self.rng.getrandbits(40):010x}", "model": model,
                       "role": "assistant", "content": [{"type": "text", "text": content}]}
        else:
            message = {"role": "user", "content": content}
        if usage is not None:
            message["usage"] = usage

        line = {
            "parentUuid": parent,
            "isSidechain": agent_id is not None,
            "userType": "external",
            "cwd": cwd,
            "sessionId": sid,
            "version": CLAUDE_VERSION,
            "gitBranch": branch,
            "type": kind,
            "message": message,
            "uuid": uid,
            "timestamp": iso(ts),
        }
        if agent_id:
            line["agentId"] = agent_id
        if agent_type:
            # Only assistant lines carry the subagent's type; the adapter
            # sniffs it from the head of the transcript.
            line["attributionAgent"] = agent_type
        out.append(line)
        return uid

    # -- Codex -------------------------------------------------------------
    def codex_session(self, cwd: str, start: datetime, bursts: int) -> None:
        rng = self.rng
        sid = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        root_thread = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        model = pick(rng, CODEX_MODELS)
        branch = rng.choice(BRANCHES)

        end = self._codex_thread(sid, root_thread, None, cwd, branch, model, start, bursts)
        self.sessions += 1

        for _ in range(rng.choices([0, 1, 2, 3], [46, 30, 16, 8])[0]):
            child = str(uuid.UUID(int=rng.getrandbits(128), version=4))
            # Starts inside the parent's run, not after it: overlapping threads
            # are the point.
            offset = timedelta(seconds=rng.randint(30, 600))
            self._codex_thread(sid, child, root_thread, cwd, branch, model,
                               min(start + offset, end), rng.randint(1, 2),
                               nickname=rng.choice(CODEX_NICKS))

    def _codex_thread(self, sid: str, thread: str, parent: str | None, cwd: str,
                      branch: str, model: str, start: datetime, bursts: int,
                      nickname: str | None = None) -> datetime:
        rng = self.rng
        day = self.codex / f"{start:%Y}" / f"{start:%m}" / f"{start:%d}"
        day.mkdir(parents=True, exist_ok=True)

        meta_payload: dict = {
            "session_id": sid,
            "id": thread,
            "cwd": cwd,
            "originator": "codex_cli_rs",
            "cli_version": CODEX_VERSION,
            "model_provider": "openai",
            "git": {"branch": branch},
        }
        if parent:
            # A dict (not a string) under `source` is what marks a subagent.
            meta_payload["source"] = {"subagent": {"thread_spawn": {}}}
            meta_payload["parent_thread_id"] = parent
            meta_payload["agent_nickname"] = nickname
        else:
            meta_payload["source"] = "cli"

        lines: list[dict] = []
        ordinal = 0
        ts = start

        def emit(kind: str, payload: dict) -> None:
            nonlocal ordinal
            lines.append({"timestamp": iso(ts), "ordinal": ordinal,
                          "type": kind, "payload": payload})
            ordinal += 1

        emit("session_meta", meta_payload)
        ts += timedelta(seconds=rng.randint(1, 5))
        emit("turn_context", {"model": model, "cwd": cwd})

        turn = 0
        for burst in range(bursts):
            if burst:
                ts += timedelta(seconds=rng.randint(420, 7_200))
            if parent is None:
                ts += timedelta(seconds=rng.randint(2, 20))
                emit("response_item", {"type": "message", "role": "user",
                                       "content": [{"type": "input_text", "text": "(prompt)"}]})
            for _ in range(rng.randint(3, 11)):
                turn += 1
                ts += timedelta(seconds=rng.randint(4, 65))
                emit("response_item", {"type": "reasoning", "summary": []})
                ts += timedelta(seconds=rng.randint(2, 30))
                call = f"call_{rng.getrandbits(48):012x}"
                emit("response_item", {"type": "function_call", "name": rng.choice(TOOLS[:6]),
                                       "call_id": call})
                ts += timedelta(seconds=rng.randint(2, 50))
                emit("response_item", {"type": "function_call_output", "call_id": call})
                ts += timedelta(seconds=rng.randint(2, 25))
                emit("response_item", {"type": "message", "role": "assistant",
                                       "content": [{"type": "output_text", "text": "ok"}]})
                # Codex reports usage on its own record, with no model on it --
                # cost.py has to attribute the model from earlier in the thread.
                cached = min(9_000 + turn * rng.randint(1_500, 8_000), 170_000)
                ts += timedelta(seconds=rng.randint(1, 10))
                emit("event_msg", {
                    "type": "token_count",
                    "info": {"last_token_usage": {
                        "input_tokens": cached + rng.randint(40, 1_800),
                        "cached_input_tokens": cached,
                        "output_tokens": rng.randint(50, 2_200),
                        "cache_write_input_tokens": rng.choice([0, 0, rng.randint(800, 14_000)]),
                    }},
                })

        name = f"rollout-{start:%Y-%m-%dT%H-%M-%S}-{thread}.jsonl"
        (day / name).write_text("".join(json.dumps(line) + "\n" for line in lines))
        self.threads += 1
        self.events += len(lines)
        return ts


def generate(root: Path) -> Corpus:
    rng = random.Random(SEED)
    corpus = Corpus(root, rng)
    weights = [w for _, _, w, _ in PROJECTS]
    paths = [p for p, _, _, _ in PROJECTS]

    day = START
    while day <= END:
        weekday = day.weekday()
        # A working rhythm: busy midweek, light weekends, the occasional day
        # off. Without this the heatmap is a uniform block and says nothing.
        if weekday >= 5:
            count = rng.choices([0, 1, 2], [58, 30, 12])[0]
        else:
            count = rng.choices([0, 1, 2, 3, 4, 5], [10, 20, 26, 22, 14, 8])[0]
        for _ in range(count):
            hour = rng.choices(
                [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22],
                [3, 8, 12, 13, 7, 6, 11, 12, 10, 8, 5, 4, 5, 4, 2])[0]
            start = day.replace(hour=hour, minute=rng.randint(0, 59),
                                second=rng.randint(0, 59))
            cwd = rng.choices(paths, weights)[0]
            bursts = rng.choices([1, 2, 3, 4, 5], [40, 27, 17, 10, 6])[0]
            if rng.random() < 0.68:
                corpus.claude_session(cwd, start, bursts)
            else:
                corpus.codex_session(cwd, start, bursts)
        day += timedelta(days=1)
    return corpus


def build(root: Path, dump: bool) -> int:
    from cc_insights import config as config_mod, cost as cost_mod, db
    from cc_insights import derive, grouping, ingest, pricing

    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    home = root / "home"
    home.mkdir()

    corpus = generate(root)
    print(f"wrote {corpus.sessions:,} sessions, {corpus.threads:,} threads, "
          f"{corpus.events:,} events to {root}")

    cfg = config_mod.load(home, create=True)
    # THE important line. Left at the defaults these globs point at
    # ~/.claude and ~/.codex -- the author's real logs -- and this script
    # would quietly rebuild the fixtures from exactly the data it exists to
    # keep out of the repository.
    cfg.source_globs = {
        "claude_code": [str(corpus.claude / "*" / "*.jsonl"),
                        str(corpus.claude / "*" / "*" / "subagents" / "**" / "*.jsonl")],
        "codex": [str(corpus.codex / "*" / "*" / "*" / "*.jsonl")],
    }
    cfg.hostname = HOSTNAME
    # `config.load` mints a fresh host_id per run, and every session, thread
    # and span id is derived from it -- so leaving it random would rewrite
    # every id in timeline.json on each regeneration and bury a real
    # behavioural diff in 30,000 lines of noise. Pinned, the whole dump is
    # reproducible except `meta.generatedAt`, which is a wall clock by design.
    cfg.host_id = "b0f7e3a2-5c14-4d8e-9a6b-2f1c7d0e4a93"
    cfg.save()

    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    db.upsert_host(conn, cfg.host_id, cfg.hostname, "darwin")

    result = ingest.ingest(conn, cfg)
    print(f"ingested {result.events_inserted:,} events "
          f"from {result.files_ingested:,} files")

    d = derive.derive(conn, idle_threshold_s=cfg.idle_threshold_s)
    print(f"derived {d.spans:,} spans over {d.threads:,} threads "
          f"({d.active_ms / 3_600_000:,.1f} h)")

    pricing.sync(conn)
    c = cost_mod.derive_costs(conn)
    print(f"priced {c.events:,} events at {c.total:,.2f} {c.currency}")

    seed_probes(conn, cfg.host_id)
    g = grouping.detect(conn, host_id=cfg.host_id, probe_fs=False)
    print(f"grouped {g.projects_grouped} paths into {len(g.plans)} projects "
          f"({g.ungrouped} ungrouped) · by rule: "
          + ", ".join(f"{k}={v}" for k, v in sorted(g.by_rule.items())))
    conn.close()

    if dump:
        subprocess.run(
            [sys.executable, str(REPO / "scripts" / "dump_fixtures.py"),
             "--config-dir", str(home)],
            check=True,
        )
    return 0


def seed_probes(conn, host_id: str) -> None:
    """Record the remote each fictional checkout would have reported.

    Real `cci group auto` learns this by running git in the checkout. These
    paths do not exist, so the answers are written straight into the probe
    cache and detection is then run with `probe_fs=False` -- the same hermetic
    mode the grouping tests use. The rule ladder still does all the work.
    """
    from cc_insights import ids

    now = int(END.timestamp() * 1000)
    known = {row[0] for row in conn.execute("SELECT project_id FROM project")}
    for path, remote, _, exists in PROJECTS:
        pid = ids.project_id(path)
        if pid not in known:
            continue
        conn.execute(
            """INSERT INTO project_probe
                   (project_id, host_id, git_remote, git_common_dir,
                    path_exists, detected_at)
               VALUES (?, ?, ?, NULL, ?, ?)
               ON CONFLICT(project_id, host_id) DO UPDATE SET
                   git_remote = excluded.git_remote,
                   path_exists = excluded.path_exists,
                   detected_at = excluded.detected_at""",
            (pid, host_id, remote, 1 if exists else 0, now),
        )
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path("/tmp/cci-demo-corpus"),
                    help="scratch directory for the logs and database")
    ap.add_argument("--dump", action="store_true",
                    help="also run scripts/dump_fixtures.py over the result")
    args = ap.parse_args()
    return build(args.root, args.dump)


if __name__ == "__main__":
    raise SystemExit(main())
