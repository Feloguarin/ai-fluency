#!/usr/bin/env python3
"""
AI Fluency v2 — one-command, zero-install analyzer of how skillfully you build with AI.

    python3 insight.py

Reads your local Claude Code transcripts (~/.claude/projects/**/*.jsonl),
estimates how skillfully you drive an AI coding agent, and writes a single
self-contained HTML report (./ai_fluency_report.html) that opens in your browser.

Design principles (see README "Methodology"):
  * It measures SKILL, not activity. Every score input is a per-prompt or
    per-opportunity RATE pushed through a saturating curve, so using the agent
    MORE can never raise your score — only using it BETTER can.
  * It only looks at YOUR real typed prompts and Claude's real tool actions.
    Tool-results, subagent turns, slash-command stubs, injected system text and
    pasted walls of text are filtered out before anything is scored.
  * Every number is auditable: baselines are recomputed from your corpus at
    runtime, formulas are documented, and thin signals are flagged "low data"
    and pulled toward a neutral 50 instead of faking confidence.

Pure Python standard library — no pip, no Ollama, no API key. One command runs the
whole pass: de-contaminate and scrub your transcripts, score them, and (as
`/ai-fluency` in Claude Code) write a Sonnet+Opus skill map grounded in the AI
Fluency framework on top. The only thing it writes is
the HTML report and a local copy of your transcripts in an archive
(~/.claude/insight-archive) so history survives Claude Code's 30-day cleanup —
pass --no-archive to skip that and read your transcripts without copying them.
"""

import argparse
import glob
import hashlib
import html
import json
import math
import os
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime

# --------------------------------------------------------------------------- #
# Constants & tunables (documented; shown in the report's methodology appendix)
# --------------------------------------------------------------------------- #

DEFAULT_DIRS = ["~/.claude/projects", "~/.claude/sessions"]

# Claude Code deletes transcripts older than its `cleanupPeriodDays` setting (default 30),
# so by default only ~30 days of history is ever on disk. We mirror each run's transcripts
# into this persistent archive so history accumulates indefinitely and survives the cleanup.
# Keep this on a PRIVATE, per-person path. A single archive folder shared between different
# people or computers (e.g. a synced team Dropbox) merges everyone's transcripts into one
# analysis — so each person must point --archive at their own location, not a shared one.
DEFAULT_ARCHIVE_DIR = "~/.claude/insight-archive"

GAP_CAP_SECONDS = 300          # idle gaps longer than this are NOT counted as active time
MAX_HUMAN_PROMPT_CHARS = 6000  # anything longer is treated as a paste/injection, not a typed prompt
PROVISIONAL_MIN_PROMPTS = 30   # below this the headline score is shown as a hedged range

EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit"}
READ_TOOLS = {"read", "grep", "glob"}

# Text that marks a "user"-role record as injected/system rather than typed by the human.
INJECTION_MARKERS = (
    "<task-notification>", "<command-name>", "<command-message>", "<command-args>",
    "<local-command-caveat>", "<local-command-stdout>", "<system-reminder>",
    "<bash-input>", "<bash-stdout>", "caveat: the messages below",
    "[request interrupted", "base directory for this skill", "<user-prompt-submit-hook>",
    "<user-memory-input>", "this session is being continued",
)

# Subagent system prompts get stored as plain user-role text with no other marker.
# They almost always open with "You are <role>…". This catches the back-door inflation.
_INJECTED_HEAD = re.compile(
    r"^\s*(you are\b|<[a-z][\w-]*>|base directory for this skill)", re.I
)

# Broad, project-extensible verification matcher (matched against real Bash commands).
VERIFY_RE = re.compile(
    r"\b("
    r"pytest|unittest|jest|vitest|mocha|go test|cargo (test|build|check)|"
    r"npm (run )?(test|build|lint)|yarn (test|build|lint)|pnpm (test|build|lint)|"
    r"ruff|eslint|flake8|mypy|tsc\b|make (test|lint|build|check)|playwright|"
    r"python\d? -m \w|\.venv/bin/python|lsof -ti|curl .*(localhost|127\.0\.0\.1)|"
    r"docker compose|docker-compose|pre-commit"
    r")",
    re.I,
)
# Clean-teardown of a live system (small bonus, folded into Verification).
TEARDOWN_RE = re.compile(r"(lsof -ti.*kill|pkill|kill -9|docker compose down|docker-compose down)", re.I)
# Verification cues inside a human prompt (used to highlight high-craft prompts).
_VERIFY_PROMPT_RE = re.compile(
    r"\b(run (the )?(tests?|suite|build|lint|checks?)|verify|confirm|make sure .* pass|"
    r"check (it|that)|show me the output|paste the output|before (you )?finish)\b",
    re.I,
)

# Direction (prompt-quality) cues.
ARTIFACT_RE = re.compile(
    r"([\w./\-]+\.(py|js|ts|tsx|jsx|html|css|md|json|sh|ya?ml|toml|rs|go|java|cpp|c|rb|sql))"
    r"|((?:/[\w.\-]+){2,})"        # multi-segment paths (not bare /word or </tag>)
    r"|(`[^`]+`)"                  # inline code / quoted token
    r"|(\b\w+\(\))",               # function() reference
    re.I,
)
CONSTRAINT_CUE = re.compile(
    r"\b(only|must|should|shouldn't|don't|do not|never|always|keep|ensure|instead of|"
    r"at most|at least|exactly|without|except|make sure|no more than|leave .* as is)\b", re.I
)
INTENT_CUE = re.compile(
    r"\b(so that|because|the goal is|in order to|for the demo|for my|for the|so i can|so we can|"
    r"so it|i need|i want .* so)\b", re.I
)
ACTION_VERB = re.compile(
    r"\b(add|create|build|make|implement|write|fix|change|update|refactor|remove|delete|run|"
    r"generate|set up|setup|install|deploy|edit|rename|move|clean|stitch|speed up|merge|split)\b", re.I
)

# Iteration cues.
CORRECTION_CUE = re.compile(
    r"\b(no|nope|wrong|not quite|that's not|thats not|actually|instead|revert|undo|redo|try again|"
    r"too (aggressive|agressive|much|many|slow|fast|big|small)|still (broken|failing|wrong|not)|"
    r"doesn't work|does not work|not working)\b", re.I
)
PRAISE_CUE = re.compile(r"\b(great|perfect|love it|nice|awesome|excellent|beautiful|exactly)\b", re.I)
CORRECTION_RATE_CEILING = 0.35   # a "high" correction rate; lower is better

# Delegation / planning tool signals.
DELEGATION_TOOLS = {"agent", "task", "workflow", "exitplanmode", "enterplanmode"}

# Dimension weights (sum to 1.0).
WEIGHTS = {
    "Direction": 0.24,
    "Verification": 0.22,
    "Context": 0.22,
    "Iteration": 0.18,
    "Toolcraft": 0.14,
}
# Opportunity-count targets for per-dimension confidence shrinkage.
TARGET_N = {"Direction": 60, "Verification": 15, "Context": 25, "Iteration": 12, "Toolcraft": 40}

# User-facing labels. "Direction" is shown as "Briefing" so it never collides with the
# "Director" archetype (the dimension measures how well you brief; the archetype, that
# you delegate — different things).
DISPLAY_NAMES = {"Direction": "Briefing", "Verification": "Verification",
                 "Context": "Context-setting", "Iteration": "Iteration", "Toolcraft": "Toolcraft"}

def disp(name):
    return DISPLAY_NAMES.get(name, name)

# Teacher content for each skill (kind, plain-English, with before/after examples and a
# weekly practice). Used to make the report explain what to improve and exactly how.
SKILL_TEACH = {
    "Direction": {
        "what_it_is": "Telling the agent what you want and giving it something to aim at: a goal plus a file, a constraint, or a way to know it worked.",
        "why_it_matters": "When your goal and your limits are clear up front, the agent gets it right the first time instead of guessing and pulling you into rounds of fixes.",
        "how_to_improve": "Before you hit enter, add one anchor to your goal: the file to touch, a rule it must not break, or a 'done when…' line. One line is plenty.",
        "examples": [
            {"before": "fix the login bug", "after": "Users stay logged out after a correct password on Safari. The check lives in src/auth/session.ts. Fix it so a valid login sets the session cookie, and keep the current tests green."},
            {"before": "add caching to the API", "after": "Cache GET /products responses in api/products.py for 60s to ease DB load on repeat reads. Don't cache authed requests, and add a test that a second call within 60s skips the DB."},
        ],
        "practice": "Before sending a prompt, add one anchor to your goal: a file path, a constraint, or a 'done when…' line.",
        "good_looks_like": "Every request says what you want plus where to work or how success is judged, so the agent acts instead of guessing.",
    },
    "Verification": {
        "what_it_is": "Having the agent prove its own work — run the tests, build, lint, or launch the app — before it tells you it's done.",
        "why_it_matters": "Code that looks right but was never run is where most AI bugs hide; checking it turns “probably works” into “I watched it work.”",
        "how_to_improve": "In the same prompt that asks for the change, name the exact command that proves it (a test, build, lint, or curl) and tell the agent to run it and show you the output before stopping.",
        "examples": [
            {"before": "Fix the off-by-one in the pagination helper.", "after": "Fix the off-by-one in the pagination helper, then run `pytest tests/test_pagination.py -x` and paste the output. Don't call it fixed until that test passes."},
            {"before": "Add a /health endpoint to the FastAPI server.", "after": "Add a /health endpoint to the FastAPI server. Start it on port 8000, curl `localhost:8000/health`, and show me the response. Run `ruff check` too and confirm it's clean before you finish."},
        ],
        "practice": "Before you accept any change, ask: “How did you verify this? Run it and show me the output.”",
        "good_looks_like": "Every change ends with proof — a passing test, a green build, a real response — pasted back to you, not just a claim.",
    },
    "Context": {
        "what_it_is": "Pointing the agent at the real code — a file, a function, a line area — and having it read that before it changes anything.",
        "why_it_matters": "When the agent sees the actual current code first, its edits fit what's really there instead of a guess, so they apply cleanly the first time.",
        "how_to_improve": "Before any edit, name the exact file (and the function or area if you can) and tell the agent to read it first. Let it look before it leaps.",
        "examples": [
            {"before": "Add retry logic to the API client.", "after": "Read src/api/client.ts first, then add retry-with-backoff to the request() method. Show me the change before you apply it."},
            {"before": "Fix the timezone bug in the date formatter.", "after": "Open src/utils/date.ts and find formatDate(). Read how it handles timezones now, then fix the off-by-one so UTC inputs render in the user's local zone."},
        ],
        "practice": "Start your next edit request with “Read <file> first, then…” so the agent grounds itself before touching anything.",
        "good_looks_like": "Every edit lands on code the agent just read, so diffs apply cleanly with nothing broken around them.",
    },
    "Iteration": {
        "what_it_is": "When the agent goes the wrong way, steering it back with a precise correction — naming what broke and the rule to follow — instead of just “no” or “try again.”",
        "why_it_matters": "A precise correction lands the fix in one round; a vague “no” makes the agent guess again, and you burn turns while the code drifts further off.",
        "how_to_improve": "When a result is wrong, say three things in one message: the symptom you saw, the rule it broke, and what to do instead. Then let it run.",
        "examples": [
            {"before": "no that's not right, try again", "after": "The retry loop catches the exception but never re-raises after the last attempt, so failures look like successes. Re-raise the original error once retries run out, and keep the existing backoff."},
            {"before": "this is wrong, fix the test", "after": "The test passes because you mocked the function under test instead of the network call. Don't mock get_user — mock requests.get inside it, and assert it was called with the real URL."},
        ],
        "practice": "Before sending a correction, check it names both the symptom and the rule. If it only says “no,” add the missing half.",
        "good_looks_like": "One sharp correction — symptom, rule, and the fix — and the agent lands it on the next try.",
    },
    "Toolcraft": {
        "what_it_is": "Letting the agent use the right tool for each step — searching the code, running commands, starting the app, working in the background — instead of forcing everything through chat.",
        "why_it_matters": "The agent works faster and more reliably when it searches and runs things for real, rather than reasoning about the code from memory.",
        "how_to_improve": "Tell the agent which action to take first — search the codebase, run the suite, start the server — so it gathers facts and checks its work with the tool built for each step.",
        "examples": [
            {"before": "How does login work in this app?", "after": "Search the codebase for the login flow (grep for auth, session, login), read the files you find, then explain how a request goes from form submit to a logged-in session."},
            {"before": "Add a retry to the API client, and make sure the tests still pass.", "after": "Add retry-with-backoff to the API client. Then run the suite in the background; if anything fails, read the failure, fix it, and report back when it's green."},
        ],
        "practice": "Add one line to your next task telling the agent which action to take first: “search for…”, “run the tests”, or “start the server and check.”",
        "good_looks_like": "You hand off a whole job and the agent searches, edits, runs, and verifies on its own — each step using the tool made for it.",
    },
}

BANDS = [
    ("Operator", 0, 39, "You use the agent as fast hands. Prompts are short and underspecified, "
     "edits often happen without reading the file first, and changes are rarely verified. The "
     "fastest gains live right here: state a goal plus one constraint, and let the agent read "
     "before it edits."),
    ("Developing", 40, 54, "Real back-and-forth is emerging and one or two habits are solid. Some "
     "prompts carry a file path or a constraint; verification happens occasionally. The gap to the "
     "next level is consistency — doing the right thing by default, not just sometimes."),
    ("Proficient", 55, 69, "You drive the agent deliberately. Most prompts are specific, edits "
     "usually follow a read of the same file, and you verify more often than not. Solid, reliable "
     "AI-assisted engineering. Remaining gains are about altitude (saying why) and orchestration."),
    ("Advanced", 70, 84, "You orchestrate rather than operate. Prompts encode goals, constraints "
     "and acceptance criteria; reading precedes editing as a habit; verification is near-automatic; "
     "you use planning and delegation fluently. You brief the agent like a senior teammate."),
    ("Expert", 85, 100, "You treat the agent as a managed engineering system: consistently "
     "high-context prompts with explicit success criteria, disciplined read→edit→verify loops, "
     "deliberate delegation, and almost no wasted correction cycles."),
]

# Archetype axes and prototypes.
# The archetype describes YOUR DRIVING STYLE, so it is built only from signals you
# control and DISCOUNTS the habits Claude does on its own. Verification and Context
# (read-before-edit, running tests) are largely the agent's defaults, so they carry
# low "agency" weight; how you brief (Direction), correct (Iteration), reach for tools
# (Toolcraft) and hand off work (Delegation) carry full weight.
ARCHETYPE_AXES = ["Direction", "Verification", "Context", "Iteration", "Toolcraft", "Delegation"]
AGENCY = {"Direction": 1.0, "Verification": 0.35, "Context": 0.15,
          "Iteration": 1.0, "Toolcraft": 0.8, "Delegation": 1.0}

# Prototype vectors over ARCHETYPE_AXES (0-100). Delegation is the axis that separates
# a hands-off delegator from a hands-on builder. These are the five explicit, recognizable
# builder archetypes; the classifier picks the nearest one from your AGENCY-WEIGHTED vector.
PROTOTYPES = {
    "Autonomous Agent": {"emoji": "🤖", "vec": [58, 65, 62, 62, 85, 96],
        "blurb": "You delegate whole, end-to-end jobs and trust the agent to run them — you set the outcome and let Claude pick the steps."},
    "Architect":        {"emoji": "🏗️", "vec": [80, 66, 88, 65, 60, 48],
        "blurb": "You plan and explore before you build — you read and design first, so changes land on a clear structure."},
    "Debugger":         {"emoji": "🐛", "vec": [62, 88, 82, 85, 60, 28],
        "blurb": "You hunt problems methodically — read to diagnose, change, verify, and repeat until it's truly fixed."},
    "Collaborator":     {"emoji": "🤝", "vec": [66, 62, 66, 80, 55, 38],
        "blurb": "You work with the agent like a teammate — ask for options, give feedback, and steer toward alignment."},
    "Sprinter":         {"emoji": "⚡", "vec": [45, 38, 52, 46, 62, 30],
        "blurb": "You move fast and direct — terse prompts, quick turns, low ceremony. Great velocity; briefing and verification are the growth edges."},
}
ARCHETYPE_MARGIN = 0.06   # cosine-similarity margin below which we emit a blended label


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _text_of(content):
    """Concatenate the text blocks of a message content (str or list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _is_tool_result(content):
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _looks_injected(text):
    head = text[:200].lstrip()
    if len(text) > MAX_HUMAN_PROMPT_CHARS:
        return True
    if _INJECTED_HEAD.match(head):
        return True
    low = text.lower()
    return any(m in low for m in INJECTION_MARKERS)


def _denamespace_tool(name):
    """mcp__<hash>__slack_read_thread -> slack_read_thread; keep core names as-is."""
    if name.startswith("mcp__"):
        parts = name.split("__")
        return parts[-1] if parts else name
    return name


def _normalize_path(p):
    """Strip machine-identifying home prefixes from a PATH so nothing personal leaks into
    the corpus/evidence: /Users/<name>/x -> ~/x, bare /Users/<name> -> ~ (else basename
    would surface the username). Sources whose logs embed absolute paths (Codex diffs,
    Cursor tool args) must run every path through this before emitting an event."""
    if not p or not isinstance(p, str):
        return p
    p = re.sub(r"^(?:/Users/|/home/)[^/]+(?=/|$)", "~", p)
    p = re.sub(r"^[A-Za-z]:\\Users\\[^\\]+(?=\\|$)", "~", p)
    return p


# Redact machine-identifying home paths from free text before it is shown in the report or
# written to the evidence bundle. Applied only at PRESENTATION, never to the scored corpus,
# so scores stay byte-identical.
_HOME_PATH_RE = re.compile(r"(?:/Users/|/home/)[^/\s]+")
_WIN_HOME_RE = re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+")


def _scrub_paths(text):
    """/Users/<name>/x -> ~/x ; bare /Users/<name> -> ~ ; same for /home/<name> and Windows."""
    if not isinstance(text, str):
        return text
    text = _HOME_PATH_RE.sub("~", text)
    text = _WIN_HOME_RE.sub("~", text)
    return text


class Corpus:
    """Everything we measured from the transcripts, cleanly separated from scoring."""

    def __init__(self):
        self.files = 0
        self.projects = set()
        self.total_bytes = 0
        self.user_records = 0
        self.filtered = Counter()       # why user records were not counted as prompts
        self.real_prompts = []          # list of dicts: text, project, session, idx
        self.tool_usage = Counter()     # de-namespaced tool name -> count
        self.total_tool_calls = 0
        self.delegation_events = 0
        self.first_ts = None
        self.last_ts = None
        self.active_seconds = 0.0
        self.signals = Counter()        # source-specific extras (e.g. permission_denials)
        # Per-session ordered timelines of {"kind": "prompt"|"tool", ...}
        self.sessions = {}              # session_id -> {"project","timeline":[...]}
        self.session_ts = {}            # session_id -> earliest parsed timestamp
        self.session_caps = {}          # session_id -> optional capabilities dict (combined runs)


# Agent-to-agent transcripts (Claude Code subagents, Workflow runs) live under a
# ".../subagents/..." path. They are NOT the user's own prompts — counting them would
# contaminate the assessment and inflate counts every time a workflow is run — so they
# are excluded from discovery (an explicitly named single file is still honored).
_SUBAGENT_RE = re.compile(r"[/\\]subagents[/\\]")


def _filter_transcripts(paths):
    return [p for p in paths if not _SUBAGENT_RE.search(p)]


def discover_files(explicit):
    if explicit:
        p = os.path.expanduser(explicit)
        if os.path.isfile(p) and p.endswith(".jsonl"):
            return [p]
        if os.path.isdir(p):
            return _filter_transcripts(sorted(glob.glob(os.path.join(p, "**", "*.jsonl"), recursive=True)))
        return []
    env = os.environ.get("CLAUDE_PROJECTS_DIR")
    roots = [env] if env else DEFAULT_DIRS
    files = []
    for r in roots:
        rp = os.path.expanduser(r)
        if os.path.isdir(rp):
            files.extend(glob.glob(os.path.join(rp, "**", "*.jsonl"), recursive=True))
    return _filter_transcripts(sorted(set(files)))


def _is_cowork(path):
    """Cowork (Claude desktop agent-mode) sessions are always named audit.jsonl, while Claude
    Code sessions are named <session-uuid>.jsonl. The basename alone discriminates the two
    sources, and it keeps working after a file is mirrored into the persistent archive."""
    return os.path.basename(path) == "audit.jsonl"


def _session_key(path):
    """A stable per-session identity used to de-duplicate live + archived copies.

    Claude Code filenames ARE globally-unique session ids, so the basename is the key.
    Cowork files are ALL named audit.jsonl — the unique id is the enclosing per-session
    folder (e.g. local_<uuid>), which the archive mirrors, so the key survives archiving.
    The 'cowork:' prefix keeps the two namespaces from ever colliding."""
    if _is_cowork(path):
        parent = os.path.basename(os.path.dirname(path))
        return "cowork:" + (parent or "audit")
    return os.path.basename(path)


def _dedupe_sessions(files):
    """When the same session shows up in more than one root (the live ~/.claude/projects dir
    AND the persistent archive — possibly under a since-renamed project folder, a different-case
    path, or a synced copy from another machine), keep a single copy of it: the largest one,
    since transcripts only ever grow, so the biggest file is the most complete. Claude Code
    session filenames are globally-unique IDs (Cowork sessions key on their unique per-session
    folder instead — see _session_key), which is what makes the dedupe robust to all of the above."""
    best = {}
    for path in files:
        key = _session_key(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = -1
        cur = best.get(key)
        if cur is None or size > cur[0]:
            best[key] = (size, path)
    return sorted(p for _, p in best.values())


def archive_transcripts(live_files, archive_dir):
    """Copy live transcripts into a persistent archive so they survive Claude Code's
    `cleanupPeriodDays` deletion. Each file is mirrored to
    <archive>/<project folder>/<session>.jsonl. We copy only when the archived copy is
    missing or strictly smaller than the live one (transcripts only grow, so a >= archive copy
    is the more complete one and must never be overwritten with a smaller/equal one). We write
    via a temp file + atomic replace, re-checking the archive size just before the swap so a
    concurrent run can't clobber a larger copy, and always clean up the temp file.
    Returns (n_new, n_updated); a stderr note is printed if any file could not be archived."""
    arch_root = os.path.expanduser(archive_dir)
    new = updated = failed = 0
    for path in live_files:
        project = os.path.basename(os.path.dirname(path)) or "default"
        dest_dir = os.path.join(arch_root, project)
        dest = os.path.join(dest_dir, os.path.basename(path))
        try:
            live_size = os.path.getsize(path)
        except OSError:
            continue
        arch_size = os.path.getsize(dest) if os.path.exists(dest) else -1
        if arch_size >= live_size:
            continue  # already archived an equal-or-more-complete copy
        tmp = dest + ".tmp"
        try:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copyfile(path, tmp)
            # Another run may have grown the archive while we were copying — don't shrink it.
            current = os.path.getsize(dest) if os.path.exists(dest) else -1
            if current >= live_size:
                continue
            os.replace(tmp, dest)  # atomic; never leaves a half-written archive copy
        except OSError:
            failed += 1
            continue
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        if arch_size < 0:
            new += 1
        else:
            updated += 1
    if failed:
        print(f"  Note: {failed} transcript(s) could not be archived to {archive_dir} "
              f"(check permissions / disk space). They were still analyzed from disk.",
              file=sys.stderr)
    return new, updated


# --------------------------------------------------------------------------- #
# Source adapters
#
# Each source of transcripts (Claude Code today; Cowork/Codex/Cursor as they land) is a
# small adapter that normalizes its on-disk format into one event stream:
#
#   {"role": "session", "project": str, "session_id": str}   — starts a session
#   {"role": "ts",      "ts": <raw timestamp>}               — active-time tick
#   {"role": "user",    "text": str}                         — a real, human-typed prompt
#   {"role": "tool",    "name": str, "file": ..., "cmd": ..., "meta": {...}}
#   {"role": "drop",    "reason": str}                       — a de-contaminated user record
#
# De-contamination lives IN the adapter (each source knows its own noise); scoring
# consumes only the normalized stream, so the scorers never learn about sources.
# --------------------------------------------------------------------------- #

def _claude_tool_event(b):
    """Map one Claude-style tool_use block to a normalized tool event. `name` keeps its
    de-namespaced case for tool_usage; parse() lowercases it for the canonical vocabulary.
    mcp__<ns>__bash de-namespaces to 'bash', so its input.command is picked up for free."""
    raw = b.get("name", "unknown")
    name = _denamespace_tool(raw)
    inp = b.get("input", {}) if isinstance(b.get("input"), dict) else {}
    fpath = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
    cmd = inp.get("command") if name.lower() == "bash" else None
    meta = {}
    if name.lower() == "bash" and inp.get("run_in_background"):
        meta["background"] = True   # a hand-off: counts toward delegation
    return {"role": "tool", "name": name, "file": fpath, "cmd": cmd, "meta": meta}


class ClaudeCodeAdapter:
    """Claude Code — ~/.claude/projects/**/*.jsonl. The original (and reference) source."""

    name = "claude-code"
    archive_enabled = True
    capabilities = {"prompts": True, "edits": True, "verify": True,
                    "reads": True, "delegation": True}

    @staticmethod
    def detect():
        if os.environ.get("CLAUDE_PROJECTS_DIR"):
            return True
        return any(os.path.isdir(os.path.expanduser(d)) for d in DEFAULT_DIRS)

    @staticmethod
    def owns_file(path):
        return not _is_cowork(path)

    @staticmethod
    def discover(explicit):
        files = discover_files(explicit)
        if explicit:
            p = os.path.expanduser(explicit)
            if os.path.isfile(p):
                return files   # an explicitly named single file is always honored
        # A directory scan skips other sources' files (a Cowork audit.jsonl mirrored into
        # the archive, or sitting inside a user-supplied directory, is not Claude Code data).
        return [p for p in files if ClaudeCodeAdapter.owns_file(p)]

    @staticmethod
    def iter_events(path):
        # The header carries the project (parent-dir name) and session id (filename) and is
        # yielded before opening the file, so even an unreadable file still registers its project.
        project = os.path.basename(os.path.dirname(path)) or "default"
        session_id = os.path.splitext(os.path.basename(path))[0]
        yield {"role": "session", "project": project, "session_id": session_id}
        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            return
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = e.get("timestamp")
                if ts is not None:
                    yield {"role": "ts", "ts": ts}
                msg = e.get("message") if isinstance(e.get("message"), dict) else {}
                role = e.get("role") or msg.get("role") or e.get("type")
                content = msg.get("content", e.get("content"))

                if role == "assistant":
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                yield _claude_tool_event(b)
                    continue

                if role != "user":
                    continue
                if _is_tool_result(content):
                    yield {"role": "drop", "reason": "tool results"}
                    continue
                if e.get("isSidechain") is True:
                    yield {"role": "drop", "reason": "subagent turns"}
                    continue
                if e.get("isMeta") is True:
                    yield {"role": "drop", "reason": "meta-injected"}
                    continue
                text = _text_of(content).strip()
                if not text:
                    yield {"role": "drop", "reason": "empty"}
                    continue
                if _looks_injected(text):
                    yield {"role": "drop", "reason": "injected / pasted"}
                    continue
                yield {"role": "user", "text": text}


# --- Claude Desktop (agent mode / "Cowork") ------------------------------------------------ #

_DEFAULT_DESKTOP_ROOT = "~/Library/Application Support/Claude/local-agent-mode-sessions"
_DESKTOP_ROOT = _DEFAULT_DESKTOP_ROOT
_UPLOADED_RE = re.compile(r"<uploaded_files>.*?</uploaded_files>", re.S)


def _desktop_roots():
    """Candidate Cowork session directories across macOS, Linux, and Windows.
    When _DESKTOP_ROOT is overridden (e.g. in tests), only inspect that explicit path."""
    roots = [_DESKTOP_ROOT]
    if _DESKTOP_ROOT == _DEFAULT_DESKTOP_ROOT:
        roots.append("~/.config/Claude/local-agent-mode-sessions")
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.append(os.path.join(appdata, "Claude", "local-agent-mode-sessions"))
    return [os.path.expanduser(r) for r in roots]


def _strip_uploaded_files(text):
    """Cowork prepends an <uploaded_files> manifest to the prompt that attached them —
    harness text, not something the user typed."""
    if not text:
        return text
    return _UPLOADED_RE.sub("", text)


class ClaudeDesktopAdapter:
    """Claude Desktop agent-mode ("Cowork") sessions — .../local-agent-mode-sessions/**/audit.jsonl.
    Records share Claude Code's message/tool_use shape, so tool extraction is reused; the
    differences are the harness envelopes (system/result/rate_limit_event/tool_use_summary),
    `_audit_timestamp` as the clock, nested subagent turns inlined in the same file (marked by
    parent_tool_use_id), and the isReplay/isSynthetic de-contamination. Files carry an
    `_audit_hmac` integrity field — read-only, never rewrite them."""

    name = "claude-desktop"
    archive_enabled = True   # sessions archive under their unique local_<uuid> folder
    capabilities = {"prompts": True, "edits": True, "verify": True,
                    "reads": True, "delegation": True}

    @staticmethod
    def detect():
        return any(os.path.isdir(r) for r in _desktop_roots())

    @staticmethod
    def owns_file(path):
        return _is_cowork(path)

    @staticmethod
    def discover(explicit):
        if explicit:
            p = os.path.expanduser(explicit)
            if os.path.isfile(p) and p.endswith(".jsonl"):
                return [p]
            if os.path.isdir(p):
                return sorted(glob.glob(os.path.join(p, "**", "audit.jsonl"), recursive=True))
            return []
        files = []
        for root in _desktop_roots():
            if os.path.isdir(root):
                files.extend(glob.glob(os.path.join(root, "**", "audit.jsonl"), recursive=True))
        return sorted(set(files))

    @staticmethod
    def _project_of(path):
        """Project = basename of the session's cwd, read from the system/init record (usually
        the first line) — but only when the session ran in a real user folder. Cowork normally
        runs each session inside its own managed sandbox (…/local_<uuid>/outputs), which carries
        no project signal, so those label as flat 'claude-desktop'."""
        try:
            with open(path, encoding="utf-8") as fh:
                for _ in range(10):
                    line = fh.readline()
                    if not line:
                        break
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("type") == "system" and isinstance(e.get("cwd"), str) and e.get("cwd"):
                        cwd = e["cwd"].rstrip("/\\")
                        in_sandbox = any(r in cwd for r in _desktop_roots())
                        base = os.path.basename(cwd)
                        if in_sandbox or not base:
                            return "claude-desktop"
                        return base
        except OSError:
            pass
        return "claude-desktop"

    @staticmethod
    def iter_events(path):
        # One audit.jsonl == one session; the unique per-session directory is the session id.
        session_id = os.path.basename(os.path.dirname(path)) or os.path.splitext(os.path.basename(path))[0]
        yield {"role": "session", "project": ClaudeDesktopAdapter._project_of(path),
               "session_id": session_id}
        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            return
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = e.get("timestamp") or e.get("_audit_timestamp")
                if ts is not None:
                    yield {"role": "ts", "ts": ts}
                typ = e.get("type")
                if typ == "result":
                    pd = e.get("permission_denials")
                    if isinstance(pd, list) and pd:
                        # The user vetoing agent actions — a Discernment signal.
                        yield {"role": "signal", "name": "permission_denials", "value": len(pd)}
                    continue
                if typ in ("system", "rate_limit_event", "tool_use_summary"):
                    continue   # init/status envelopes and summaries — never prompts or actions
                if e.get("parent_tool_use_id"):
                    # A nested subagent turn inlined in the parent's file (Claude Code keeps
                    # these in separate subagents/ files; Cowork inlines them). Not the user's
                    # own prompting, and its tool calls aren't the top-level agent's either.
                    if typ == "user":
                        yield {"role": "drop", "reason": "subagent turns"}
                    continue
                msg = e.get("message") if isinstance(e.get("message"), dict) else {}
                role = msg.get("role") or typ
                content = msg.get("content")

                if role == "assistant":
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                yield _claude_tool_event(b)
                    continue

                if role != "user":
                    continue
                if _is_tool_result(content) or e.get("tool_use_result") is not None:
                    yield {"role": "drop", "reason": "tool results"}
                    continue
                if e.get("isReplay"):
                    yield {"role": "drop", "reason": "replays"}
                    continue
                if e.get("isSynthetic"):
                    yield {"role": "drop", "reason": "meta-injected"}
                    continue
                if e.get("isSidechain") or e.get("subagent_type"):
                    yield {"role": "drop", "reason": "subagent turns"}
                    continue
                text = _strip_uploaded_files(_text_of(content)).strip()
                if not text:
                    yield {"role": "drop", "reason": "empty"}
                    continue
                if _looks_injected(text):
                    yield {"role": "drop", "reason": "injected / pasted"}
                    continue
                yield {"role": "user", "text": text}


# --- OpenAI Codex (CLI + the post-2026-07-09 ChatGPT desktop app) --------------------------- #

_CODEX_ROOTS = ["~/.codex/sessions", "~/.codex/archived_sessions"]
# NEVER widen discovery beyond rollout-*.jsonl under these roots: ~/.codex/auth.json holds
# live OAuth tokens and config.toml can hold MCP credentials — they must not be ingested.

# Wrappers the Codex harness injects into otherwise-user-role text (not human-typed).
_CODEX_INJECT_PREFIXES = ("<environment_context>", "# agents.md instructions for",
                          "<turn_aborted>", "# in app browser:", "<image")


def _codex_args(a):
    """Codex tool arguments arrive as a JSON-encoded STRING (sometimes already a dict)."""
    if isinstance(a, dict):
        return a
    if isinstance(a, str):
        try:
            return json.loads(a)
        except (ValueError, TypeError):
            return {}
    return {}


def _codex_message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") in ("input_text", "text"))
    return ""


def _codex_is_injected(text):
    if _looks_injected(text):
        return True
    low = text.lstrip().lower()
    return any(low.startswith(p) for p in _CODEX_INJECT_PREFIXES)


def _codex_apply_patch_events(patch):
    """A unified-diff string -> one edit/write event per file the patch touches."""
    out = []
    if not isinstance(patch, str):
        return out
    for line in patch.splitlines():
        s = line.strip()
        for marker, canon in (("*** Add File:", "write"),
                              ("*** Update File:", "edit"),
                              ("*** Delete File:", "edit")):
            if s.startswith(marker):
                out.append({"role": "tool", "name": canon,
                            "file": _normalize_path(s[len(marker):].strip()),
                            "cmd": None, "meta": {}})
                break
    return out


def _codex_project(repo, cwd):
    if isinstance(repo, str) and repo:
        base = repo.rstrip("/").split("/")[-1]
        if base.endswith(".git"):
            base = base[:-4]
        if base:
            return base
    if isinstance(cwd, str) and cwd:
        return os.path.basename(cwd.rstrip("/")) or "codex"
    return "codex"


def _codex_response_events(payload):
    """Map one Codex `response_item` payload to a list of normalized events (possibly empty)."""
    ptype = payload.get("type")
    if ptype == "message":
        role = payload.get("role")
        if role == "user":
            text = _codex_message_text(payload.get("content")).strip()
            if not text:
                return [{"role": "drop", "reason": "empty"}]
            if _codex_is_injected(text):
                return [{"role": "drop", "reason": "injected / pasted"}]
            return [{"role": "user", "text": text}]
        if role in ("developer", "system"):
            return [{"role": "drop", "reason": "system"}]   # harness prose; counted for transparency
        return []   # assistant prose -> skipped (not a prompt, not an action)
    if ptype == "function_call":
        name = (payload.get("name") or "").strip()
        args = _codex_args(payload.get("arguments"))
        if name == "exec_command":
            return [{"role": "tool", "name": "bash", "file": None,
                     "cmd": args.get("cmd") or args.get("command"),
                     "meta": {"workdir": args.get("workdir")}}]
        if name == "update_plan":
            return [{"role": "tool", "name": "enterplanmode", "file": None, "cmd": None, "meta": {}}]
        if name:
            fpath = _normalize_path(args.get("path")) if isinstance(args, dict) else None
            return [{"role": "tool", "name": name.lower(), "file": fpath, "cmd": None, "meta": {}}]
        return []
    if ptype == "custom_tool_call":
        name = (payload.get("name") or "").strip()
        if name == "apply_patch":
            return _codex_apply_patch_events(payload.get("input", ""))
        if name:
            return [{"role": "tool", "name": name.lower(), "file": None, "cmd": None, "meta": {}}]
        return []
    if ptype == "web_search_call":
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        return [{"role": "tool", "name": "web_search", "file": None, "cmd": None,
                 "meta": {"query": action.get("query")}}]
    if ptype == "tool_search_call":
        return [{"role": "tool", "name": "tool_search", "file": None, "cmd": None, "meta": {}}]
    if ptype == "image_generation_call":
        return [{"role": "tool", "name": "image_generation", "file": None, "cmd": None, "meta": {}}]
    return []   # reasoning / *_output / unknown payloads -> ignored (reasoning is ciphertext by design)


class CodexAdapter:
    """OpenAI Codex — ~/.codex/sessions/**/rollout-*.jsonl, written by both the Codex CLI and
    the ChatGPT desktop app (which IS the Codex app since 2026-07-09). We read the
    `response_item` stream only (the `event_msg` stream is parallel telemetry — using both
    double-counts). `reads` is False: Codex has no read tool (files are read via shell), so
    read-before-edit grounding (Context) is not reliably observable and is honestly marked
    not-measurable rather than scored."""

    name = "codex"
    archive_enabled = False   # date-foldered and not auto-deleted; nothing to preserve
    capabilities = {"prompts": True, "edits": True, "verify": True,
                    "reads": False, "delegation": True}

    @staticmethod
    def detect():
        return any(os.path.isdir(os.path.expanduser(r)) for r in _CODEX_ROOTS)

    @staticmethod
    def owns_file(path):
        return os.path.basename(path).startswith("rollout-")

    @staticmethod
    def discover(explicit):
        if explicit:
            p = os.path.expanduser(explicit)
            if os.path.isfile(p) and p.endswith(".jsonl"):
                return [p]
            if os.path.isdir(p):
                got = sorted(glob.glob(os.path.join(p, "**", "rollout-*.jsonl"), recursive=True))
                return got or sorted(glob.glob(os.path.join(p, "**", "*.jsonl"), recursive=True))
            return []
        files = []
        for r in _CODEX_ROOTS:
            rp = os.path.expanduser(r)
            if os.path.isdir(rp):
                files.extend(glob.glob(os.path.join(rp, "**", "rollout-*.jsonl"), recursive=True))
        return sorted(set(files))

    @staticmethod
    def iter_events(path):
        session_id = os.path.splitext(os.path.basename(path))[0]
        header = None
        pending = []   # events seen before session_meta is known -> attributed to the real session

        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            yield {"role": "session", "project": "codex", "session_id": session_id}
            return
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                typ = e.get("type")
                payload = e.get("payload") if isinstance(e.get("payload"), dict) else {}
                ts = e.get("timestamp")

                if typ == "session_meta":
                    git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
                    repo = git.get("repository_url") if git else None
                    header = {"role": "session",
                              "project": _codex_project(repo, payload.get("cwd")),
                              "session_id": payload.get("id") or session_id}
                    yield header
                    if ts is not None:
                        yield {"role": "ts", "ts": ts}
                    for ev in pending:   # records seen before the header belong to this session
                        yield ev
                    pending = []
                    continue

                evs = []
                if ts is not None:
                    evs.append({"role": "ts", "ts": ts})
                if typ == "response_item":
                    evs.extend(_codex_response_events(payload))
                # turn_context / event_msg / compacted contribute only their timestamp (active time)
                if header is None:
                    pending.extend(evs)
                else:
                    for ev in evs:
                        yield ev
        if header is None:
            # no session_meta anywhere -> fall back to a default header, then the buffered events
            yield {"role": "session", "project": "codex", "session_id": session_id}
            for ev in pending:
                yield ev


# --- Cursor IDE ----------------------------------------------------------------------------- #

_DEFAULT_CURSOR_GLOBAL = "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb"
_DEFAULT_CURSOR_WORKSPACES = "~/Library/Application Support/Cursor/User/workspaceStorage"
_CURSOR_GLOBAL = _DEFAULT_CURSOR_GLOBAL
_CURSOR_WORKSPACES = _DEFAULT_CURSOR_WORKSPACES


def _cursor_global_paths():
    """Candidate Cursor globalStorage DBs across macOS, Linux, and Windows.
    When _CURSOR_GLOBAL is overridden (e.g. in tests), only inspect that explicit path."""
    paths = [_CURSOR_GLOBAL]
    if _CURSOR_GLOBAL == _DEFAULT_CURSOR_GLOBAL:
        paths.append("~/.config/Cursor/User/globalStorage/state.vscdb")
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.append(os.path.join(appdata, "Cursor", "User", "globalStorage", "state.vscdb"))
    return [os.path.expanduser(p) for p in paths]


def _cursor_workspace_dirs():
    """Candidate Cursor workspaceStorage roots across macOS, Linux, and Windows."""
    dirs = [_CURSOR_WORKSPACES]
    if _CURSOR_WORKSPACES == _DEFAULT_CURSOR_WORKSPACES:
        dirs.append("~/.config/Cursor/User/workspaceStorage")
        appdata = os.environ.get("APPDATA")
        if appdata:
            dirs.append(os.path.join(appdata, "Cursor", "User", "workspaceStorage"))
    return [os.path.expanduser(d) for d in dirs]


class CursorAdapter:
    """Cursor IDE — SQLite state.vscdb. The global DB's cursorDiskKV table holds composer
    sessions (composerData:<id>) and messages (bubbleId:<composerId>:<bubbleId>); Cursor 3.x
    adds a composerHeaders SQL table (the cheap enumeration path, with isSubagent flags);
    older per-workspace DBs use ItemTable/aiService.*. The live DB can be tens of GB and
    WAL-mode while Cursor is open, so we COPY it first and open the copy strictly read-only.
    The agentKv:blob:* family (3.x runtime state, part binary) is deliberately ignored —
    bubbles remain the message store; if that ever migrates, parsing degrades to fewer
    sessions rather than crashing (every key is treated as optional)."""

    name = "cursor"
    archive_enabled = False   # cumulative store with no TTL; nothing to preserve
    capabilities = {"prompts": True, "edits": True, "verify": True,
                    "reads": True, "delegation": True}

    @staticmethod
    def capabilities_observed(corpus):
        """Chat-only Cursor data (no agent/composer tool events at all) can't show edits,
        verification, grounding, or tool breadth — mask them rather than score silence."""
        if corpus.total_tool_calls:
            return CursorAdapter.capabilities
        return {"prompts": True, "edits": False, "verify": False,
                "reads": False, "delegation": False}

    @staticmethod
    def detect():
        if any(os.path.exists(g) for g in _cursor_global_paths()):
            return True
        return any(glob.glob(os.path.join(ws, "*", "state.vscdb")) for ws in _cursor_workspace_dirs())

    @staticmethod
    def discover(explicit):
        if explicit:
            p = os.path.expanduser(explicit)
            if os.path.isfile(p) and p.endswith(".vscdb"):
                return [p]
            if os.path.isdir(p):
                return sorted(glob.glob(os.path.join(p, "**", "state.vscdb"), recursive=True))
            return []
        out = []
        for g in _cursor_global_paths():
            if os.path.exists(g) and g not in out:
                out.append(g)
        ws_dbs = []
        for ws in _cursor_workspace_dirs():
            ws_dbs.extend(glob.glob(os.path.join(ws, "*", "state.vscdb")))
        out.extend(sorted(set(ws_dbs)))
        return out

    @staticmethod
    def iter_events(path):
        tmpdir = tempfile.mkdtemp(prefix="insight-cursor-")
        tmpdb = os.path.join(tmpdir, "state.vscdb")
        conn = None
        try:
            try:
                shutil.copyfile(path, tmpdb)
                # The live DB is WAL-mode while Cursor is open: recently-committed rows live
                # in the -wal sidecar, not the main file. Copy the sidecars too (and DON'T use
                # immutable=1, which makes SQLite ignore the WAL) so a running Cursor's
                # newest history is still read. Verified failure mode: the .vscdb alone can
                # look empty/stale.
                for sfx in ("-wal", "-shm"):
                    side = path + sfx
                    if os.path.exists(side):
                        try:
                            shutil.copyfile(side, tmpdb + sfx)
                        except OSError:
                            pass
            except OSError:
                return
            try:
                conn = sqlite3.connect(f"file:{tmpdb}?mode=ro", uri=True)
            except sqlite3.Error:
                return
            try:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            except sqlite3.Error:
                return
            if "cursorDiskKV" in tables:
                yield from CursorAdapter._iter_disk_kv(conn, "composerHeaders" in tables)
            elif "ItemTable" in tables:
                yield from CursorAdapter._iter_item_table(conn, path)
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def _composer_rows(conn, has_headers):
        """Yield (composer_id, composerData dict) for every top-level (non-subagent) session.
        Cursor 3.x: enumerate via the composerHeaders table (has an isSubagent flag). Older:
        scan composerData: keys and exclude any id another composer lists as a sub-composer."""
        inner = conn.cursor()
        if has_headers:
            try:
                ids = [r[0] for r in conn.execute(
                    "SELECT composerId FROM composerHeaders "
                    "WHERE COALESCE(isSubagent, 0) = 0 ORDER BY COALESCE(createdAt, 0)")]
            except sqlite3.Error:
                ids = None
            if ids is not None:
                for cid in ids:
                    try:
                        row = inner.execute("SELECT value FROM cursorDiskKV WHERE key = ? LIMIT 1",
                                            (f"composerData:{cid}",)).fetchone()
                    except sqlite3.Error:
                        continue
                    if not row:
                        continue
                    try:
                        yield cid, json.loads(row[0])
                    except (ValueError, TypeError):
                        continue
                return
        # Fallback: no headers table (pre-3.x) — materialize the composer list (dozens of
        # rows; the heavy data is in bubbles) and exclude ids referenced as sub-composers.
        try:
            rows = conn.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'").fetchall()
        except sqlite3.Error:
            return
        parsed = []
        subagents = set()
        for key, value in rows:
            try:
                data = json.loads(value)
            except (ValueError, TypeError):
                continue
            cid = key.split(":", 1)[1] if ":" in key else key
            parsed.append((cid, data))
            for field in ("subagentComposerIds", "subComposerIds"):
                v = data.get(field)
                if isinstance(v, list):
                    subagents.update(x for x in v if isinstance(x, str))
        for cid, data in parsed:
            if cid in subagents:
                continue
            yield cid, data

    @staticmethod
    def _composer_project(data):
        wsi = data.get("workspaceIdentifier")
        if isinstance(wsi, dict):
            uri = wsi.get("uri")
            fsp = uri.get("fsPath") if isinstance(uri, dict) else None
            if isinstance(fsp, str) and fsp:
                base = os.path.basename(fsp.rstrip("/\\"))
                if base:
                    return base
        return "cursor"

    @staticmethod
    def _iter_disk_kv(conn, has_headers):
        inner = conn.cursor()
        for composer_id, data in CursorAdapter._composer_rows(conn, has_headers):
            yield {"role": "session", "project": CursorAdapter._composer_project(data),
                   "session_id": composer_id}
            headers = data.get("fullConversationHeadersOnly") or data.get("conversation") or []
            bubble_ids = [h["bubbleId"] for h in headers
                          if isinstance(h, dict) and h.get("bubbleId")] if isinstance(headers, list) else []
            if not bubble_ids:
                try:
                    brows = inner.execute("SELECT key FROM cursorDiskKV WHERE key LIKE ?",
                                          (f"bubbleId:{composer_id}:%",)).fetchall()
                    bubble_ids = [k.split(":")[-1] for (k,) in brows]
                except sqlite3.Error:
                    bubble_ids = []
            for bid in bubble_ids:
                try:
                    brow = inner.execute("SELECT value FROM cursorDiskKV WHERE key = ? LIMIT 1",
                                         (f"bubbleId:{composer_id}:{bid}",)).fetchone()
                except sqlite3.Error:
                    continue
                if not brow:
                    continue
                try:
                    bubble = json.loads(brow[0])
                except (ValueError, TypeError):
                    continue
                yield from CursorAdapter._bubble_events(bubble)

    @staticmethod
    def _iter_item_table(conn, path):
        # Legacy pre-Composer chat panel (workspace ItemTable / aiService.prompts): prompts
        # only — no tool calls, no timestamps. Kept as a fallback for old installs.
        sid = os.path.basename(os.path.dirname(path)) or "cursor-workspace"
        yield {"role": "session", "project": "cursor", "session_id": sid}
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = 'aiService.prompts' LIMIT 1").fetchone()
        except sqlite3.Error:
            return
        if not row:
            return
        try:
            prompts = json.loads(row[0])
        except (ValueError, TypeError):
            return
        if not isinstance(prompts, list):
            return
        for p in prompts:
            text = (p.get("text") if isinstance(p, dict) else str(p)) or ""
            text = text.strip()
            if not text:
                continue
            if _looks_injected(text):
                yield {"role": "drop", "reason": "injected / pasted"}
                continue
            yield {"role": "user", "text": text}

    @staticmethod
    def _bubble_events(bubble):
        if not isinstance(bubble, dict):
            return
        ts = bubble.get("createdAt")
        if ts:
            yield {"role": "ts", "ts": ts}
        if bubble.get("type") == 1:   # user message
            text = (bubble.get("text") or "").strip()
            if not text:
                return
            if _looks_injected(text):
                yield {"role": "drop", "reason": "injected / pasted"}
                return
            yield {"role": "user", "text": text}
            return
        tfd = bubble.get("toolFormerData")
        if isinstance(tfd, dict):
            canon, fpath, cmd = CursorAdapter._map_tool(tfd.get("name") or tfd.get("toolName") or "", tfd)
            meta = {}
            decision = tfd.get("userDecision")
            if decision:
                meta["decision"] = decision
            yield {"role": "tool", "name": canon, "file": fpath, "cmd": cmd, "meta": meta}
            if decision == "rejected":
                # The user vetoing an agent action — a Discernment signal.
                yield {"role": "signal", "name": "tool_rejections", "value": 1}

    @staticmethod
    def _map_tool(name, tfd):
        n = (name or "").lower()
        params = tfd.get("params") or tfd.get("rawArgs") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (ValueError, TypeError):
                params = {}
        if not isinstance(params, dict):
            params = {}
        f = params.get("target_file") or params.get("path") or params.get("file_path")
        if n in ("run_terminal_cmd", "run_terminal_command", "terminal"):
            return "bash", None, params.get("command")
        if n in ("read_file", "read"):
            return "read", _normalize_path(f), None
        if n in ("write", "write_file", "create_file"):
            return "write", _normalize_path(f), None
        if n in ("edit_file", "apply_diff", "search_replace", "edit", "str_replace", "multi_edit"):
            return "edit", _normalize_path(f), None
        return (n or "tool"), _normalize_path(f), None


# Registry of available sources. Order matters for auto-detection (claude-code first).
ADAPTERS = {a.name: a for a in (ClaudeCodeAdapter, ClaudeDesktopAdapter, CodexAdapter, CursorAdapter)}


def get_adapter(name):
    return ADAPTERS.get(name)


def detect_adapter():
    """First source whose data is present on this machine; claude-code wins ties."""
    for a in ADAPTERS.values():
        try:
            if a.detect():
                return a
        except Exception:
            continue
    return ClaudeCodeAdapter


def parse(files, adapter=None):
    """Build a Corpus from `files` using `adapter` (default: Claude Code). Source-agnostic:
    it consumes the adapter's normalized event stream and accounts every field the scorers
    depend on, so adding a source never touches this function or the scorers."""
    if adapter is None:
        adapter = ClaudeCodeAdapter
    c = Corpus()
    c.files = len(files)

    def _flush(cur):
        # Gap-capped active time is computed per session window; a new `session` header
        # flushes the prior one. Single-header sources (one session per file) flush once at
        # EOF, so their numbers are identical to the old per-file computation.
        ts_in = cur["ts"]
        if len(ts_in) >= 2:
            ts_in.sort()
            c.active_seconds += sum(
                min((ts_in[i + 1] - ts_in[i]).total_seconds(), GAP_CAP_SECONDS)
                for i in range(len(ts_in) - 1)
            )
        elif ts_in:
            ts_in.sort()
        if cur["timeline"]:
            c.sessions[cur["session_id"]] = {"project": cur["project"], "timeline": cur["timeline"]}
            if ts_in:
                c.session_ts[cur["session_id"]] = ts_in[0]

    for path in files:
        try:
            c.total_bytes += os.path.getsize(path)
        except OSError:
            pass
        cur = {"project": "default",
               "session_id": os.path.splitext(os.path.basename(path))[0],
               "timeline": [], "ts": [], "idx": 0}
        for ev in adapter.iter_events(path):
            role = ev.get("role")
            if role == "session":
                _flush(cur)
                cur = {"project": ev.get("project") or "default",
                       "session_id": ev.get("session_id") or cur["session_id"],
                       "timeline": [], "ts": [], "idx": 0}
                c.projects.add(cur["project"])
                continue
            if role == "ts":
                ts = _parse_ts(ev.get("ts"))
                if ts:
                    cur["ts"].append(ts)
                    c.first_ts = ts if c.first_ts is None or ts < c.first_ts else c.first_ts
                    c.last_ts = ts if c.last_ts is None or ts > c.last_ts else c.last_ts
                continue
            if role == "signal":
                c.signals[ev.get("name", "signal")] += int(ev.get("value", 1) or 0)
                continue
            if role == "drop":
                c.user_records += 1
                c.filtered[ev.get("reason", "filtered")] += 1
                continue
            if role == "user":
                c.user_records += 1
                cur["idx"] += 1
                text = ev.get("text", "")
                rec = {"text": text, "project": cur["project"],
                       "session": cur["session_id"], "idx": cur["idx"]}
                c.real_prompts.append(rec)
                cur["timeline"].append({"kind": "prompt", "text": text, "rec": rec})
                continue
            if role == "tool":
                name = ev.get("name", "unknown")
                c.tool_usage[name] += 1
                c.total_tool_calls += 1
                lname = name.lower()
                if lname in DELEGATION_TOOLS:
                    c.delegation_events += 1
                if isinstance(ev.get("meta"), dict) and ev["meta"].get("background"):
                    c.delegation_events += 1
                cur["timeline"].append({"kind": "tool", "name": lname,
                                        "file": ev.get("file"), "cmd": ev.get("cmd")})
                continue
        _flush(cur)
    return c


# --------------------------------------------------------------------------- #
# Scoring helpers
# --------------------------------------------------------------------------- #

def squash(x, target):
    """Saturating curve: hitting `target` maxes the signal; exceeding adds nothing."""
    if target <= 0:
        return 0.0
    return max(0.0, min(1.0, x / target))


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _run_fingerprint(corpus):
    """A stable hash of THIS run's de-contaminated prompt set. It binds an AI analysis
    (the Opus-stage skill map) to the exact data it was written from, so a stale or
    foreign ``analysis.json`` — e.g. left over from a previous run or another person on a
    machine that reuses the fixed ``~/.claude/insight/`` paths — carries a different
    fingerprint and is refused at merge time. This is what stops one person's written
    verdict from ever rendering inside someone else's report."""
    h = hashlib.sha256()
    for p in sorted(corpus.real_prompts, key=lambda r: (r["session"], r["idx"])):
        h.update(f"{p['session']}\x1f{p['idx']}\x1f{p['text']}\x1e".encode("utf-8"))
    h.update(f"|n={len(corpus.real_prompts)}".encode("utf-8"))
    return h.hexdigest()[:16]


def _is_action_prompt(text):
    return bool(ACTION_VERB.search(text))


# --------------------------------------------------------------------------- #
# The five dimensions — each returns (score_0_100, detail_dict, evidence_list)
# --------------------------------------------------------------------------- #

def score_direction(corpus):
    prompts = corpus.real_prompts
    n = len(prompts)
    if n == 0:
        return 0.0, {"n": 0}, []
    constraint = artifact = intent = 0
    weak_examples = []
    for p in prompts:
        t = p["text"]
        has_artifact = bool(ARTIFACT_RE.search(t))
        has_constraint = bool(CONSTRAINT_CUE.search(t) and ACTION_VERB.search(t))
        has_intent = bool(INTENT_CUE.search(t))
        artifact += 1 if has_artifact else 0
        constraint += 1 if has_constraint else 0
        intent += 1 if has_intent else 0
        if _is_action_prompt(t) and not (has_artifact or has_constraint or has_intent) and len(t) < 120:
            weak_examples.append(p)
    constraint_rate = constraint / n
    artifact_rate = artifact / n
    intent_rate = intent / n
    # front-loading: penalize rules first revealed via a high-info correction
    corr = _find_corrections(corpus)
    new_rule_corrections = sum(1 for x in corr if x["high_info"])
    action_prompts = max(1, sum(1 for p in prompts if _is_action_prompt(p["text"])))
    front_loading = 1 - clamp(new_rule_corrections / action_prompts, 0, 1)
    score = 100 * (
        0.30 * squash(constraint_rate, 0.45)
        + 0.20 * squash(artifact_rate, 0.45)
        + 0.25 * squash(intent_rate, 0.30)
        + 0.25 * front_loading
    )
    detail = {
        "n": n, "constraint_rate": constraint_rate, "artifact_rate": artifact_rate,
        "intent_rate": intent_rate, "front_loading": front_loading,
    }
    return score, detail, weak_examples[:6]


def _iter_sessions(corpus):
    for sid, s in corpus.sessions.items():
        yield sid, s["project"], s["timeline"]


def _find_corrections(corpus):
    """Correction turns: short rejections that follow an assistant action, praise-guarded."""
    out = []
    sess_src = getattr(corpus, "session_source", None) or {}
    for sid, project, timeline in _iter_sessions(corpus):
        saw_tool = False
        for ev in timeline:
            if ev["kind"] == "tool":
                saw_tool = True
                continue
            t = ev["text"]
            head = t[:160]
            if CORRECTION_CUE.search(head) and not PRAISE_CUE.search(head) and saw_tool:
                high_info = bool(
                    re.search(r"\d", t) or ARTIFACT_RE.search(t) or len(t.split()) >= 8
                    or INTENT_CUE.search(t)
                )
                item = {"session": sid, "project": project, "text": t, "high_info": high_info}
                src = (ev.get("rec") or {}).get("source") or sess_src.get(sid)
                if src:
                    item["source"] = src
                out.append(item)
            saw_tool = False  # reset: correction must directly follow an action turn
    return out


def score_iteration(corpus):
    prompts = corpus.real_prompts
    n = len(prompts)
    corr = _find_corrections(corpus)
    k = len(corr)
    if n == 0:
        return 50.0, {"n": 0, "corrections": 0}, []
    rate = k / n
    specificity = (sum(1 for x in corr if x["high_info"]) / k) if k else 1.0
    score = 100 * (0.6 * (1 - clamp(rate / CORRECTION_RATE_CEILING, 0, 1)) + 0.4 * specificity)
    low_info = [x for x in corr if not x["high_info"]]
    # Confidence is keyed on prompt count n (the opportunity count), NOT correction count k:
    # a user with many clean prompts and zero corrections has STRONG evidence of good iteration,
    # so it must not be shrunk toward 50 as if it were "no data".
    detail = {"n": n, "corrections": k, "correction_rate": rate, "specificity": specificity}
    return score, detail, low_info[:4]


def score_context(corpus):
    total_edits = 0
    grounded = 0
    blind_examples = []
    sess_src = getattr(corpus, "session_source", None) or {}
    for sid, project, timeline in _iter_sessions(corpus):
        read_paths = set()
        edited_paths = set()
        written_paths = set()   # files the agent authored this session (grounded to edit)
        for ev in timeline:
            if ev["kind"] != "tool":
                continue
            name, fpath = ev["name"], ev.get("file")
            if name in READ_TOOLS and fpath:
                read_paths.add(fpath)
            elif name in EDIT_TOOLS:
                total_edits += 1
                if not fpath:
                    grounded += 1  # can't attribute; don't penalize
                    continue
                is_new_write = (name == "write" and fpath not in read_paths and fpath not in edited_paths)
                # grounded if it was read, OR authored earlier this session, OR is being created now
                if fpath in read_paths or fpath in written_paths or is_new_write:
                    grounded += 1
                else:
                    item = {"session": sid, "project": project, "file": fpath}
                    if sid in sess_src:
                        item["source"] = sess_src[sid]
                    blind_examples.append(item)
                if name == "write":
                    written_paths.add(fpath)
                edited_paths.add(fpath)
    if total_edits == 0:
        return 50.0, {"n": 0, "grounded": 0, "total_edits": 0, "rate": None}, []
    rate = grounded / total_edits
    score = 100 * squash(rate, 0.85)
    return score, {"n": total_edits, "grounded": grounded, "total_edits": total_edits, "rate": rate}, blind_examples[:4]


def score_verification(corpus):
    episodes = 0
    verified = 0
    teardown_bonus = 0
    unverified_examples = []
    sess_src = getattr(corpus, "session_source", None) or {}
    for sid, project, timeline in _iter_sessions(corpus):
        open_ep = False
        ep_files = []
        for ev in timeline:
            if ev["kind"] == "prompt":
                # a "run it / does it work / confirm" prompt verifies an open episode
                if open_ep and re.search(r"\b(run it|does it work|confirm|check (it|that)|verify|did it work)\b",
                                         ev["text"], re.I):
                    verified += 1
                    open_ep = False
                continue
            name = ev["name"]
            cmd = ev.get("cmd") or ""
            if name in EDIT_TOOLS:
                if not open_ep:
                    open_ep = True
                    episodes += 1
                    ep_files = []
                if ev.get("file"):
                    ep_files.append(os.path.basename(ev["file"]))
            elif name == "bash":
                if TEARDOWN_RE.search(cmd):
                    teardown_bonus = 5
                if open_ep and VERIFY_RE.search(cmd):
                    verified += 1
                    open_ep = False
            elif name in READ_TOOLS and open_ep and ev.get("file") and os.path.basename(ev["file"]) in ep_files:
                # re-reading the just-edited file is a (weak) check
                verified += 1
                open_ep = False
        if open_ep:
            item = {"session": sid, "project": project,
                    "files": ", ".join(sorted(set(ep_files))[:3]) or "files"}
            if sid in sess_src:
                item["source"] = sess_src[sid]
            unverified_examples.append(item)
    if episodes == 0:
        return 50.0, {"n": 0, "episodes": 0, "verified": 0, "rate": None}, []
    rate = verified / episodes
    score = min(100, 100 * squash(rate, 0.60) + teardown_bonus)
    return score, {"n": episodes, "episodes": episodes, "verified": verified, "rate": rate,
                   "teardown_bonus": teardown_bonus}, unverified_examples[:4]


def score_toolcraft(corpus):
    total = corpus.total_tool_calls
    if total == 0:
        return 0.0, {"n": 0, "distinct": 0, "evenness": 0.0, "delegation_events": 0}, []
    # Collapse case-variant duplicates (e.g. "Bash" vs "bash") for an honest distinct count.
    merged = Counter()
    for name, cnt in corpus.tool_usage.items():
        merged[name.lower()] += cnt
    distinct = len(merged)
    breadth = squash(distinct / 20, 1.0)
    # Shannon evenness of the usage distribution.
    counts = list(merged.values())
    H = -sum((x / total) * math.log(x / total) for x in counts if x > 0)
    evenness = (H / math.log(distinct)) if distinct > 1 else 0.0
    active_hours = max(corpus.active_seconds / 3600, 0.5)
    delegation = squash(corpus.delegation_events / active_hours, 2.0)
    score = 100 * (0.45 * breadth + 0.30 * evenness + 0.25 * delegation)
    detail = {"n": total, "distinct": distinct, "evenness": evenness,
              "delegation_events": corpus.delegation_events}
    return score, detail, []


# --------------------------------------------------------------------------- #
# Aggregate: confidence shrinkage, overall score, band, archetype
# --------------------------------------------------------------------------- #

def shrink(score, n, target_n):
    c = min(1.0, n / target_n) if target_n else 1.0
    return 50 + (score - 50) * c, c


def band_for(score):
    for name, lo, hi, meaning in BANDS:
        if lo <= score <= hi:
            return name, meaning
    return BANDS[-1][0], BANDS[-1][3]


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def classify_archetype(dim_scores, delegation_score):
    """Nearest-prototype over your DRIVING-STYLE vector, with a margin guard.

    The vector adds a Delegation axis and is AGENCY-WEIGHTED: axes you control
    (Direction, Iteration, Toolcraft, Delegation) count fully, while axes the agent
    mostly drives on its own (Verification, Context) are heavily discounted — so the
    archetype reflects how *you* drive, not Claude's built-in habits.
    """
    scores = dict(dim_scores)
    scores["Delegation"] = delegation_score
    V = [scores[ax] for ax in ARCHETYPE_AXES]
    names = list(PROTOTYPES.keys())
    mat = [PROTOTYPES[n]["vec"] for n in names]
    # z-score each axis across prototypes + the user vector, then apply agency weights
    cols = list(zip(*(mat + [V])))
    means = [statistics.mean(col) for col in cols]
    stds = [statistics.pstdev(col) or 1.0 for col in cols]
    w = [AGENCY[ax] for ax in ARCHETYPE_AXES]

    def zw(vec):
        return [w[i] * (v - means[i]) / stds[i] for i, v in enumerate(vec)]

    vz = zw(V)
    sims = sorted(((round(_cosine(vz, zw(PROTOTYPES[n]["vec"])), 3), n) for n in names), reverse=True)
    top_sim, top = sims[0]
    second_sim, second = sims[1]
    blended = (top_sim - second_sim) < ARCHETYPE_MARGIN
    second_short = second.replace("The ", "")
    article = "an" if second_short[:1] in "AEIOU" else "a"
    return {
        "primary": top, "primary_sim": top_sim, "secondary": second, "secondary_sim": second_sim,
        "blended": blended, "all": sims, "delegation_score": round(delegation_score),
        "label": f"{PROTOTYPES[top]['emoji']} {top}" + (f", with {article} {second_short} streak" if blended else ""),
        "blurb": PROTOTYPES[top]["blurb"],
    }


# --------------------------------------------------------------------------- #
# Analysis orchestration
# --------------------------------------------------------------------------- #

def _measurable_dims(capabilities):
    """Which dimensions a source can honestly observe. None -> all (the Claude Code default).
    An unobservable dimension is excluded and the weights renormalize over the rest — it is
    never scored as 0 and never guessed: honesty over coverage."""
    if not capabilities:
        return list(WEIGHTS)
    cap = capabilities
    out = []
    if cap.get("prompts", True):
        out += ["Direction", "Iteration"]
    if cap.get("verify", True) and cap.get("edits", True):
        out.append("Verification")
    if cap.get("reads", True) and cap.get("edits", True):
        out.append("Context")
    if any(cap.get(k, True) for k in ("edits", "verify", "reads", "delegation")):
        out.append("Toolcraft")   # tool breadth is observable whenever tool use is observable
    return [n for n in WEIGHTS if n in out]   # keep canonical order


def analyze(corpus, capabilities=None):
    raw, detail, evidence = {}, {}, {}
    for name, fn in (("Direction", score_direction), ("Verification", score_verification),
                     ("Context", score_context), ("Iteration", score_iteration),
                     ("Toolcraft", score_toolcraft)):
        s, d, ev = fn(corpus)
        raw[name], detail[name], evidence[name] = s, d, ev

    shrunk, conf = {}, {}
    for name in raw:
        shrunk[name], conf[name] = shrink(raw[name], detail[name].get("n", 0), TARGET_N[name])

    measurable = _measurable_dims(capabilities)
    na_dims = [n for n in WEIGHTS if n not in measurable]
    wsum = sum(WEIGHTS[n] for n in measurable) or 1.0
    overall_raw = round(sum(WEIGHTS[n] * raw[n] for n in measurable) / wsum)
    overall = round(sum(WEIGHTS[n] * shrunk[n] for n in measurable) / wsum)
    band, band_meaning = band_for(overall)
    # Delegation is a user-driven archetype axis (handoffs per active hour).
    active_hours = max(corpus.active_seconds / 3600, 0.5)
    delegation_score = 100 * squash(corpus.delegation_events / active_hours, 2.0)
    archetype = classify_archetype(shrunk, delegation_score)

    # length distribution of real prompts (context only)
    lens = [len(p["text"]) for p in corpus.real_prompts]
    words = [len(p["text"].split()) for p in corpus.real_prompts]
    dist = {}
    if lens:
        dist = {
            "median_chars": int(statistics.median(lens)),
            "mean_chars": int(statistics.mean(lens)),
            "median_words": int(statistics.median(words)),
            "under_80_pct": round(100 * sum(1 for L in lens if L < 80) / len(lens)),
        }

    return {
        "raw": raw, "shrunk": shrunk, "conf": conf, "detail": detail, "evidence": evidence,
        "overall_raw": overall_raw, "overall": overall, "band": band, "band_meaning": band_meaning,
        "archetype": archetype, "dist": dist, "fingerprint": _run_fingerprint(corpus),
        "measurable": measurable, "na_dims": na_dims,
    }


def merge_corpora(entries):
    """Merge per-source corpora into one, tagging every real prompt (and its timeline rec —
    same object, preserving the parse() identity invariant) with its source. Session ids
    live in disjoint namespaces per source; a collision is disambiguated with a source prefix."""
    m = Corpus()
    m.session_source = {}   # session_id -> source name (report/evidence provenance)
    for source, c in entries:
        m.files += c.files
        m.projects |= c.projects
        m.total_bytes += c.total_bytes
        m.user_records += c.user_records
        m.filtered += c.filtered
        m.tool_usage += c.tool_usage
        m.total_tool_calls += c.total_tool_calls
        m.delegation_events += c.delegation_events
        m.active_seconds += c.active_seconds
        m.signals += c.signals
        for ts in (c.first_ts, c.last_ts):
            if ts:
                m.first_ts = ts if m.first_ts is None or ts < m.first_ts else m.first_ts
                m.last_ts = ts if m.last_ts is None or ts > m.last_ts else m.last_ts
        for sid, sess in c.sessions.items():
            key = sid if sid not in m.sessions else f"{source}:{sid}"
            timeline = []
            for ev in sess["timeline"]:
                if ev.get("kind") == "prompt":
                    rec = dict(ev["rec"], source=source, session=key)
                    m.real_prompts.append(rec)
                    timeline.append({"kind": "prompt", "text": ev["text"], "rec": rec})
                else:
                    timeline.append(ev)
            m.sessions[key] = {"project": sess["project"], "timeline": timeline}
            m.session_source[key] = source
            if sid in getattr(c, "session_ts", {}):
                m.session_ts[key] = c.session_ts[sid]
            if sid in getattr(c, "session_caps", {}):
                m.session_caps[key] = c.session_caps[sid]
        # prompts from sessions whose timeline never made it into c.sessions don't exist:
        # parse() only records prompts through timelines, so the rebuild above is complete.
    return m


def analyze_combined(entries):
    """ONE score and ONE profile across every source, assembled honestly: each dimension is
    scored over the merged data of only the sources that can observe it (per their
    capability masks), so e.g. Codex's read-less edits can never fake 'ungrounded' Context.
    Evidence volume then drives the same per-dimension confidence shrinkage as ever.

    `entries` is a list of dicts: {"source": str, "corpus": Corpus, "caps": dict,
    "overall": int, "band": str} (per-source overall/band ride along for the report's
    source-mix panel). Returns (merged_corpus, result) shaped exactly like analyze()'s."""
    full = merge_corpora([(e["source"], e["corpus"]) for e in entries])
    caps_by_source = {e["source"]: e["caps"] for e in entries}
    for sid, src in getattr(full, "session_source", {}).items():
        if src in caps_by_source:
            full.session_caps[sid] = caps_by_source[src]
    subs = {}   # frozenset of source names -> merged sub-corpus (cache)

    def corpus_for(dim):
        obs = [e for e in entries if dim in _measurable_dims(e["caps"])]
        if not obs:
            return None, []
        names = frozenset(e["source"] for e in obs)
        if len(obs) == len(entries):
            return full, obs
        if names not in subs:
            subs[names] = merge_corpora([(e["source"], e["corpus"]) for e in obs])
        return subs[names], obs

    raw, detail, evidence = {}, {}, {}
    contributors = {}
    for name, fn in (("Direction", score_direction), ("Verification", score_verification),
                     ("Context", score_context), ("Iteration", score_iteration),
                     ("Toolcraft", score_toolcraft)):
        sub, obs = corpus_for(name)
        contributors[name] = [e["source"] for e in obs]
        if sub is None:
            raw[name], detail[name], evidence[name] = 0.0, {"n": 0}, []
            continue
        s, d, ev = fn(sub)
        raw[name], detail[name], evidence[name] = s, d, ev

    shrunk, conf = {}, {}
    for name in raw:
        shrunk[name], conf[name] = shrink(raw[name], detail[name].get("n", 0), TARGET_N[name])

    measurable = [n for n in WEIGHTS if contributors.get(n)]
    na_dims = [n for n in WEIGHTS if n not in measurable]
    wsum = sum(WEIGHTS[n] for n in measurable) or 1.0
    overall_raw = round(sum(WEIGHTS[n] * raw[n] for n in measurable) / wsum)
    overall = round(sum(WEIGHTS[n] * shrunk[n] for n in measurable) / wsum)
    band, band_meaning = band_for(overall)
    active_hours = max(full.active_seconds / 3600, 0.5)
    delegation_score = 100 * squash(full.delegation_events / active_hours, 2.0)
    archetype = classify_archetype(shrunk, delegation_score)

    lens = [len(p["text"]) for p in full.real_prompts]
    words = [len(p["text"].split()) for p in full.real_prompts]
    dist = {}
    if lens:
        dist = {
            "median_chars": int(statistics.median(lens)),
            "mean_chars": int(statistics.mean(lens)),
            "median_words": int(statistics.median(words)),
            "under_80_pct": round(100 * sum(1 for L in lens if L < 80) / len(lens)),
        }

    sources_out = []
    for e in entries:
        srow = {"source": e["source"], "overall": e["overall"], "band": e["band"],
                "real_prompts": len(e["corpus"].real_prompts),
                "sessions": e["corpus"].files,
                "active_hours": round(e["corpus"].active_seconds / 3600, 1),
                "not_measurable": [n for n in WEIGHTS
                                   if n not in _measurable_dims(e["caps"])]}
        if isinstance(e.get("dims"), dict):
            srow["dims"] = e["dims"]
        sources_out.append(srow)

    result = {
        "raw": raw, "shrunk": shrunk, "conf": conf, "detail": detail, "evidence": evidence,
        "overall_raw": overall_raw, "overall": overall, "band": band, "band_meaning": band_meaning,
        "archetype": archetype, "dist": dist, "fingerprint": _run_fingerprint(full),
        "measurable": measurable, "na_dims": na_dims,
        # Combined-mode extras (absent from single-source results):
        "combined": True,
        "dim_sources": contributors,
        "sources": sources_out,
    }
    return full, result


def build_action_plan(corpus, result):
    """Growth cards ranked by impact = (target - score) * weight. The teaching copy
    comes from SKILL_TEACH; user-specific evidence comes from result['evidence']."""
    TARGET = 85
    dims = result.get("measurable") or list(WEIGHTS)   # never coach an unmeasured dimension
    cards = []
    for name in dims:
        score = result["shrunk"][name]
        impact = (TARGET - score) * WEIGHTS[name]
        cards.append({"dim": name, "score": round(score), "impact": impact,
                      "weak": result["evidence"].get(name, []),
                      "detail": result["detail"][name]})
    cards.sort(key=lambda c: c["impact"], reverse=True)
    # strength callout = highest shrunk score
    strength = max(dims, key=lambda n: result["shrunk"][n])
    return cards, strength


def _shortest_action_prompt(corpus):
    cands = [p["text"] for p in corpus.real_prompts if _is_action_prompt(p["text"]) and len(p["text"]) < 40]
    return min(cands, key=len) if cands else None


def _best_prompts(corpus, limit=3):
    """Pick up to `limit` real prompts that already demonstrate strong AI-fluency habits
    (at least 2 concrete anchors: named file/symbol, explicit rule, stated why, or built-in
    verification check). Deterministic, prefers prompts that landed cleanly without an
    immediate follow-up correction and spans distinct projects/tools."""
    followed_by_corr = set()
    for sid, _, timeline in _iter_sessions(corpus):
        last_prompt_key = None
        saw_tool = False
        for ev in timeline:
            if ev["kind"] == "tool":
                saw_tool = True
                continue
            t = ev["text"]
            head = t[:160]
            if saw_tool and CORRECTION_CUE.search(head) and not PRAISE_CUE.search(head) and last_prompt_key:
                followed_by_corr.add(last_prompt_key)
            rec = ev.get("rec") or {}
            last_prompt_key = (rec.get("session", sid), rec.get("idx"))
            saw_tool = False

    scored = []
    for p in corpus.real_prompts:
        t = p["text"].strip()
        if len(t) < 40 or len(t) > 700:
            continue
        head = t[:80]
        if CORRECTION_CUE.search(head) and not PRAISE_CUE.search(head):
            continue
        habits = []
        if ARTIFACT_RE.search(t):
            habits.append("Named target")
        if CONSTRAINT_CUE.search(t) and ACTION_VERB.search(t):
            habits.append("Clear rule")
        if INTENT_CUE.search(t):
            habits.append("Stated why")
        if _VERIFY_PROMPT_RE.search(t) or VERIFY_RE.search(t):
            habits.append("Built-in check")
        if len(habits) < 2:
            continue
        clean_turn = (p["session"], p["idx"]) not in followed_by_corr
        sweet_len = 70 <= len(t) <= 320
        rank = (len(habits), int(clean_turn), int(sweet_len), min(len(t), 350))
        scored.append((rank, p, habits))

    scored.sort(key=lambda x: (x[0], x[1]["session"], x[1]["idx"]), reverse=True)
    picked, seen_text, seen_proj = [], set(), set()

    def _emit(p, habits):
        item = {
            "text": _scrub_paths(p["text"].strip()[:420]),
            "project": _project_label(p["project"]),
            "habits": habits,
        }
        if p.get("source"):
            item["source"] = p["source"]
        return item

    # First pass: maximize diversity across projects/tools.
    for _, p, habits in scored:
        if len(picked) >= limit:
            break
        key_text = p["text"].strip().lower()
        proj_key = (p.get("source") or "", p["project"])
        if key_text in seen_text or proj_key in seen_proj:
            continue
        seen_text.add(key_text)
        seen_proj.add(proj_key)
        picked.append(_emit(p, habits))

    # Second pass: fill remaining slots if all best prompts came from one project.
    for _, p, habits in scored:
        if len(picked) >= limit:
            break
        key_text = p["text"].strip().lower()
        if key_text in seen_text:
            continue
        seen_text.add(key_text)
        picked.append(_emit(p, habits))

    return picked


def build_evidence(corpus, result, cards, archive_info=None, source=None, capabilities=None):
    """Serialize a de-contaminated EVIDENCE bundle for the two-model analysis pipeline
    (Sonnet 4.6 explores it; Opus 4.8 analyzes it against the bundled AI-fluency
    framework). It contains your real prompts/behavior with home paths scrubbed, and is
    git-ignored. Deterministic (no randomness) so runs are reproducible."""
    prompts = corpus.real_prompts
    sample, seen = [], set()

    def add(p):
        k = (p["session"], p["idx"])
        if k in seen:
            return
        seen.add(k)
        item = {"text": _scrub_paths(p["text"][:600]), "project": _project_label(p["project"]),
                "chars": len(p["text"])}
        if p.get("source"):
            item["source"] = p["source"]   # combined runs tag each prompt with its tool
        sample.append(item)

    by_len = sorted(prompts, key=lambda p: len(p["text"]))
    for p in by_len[:6]:                 # the terse nudges
        add(p)
    for p in by_len[-14:]:               # the rich, intent-carrying prompts
        add(p)
    stride = max(1, len(prompts) // 20)  # an even spread through the timeline
    for p in prompts[::stride]:
        if len(sample) >= 50:
            break
        add(p)

    def clean_ex(items):
        out = []
        for e in items or []:
            if not isinstance(e, dict):
                continue
            c = {}
            if e.get("text"):
                c["text"] = _scrub_paths(str(e["text"])[:300])
            if e.get("file"):
                c["file"] = os.path.basename(str(e["file"]))
            if e.get("files"):
                c["files"] = str(e["files"])
            if e.get("project"):
                c["project"] = _project_label(e["project"])
            if e.get("source"):
                c["source"] = str(e["source"])
            if c:
                out.append(c)
        return out

    span_days = (corpus.last_ts - corpus.first_ts).days if corpus.first_ts and corpus.last_ts else 0
    a = result["archetype"]
    bundle_sources = result.get("sources") if result.get("combined") else None
    return {
        "schema": "claude-insight-evidence/1",
        # Additive multi-source fields (schema stays /1 for backward compatibility):
        "source": source or "claude-code",
        "capabilities": capabilities or {"prompts": True, "edits": True, "verify": True,
                                         "reads": True, "delegation": True},
        "not_measurable": result.get("na_dims", []),
        # Combined runs: per-source sub-scores + which sources fed each dimension.
        **({"sources": bundle_sources, "dim_sources": result.get("dim_sources")}
           if bundle_sources else {}),
        "meta": {
            "sessions": corpus.files, "projects": len(corpus.projects),
            "real_prompts": len(prompts), "user_records": corpus.user_records,
            "filtered_noise": dict(corpus.filtered),
            "span_days": span_days,
            "active_hours": round(corpus.active_seconds / 3600, 1),
            "archive": archive_info,
            "prompt_distribution": result["dist"],
            # Binds any analysis produced from this bundle back to this exact run; the
            # analysis stage must echo it so a stale/foreign analysis can be refused.
            "run_fingerprint": result.get("fingerprint"),
        },
        "scores": {
            "overall": result["overall"], "overall_raw": result["overall_raw"],
            "band": result["band"], "weights": WEIGHTS,
            "dimensions_raw": {k: round(v) for k, v in result["raw"].items()},
            "dimensions_adjusted": {k: round(v) for k, v in result["shrunk"].items()},
            "confidence": {k: round(v, 2) for k, v in result["conf"].items()},
            "dimension_names": DISPLAY_NAMES,
        },
        "dimension_detail": result["detail"],
        "archetype": {"primary": a["primary"], "secondary": a["secondary"],
                      "blended": a.get("blended")},
        "behavior": {
            "sample_prompts": sample,
            "strong_examples": _best_prompts(corpus),
            "weak_examples": {c["dim"]: clean_ex(c["weak"]) for c in cards},
            "tool_usage": dict(corpus.tool_usage),
            "delegation_events": corpus.delegation_events,
            "signals": dict(corpus.signals),
        },
    }


def _dots_html(level):
    """Render a 1-5 visual segmented level meter."""
    try:
        lvl = max(0, min(5, int(level)))
    except (TypeError, ValueError):
        lvl = 0
    return '<span class="segs" aria-label="Level ' + str(lvl) + ' of 5">' + "".join(
        f'<i class="{"on" if i < lvl else ""}"></i>' for i in range(5)
    ) + '</span>'


def _svg_gauge(score, band, raw, hero_srcline=""):
    """Precision 240-degree open arc gauge with band boundary ticks (40/55/70/85)."""
    cx, cy, r = 100.0, 96.0, 72.0
    a_start, sweep = 150.0, 240.0

    def pt(deg, rad=r):
        t = math.radians(deg)
        return cx + rad * math.cos(t), cy + rad * math.sin(t)

    def arc(pct):
        pct = max(0.0, min(100.0, float(pct)))
        span = sweep * (pct / 100.0)
        x0, y0 = pt(a_start)
        x1, y1 = pt(a_start + span)
        large = 1 if span > 180.0 else 0
        return f"M {x0:.1f} {y0:.1f} A {r:.1f} {r:.1f} 0 {large} 1 {x1:.1f} {y1:.1f}"

    ticks = []
    for bound in (40, 55, 70, 85):
        deg = a_start + sweep * (bound / 100.0)
        x0, y0 = pt(deg, r - 6)
        x1, y1 = pt(deg, r + 6)
        ticks.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" stroke="var(--bg)" stroke-width="2"/>')

    next_note = "Top tier"
    for name, lo, _, _ in BANDS:
        if score < lo:
            pts = lo - score
            next_note = f"{pts} pt{'s' if pts != 1 else ''} to {name}"
            break

    fill_path = (f'<path d="{arc(max(score, 1))}" fill="none" stroke="var(--accent)" '
                 f'stroke-width="11" stroke-linecap="round"/>') if score > 0 else ""
    return (
        f'<div class="gauge-wrap">'
        f'<svg viewBox="0 0 200 156" class="gauge-svg" role="img" aria-label="Fluency score {score} out of 100">'
        f'<path d="{arc(100)}" fill="none" stroke="var(--track)" stroke-width="11" stroke-linecap="round"/>'
        f'{fill_path}{"".join(ticks)}'
        f'<text x="100" y="96" text-anchor="middle" class="g-num">{score}</text>'
        f'<text x="100" y="114" text-anchor="middle" class="g-sub">/ 100</text>'
        f'<text x="100" y="138" text-anchor="middle" class="g-band">{_esc(band)}</text>'
        f'</svg>'
        f'<div class="g-meta"><span>{_esc(next_note)}</span><span>raw {raw} · adj {score}</span></div>'
        f'{hero_srcline}'
        f'</div>'
    )


def _svg_radar(result):
    """5-axis fluency pentagon radar (inline SVG) vs. the 85-pt target ring.
    Masked (not measurable) axes are omitted from the polygon and marked '—' — never faked as 0."""
    axes = [("Direction", "Briefing"), ("Verification", "Verify"), ("Context", "Context"),
            ("Iteration", "Iteration"), ("Toolcraft", "Toolcraft")]
    na_set = set(result.get("na_dims") or [])
    cx, cy, R = 160.0, 118.0, 80.0
    angles = [-90 + i * 72 for i in range(5)]

    def pt(deg, frac):
        t = math.radians(deg)
        return cx + R * frac * math.cos(t), cy + R * frac * math.sin(t)

    rings = []
    for frac in (0.25, 0.50, 0.75, 1.0):
        pts = " ".join(f"{pt(a, frac)[0]:.1f},{pt(a, frac)[1]:.1f}" for a in angles)
        rings.append(f'<polygon points="{pts}" fill="none" stroke="var(--line)" stroke-width="1"/>')
    tgt_pts = " ".join(f"{pt(a, 0.85)[0]:.1f},{pt(a, 0.85)[1]:.1f}" for a in angles)
    rings.append(f'<polygon points="{tgt_pts}" fill="none" stroke="var(--ok)" stroke-width="1" stroke-dasharray="3 3" opacity="0.55"/>')

    spokes = []
    for a in angles:
        x1, y1 = pt(a, 1.0)
        spokes.append(f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" stroke="var(--line)" stroke-width="1"/>')

    poly_pts = []
    dots = []
    labels = []
    for i, (key, short_lbl) in enumerate(axes):
        a = angles[i]
        lx, ly = pt(a, 1.22)
        if i == 0:
            anc, dy = "middle", -3
        elif i in (1, 2):
            anc, dy = "start", 4
        else:
            anc, dy = "end", 4

        if key in na_set:
            labels.append(
                f'<text x="{lx:.1f}" y="{ly + dy:.1f}" text-anchor="{anc}" class="r-lbl na">'
                f'{short_lbl} —</text>'
            )
            continue

        sc = round(result["shrunk"][key])
        vx, vy = pt(a, max(0.06, sc / 100.0))
        poly_pts.append(f"{vx:.1f},{vy:.1f}")
        dot_col = "var(--ok)" if sc >= 75 else ("var(--warn)" if sc < 55 else "var(--accent)")
        dots.append(f'<circle cx="{vx:.1f}" cy="{vy:.1f}" r="4.5" fill="{dot_col}" stroke="var(--surface)" stroke-width="1.5"/>')
        labels.append(
            f'<text x="{lx:.1f}" y="{ly + dy:.1f}" text-anchor="{anc}" class="r-lbl">'
            f'{short_lbl} <tspan class="r-val">{sc}</tspan></text>'
        )

    poly = (f'<polygon points="{" ".join(poly_pts)}" fill="var(--accent-soft)" '
            f'stroke="var(--accent)" stroke-width="2" stroke-linejoin="round"/>') if len(poly_pts) >= 3 else ""

    return (
        f'<div class="radar-wrap">'
        f'<div class="panel-k">Fluency shape <span class="legend-t">--- 85 target</span></div>'
        f'<svg viewBox="0 0 320 236" class="radar-svg" role="img" aria-label="Five-dimension fluency radar">'
        f'{"".join(rings)}{"".join(spokes)}{poly}{"".join(dots)}{"".join(labels)}'
        f'</svg></div>'
    )


def _impact_chart_html(cards):
    """Visual score-upside bars showing exact overall points available per weak dimension."""
    gains = [(c["dim"], round(c["impact"], 1), c["score"]) for c in cards if c["impact"] > 0.4]
    if not gains:
        return ('<div class="impact-box"><div class="panel-k">Score upside</div>'
                '<p class="muted-sm">Every measured dimension is at or above the 85-pt mastery target.</p></div>')
    max_g = max(g for _, g, _ in gains) or 1.0
    rows = []
    for dim, pts, sc in gains[:4]:
        w = max(6, round(100 * pts / max_g))
        tone = "bad" if sc < 55 else "warn"
        rows.append(
            f'<div class="imp-row">'
            f'<span class="imp-n">{_esc(disp(dim))}</span>'
            f'<span class="imp-tr"><i class="{tone}" style="width:{w}%"></i></span>'
            f'<span class="imp-v">+{pts:.1f} pts</span>'
            f'</div>'
        )
    return (
        '<div class="impact-box">'
        '<div class="panel-k">Score upside <span class="muted-sm">(pts to 85 target)</span></div>'
        + "".join(rows) + '</div>'
    )


def _prompt_anatomy_html(result):
    """4-column visual target-bullet strip for the observable prompt & verification rates."""
    dd = result["detail"].get("Direction") or {}
    vd = result["detail"].get("Verification") or {}
    na_set = set(result.get("na_dims") or [])
    metrics = [
        ("Named target", "File, symbol, or path", dd.get("artifact_rate"), 0.45),
        ("Clear rule", "Constraint or boundary", dd.get("constraint_rate"), 0.45),
        ("Stated why", "Goal or context", dd.get("intent_rate"), 0.30),
    ]
    if "Verification" not in na_set and vd.get("rate") is not None:
        metrics.append(("Verified edits", "Test, build, or check", vd.get("rate"), 0.60))
    else:
        cd = result["detail"].get("Iteration") or {}
        metrics.append(("Specific fixes", "Symptom + rule", cd.get("specificity"), 0.75))

    tiles = []
    for label, sub, rate, target in metrics:
        if rate is None:
            pct, bar_w, hit = "—", 0, False
        else:
            p = round(rate * 100)
            pct = f"{p}%"
            bar_w = min(100, p)
            hit = rate >= target
        tgt_x = round(target * 100)
        tone = "ok" if hit else ("warn" if (rate or 0) >= target * 0.5 else "bad")
        tiles.append(
            f'<div class="anat">'
            f'<div class="anat-top"><span class="anat-l">{_esc(label)}</span>'
            f'<span class="anat-v {tone}">{pct}</span></div>'
            f'<div class="bullet"><i class="{tone}" style="width:{bar_w}%"></i>'
            f'<b class="tick" style="left:{tgt_x}%" title="Target {tgt_x}%"></b></div>'
            f'<div class="anat-s">{_esc(sub)} · target {tgt_x}%</div>'
            f'</div>'
        )
    return '<div class="anat-grid">' + "".join(tiles) + '</div>'


def _data_visuals_html(corpus):
    """Three compact side-by-side data charts: Signal-vs-Noise bar, Prompt Length mix, Top Tools."""
    # 1. Signal vs Noise stacked bar
    real_n = len(corpus.real_prompts)
    total_u = max(1, corpus.user_records)
    real_pct = round(100 * real_n / total_u)
    noise_items = corpus.filtered.most_common(4)
    segs = [f'<i class="seg-real" style="width:{max(real_pct, 2 if real_n else 0)}%" title="Real prompts: {real_n:,}"></i>']
    legend = [f'<span><b class="sw real"></b><b>{real_n:,}</b> real prompts ({real_pct}%)</span>']
    palette = ("n1", "n2", "n3", "n4")
    for idx, (k, v) in enumerate(noise_items):
        p = round(100 * v / total_u)
        segs.append(f'<i class="seg-{palette[idx]}" style="width:{max(p, 1)}%" title="{_esc(k)}: {v:,}"></i>')
        legend.append(f'<span><b class="sw {palette[idx]}"></b><b>{v:,}</b> {_esc(k)}</span>')
    noise_card = (
        '<div class="viz-card">'
        '<div class="panel-k">Signal vs. filtered noise</div>'
        f'<div class="stack-bar">{"".join(segs)}</div>'
        f'<div class="stack-leg">{"".join(legend)}</div>'
        '</div>'
    )

    # 2. Prompt length distribution (<80 / 80-200 / >200 chars)
    lens = [len(p["text"]) for p in corpus.real_prompts]
    tot_p = max(1, len(lens))
    short_n = sum(1 for L in lens if L < 80)
    mid_n = sum(1 for L in lens if 80 <= L <= 200)
    long_n = sum(1 for L in lens if L > 200)
    sp, mp, lp = round(100 * short_n / tot_p), round(100 * mid_n / tot_p), round(100 * long_n / tot_p)
    med = int(statistics.median(lens)) if lens else 0
    len_card = (
        '<div class="viz-card">'
        f'<div class="panel-k">Prompt length mix <span class="muted-sm">median {med} chars</span></div>'
        f'<div class="stack-bar">'
        f'<i class="seg-n2" style="width:{sp}%" title="Short (<80): {short_n}"></i>'
        f'<i class="seg-acc" style="width:{mp}%" title="Focused (80-200): {mid_n}"></i>'
        f'<i class="seg-real" style="width:{lp}%" title="Detailed (>200): {long_n}"></i>'
        f'</div>'
        f'<div class="stack-leg">'
        f'<span><b class="sw n2"></b><b>{sp}%</b> short (&lt;80)</span>'
        f'<span><b class="sw acc"></b><b>{mp}%</b> focused (80–200)</span>'
        f'<span><b class="sw real"></b><b>{lp}%</b> detailed (&gt;200)</span>'
        f'</div></div>'
    )

    # 3. Top 6 tools bar chart (case-folded across sources so Bash/bash and Edit/edit merge)
    merged_tools = Counter()
    nice_name = {}
    for tname, cnt in corpus.tool_usage.items():
        key = tname.lower()
        merged_tools[key] += cnt
        if key not in nice_name or (tname[:1].isupper() and not nice_name[key][:1].isupper()):
            nice_name[key] = tname
    top_tools = merged_tools.most_common(6)
    if top_tools:
        mx = top_tools[0][1] or 1
        trows = []
        for key, cnt in top_tools:
            lbl = nice_name.get(key, key)
            w = max(4, round(100 * cnt / mx))
            trows.append(
                f'<div class="tbar"><span>{_esc(lbl[:16])}</span>'
                f'<b><i style="width:{w}%"></i></b><em>{cnt:,}</em></div>'
            )
        tools_body = "".join(trows)
    else:
        tools_body = '<p class="muted-sm">No tool calls recorded in these logs.</p>'
    tools_card = (
        '<div class="viz-card">'
        f'<div class="panel-k">Top tools <span class="muted-sm">{corpus.total_tool_calls:,} calls · {corpus.delegation_events} delegations</span></div>'
        f'{tools_body}</div>'
    )

    return f'<div class="viz-grid">{noise_card}{len_card}{tools_card}</div>'


def _analysis_section_html(analysis, dim_html=""):
    """Render the unified AI-authored 4D skill map in a clean 2x2 editorial grid,
    with compact Tufte bullet-chart dimension rows nested underneath."""
    if not analysis or not isinstance(analysis, dict):
        return ""
    cards = []
    for s in analysis.get("skill_map") or []:
        if not isinstance(s, dict):
            continue
        comp = _esc(s.get("competency", "?"))
        lvl = s.get("level", "?")
        label = _esc(s.get("level_label", ""))
        summ = _esc(s.get("summary", ""))
        nxt = _esc(s.get("next_move", ""))
        ev = "".join(f'<li>“{_esc(str(x)[:160])}”</li>' for x in (s.get("evidence") or [])[:2])
        cards.append(
            f'<div class="comp-card">'
            f'<div class="comp-h"><b>{comp}</b>'
            f'<span class="comp-lvl">{_dots_html(lvl)}<span class="pill">L{_esc(lvl)} · {label}</span></span></div>'
            f'<p class="comp-s">{summ}</p>'
            + (f'<ul class="mini-ev">{ev}</ul>' if ev else "")
            + (f'<div class="comp-next"><span>Next →</span> {nxt}</div>' if nxt else "")
            + '</div>'
        )
    parts = [
        '<section><h3>Skill map — analyzed against the AI Fluency framework</h3>',
        f'<div class="comp-grid">{"".join(cards)}</div>',
    ]
    strengths = analysis.get("strengths") or []
    if strengths:
        pills = "".join(f'<span class="str-chip">✓ {_esc(s)}</span>' for s in strengths[:4])
        parts.append(f'<div class="str-strip"><span class="str-k">Strengths</span>{pills}</div>')
    if dim_html:
        parts.append('<div class="subh">Behind the map — 5 measured dimensions <span class="muted-sm">(target 85)</span></div>' + dim_html)
    parts.append('<p class="foot-note">4D levels by Claude Opus 4.8 (explored by Sonnet 4.6) from de-contaminated evidence; scores computed deterministically.</p>')
    parts.append('</section>')
    return "".join(parts)


# Human-friendly source labels for the report.
NICE_SOURCE = {"claude-code": "Claude Code", "claude-desktop": "Cowork (Claude desktop)",
               "codex": "Codex / ChatGPT app", "cursor": "Cursor"}


def _dim_sources_note(result, name, n_sources):
    """'measured from: …' provenance on a combined report's dimension row (only when it
    is NOT fed by every source — the common all-sources case needs no callout)."""
    dim_sources = result.get("dim_sources") or {}
    feeding = dim_sources.get(name) or []
    if not feeding or n_sources <= 1 or len(feeding) == n_sources:
        return ""
    names = ", ".join(NICE_SOURCE.get(s, s) for s in feeding)
    return f'<span class="wt"> · {_esc(names)}</span>'


def _highlight_prompt_html(text):
    """Wrap Target, Rule, Why, and Check cues inside a prompt with <mark> spans while
    keeping .textContent identical so copyPrompt() still copies clean plain text."""
    if not text:
        return ""
    spans = []
    # Priority 0: Targets (backticks/files/paths) — if a backtick holds a test cmd, tag as Check!
    for m in ARTIFACT_RE.finditer(text):
        seg = m.group(0)
        if VERIFY_RE.search(seg) and not re.search(r"^[\w./\-]+\.(py|ts|tsx|js|jsx|css|html|md|json|go|rs)$", seg, re.I):
            spans.append((0, m.start(), m.end(), "hl-v", "Built-in check"))
        else:
            spans.append((0, m.start(), m.end(), "hl-t", "Named target"))
    # Priority 1: Verification phrases / commands
    for rx in (_VERIFY_PROMPT_RE, VERIFY_RE):
        for m in rx.finditer(text):
            spans.append((1, m.start(), m.end(), "hl-v", "Built-in check"))
    # Priority 2: Intent ("why")
    for m in INTENT_CUE.finditer(text):
        spans.append((2, m.start(), m.end(), "hl-w", "Stated why"))
    # Priority 3: Constraints ("rule")
    for m in CONSTRAINT_CUE.finditer(text):
        spans.append((3, m.start(), m.end(), "hl-r", "Clear rule"))

    spans.sort(key=lambda x: (x[0], -(x[2] - x[1]), x[1]))
    chosen = []
    for prio, st, en, cls, title in spans:
        if en <= st:
            continue
        if any(not (en <= c[0] or st >= c[1]) for c in chosen):
            continue
        chosen.append((st, en, cls, title))
    chosen.sort(key=lambda x: x[0])

    out = []
    pos = 0
    for st, en, cls, title in chosen:
        if st > pos:
            out.append(_esc(text[pos:st]))
        out.append(f'<mark class="{cls}" title="{title}">{_esc(text[st:en])}</mark>')
        pos = en
    if pos < len(text):
        out.append(_esc(text[pos:]))
    return "".join(out)


def _subcorpus_analyze(corpus, sids, fallback_caps=None, light_shrink=False):
    """Score a subset of sessions cleanly using the exact same dimension scorers and
    per-session capability masks (so Codex sessions never poison Context)."""
    sid_set = set(sids)
    sess_caps = getattr(corpus, "session_caps", None) or {}

    def make_sub(filter_dim=None):
        sub = Corpus()
        for sid in sid_set:
            sess = corpus.sessions.get(sid)
            if not sess:
                continue
            caps = sess_caps.get(sid) or fallback_caps or {}
            if filter_dim and filter_dim not in _measurable_dims(caps):
                continue
            sub.files += 1
            sub.projects.add(sess["project"])
            tl = []
            for ev in sess["timeline"]:
                if ev.get("kind") == "prompt":
                    rec = ev["rec"]
                    sub.real_prompts.append(rec)
                    tl.append(ev)
                elif ev.get("kind") == "tool":
                    nm = ev["name"]
                    sub.tool_usage[nm] += 1
                    sub.total_tool_calls += 1
                    if nm in DELEGATION_TOOLS:
                        sub.delegation_events += 1
                    tl.append(ev)
            sub.sessions[sid] = {"project": sess["project"], "timeline": tl}
        return sub

    full_sub = make_sub(None)
    if not full_sub.real_prompts:
        return None
    full_sub.active_seconds = max(1800.0, len(full_sub.real_prompts) * 60.0)

    raw, shrunk, measurable = {}, {}, []
    for name, fn in (("Direction", score_direction), ("Verification", score_verification),
                     ("Context", score_context), ("Iteration", score_iteration),
                     ("Toolcraft", score_toolcraft)):
        dsub = make_sub(name)
        if not dsub.real_prompts:
            continue
        dsub.active_seconds = max(1800.0, len(dsub.real_prompts) * 60.0)
        sc, det, _ = fn(dsub)
        n = det.get("n", 0)
        if n == 0 and name in ("Context", "Verification"):
            continue
        tgt = max(4, TARGET_N[name] // 3) if light_shrink else TARGET_N[name]
        sh, _ = shrink(sc, n, tgt)
        raw[name] = sc
        shrunk[name] = sh
        measurable.append(name)

    if not measurable:
        return None
    wsum = sum(WEIGHTS[n] for n in measurable) or 1.0
    overall = round(sum(WEIGHTS[n] * shrunk[n] for n in measurable) / wsum)
    corr = _find_corrections(full_sub)
    corr_rate = round(100 * len(corr) / max(1, len(full_sub.real_prompts)))
    return {
        "overall": overall,
        "prompts": len(full_sub.real_prompts),
        "corr_rate": corr_rate,
        "dims": {k: round(shrunk[k]) for k in measurable},
    }


def _trajectory_chart_html(corpus, result, fallback_caps=None, history=None):
    """Inline SVG score-trajectory sparkline across chronological session buckets."""
    ts_map = getattr(corpus, "session_ts", None) or {}
    dated = [(ts_map[sid], sid) for sid in corpus.sessions if sid in ts_map]
    dated.sort(key=lambda x: x[0])

    points = []
    if len(dated) >= 3 and (dated[-1][0] - dated[0][0]).days >= 1:
        # Group into up to 6 balanced chronological buckets so noisy 1-prompt days coalesce.
        k_buckets = min(6, max(3, len(dated) // 2))
        buckets = [[] for _ in range(k_buckets)]
        t0 = dated[0][0].timestamp()
        span = max(1.0, dated[-1][0].timestamp() - t0)
        for ts, sid in dated:
            idx = min(k_buckets - 1, int(((ts.timestamp() - t0) / span) * k_buckets))
            buckets[idx].append((ts, sid))
        for b in buckets:
            if not b:
                continue
            stat = _subcorpus_analyze(corpus, [sid for _, sid in b], fallback_caps, light_shrink=True)
            if stat:
                lbl = b[0][0].strftime("%b %-d") if sys.platform != "win32" else b[0][0].strftime("%b %d")
                points.append((lbl, stat["overall"], stat["prompts"]))

    # Fall back to prior saved snapshot trajectory if sessions lacked dates
    span_days = (dated[-1][0] - dated[0][0]).days if dated else 0
    mode_lbl = f"{span_days}d" if span_days >= 1 else "span"
    if len(points) < 3 and history and len(history) >= 2:
        points = [(h.get("label", "?"), int(h.get("overall", 0)), int(h.get("prompts", 0)))
                  for h in history[-8:]]
        mode_lbl = f"{len(points)} runs"

    if len(points) < 3:
        return (
            '<div class="viz-card">'
            '<div class="panel-k">Score trajectory</div>'
            '<p class="muted-sm" style="margin-top:10px">Build across 3+ days to unlock your chronological score trajectory.</p>'
            '</div>'
        )

    W, H = 340, 128
    px0, px1, py0, py1 = 26, W - 20, 18, H - 24
    lo = max(0, min(p[1] for p in points) - 12)
    hi = min(100, max(p[1] for p in points) + 10)
    if hi - lo < 25:
        mid = (hi + lo) / 2
        lo, hi = max(0, mid - 15), min(100, mid + 15)

    def xy(i, val):
        x = px0 + (px1 - px0) * (i / max(1, len(points) - 1))
        y = py1 - (py1 - py0) * ((val - lo) / max(1.0, hi - lo))
        return x, y

    coords = [xy(i, p[1]) for i, p in enumerate(points)]
    line_d = " ".join(f"{'M' if i == 0 else 'L'} {x:.1f} {y:.1f}" for i, (x, y) in enumerate(coords))
    area_d = f"{line_d} L {coords[-1][0]:.1f} {py1:.1f} L {coords[0][0]:.1f} {py1:.1f} Z"
    _, base_y = xy(0, result["overall"])

    dots = []
    for i, ((x, y), (lbl, sc, pr)) in enumerate(zip(coords, points)):
        col = "var(--ok)" if sc >= 75 else ("var(--warn)" if sc < 55 else "var(--accent)")
        dots.append(
            f'<g><circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{col}" stroke="var(--surface)" stroke-width="1.5">'
            f'<title>{_esc(lbl)}: {sc}/100 ({pr} prompts)</title></circle>'
            f'<text x="{x:.1f}" y="{max(11.0, y - 8):.1f}" text-anchor="middle" class="tr-v">{sc}</text></g>'
        )

    delta = points[-1][1] - points[0][1]
    d_cls = "ok" if delta > 2 else ("bad" if delta < -2 else "acc")
    d_str = f"{delta:+d} pts · {mode_lbl}"

    return (
        '<div class="viz-card">'
        f'<div class="panel-k">Score trajectory <span class="delta-pill {d_cls}">{d_str}</span></div>'
        f'<svg viewBox="0 0 {W} {H}" class="traj-svg" role="img" aria-label="Score trajectory over time">'
        f'<line x1="{px0}" y1="{base_y:.1f}" x2="{px1}" y2="{base_y:.1f}" stroke="var(--line)" stroke-dasharray="3 3"/>'
        f'<path d="{area_d}" fill="var(--accent-soft)"/>'
        f'<path d="{line_d}" fill="none" stroke="var(--accent)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>'
        f'{"".join(dots)}'
        f'<text x="{px0}" y="{H - 5}" text-anchor="start" class="tr-x">{_esc(points[0][0])}</text>'
        f'<text x="{(px0 + px1) / 2:.1f}" y="{H - 5}" text-anchor="middle" class="tr-x">Avg {result["overall"]}</text>'
        f'<text x="{px1}" y="{H - 5}" text-anchor="end" class="tr-x">{_esc(points[-1][0])}</text>'
        '</svg></div>'
    )


def _project_breakdown_html(corpus, fallback_caps=None):
    """Per-project score bars comparing prompt craft across repos."""
    by_proj = defaultdict(list)
    for sid, sess in corpus.sessions.items():
        by_proj[_project_label(sess["project"])].append(sid)
    rows = []
    for pname, sids in by_proj.items():
        stat = _subcorpus_analyze(corpus, sids, fallback_caps, light_shrink=True)
        if stat and stat["prompts"] >= 1:
            rows.append((stat["prompts"], stat["overall"], stat["corr_rate"], pname))
    if len(rows) < 2:
        return ""
    rows.sort(key=lambda x: (-x[0], -x[1]))
    bars = []
    for prompts, sc, cr, pname in rows[:5]:
        tone = "ok" if sc >= 75 else ("bad" if sc < 55 else "acc")
        bars.append(
            f'<div class="proj-row">'
            f'<span class="proj-n" title="{_esc(pname)}">{_esc(pname)}</span>'
            f'<span class="bullet"><i class="{tone}" style="width:{sc}%"></i><b class="tick" style="left:85%"></b></span>'
            f'<span class="proj-m"><b>{sc}</b> · {prompts}p</span>'
            f'</div>'
        )
    return (
        '<div class="viz-card">'
        '<div class="panel-k">Score by project <span class="muted-sm">(target 85)</span></div>'
        + "".join(bars) + '</div>'
    )


def _retry_spiral_html(corpus):
    """Visualize first-try clean vs. 1-correction vs. 2+ retry spirals across sessions."""
    clean = one_fix = spiral = 0
    max_streak = 0
    for _, _, timeline in _iter_sessions(corpus):
        saw_tool = False
        streak = 0
        had_episode = False
        for ev in timeline:
            if ev["kind"] == "tool":
                saw_tool = True
                had_episode = True
                continue
            head = ev["text"][:160]
            is_corr = bool(saw_tool and CORRECTION_CUE.search(head) and not PRAISE_CUE.search(head))
            if is_corr:
                streak += 1
                max_streak = max(max_streak, streak)
            elif had_episode:
                if streak == 0:
                    clean += 1
                elif streak == 1:
                    one_fix += 1
                else:
                    spiral += 1
                streak = 0
                had_episode = False
            saw_tool = False
        if had_episode:
            if streak == 0:
                clean += 1
            elif streak == 1:
                one_fix += 1
            else:
                spiral += 1

    tot = clean + one_fix + spiral
    if tot == 0:
        return (
            '<div class="viz-card">'
            '<div class="panel-k">Turn resolution</div>'
            '<p class="muted-sm" style="margin-top:10px">No tool-assisted edit turns logged yet.</p>'
            '</div>'
        )
    cp = round(100 * clean / tot)
    op = round(100 * one_fix / tot)
    sp = max(0, 100 - cp - op)
    return (
        '<div class="viz-card">'
        f'<div class="panel-k">First-try vs. retry spiral <span class="muted-sm">{tot} episodes</span></div>'
        '<div class="stack-bar">'
        f'<i class="seg-real" style="width:{cp}%" title="Landed clean: {clean}"></i>'
        f'<i class="seg-n2" style="width:{op}%" title="1 retry: {one_fix}"></i>'
        f'<i class="seg-n3" style="width:{sp}%" title="2+ retry spiral: {spiral}"></i>'
        '</div>'
        '<div class="stack-leg">'
        f'<span><b class="sw real"></b><b>{cp}%</b> first-try clean ({clean})</span>'
        f'<span><b class="sw n2"></b><b>{op}%</b> 1 retry ({one_fix})</span>'
        f'<span><b class="sw n3"></b><b>{sp}%</b> 2+ spiral ({spiral})</span>'
        '</div>'
        + (f'<p class="muted-sm" style="margin-top:8px">Longest retry chain: <b>{max_streak}</b> back-to-back corrections.</p>'
           if max_streak >= 2 else '<p class="muted-sm" style="margin-top:8px">Zero multi-turn retry spirals detected.</p>')
        + '</div>'
    )


def _patterns_section_html(corpus, result, fallback_caps=None, history=None):
    """Trajectory, project breakdown, and first-try resolution visuals."""
    traj = _trajectory_chart_html(corpus, result, fallback_caps, history)
    proj = _project_breakdown_html(corpus, fallback_caps)
    spiral = _retry_spiral_html(corpus)
    cards = [traj] + ([proj] if proj else []) + [spiral]
    cls = "viz-grid" if len(cards) == 3 else "viz-grid two"
    return f'<section><h3>Patterns across your sessions</h3><div class="{cls}">{"".join(cards)}</div></section>'


def _verdict_strip_html(corpus, result, cards, strength):
    """Crisp 3-tile takeaway strip replacing the multi-paragraph deterministic summary."""
    s_name = disp(strength)
    s_sc = round(result["shrunk"][strength])
    if cards and cards[0]["score"] < 85:
        b = cards[0]
        b_name = disp(b["dim"])
        b_sc = b["score"]
        b_up = round(b["impact"], 1)
        b_move = SKILL_TEACH[b["dim"]]["practice"]
        b_tile = (f'<div class="vcard v-warn"><div class="vk">⚡ Biggest bottleneck</div>'
                  f'<div class="vt">{_esc(b_name)} · {b_sc}/100</div>'
                  f'<div class="vs">+{b_up:.1f} pts available toward 85 target</div></div>')
        n_tile = (f'<div class="vcard v-acc"><div class="vk">🎯 #1 habit next session</div>'
                  f'<div class="vt">Fix {_esc(b_name)}</div>'
                  f'<div class="vs">{_esc(b_move)}</div></div>')
    else:
        b_tile = ('<div class="vcard v-ok"><div class="vk">⚡ Bottleneck</div>'
                  '<div class="vt">All measured skills ≥ 85</div>'
                  '<div class="vs">Consistent expert-level execution</div></div>')
        n_tile = ('<div class="vcard v-acc"><div class="vk">🎯 Next stretch</div>'
                  '<div class="vt">Stay sharp across tools</div>'
                  '<div class="vs">Keep target, rule, why, and check in one sentence.</div></div>')

    return (
        '<div class="verdict-grid">'
        f'<div class="vcard v-ok"><div class="vk">✓ Strongest habit</div>'
        f'<div class="vt">{_esc(s_name)} · {s_sc}/100</div>'
        f'<div class="vs">{len(corpus.real_prompts):,} real prompts across {len(corpus.projects)} projects</div></div>'
        f'{b_tile}{n_tile}</div>'
    )


def _dojo_html(corpus, cards, analysis=None):
    """Interactive 10-second prompt rewrite trainer with live Target/Rule/Why/Check lights."""
    samples = []
    seen = set()
    if analysis and isinstance(analysis, dict):
        for g in (analysis.get("top_growth") or []):
            if not isinstance(g, dict):
                continue
            bef = str(g.get("example_before") or "").strip()
            aft = str(g.get("example_after") or "").strip()
            if bef and bef.lower() not in seen:
                seen.add(bef.lower())
                samples.append((bef[:220], aft[:420]))
    if not samples:
        for c in (cards or []):
            for e in (c.get("weak") or []):
                txt = _scrub_paths(str(e.get("text") or "")).strip()
                if txt and txt.lower() not in seen:
                    seen.add(txt.lower())
                    samples.append((txt[:220], ""))
                if len(samples) >= 3:
                    break
            if len(samples) >= 3:
                break
    if not samples:
        sh = _shortest_action_prompt(corpus)
        if sh:
            samples.append((_scrub_paths(sh)[:220], ""))
    if not samples:
        samples.append(("update the script", ""))

    chips = []
    for idx, (bef, aft) in enumerate(samples[:3]):
        lbl = bef[:34] + ("…" if len(bef) > 34 else "")
        act = " active" if idx == 0 else ""
        chips.append(
            f'<button type="button" class="dchip{act}" data-before="{_esc(bef)}" data-after="{_esc(aft)}" '
            f'onclick="dojoPick(this)">“{_esc(lbl)}”</button>'
        )
    first_bef, first_aft = samples[0]
    peek_btn = (
        '<button type="button" class="copy-btn" onclick="dojoPeek(this)">Peek rewrite</button>'
        if first_aft else
        '<button type="button" class="copy-btn" style="display:none" onclick="dojoPeek(this)">Peek rewrite</button>'
    )
    return (
        '<div class="action-card dojo">'
        '<div class="panel-k">⚡ 10-second rewrite dojo <span class="dojo-score">0/4 anchors</span></div>'
        f'<div class="dchips">{"".join(chips)}</div>'
        f'<textarea class="dojo-in" rows="3" oninput="dojoEval(this)" placeholder="Rewrite this prompt with Target + Rule + Why + Check...">{_esc(first_bef)}</textarea>'
        '<div class="dojo-lights">'
        '<span class="dl" data-k="target">Target</span>'
        '<span class="dl" data-k="rule">Rule</span>'
        '<span class="dl" data-k="why">Why</span>'
        '<span class="dl" data-k="check">Check</span>'
        '<div class="dojo-btns">'
        f'{peek_btn}'
        '<button type="button" class="copy-btn" onclick="dojoCopy(this)">Copy rewrite</button>'
        '</div></div>'
        f'<div class="dojo-peek" style="display:none" data-after="{_esc(first_aft)}"></div>'
        '</div>'
    )


def _detect_verify_cmd(corpus):
    """Find the user's most common real verification command for the drop-in rules card."""
    cmds = Counter()
    for _, _, timeline in _iter_sessions(corpus):
        for ev in timeline:
            if ev.get("kind") == "tool" and ev.get("name") == "bash":
                cmd = (ev.get("cmd") or "").strip()
                if cmd and VERIFY_RE.search(cmd):
                    short = cmd.split("&&")[0].strip()[:48]
                    cmds[short] += 1
    if cmds:
        return cmds.most_common(1)[0][0]
    return "the project test suite"


def _rules_generator_html(corpus, cards, source=None):
    """1-click copyable CLAUDE.md / AGENTS.md / .cursorrules block tailored to top weak dims."""
    cmd = _detect_verify_cmd(corpus)
    weak_dims = [c["dim"] for c in (cards or [])[:2]]
    rules = []
    if "Context" in weak_dims or "Direction" in weak_dims:
        rules.append("- Read the target file(s) before making any edit.")
    rules.append("- Before coding non-trivial changes, confirm the exact files and untouched invariants.")
    if "Iteration" in weak_dims:
        rules.append("- If a check fails, state the root cause and fix rule before applying the next edit.")
    rules.append(f"- Run `{cmd}` after edits and paste the real output before declaring done.")
    if "Toolcraft" in weak_dims:
        rules.append("- Prefer search/read tools over guessing paths; ask before adding new dependencies.")

    default_file = "CLAUDE.md"
    if source == "codex":
        default_file = "AGENTS.md"
    elif source == "cursor":
        default_file = ".cursorrules"

    tabs = []
    for fn in ("CLAUDE.md", "AGENTS.md", ".cursorrules"):
        act = " active" if fn == default_file else ""
        tabs.append(f'<button type="button" class="rftab{act}" onclick="setRuleFile(this,\'{fn}\')">{fn}</button>')

    body = "## AI Collaboration Rules\n" + "\n".join(rules[:4])
    return (
        '<div class="action-card rules-card">'
        f'<div class="panel-k">📋 Project guardrail <div class="rftabs">{"".join(tabs)}</div></div>'
        '<p class="muted-sm" style="margin-bottom:8px">Paste at the bottom of your <b class="rf-name">'
        f'{default_file}</b> so your agent enforces your #1 growth habits automatically:</p>'
        f'<pre class="rules-pre copy-src">{_esc(body)}</pre>'
        '<div style="display:flex;justify-content:flex-end;margin-top:8px">'
        '<button type="button" class="copy-btn" onclick="copyPre(this)">Copy rule block</button>'
        '</div></div>'
    )


def _best_prompts_html(best_prompts, anatomy_html=""):
    """Render Prompt Anatomy target bars + inline-highlighted 'Your best prompts' playbook."""
    key_legend = (
        '<div class="hl-key">'
        '<span>Inside each prompt:</span>'
        '<mark class="hl-t">Target</mark>'
        '<mark class="hl-r">Rule</mark>'
        '<mark class="hl-w">Why</mark>'
        '<mark class="hl-v">Check</mark>'
        '</div>'
    )
    if not best_prompts:
        body = (
            '<div class="empty-box">'
            'No multi-anchor prompts stood out yet — pair a goal with two or more anchors '
            '(a file/symbol, a clear rule, a why, or a test check) and your strongest real prompts will appear here.'
            '</div>'
        )
    else:
        cards = []
        for bp in best_prompts:
            chips = "".join(f'<span class="hchip">✓ {_esc(h)}</span>' for h in bp.get("habits") or [])
            src = bp.get("source")
            src_tag = f" · {_esc(NICE_SOURCE.get(src, src))}" if src else ""
            proj = _esc(bp.get("project", ""))
            cards.append(
                f'<div class="best-card">'
                f'<div class="best-top"><span class="best-loc">{proj}{src_tag}</span></div>'
                f'<p class="best-q">“{_highlight_prompt_html(bp["text"])}”</p>'
                f'<div class="best-chips">{chips}</div>'
                f'</div>'
            )
        body = f'{key_legend}<div class="best-grid">{"".join(cards)}</div>'
    return (
        '<section><h3>Your best prompts — your personal playbook</h3>'
        f'{anatomy_html}{body}</section>'
    )


def _growth_cards_html(analysis):
    """Split Opus rewrites into (featured_card_1, collapsed_more_cards) with inline highlights & Copy."""
    if not analysis or not isinstance(analysis, dict):
        return "", ""
    items = [g for g in (analysis.get("top_growth") or []) if isinstance(g, dict)]
    if not items:
        return "", ""
    featured = ""
    more = []
    for i, g in enumerate(items[:4]):
        title = _esc(g.get("title", "Your next growth move"))
        why = _esc(g.get("why", ""))
        how = _esc(g.get("how", ""))
        before = g.get("example_before")
        after = g.get("example_after")
        src = g.get("source")
        src_pill = f'<span class="tool-tag">{_esc(NICE_SOURCE.get(src, src))}</span>' if src else ""
        src_tag = f" · {_esc(NICE_SOURCE.get(src, src))}" if src else ""
        ba = ""
        if before and after:
            works = g.get("why_it_works")
            works_html = ""
            if isinstance(works, list) and works:
                works_html = ('<div class="works-chips">' +
                              "".join(f'<span>+ {_esc(str(w)[:140])}</span>' for w in works[:3]) +
                              '</div>')
            ba = (
                f'<div class="ba">'
                f'<div class="before"><div class="ba-h"><span>Before{src_tag}</span></div>'
                f'<p>“{_esc(str(before)[:400])}”</p></div>'
                f'<div class="after"><div class="ba-h"><span>How to write it instead</span>'
                f'<button type="button" class="copy-btn" onclick="copyPrompt(this)">Copy</button></div>'
                f'<p class="copy-src">“{_highlight_prompt_html(str(after)[:600])}”</p></div>'
                f'</div>{works_html}'
            )
        if i == 0:
            featured = (
                f'<div class="gcard featured">'
                f'<div class="focus-k">🎯 Start here · 01 · #1 move next session</div>'
                f'<div class="gcard-h" style="margin-top:6px"><div class="gtitle"><h4>{title}</h4></div>'
                f'<div class="gbadges">{src_pill}<span class="pill">written for you</span></div></div>'
                + (f'<p class="ghow"><b>Move:</b> {how}</p>' if how else "")
                + ba + '</div>'
            )
        else:
            teaser = f'“{_esc(str(before)[:42])}”' if before else why
            more.append(
                f'<details class="gmore"><summary>'
                f'<span class="gnum">0{i + 1}</span><b>{title}</b>'
                f'{src_pill}<span class="gmore-t">{teaser}</span><span class="gmore-c">Expand ▾</span>'
                f'</summary><div class="gmore-body">'
                + (f'<p class="gwhy" style="margin-bottom:6px">{why}</p>' if why else "")
                + (f'<p class="ghow" style="margin-top:0"><b>Move:</b> {how}</p>' if how else "")
                + ba + '</div></details>'
            )
    return featured, "".join(more)


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

def _project_label(name):
    """Claude encodes an absolute path with '-' for '/', so we can't perfectly
    recover hyphenated names. Drop the home/boilerplate prefix and show the rest.
    '-Users-me-Dropbox-AI-platzi-executive-assistant' -> 'AI platzi executive assistant'."""
    s = re.sub(r"^-?(?:Users|home)-[^-]+(?:-|$)", "", name)  # strip -Users-/-home-<user>- (mac & linux)
    s = re.sub(r"^Dropbox-", "", s)                          # strip a common cloud-folder prefix
    s = s.replace("-", " ").strip()
    # Nothing left -> the session ran in $HOME itself; never echo the raw name (it holds the username).
    if not s:
        return "home" if re.match(r"^-?(?:Users|home)-", name) else name
    return s


def terminal_summary(corpus, result, source=None):
    a = result["archetype"]
    lines = [
        "",
        f"  AI Fluency Score: {result['overall']}/100  ({result['band']})",
        f"  Archetype: {a['label']}",
        f"  Based on {len(corpus.real_prompts)} real prompts across {len(corpus.projects)} projects, "
        f"{corpus.files} sessions ({corpus.total_bytes/1e6:.1f} MB).",
    ]
    na = result.get("na_dims") or []
    if na:
        names = ", ".join(disp(n) for n in na)
        lines.append(f"  Note: {names} not measurable from {source or 'this source'}; "
                     f"the score covers the remaining dimensions.")
    lines.append("")
    return "\n".join(lines)


def _esc(s):
    return html.escape(str(s))


# Each archetype's encouraging "next gain" — frames the top growth lever as a natural
# progression for that style rather than a deficit.
ARCH_PATHS = {
    "Autonomous Agent": "You already hand off whole jobs well — add one sharp sentence of intent per hand-off and far more will land right the first time, with less back-and-forth.",
    "Architect": "Your planning is a real strength — pair it with a quick check after each change so your designs ship proven, not just drawn.",
    "Debugger": "Your diagnostic discipline is excellent — capture each fix as a small reusable rule so the same bug never costs you twice.",
    "Collaborator": "Your back-and-forth keeps things aligned — front-loading a constraint or two will get you there in fewer rounds.",
    "Sprinter": "Your speed is real — a one-line brief plus a quick test keeps that speed from turning into rework.",
}

_SIG_DESC = {
    "Delegation": "how much you hand off — you give the agent whole jobs and trust it to run them end-to-end",
    "Toolcraft": "the range of tools you bring to bear — you reach past the shell for the right instrument",
    "Iteration": "how cleanly you change course — your corrections tend to name the fix, not just reject",
    "Briefing": "how concretely you frame requests when it matters",
}

# The specific, evidence-grounded line that explains each dimension as a growth edge.
_GROWTH_LINE = {
    "Direction": "{s}s win on how sharply they frame the work they hand off — and right now yours are often one-liners like “{ex}”, so the agent fills gaps you could have decided.",
    "Verification": "Right now changes often move on without a test, build or run to confirm them — the cheapest reliability you can buy back.",
    "Context": "Right now some edits land before the file has been read that session — an easy blind-edit risk to remove.",
    "Iteration": "Right now corrections lean toward brief rejections; naming the symptom and the exact rule resolves loops in fewer turns.",
    "Toolcraft": "Right now most work funnels through one tool — reaching for search, planning and delegation widens what you can take on.",
}


def build_assessment(corpus, result, cards, source=None):
    """A coherent, truthful written read — synthesizes the numbers into one story
    grounded in THIS person's actual prompt style, top signal, and source tool(s)."""
    a = result["archetype"]
    arch = a["primary"]
    short = arch.replace("The ", "")
    art = "an" if short[:1] in "AEIOU" else "a"
    deleg = a["delegation_score"]
    n_deleg = corpus.delegation_events
    median = result["dist"].get("median_chars")
    measurable = set(result.get("measurable") or list(WEIGHTS))

    if result.get("combined"):
        agent_label = "AI"
    elif source in NICE_SOURCE:
        agent_label = NICE_SOURCE[source]
    else:
        agent_label = source or "Claude"

    if isinstance(median, int):
        if median < 80:
            prompt_style = f"short, fast prompts (median {median} characters)"
        elif median <= 200:
            prompt_style = f"concise, focused prompts (median {median} characters)"
        else:
            prompt_style = f"detailed, context-rich prompts (median {median} characters)"
    else:
        prompt_style = "your prompts"

    # Signature strength = strongest measurable USER-driven signal (not agent defaults).
    user_signals = {}
    if "Direction" in measurable:
        user_signals["Briefing"] = result["shrunk"]["Direction"]
    if "Iteration" in measurable:
        user_signals["Iteration"] = result["shrunk"]["Iteration"]
    if "Toolcraft" in measurable:
        user_signals["Toolcraft"] = result["shrunk"]["Toolcraft"]
        user_signals["Delegation"] = float(deleg)
    if not user_signals:
        user_signals["Briefing"] = result["shrunk"]["Direction"]
    sig = max(user_signals, key=user_signals.get)
    sig_score = round(user_signals[sig])

    if (sig == "Delegation" or deleg >= 45) and n_deleg > 0:
        lead_signal = (f"Your delegation rate stands out at <b>{deleg}/100</b> "
                       f"(from {n_deleg} hand-offs to subagents, background jobs and planning), "
                       f"paired with {prompt_style}.")
    else:
        lead_signal = (f"Your clearest self-driven signal is <b>{_esc(sig)} ({sig_score}/100)</b>, "
                       f"paired with {prompt_style}.")

    blurb = a["blurb"]
    if result.get("combined") or source not in (None, "claude-code", "claude-desktop"):
        blurb = blurb.replace("let Claude pick", "let the agent pick")
    p1 = f"You drive {_esc(agent_label)} like <b>{_esc(a['label'])}</b>. {_esc(blurb)} {lead_signal}"

    loop_ok = ("Context" in measurable and "Verification" in measurable
               and result["shrunk"]["Context"] >= 55 and result["shrunk"]["Verification"] >= 55)
    p2_mid = ("That, plus the disciplined read→edit→verify loop your sessions show, is"
              if loop_ok else "That is")
    p2 = (f"Your strongest <i>self-driven</i> habit is {_esc(_SIG_DESC.get(sig, sig.lower()))}. "
          f"{p2_mid} why your overall score lands at <b>{result['overall']}/100 ({_esc(result['band'])})</b>.")

    parts = [f'<p>{p1}</p>', f'<p>{p2}</p>']
    if cards:
        growth = cards[0]["dim"]
        growth_disp = disp(growth)
        example = _shortest_action_prompt(corpus) or "run it"
        path_why = ARCH_PATHS.get(arch, "Keep building the habits below and your next run will show the gain.")
        gline = _GROWTH_LINE.get(growth, "").format(s=_esc(short), ex=_esc(example))
        p3 = (f"Biggest lever: <b>{_esc(growth_disp)}</b> — {gline} {_esc(path_why)}")
        parts.append(f'<p>{p3}</p>')

    return "".join(parts)


def build_html(corpus, result, cards, strength, archive_info=None, analysis=None, analysis_note=None,
               source=None, history_runs=None):
    a = result["archetype"]
    d = result["dist"]
    has_ai = bool(analysis and isinstance(analysis, dict) and analysis.get("skill_map"))
    days = (corpus.last_ts - corpus.first_ts).days if corpus.first_ts and corpus.last_ts else 0
    active_h = corpus.active_seconds / 3600
    provisional = len(corpus.real_prompts) < PROVISIONAL_MIN_PROMPTS

    def dim_rate_line(name):
        det = result["detail"][name]
        if name == "Verification" and det.get("rate") is not None:
            return f"{det['verified']}/{det['episodes']} edit-bursts verified ({det['rate']*100:.0f}%)"
        if name == "Context" and det.get("rate") is not None:
            return f"{det['grounded']}/{det['total_edits']} edits read-first ({det['rate']*100:.0f}%)"
        if name == "Direction":
            return (f"{det['constraint_rate']*100:.0f}% rules · "
                    f"{det['artifact_rate']*100:.0f}% targets · {det['intent_rate']*100:.0f}% why")
        if name == "Iteration":
            return f"{det['corrections']} corrections ({det['correction_rate']*100:.0f}%); {det['specificity']*100:.0f}% specific"
        if name == "Toolcraft":
            return f"{det.get('distinct', 0)} tools · evenness {det.get('evenness', 0.0):.2f} · {det.get('delegation_events', 0)} delegations"
        return ""

    # source-aware framing
    combined = bool(result.get("combined"))
    srcs = result.get("sources") or []
    if combined:
        tool_list = " · ".join(NICE_SOURCE.get(s["source"], s["source"]) for s in srcs)
        meta_line = f"{tool_list} · {len(corpus.real_prompts):,} prompts · {days}d span"
        hero_srcline = f'<div class="g-subline">{len(srcs)} tools blended · {len(corpus.real_prompts):,} prompts</div>'
        agent_noun = "your AI tools"
    else:
        nice_src = NICE_SOURCE.get(source, source or "Claude Code")
        meta_line = f"{nice_src} · {len(corpus.real_prompts):,} prompts · {days}d span"
        hero_srcline = ""
        agent_noun = nice_src

    # Hero Visuals: Gauge + 5-Axis Radar + Compact Driving Style (with inline archetype bars!)
    gauge_html = _svg_gauge(result["overall"], result["band"], result["overall_raw"], hero_srcline)
    radar_html = _svg_radar(result)
    arch_blurb = a["blurb"]
    if combined or source not in (None, "claude-code", "claude-desktop"):
        arch_blurb = arch_blurb.replace("let Claude pick", "let the agent pick")
    aff_rows = []
    for sim, nm in a["all"]:
        pct = max(4, round((sim + 1) / 2 * 100))
        is_top = " top" if nm == a["primary"] else ""
        aff_rows.append(
            f'<div class="mini-bar{is_top}"><span>{PROTOTYPES[nm]["emoji"]} {_esc(nm)}</span>'
            f'<b><i style="width:{pct}%"></i></b><em>{sim:+.2f}</em></div>'
        )
    arch_hedge = (
        f'<div class="mini-warn">Provisional ({len(corpus.real_prompts)} prompts) — style may shift.</div>'
        if provisional else ""
    )

    # Multi-source section: source cards + color-coded Tool x Dimension Heatmap
    source_mix_html = ""
    if combined and srcs:
        tiles = []
        for s in srcs:
            na_note = (f'<span class="sm-na">{", ".join(disp(n) for n in s["not_measurable"])} n/a</span>'
                       if s["not_measurable"] else "")
            tone = "ok" if s["overall"] >= 75 else ("warn" if s["overall"] < 55 else "acc")
            tiles.append(
                f'<div class="src">'
                f'<div class="src-top"><span class="sn">{_esc(NICE_SOURCE.get(s["source"], s["source"]))}</span>'
                f'<span class="sb">{_esc(s["band"])}</span></div>'
                f'<div class="sv">{s["overall"]}<span>/100</span></div>'
                f'<div class="sbar"><i class="{tone}" style="width:{s["overall"]}%"></i></div>'
                f'<div class="sd">{s["real_prompts"]:,} prompts · {s["sessions"]} sess · {s["active_hours"]:.0f} h {na_note}</div>'
                f'</div>'
            )
        cmp_html = ""
        if len(srcs) >= 2 and all(isinstance(s.get("dims"), dict) for s in srcs):
            th = "<th>Dimension</th>" + "".join(
                f'<th>{_esc(NICE_SOURCE.get(s["source"], s["source"]))}</th>' for s in srcs
            )
            rows = []
            for dname in WEIGHTS:
                cells = []
                for s in srcs:
                    v = s["dims"].get(dname)
                    if v is None:
                        cells.append('<td class="hm na">—</td>')
                    else:
                        iv = int(round(v))
                        tier = "hm-hi" if iv >= 75 else ("hm-lo" if iv < 55 else "hm-mid")
                        cells.append(f'<td class="hm {tier}">{iv}</td>')
                rows.append(f'<tr><td>{_esc(disp(dname))}</td>{"".join(cells)}</tr>')
            cmp_html = (
                '<div class="cmp-wrap">'
                '<div class="subh">How your habits compare across tools <span class="muted-sm">(heatmap · “—” = not logged)</span></div>'
                f'<table class="cmp"><thead><tr>{th}</tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table></div>'
            )
        source_mix_html = (
            '<section><h3>Where this comes from</h3>'
            f'<div class="src-grid">{"".join(tiles)}</div>{cmp_html}</section>'
        )

    # Unified Skill Map — Tufte bullet-chart rows (used both nested under 4Ds and standalone!)
    skill_by_dim = {sk["dim"]: sk for sk in _skill_levels(result)}
    na_set = set(result.get("na_dims") or [])
    src_label = source or "this source"
    honesty_na_html = ""
    if na_set:
        names = ", ".join(disp(n) for n in WEIGHTS if n in na_set)
        verb = "is" if len(na_set) == 1 else "are"
        honesty_na_html = (
            f'<div class="notice"><b>{_esc(names)}</b> {verb} not observable in {_esc(src_label)} logs — '
            f'excluded rather than guessed (weights renormalized).</div>'
        )

    dim_rows = []
    det_rows = []
    order = sorted((n for n in WEIGHTS if n not in na_set),
                   key=lambda n: result["shrunk"][n], reverse=True)
    for name in list(order) + [n for n in WEIGHTS if n in na_set]:
        if name in na_set:
            na_row = (
                f'<div class="trow na-row">'
                f'<div class="trow-l"><b>{_esc(disp(name))}</b> <span class="tag ld">not measurable</span>'
                f'<div class="trow-r">Not observable in {_esc(src_label)} logs</div></div>'
                f'<div class="trow-m"><div class="bullet"></div></div>'
                f'<div class="trow-s">—</div></div>'
            )
            dim_rows.append(na_row)
            det_rows.append(na_row)
            continue

        sc = round(result["shrunk"][name])
        lowdata = result["conf"][name] < 0.75
        tag = ""
        if name == strength:
            tag = '<span class="tag s">Strength</span>'
        elif cards and name == cards[0]["dim"]:
            tag = '<span class="tag w">Top lever</span>'
        ld = '<span class="tag ld">low data</span>' if lowdata else ""
        tone = "ok" if sc >= 75 else ("bad" if sc < 55 else "acc")
        rate_str = _esc(dim_rate_line(name))
        src_note = _dim_sources_note(result, name, len(srcs))
        wt = int(WEIGHTS[name] * 100)

        dim_rows.append(
            f'<div class="trow">'
            f'<div class="trow-l"><b>{_esc(disp(name))}</b> {tag}{ld}'
            f'<div class="trow-r">{rate_str}<span class="wt"> · {wt}% wt</span>{src_note}</div></div>'
            f'<div class="trow-m"><div class="bullet"><i class="{tone}" style="width:{sc}%"></i>'
            f'<b class="tick" style="left:85%" title="Target 85"></b></div></div>'
            f'<div class="trow-s">{sc}<span>/100</span></div></div>'
        )
        sk = skill_by_dim[name]
        det_rows.append(
            f'<div class="trow det-row">'
            f'<div class="trow-l"><b>{_esc(disp(name))}</b> {tag}{ld}'
            f'<div class="trow-r">{rate_str}<span class="wt"> · {wt}% wt</span>{src_note}</div>'
            f'<div class="trow-coach"><span>Now:</span> {_esc(sk["now"])} &nbsp;<b>Next →</b> {_esc(sk["next"])}</div></div>'
            f'<div class="trow-m"><div class="trow-lvl">{_dots_html(sk["level"])} <span>Level {sk["level"]}/5</span></div>'
            f'<div class="bullet"><i class="{tone}" style="width:{sc}%"></i>'
            f'<b class="tick" style="left:85%" title="Target 85"></b></div></div>'
            f'<div class="trow-s">{sc}<span>/100</span></div></div>'
        )

    dim_table_html = honesty_na_html + '<div class="tufte">' + "".join(dim_rows) + '</div>'
    det_table_html = honesty_na_html + '<div class="tufte">' + "".join(det_rows) + '</div>'

    if has_ai:
        skill_map_section = _analysis_section_html(analysis, dim_html=dim_table_html)
    else:
        skill_map_section = f'<section><h3>Your skill map</h3>{det_table_html}</section>'

    analysis_status_html = ""
    if not has_ai and analysis_note:
        analysis_status_html = (
            '<div class="prov">ℹ️ <b>Deterministic report only.</b> '
            f'{_esc(analysis_note)} — the Sonnet&nbsp;+&nbsp;Opus skill map was not added. '
            'Re-run <code>/ai-fluency</code> inside Claude Code to include it.</div>'
        )

    ai_read = (analysis.get("overall_read") or analysis.get("summary")) if has_ai else None
    coach_read_html = (
        f'<div class="takeaway"><div class="tk-k">Coach takeaway</div><p>{_esc(ai_read)}</p></div>'
        if ai_read else
        _verdict_strip_html(corpus, result, cards, strength)
    )

    # Deterministic evidence quotes & growth cards (featured #1 + collapsed #2 accordion)
    def evidence_html(card):
        name = card["dim"]
        ev = card["weak"]
        if not ev:
            return '<span class="ev-none">No weak moments spotted — already a habit. ✓</span>'
        proj_counts = Counter(p["project"] for p in corpus.real_prompts)
        items = []
        for e in ev[:3]:
            src_tag = f" · {_esc(NICE_SOURCE.get(e['source'], e['source']))}" if e.get("source") else ""
            if name in ("Direction", "Iteration"):
                proj = e["project"]; txt = _scrub_paths(e["text"])
                small = " (small sample)" if proj_counts.get(proj, 0) < 10 else ""
                items.append(f'<li>“{_esc(txt[:120])}” <span class="loc">— {_esc(_project_label(proj))}{src_tag}{small}</span></li>')
            elif name == "Context":
                items.append(f'<li>Edited <code>{_esc(os.path.basename(e["file"]))}</code> without reading first <span class="loc">— {_esc(_project_label(e["project"]))}{src_tag}</span></li>')
            elif name == "Verification":
                items.append(f'<li>Edits to <code>{_esc(e["files"])}</code> with no check after <span class="loc">— {_esc(_project_label(e["project"]))}{src_tag}</span></li>')
        return f'<ul class="mini-ev">{"".join(items)}</ul>'

    det_featured = ""
    det_more = []
    for i, card in enumerate(cards[:2]):
        name = card["dim"]
        t = SKILL_TEACH[name]
        ex = t["examples"][0]
        ba_det = (
            f'<div class="gwhere"><span class="subk">From your sessions</span>{evidence_html(card)}</div>'
            f'<p class="ghow"><b>Move:</b> {_esc(t["practice"])}</p>'
            f'<p class="exgen">Generic illustration — <b>not</b> from your sessions:</p>'
            f'<div class="ba">'
            f'<div class="before"><div class="ba-h"><span>Instead of</span></div><p>“{_esc(ex["before"])}”</p></div>'
            f'<div class="after"><div class="ba-h"><span>Stronger</span>'
            f'<button type="button" class="copy-btn" onclick="copyPrompt(this)">Copy</button></div>'
            f'<p class="copy-src">“{_highlight_prompt_html(ex["after"])}”</p></div></div>'
        )
        if i == 0:
            det_featured = (
                f'<div class="gcard featured">'
                f'<div class="focus-k">🎯 Start here · 01 · #1 move ({_esc(disp(name))})</div>'
                f'<div class="gcard-h" style="margin-top:6px"><div class="gtitle"><h4>{_esc(disp(name))}</h4>'
                f'<p class="gwhy">{_esc(t["why_it_matters"])}</p></div>'
                f'<div class="gbadges"><span class="pill">{card["score"]}/100</span></div></div>'
                f'{ba_det}</div>'
            )
        else:
            det_more.append(
                f'<details class="gmore"><summary>'
                f'<span class="gnum">0{i + 1}</span><b>{_esc(disp(name))}</b>'
                f'<span class="pill">{card["score"]}/100</span>'
                f'<span class="gmore-t">{_esc(t["practice"][:64])}…</span><span class="gmore-c">Expand ▾</span>'
                f'</summary><div class="gmore-body">{ba_det}</div></details>'
            )

    # Top merged Start-here focus card + right-hand Score Upside & Keep-doing-this column
    ai_featured, ai_more = _growth_cards_html(analysis)
    if ai_featured:
        featured_card = ai_featured
        more_cards = ai_more
    else:
        featured_card = det_featured
        more_cards = "".join(det_more)

    impact_html = _impact_chart_html(cards)
    s_det = dim_rate_line(strength)
    strong_score = round(result["shrunk"][strength])
    s_head = "Keep doing this" if strong_score >= 55 else "Strongest base"
    strength_card = (
        f'<div class="keep-card"><div class="keep-k">✓ {s_head} · {_esc(disp(strength))} ({strong_score}/100)</div>'
        f'<div class="keep-v">{_esc(s_det)}</div></div>'
    )
    side_col_html = f'<div class="side-col">{impact_html}{strength_card}</div>'

    # Interactive Rewrite Dojo + 1-click Drop-In Guardrail Rules
    dojo_html = _dojo_html(corpus, cards, analysis=analysis if has_ai else None)
    rules_src = (srcs[0]["source"] if (combined and len(srcs) == 1) else (None if combined else source))
    rules_html = _rules_generator_html(corpus, cards, source=rules_src)
    action_row_html = f'<div class="action-row">{dojo_html}{rules_html}</div>'

    # Prompt Anatomy + Your Best Prompts
    best_prompts_html = _best_prompts_html(_best_prompts(corpus), anatomy_html=_prompt_anatomy_html(result))

    # Session Trajectory + Project Breakdown + First-Try vs Retry Spiral
    fallback_caps = {
        "prompts": True, "edits": True,
        "verify": "Verification" not in na_set,
        "reads": "Context" not in na_set,
        "delegation": "Toolcraft" not in na_set,
    }
    patterns_html = _patterns_section_html(
        corpus, result, fallback_caps=fallback_caps,
        history=history_runs if "history_runs" in locals() else None,
    )

    # Archive tile + conditional Claude Code 30-day retention note
    archive_tile = retention_note = ""
    arch_dir_disp = _esc(archive_info["dir"]) if archive_info else _esc(DEFAULT_ARCHIVE_DIR)
    if archive_info:
        archive_tile = (f'<div class="kpi"><div class="n">{archive_info["archived_sessions"]:,}</div>'
                        f'<div class="l">sessions in your archive</div></div>')
    has_claude_code = (
        (not combined and source in (None, "claude-code"))
        or (combined and any(s["source"] == "claude-code" for s in srcs))
    )
    if days <= 32 and has_claude_code:
        grew = (f' Preserved <b>{archive_info["new"]:,}</b> new session(s) to <code>{arch_dir_disp}</code>.'
                if (archive_info and archive_info.get("enabled")) else "")
        retention_note = (
            f'<div class="notice" style="margin-top:12px"><b>Why ~{days} days?</b> Claude Code deletes transcripts '
            f'older than <code>cleanupPeriodDays</code> (default 30). Raise it in <code>~/.claude/settings.json</code> '
            f'(e.g. <code>"cleanupPeriodDays": 365</code>) to keep a full year.{grew}</div>'
        )

    prov_banner = (
        f'<div class="prov">⚠️ Provisional: {len(corpus.real_prompts)} real prompts — treat score as ±10; '
        f'sharpens as you build with {_esc(agent_noun)} more.</div>'
        if provisional else ""
    )

    low_dims = [disp(n) for n in (result.get("measurable") or list(WEIGHTS)) if result["conf"].get(n, 1.0) < 0.75]
    if low_dims:
        low_data_note = f'On this run, {", ".join(low_dims)} had limited data and {"is" if len(low_dims) == 1 else "are"} flagged “low data”.'
    else:
        low_data_note = "All measured dimensions reached strong data confidence on this run."

    data_viz_html = _data_visuals_html(corpus)

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your AI Fluency Report</title>
<style>
:root{{
  --bg:#f6f5f0;--surface:#ffffff;--inset:#f0efe9;--line:#e4e2da;--track:#e8e6de;
  --ink:#161615;--ink2:#3f3e3a;--mut:#6d6c66;
  --accent:#2563eb;--accent-soft:rgba(37,99,235,.12);
  --ok:#15803d;--ok-bg:#eaf7ee;--ok-bd:#bbf0cb;
  --warn:#b45309;--warn-bg:#fef6e6;--warn-bd:#f7d89c;
  --bad:#be123c;--bad-bg:#fef0f2;--bad-bd:#f8c4ce;
  --hl-t:rgba(37,99,235,.14);--hl-r:rgba(180,83,9,.16);--hl-w:rgba(14,116,144,.14);--hl-v:rgba(21,128,61,.16);
}}
[data-theme="dark"]{{
  --bg:#101214;--surface:#171a1e;--inset:#1f2329;--line:#292e36;--track:#262b33;
  --ink:#f3f4f6;--ink2:#d1d5db;--mut:#8f96a3;
  --accent:#60a5fa;--accent-soft:rgba(96,165,250,.15);
  --ok:#34d399;--ok-bg:rgba(52,211,153,.11);--ok-bd:rgba(52,211,153,.28);
  --warn:#fbbf24;--warn-bg:rgba(251,191,36,.11);--warn-bd:rgba(251,191,36,.28);
  --bad:#fb7185;--bad-bg:rgba(251,113,133,.11);--bad-bd:rgba(251,113,133,.28);
  --hl-t:rgba(96,165,250,.22);--hl-r:rgba(251,191,36,.22);--hl-w:rgba(34,211,238,.18);--hl-v:rgba(52,211,153,.22);
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,'Inter','Segoe UI',Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;padding-bottom:72px;font-variant-numeric:tabular-nums}}
.wrap{{max-width:1040px;margin:0 auto;padding:0 24px}}
.topbar{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:24px 0 18px;border-bottom:1px solid var(--line);flex-wrap:wrap}}
.brand{{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}}
.brand h1{{font-size:20px;font-weight:700;letter-spacing:-.02em}}
.brand .meta{{font-size:13px;color:var(--mut)}}
.theme-btn{{background:var(--surface);color:var(--ink2);border:1px solid var(--line);border-radius:8px;padding:6px 12px;
  font-size:12.5px;font-weight:600;cursor:pointer}}
.theme-btn:hover{{border-color:var(--accent);color:var(--accent)}}
.prov{{background:var(--warn-bg);border:1px solid var(--warn-bd);color:var(--ink);border-radius:10px;padding:10px 14px;margin-top:16px;font-size:13.5px}}
.hero{{display:grid;grid-template-columns:230px 1fr 290px;gap:14px;margin-top:20px;align-items:stretch}}
.panel{{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:18px 20px}}
.panel-k{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;gap:8px}}
.legend-t{{font-weight:500;letter-spacing:0;text-transform:none;color:var(--ok);font-size:11px}}
.gauge-wrap{{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%}}
.gauge-svg{{width:190px;max-width:100%;height:auto;overflow:visible}}
.g-num{{font-size:44px;font-weight:800;fill:var(--ink);letter-spacing:-.03em}}
.g-sub{{font-size:12px;fill:var(--mut)}}
.g-band{{font-size:14px;font-weight:700;fill:var(--accent)}}
.g-meta{{display:flex;gap:10px;font-size:11.5px;color:var(--mut);margin-top:4px;flex-wrap:wrap;justify-content:center}}
.g-subline{{font-size:11.5px;color:var(--mut);margin-top:4px;text-align:center}}
.radar-wrap{{display:flex;flex-direction:column;height:100%}}
.radar-svg{{width:100%;max-height:230px;margin:auto 0}}
.r-lbl{{font-size:11px;fill:var(--ink2);font-weight:600}}
.r-lbl.na{{fill:var(--mut);font-weight:500}}
.r-val{{fill:var(--ink);font-weight:800}}
.arch-h{{display:flex;align-items:center;gap:8px;margin-bottom:4px}}
.arch-h h2{{font-size:17px;font-weight:700;letter-spacing:-.015em}}
.arch-b{{font-size:13px;color:var(--mut);margin-bottom:12px;line-height:1.45}}
.mini-bar{{display:grid;grid-template-columns:126px 1fr 38px;align-items:center;gap:8px;font-size:12px;color:var(--mut);margin:5px 0}}
.mini-bar.top{{color:var(--ink);font-weight:600}}
.mini-bar b{{display:block;height:6px;background:var(--track);border-radius:99px;overflow:hidden}}
.mini-bar b>i{{display:block;height:100%;background:var(--mut);border-radius:99px;opacity:.45}}
.mini-bar.top b>i{{background:var(--accent);opacity:1}}
.mini-bar em{{font-style:normal;text-align:right;font-size:11.5px}}
.mini-warn{{font-size:11.5px;color:var(--warn);margin-top:8px}}
.takeaway{{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:13px 18px;margin-top:14px;font-size:14px;color:var(--ink2);line-height:1.55}}
.takeaway b{{color:var(--ink)}}
.tk-k{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:var(--accent);margin-bottom:4px}}
.verdict-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:14px}}
.vcard{{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px 15px}}
.vk{{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--mut)}}
.v-ok .vk{{color:var(--ok)}} .v-warn .vk{{color:var(--warn)}} .v-acc .vk{{color:var(--accent)}}
.vt{{font-size:15.5px;font-weight:700;color:var(--ink);margin:3px 0 2px}}
.vs{{font-size:12.5px;color:var(--mut);line-height:1.4}}
section{{margin:30px 0}}
h3{{font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--mut);padding-bottom:8px;border-bottom:1px solid var(--line);margin-bottom:14px}}
.subh{{font-size:12px;font-weight:700;color:var(--ink2);margin:18px 0 8px;display:flex;align-items:center;gap:8px}}
.muted-sm{{font-size:12px;color:var(--mut);font-weight:400}}
.focus-row{{display:grid;grid-template-columns:1.5fr 1fr;gap:14px;margin-bottom:12px;align-items:stretch}}
.side-col{{display:flex;flex-direction:column;gap:12px}}
.focus-k{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:var(--accent)}}
.impact-box{{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px 18px;flex:1;display:flex;flex-direction:column;justify-content:center}}
.imp-row{{display:grid;grid-template-columns:104px 1fr 56px;align-items:center;gap:10px;margin:5px 0;font-size:12.5px}}
.imp-n{{color:var(--ink2);font-weight:600}}
.imp-tr{{height:8px;background:var(--track);border-radius:99px;overflow:hidden}}
.imp-tr i{{display:block;height:100%;border-radius:99px}}
.imp-tr i.bad{{background:var(--bad)}} .imp-tr i.warn{{background:var(--warn)}}
.imp-v{{text-align:right;font-weight:700;color:var(--ink);font-size:12px}}
.keep-card{{background:var(--ok-bg);border:1px solid var(--ok-bd);border-radius:12px;padding:13px 16px}}
.keep-k{{font-weight:700;color:var(--ok);font-size:13px}}
.keep-v{{color:var(--ink2);font-size:12.5px;margin-top:2px}}
.gcard{{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px 20px}}
.gcard.featured{{margin-bottom:0}}
.gcard-h{{display:flex;align-items:flex-start;gap:12px;justify-content:space-between;flex-wrap:wrap}}
.gnum{{font-family:ui-monospace,'SF Mono',Menlo,monospace;font-size:11.5px;font-weight:700;color:var(--mut);
  background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:2px 7px}}
.gtitle{{flex:1;min-width:200px}}
.gtitle h4{{font-size:16px;font-weight:700;letter-spacing:-.01em;color:var(--ink)}}
.gwhy{{font-size:13px;color:var(--mut);margin-top:2px}}
.gbadges{{display:flex;gap:6px;align-items:center;flex-wrap:wrap}}
.pill{{font-size:11px;font-weight:600;color:var(--ink2);background:var(--inset);border:1px solid var(--line);border-radius:99px;padding:2px 9px;white-space:nowrap}}
.tool-tag{{font-size:11px;font-weight:700;color:var(--accent);background:var(--accent-soft);border-radius:99px;padding:2px 9px;white-space:nowrap}}
.ghow{{font-size:13.5px;color:var(--ink2);margin:8px 0}}
.gwhere{{margin:8px 0 6px}}
.subk{{display:block;font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin-bottom:4px}}
.exgen{{font-size:12px;color:var(--mut);font-style:italic;margin:6px 0}}
.ba{{display:grid;grid-template-columns:1fr 1.35fr;gap:10px;margin-top:6px}}
.before,.after{{border-radius:10px;padding:11px 13px;font-size:13px;font-family:ui-monospace,'SF Mono',Menlo,monospace;line-height:1.55}}
.before{{background:var(--bad-bg);border:1px solid var(--bad-bd);color:var(--ink)}}
.after{{background:var(--ok-bg);border:1px solid var(--ok-bd);color:var(--ink)}}
.ba-h{{display:flex;justify-content:space-between;align-items:center;margin-bottom:5px;font-family:-apple-system,BlinkMacSystemFont,'Inter',sans-serif}}
.ba-h span{{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--mut)}}
.copy-btn{{background:var(--surface);color:var(--ink2);border:1px solid var(--line);border-radius:6px;padding:2px 8px;font-size:11px;font-weight:600;cursor:pointer}}
.copy-btn:hover{{border-color:var(--ok);color:var(--ok)}}
.works-chips{{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}}
.works-chips span{{font-size:11.5px;color:var(--ink2);background:var(--inset);border:1px solid var(--line);border-radius:7px;padding:2px 8px}}
details.gmore{{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:0;margin-top:10px;overflow:hidden}}
details.gmore>summary{{display:flex;align-items:center;gap:10px;padding:11px 16px;cursor:pointer;list-style:none;font-size:13.5px;color:var(--ink);flex-wrap:wrap}}
details.gmore>summary::-webkit-details-marker{{display:none}}
details.gmore>summary b{{font-weight:600}}
.gmore-t{{color:var(--mut);font-size:12.5px;flex:1;min-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.gmore-c{{font-size:11.5px;color:var(--accent);font-weight:600;margin-left:auto}}
.gmore-body{{padding:0 16px 14px;border-top:1px solid var(--line);padding-top:12px}}
.action-row{{display:grid;grid-template-columns:1.15fr 1fr;gap:14px;margin-top:14px}}
.action-card{{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:15px 18px}}
.dchips{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}}
.dchip{{background:var(--inset);color:var(--ink2);border:1px solid var(--line);border-radius:99px;padding:3px 10px;font-size:11.5px;cursor:pointer;font-family:ui-monospace,'SF Mono',Menlo,monospace}}
.dchip.active{{background:var(--accent-soft);color:var(--accent);border-color:var(--accent);font-weight:600}}
.dojo-in{{width:100%;border:1px solid var(--line);border-radius:10px;padding:9px 11px;background:var(--inset);color:var(--ink);
  font:13px/1.5 ui-monospace,'SF Mono',Menlo,monospace;resize:vertical}}
.dojo-in:focus{{outline:2px solid var(--accent-soft);border-color:var(--accent)}}
.dojo-lights{{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:8px}}
.dl{{font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;background:var(--inset);color:var(--mut);border:1px solid var(--line)}}
.dl.on{{background:var(--ok-bg);color:var(--ok);border-color:var(--ok-bd)}}
.dojo-score{{font-size:11px;font-weight:700;color:var(--accent);text-transform:none;letter-spacing:0}}
.dojo-btns{{margin-left:auto;display:flex;gap:6px}}
.dojo-peek{{margin-top:8px;padding:8px 10px;border-radius:8px;background:var(--ok-bg);border:1px solid var(--ok-bd);font:12px/1.45 ui-monospace,'SF Mono',Menlo,monospace;color:var(--ink)}}
.rftabs{{display:inline-flex;gap:4px}}
.rftab{{background:var(--inset);color:var(--mut);border:1px solid var(--line);border-radius:6px;padding:2px 7px;font-size:10.5px;font-weight:700;cursor:pointer;text-transform:none;letter-spacing:0}}
.rftab.active{{background:var(--accent-soft);color:var(--accent);border-color:var(--accent)}}
.rules-pre{{background:var(--inset);border:1px solid var(--line);border-radius:10px;padding:10px 12px;font:12px/1.5 ui-monospace,'SF Mono',Menlo,monospace;
  color:var(--ink);white-space:pre-wrap;word-break:break-word}}
mark{{color:inherit;border-radius:4px;padding:0 3px;font-weight:500}}
mark.hl-t{{background:var(--hl-t)}} mark.hl-r{{background:var(--hl-r)}} mark.hl-w{{background:var(--hl-w)}} mark.hl-v{{background:var(--hl-v)}}
.hl-key{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:11.5px;color:var(--mut);margin-bottom:10px}}
.anat-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}}
.anat{{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px 14px}}
.anat-top{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:7px}}
.anat-l{{font-size:12.5px;font-weight:600;color:var(--ink2)}}
.anat-v{{font-size:18px;font-weight:800}}
.anat-v.ok{{color:var(--ok)}} .anat-v.warn{{color:var(--warn)}} .anat-v.bad{{color:var(--bad)}}
.anat-s{{font-size:11.5px;color:var(--mut);margin-top:6px}}
.bullet{{position:relative;height:8px;background:var(--track);border-radius:99px;overflow:visible}}
.bullet>i{{display:block;height:100%;border-radius:99px}}
.bullet>i.ok{{background:var(--ok)}} .bullet>i.acc{{background:var(--accent)}} .bullet>i.warn{{background:var(--warn)}} .bullet>i.bad{{background:var(--bad)}}
.bullet .tick{{position:absolute;top:-3px;width:2px;height:14px;background:var(--ink);opacity:.45;border-radius:1px}}
.best-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}}
.best-card{{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px;display:flex;flex-direction:column;gap:8px}}
.best-top{{display:flex;justify-content:space-between;align-items:center}}
.best-loc{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--mut)}}
.best-q{{font-size:13px;color:var(--ink);font-family:ui-monospace,'SF Mono',Menlo,monospace;line-height:1.55;flex:1}}
.best-chips{{display:flex;flex-wrap:wrap;gap:5px}}
.hchip{{font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;background:var(--ok-bg);color:var(--ok);border:1px solid var(--ok-bd)}}
.empty-box{{background:var(--surface);border:1px dashed var(--line);border-radius:12px;padding:14px 16px;font-size:13.5px;color:var(--mut)}}
.viz-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
.viz-grid.two{{grid-template-columns:repeat(2,1fr)}}
.viz-card{{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
.traj-svg{{width:100%;height:auto;display:block}}
.tr-v{{font-size:10px;font-weight:700;fill:var(--ink)}}
.tr-x{{font-size:10px;fill:var(--mut)}}
.delta-pill{{font-size:10.5px;font-weight:700;padding:1px 7px;border-radius:99px;text-transform:none;letter-spacing:0}}
.delta-pill.ok{{background:var(--ok-bg);color:var(--ok)}} .delta-pill.bad{{background:var(--bad-bg);color:var(--bad)}} .delta-pill.acc{{background:var(--accent-soft);color:var(--accent)}}
.proj-row{{display:grid;grid-template-columns:108px 1fr 62px;align-items:center;gap:9px;margin:7px 0;font-size:12px}}
.proj-n{{color:var(--ink2);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.proj-m{{text-align:right;color:var(--mut);font-size:11.5px}} .proj-m b{{color:var(--ink)}}
.comp-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}}
.comp-card{{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:15px 17px;display:flex;flex-direction:column;gap:7px}}
.comp-h{{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}}
.comp-h b{{font-size:15.5px}}
.comp-lvl{{display:inline-flex;align-items:center;gap:8px}}
.segs{{display:inline-flex;gap:3px}}
.segs i{{display:inline-block;width:12px;height:6px;border-radius:2px;background:var(--track)}}
.segs i.on{{background:var(--accent)}}
.comp-s{{font-size:13.5px;color:var(--ink2)}}
.mini-ev{{list-style:none;display:flex;flex-direction:column;gap:4px}}
.mini-ev li{{font-size:12.5px;color:var(--mut);background:var(--inset);border-radius:7px;padding:5px 9px}}
.loc{{color:var(--mut);font-size:11.5px}}
.comp-next{{font-size:13px;color:var(--ink);padding-top:6px;border-top:1px solid var(--line);margin-top:auto}}
.comp-next span{{color:var(--accent);font-weight:700}}
.str-strip{{display:flex;flex-wrap:wrap;align-items:center;gap:7px;margin-top:12px}}
.str-k{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin-right:4px}}
.str-chip{{font-size:12.5px;color:var(--ok);background:var(--ok-bg);border:1px solid var(--ok-bd);border-radius:8px;padding:3px 10px}}
.tufte{{background:var(--surface);border:1px solid var(--line);border-radius:12px;overflow:hidden}}
.trow{{display:grid;grid-template-columns:1.5fr 1fr 72px;align-items:center;gap:16px;padding:12px 16px;border-bottom:1px solid var(--line)}}
.trow:last-child{{border-bottom:none}}
.trow-l b{{font-size:14.5px}}
.trow-r{{font-size:12px;color:var(--mut);margin-top:2px}} .wt{{opacity:.75}}
.trow-coach{{font-size:12.5px;color:var(--ink2);margin-top:5px}} .trow-coach span{{color:var(--mut);font-weight:600}}
.trow-lvl{{display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--mut);margin-bottom:5px}}
.trow-s{{text-align:right;font-size:18px;font-weight:800}} .trow-s span{{font-size:11px;font-weight:400;color:var(--mut)}}
.tag{{font-size:10px;padding:2px 7px;border-radius:99px;font-weight:700;margin-left:5px;vertical-align:middle}}
.tag.s{{background:var(--ok-bg);color:var(--ok)}} .tag.w{{background:var(--bad-bg);color:var(--bad)}} .tag.ld{{background:var(--inset);color:var(--mut)}}
.notice{{background:var(--inset);border:1px solid var(--line);border-radius:10px;padding:10px 14px;font-size:13px;color:var(--ink2);margin-bottom:10px}}
.src-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}}
.src{{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
.src-top{{display:flex;justify-content:space-between;align-items:center}}
.src .sn{{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--ink2)}}
.src .sb{{font-size:11px;color:var(--mut)}}
.src .sv{{font-size:26px;font-weight:800;margin:4px 0 6px;letter-spacing:-.02em}} .src .sv span{{font-size:12px;font-weight:400;color:var(--mut)}}
.sbar{{height:6px;background:var(--track);border-radius:99px;overflow:hidden;margin-bottom:8px}}
.sbar i{{display:block;height:100%;border-radius:99px}}
.sbar i.ok{{background:var(--ok)}} .sbar i.acc{{background:var(--accent)}} .sbar i.warn{{background:var(--warn)}}
.src .sd{{font-size:12px;color:var(--mut)}} .sm-na{{color:var(--warn);font-weight:600}}
.cmp-wrap{{overflow-x:auto}}
.cmp{{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:13.5px}}
.cmp th,.cmp td{{padding:9px 14px;text-align:center;border-bottom:1px solid var(--line)}}
.cmp th:first-child,.cmp td:first-child{{text-align:left;font-weight:600;color:var(--ink)}}
.cmp th{{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);background:var(--inset)}}
.cmp tr:last-child td{{border-bottom:none}}
.hm{{font-weight:700}}
.hm-hi{{background:var(--ok-bg);color:var(--ok)}}
.hm-mid{{background:var(--inset);color:var(--ink2)}}
.hm-lo{{background:var(--bad-bg);color:var(--bad)}}
.hm.na{{color:var(--mut);font-weight:400}}
.kpi-strip{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:12px}}
.kpi{{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:11px 13px}}
.kpi .n{{font-size:20px;font-weight:800;letter-spacing:-.02em;color:var(--ink)}}
.kpi .l{{font-size:11.5px;color:var(--mut);margin-top:1px}}
.stack-bar{{display:flex;height:12px;border-radius:6px;overflow:hidden;background:var(--track);gap:2px;margin:8px 0 10px}}
.stack-bar i{{display:block;height:100%}}
.seg-real{{background:var(--ok)}} .seg-acc{{background:var(--accent)}}
.seg-n1{{background:#94a3b8}} .seg-n2{{background:var(--warn)}} .seg-n3{{background:var(--bad)}} .seg-n4{{background:#64748b}}
.stack-leg{{display:flex;flex-wrap:wrap;gap:6px 12px;font-size:12px;color:var(--mut)}}
.sw{{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px;vertical-align:middle}}
.sw.real{{background:var(--ok)}} .sw.acc{{background:var(--accent)}}
.sw.n1{{background:#94a3b8}} .sw.n2{{background:var(--warn)}} .sw.n3{{background:var(--bad)}} .sw.n4{{background:#64748b}}
.tbar{{display:grid;grid-template-columns:92px 1fr 38px;align-items:center;gap:8px;font-size:12px;color:var(--ink2);margin:5px 0}}
.tbar span{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.tbar b{{display:block;height:7px;background:var(--track);border-radius:99px;overflow:hidden}}
.tbar b>i{{display:block;height:100%;background:var(--accent);border-radius:99px}}
.tbar em{{font-style:normal;text-align:right;color:var(--mut);font-size:11.5px}}
section details:not(.gmore){{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:11px 15px;margin-top:14px}}
section details:not(.gmore)>summary{{cursor:pointer;color:var(--mut);font-size:13px;font-weight:600}}
section details:not(.gmore) p{{color:var(--mut);font-size:12.5px;margin-top:8px;line-height:1.6}}
.foot-note{{font-size:12px;color:var(--mut);margin-top:10px}}
code{{background:var(--inset);padding:1px 5px;border-radius:4px;font-size:12.5px;font-family:ui-monospace,'SF Mono',Menlo,monospace}}
footer{{text-align:center;color:var(--mut);font-size:12.5px;margin-top:40px;padding-top:16px;border-top:1px solid var(--line)}}
@media(max-width:860px){{.hero,.focus-row,.action-row,.verdict-grid,.comp-grid,.viz-grid,.viz-grid.two{{grid-template-columns:1fr}}.anat-grid{{grid-template-columns:repeat(2,1fr)}}}}
@media(max-width:640px){{.ba{{grid-template-columns:1fr}}.trow{{grid-template-columns:1fr;gap:8px}}.trow-s{{text-align:left}}}}
</style></head><body><div class="wrap">

<div class="topbar">
  <div class="brand">
    <h1>AI Fluency</h1>
    <span class="meta">{_esc(meta_line)}</span>
  </div>
  <button type="button" class="theme-btn" id="themeToggle" onclick="toggleTheme()">☾ Dark</button>
</div>

{prov_banner}
{analysis_status_html}

<div class="hero">
  <div class="panel">{gauge_html}</div>
  <div class="panel">{radar_html}</div>
  <div class="panel">
    <div class="panel-k">Driving style <span>{a['delegation_score']}/100 delegation</span></div>
    <div class="arch-h"><h2>{_esc(a['label'])}</h2></div>
    <p class="arch-b">{_esc(arch_blurb)}</p>
    {"".join(aff_rows)}
    {arch_hedge}
  </div>
</div>

{coach_read_html}

<section>
  <h3>What to improve — and exactly how</h3>
  <div class="focus-row">{featured_card}{side_col_html}</div>
  {more_cards}
  {action_row_html}
</section>

{best_prompts_html}

{patterns_html}

{skill_map_section}

{source_mix_html}

<section>
  <h3>How much data this is based on</h3>
  <div class="kpi-strip">
    <div class="kpi"><div class="n">{len(corpus.real_prompts):,}</div><div class="l">real prompts</div></div>
    <div class="kpi"><div class="n">{corpus.files}</div><div class="l">sessions</div></div>
    <div class="kpi"><div class="n">{len(corpus.projects)}</div><div class="l">projects</div></div>
    <div class="kpi"><div class="n">{days}d</div><div class="l">span</div></div>
    <div class="kpi"><div class="n">{active_h:.0f}h</div><div class="l">active time</div></div>
    <div class="kpi"><div class="n">{corpus.total_bytes/1e6:.1f} MB</div><div class="l">parsed</div></div>
    {archive_tile}
  </div>
  {data_viz_html}
  {retention_note}
  <details><summary>Methodology &amp; honesty (click to expand)</summary>
    <p><b>Only real prompts are scored.</b> Tool results, subagent turns, meta/injected harness blocks, and pastes over {MAX_HUMAN_PROMPT_CHARS:,} chars are stripped before scoring.</p>
    <p><b>Rate-based &amp; saturated.</b> Every dimension is a per-prompt or per-opportunity rate passed through min(1, rate/target) so volume can never inflate the score. Weights: Briefing 24%, Verification 22%, Context-setting 22%, Iteration 18%, Toolcraft 14%.</p>
    <p><b>Confidence shrinkage.</b> Thin dimensions pull toward 50 in proportion to opportunity count. {_esc(low_data_note)}</p>
    <p><b>Agency separation.</b> Score rates the collaboration; archetype rates your driving style alone, discounting agent-driven Verification ({int(AGENCY['Verification']*100)}%) and Context ({int(AGENCY['Context']*100)}%). Active time caps idle gaps at {GAP_CAP_SECONDS//60} min.</p>
    <p><b>Limits:</b> Heuristic and English-biased; terse conversational follow-ups can under-score Briefing.</p>
  </details>
</section>

<footer>Generated locally by AI Fluency v2 · your transcripts never left this machine.</footer>
</div>
<script>
function toggleTheme(){{
  var r=document.documentElement,d=r.getAttribute('data-theme')==='dark';
  r.setAttribute('data-theme',d?'light':'dark');
  var b=document.getElementById('themeToggle');
  if(b)b.textContent=d?'☾ Dark':'☀ Light';
}}
function _copyTxt(txt,btn,lbl){{
  function ok(){{btn.textContent='Copied ✓';setTimeout(function(){{btn.textContent=lbl;}},1600);}}
  function fb(){{var t=document.createElement('textarea');t.value=txt;t.style.position='fixed';t.style.opacity='0';document.body.appendChild(t);t.select();try{{document.execCommand('copy');ok();}}catch(e){{}}document.body.removeChild(t);}}
  if(navigator.clipboard&&navigator.clipboard.writeText){{navigator.clipboard.writeText(txt).then(ok,fb);}}else{{fb();}}
}}
function copyPrompt(btn){{
  var p=btn.closest('.after').querySelector('.copy-src');
  if(!p)return;
  _copyTxt(p.textContent.replace(/^[“"]|[”"]$/g,''),btn,'Copy');
}}
function copyPre(btn){{
  var p=btn.closest('.rules-card').querySelector('.copy-src');
  if(p)_copyTxt(p.textContent,btn,'Copy rule block');
}}
function setRuleFile(btn,fn){{
  var card=btn.closest('.rules-card');
  card.querySelectorAll('.rftab').forEach(function(b){{b.classList.remove('active');}});
  btn.classList.add('active');
  var nm=card.querySelector('.rf-name');
  if(nm)nm.textContent=fn;
}}
function dojoEval(ta){{
  var card=ta.closest('.dojo'),t=ta.value||'';
  var has={{
    target:/([\\w./\\-]+\\.(py|js|ts|tsx|jsx|html|css|md|json|sh|ya?ml|toml|rs|go|java)|`[^`]+`|\\b\\w+\\(\\)|\\/\\w+\\/\\w+)/i.test(t),
    rule:/\\b(only|must|should|shouldn't|don't|do not|never|always|keep|ensure|instead of|without|except|exactly|\\d+(px|ms|s|%|\\s+rows?|\\s+chunks?))\\b/i.test(t),
    why:/\\b(so that|because|in order to|for the|so it|so we|so i|to prevent|to avoid)\\b/i.test(t),
    check:/\\b(pytest|unittest|jest|vitest|npm test|npm run|ruff|mypy|tsc|curl|run (the )?(tests?|suite|build|lint|checks?)|verify|confirm)\\b/i.test(t)
  }};
  var n=0;
  card.querySelectorAll('.dl').forEach(function(el){{
    var ok=!!has[el.getAttribute('data-k')];
    el.classList.toggle('on',ok);
    if(ok)n++;
  }});
  var sc=card.querySelector('.dojo-score');
  if(sc)sc.textContent=n+'/4 anchors'+(n===4?' · Ready ✓':'');
}}
function dojoPick(btn){{
  var card=btn.closest('.dojo'),ta=card.querySelector('.dojo-in');
  card.querySelectorAll('.dchip').forEach(function(b){{b.classList.remove('active');}});
  btn.classList.add('active');
  ta.value=btn.getAttribute('data-before')||'';
  dojoEval(ta);
  var pk=card.querySelector('.dojo-peek'),pb=card.querySelector('[onclick*="dojoPeek"]');
  var aft=btn.getAttribute('data-after')||'';
  if(pk){{pk.style.display='none';pk.setAttribute('data-after',aft);pk.textContent='';}}
  if(pb){{pb.style.display=aft?'inline-block':'none';}}
}}
function dojoPeek(btn){{
  var card=btn.closest('.dojo'),pk=card.querySelector('.dojo-peek'),aft=pk.getAttribute('data-after')||'';
  if(!aft)return;
  if(pk.style.display==='none'){{pk.textContent=aft;pk.style.display='block';}}else{{pk.style.display='none';}}
}}
function dojoCopy(btn){{
  var ta=btn.closest('.dojo').querySelector('.dojo-in');
  if(ta)_copyTxt(ta.value,btn,'Copy rewrite');
}}
document.addEventListener('DOMContentLoaded',function(){{document.querySelectorAll('.dojo-in').forEach(dojoEval);}});
</script>
</body></html>"""


def _skill_levels(result):
    """Map dimension scores to L1-L5 skill levels with now/next text."""
    def lvl(score):
        return max(1, min(5, int(score // 20) + 1))
    s = result["shrunk"]
    defs = [
        ("Briefing & specificity", "Direction",
         "name a goal + one anchor (path, constraint, or acceptance test) in most action prompts",
         {1: "Mostly short nudges with little context.", 2: "Occasional context; one constraint sometimes.",
          3: "Most prompts carry a goal + one anchor.", 4: "Goal + constraint + criterion are common.",
          5: "Consistently high-context with front-loaded rules."}),
        ("Verification discipline", "Verification",
         "end edit-bursts by running the tests / the app before moving on",
         {1: "Edits accepted blind, almost no checks.", 2: "Verifies occasionally.",
          3: "Verifies most bursts of edits.", 4: "Verifies nearly every change.",
          5: "Verification is a reflex — stated up front and layered."}),
        ("Context grounding (read→edit)", "Context",
         "have the agent read the target file before changing it",
         {1: "Often edits files it never read.", 2: "Reads before editing about half the time.",
          3: "Usually points the agent at the right place first.", 4: "Routinely reads target + deps before changing.",
          5: "Deliberate exploration before non-trivial changes."}),
        ("Iteration & recovery", "Iteration",
         "make corrections name a symptom + the exact rule, in one line",
         {1: "Low-info rejections, long loops.", 2: "Corrects but vaguely.",
          3: "Mixes precise and bare corrections.", 4: "Low correction rate, mostly specific.",
          5: "Surgical feedback; turns misses into reusable rules."}),
        ("Toolcraft & orchestration", "Toolcraft",
         "reach past the shell — search, planning, delegation for the right jobs",
         {1: "Effectively one tool.", 2: "The core trio (Bash/Read/Edit).",
          3: "Adds search/web and some planning.", 4: "Comfortable with MCP + balanced spread.",
          5: "20+ tools used appropriately, low concentration."}),
    ]
    out = []
    for name, dim, nxt, rub in defs:
        L = lvl(s[dim])
        out.append({"name": name, "dim": dim, "level": L, "now": rub[L],
                    "what": SKILL_TEACH[dim]["what_it_is"],
                    "next": nxt if L < 5 else "maintain this — it's a real strength."})
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _merge_archive(files, adapter, args):
    """Default-mode archive maintenance: mirror this run's transcripts into the persistent
    archive, then merge live + archived (deduped by session identity). Only files belonging
    to `adapter` are read back, so one source's archived sessions never leak into another's
    report. Returns (files, archive_info)."""
    archive_dir = os.path.expanduser(args.archive)
    new = updated = 0
    if not args.no_archive:
        new, updated = archive_transcripts(files, archive_dir)
    arch_files = _filter_transcripts(glob.glob(os.path.join(archive_dir, "**", "*.jsonl"), recursive=True))
    arch_files = [p for p in arch_files if adapter.owns_file(p)]
    merged = _dedupe_sessions(files + arch_files)
    archive_info = {
        "dir": args.archive, "enabled": not args.no_archive,
        "live_sessions": len(files), "archived_sessions": len(arch_files),
        "merged_sessions": len(merged), "new": new, "updated": updated,
    }
    # If most of what we're analyzing comes only from the archive (not this machine's
    # live transcripts), a shared/synced archive could be feeding in someone else's data.
    archive_only = archive_info["merged_sessions"] - archive_info["live_sessions"]
    if archive_only > max(25, 2 * archive_info["live_sessions"]):
        print(f"  Note: {archive_only} of {archive_info['merged_sessions']} analyzed sessions exist "
              f"only in the archive ({args.archive}), not in your live transcripts. If that archive "
              f"is shared or synced across people/machines, this report may mix in data that isn't "
              f"yours — point --archive at a private, per-person path.", file=sys.stderr)
    return merged, archive_info


def _load_analysis(args, current_fp):
    """Load + provenance-gate an --analysis file. Returns (analysis, note, error_rc);
    error_rc is non-zero only when the file itself is unreadable. Shared by the
    single-source and combined paths — the fingerprint math is identical."""
    if not args.analysis:
        return None, None, 0
    try:
        with open(os.path.expanduser(args.analysis), encoding="utf-8") as f:
            analysis = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Could not read --analysis {args.analysis}: {e}", file=sys.stderr)
        return None, None, 1
    # Don't blindly trust the analysis file: it lives at a fixed, reused path, so it
    # may be empty (the AI stage no-op'd) or left over from a different run/person.
    # Validate shape + provenance; on any failure render the deterministic report
    # only, and say so, rather than pasting someone else's verdict into this report.
    if not isinstance(analysis, dict) or not analysis.get("skill_map"):
        print("  Note: --analysis had no usable skill map (the AI stage may not have run); "
              "rendering the deterministic report only.", file=sys.stderr)
        return None, "the AI skill-map stage returned no usable output", 0
    if args.analysis_evidence:
        # Deterministic provenance gate: the analysis is valid for this run only if the
        # evidence it was built from fingerprints to THIS run's data. insight.py wrote
        # that fingerprint, so this check never depends on the model copying anything.
        evidence_fp = None
        try:
            with open(os.path.expanduser(args.analysis_evidence), encoding="utf-8") as f:
                evidence_fp = (json.load(f).get("meta") or {}).get("run_fingerprint")
        except (OSError, json.JSONDecodeError):
            evidence_fp = None
        if evidence_fp != current_fp:
            print(f"  Note: the --analysis does not match this run (its evidence fingerprint "
                  f"{evidence_fp} != {current_fp}). Ignoring it so it can't leak into this "
                  f"report; rendering the deterministic report only.", file=sys.stderr)
            return None, ("the saved AI analysis was produced from a different run / "
                          "dataset, so it was not used"), 0
        return analysis, None, 0
    # Manual --analysis with no evidence binding: if the file itself happens to carry a
    # run_fingerprint, honor it; otherwise merge (back-compat with hand-written analyses).
    supplied_fp = analysis.get("run_fingerprint")
    if supplied_fp and current_fp and supplied_fp != current_fp:
        print(f"  Note: the supplied --analysis was produced from a DIFFERENT run "
              f"(fingerprint {supplied_fp} != {current_fp}). Ignoring it so it can't "
              f"leak into this report; rendering the deterministic report only.",
              file=sys.stderr)
        return None, ("the saved AI analysis was produced from a different run / "
                      "dataset, so it was not used"), 0
    return analysis, None, 0


def _record_and_read_history(archive_dir, source_key, result, corpus, record=True):
    """Read and optionally append a tiny score snapshot in <archive>/insight_history.json.
    Stored as .json (never .jsonl) and skipped when --no-archive or explicit paths are used."""
    if not archive_dir:
        return []
    path = os.path.join(os.path.expanduser(archive_dir), "insight_history.json")
    hist = []
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
                if isinstance(loaded, list):
                    hist = loaded
    except (OSError, json.JSONDecodeError):
        hist = []

    scoped = [h for h in hist if isinstance(h, dict) and h.get("source") == source_key]
    fp = result.get("fingerprint")
    if record and (not scoped or scoped[-1].get("fp") != fp):
        now = datetime.now()
        lbl = now.strftime("%b %-d") if sys.platform != "win32" else now.strftime("%b %d")
        entry = {
            "ts": now.isoformat(timespec="seconds"),
            "label": lbl,
            "source": source_key,
            "fp": fp,
            "overall": int(result["overall"]),
            "prompts": len(corpus.real_prompts),
        }
        hist.append(entry)
        hist = hist[-60:]
        scoped = [h for h in hist if isinstance(h, dict) and h.get("source") == source_key]
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(hist, fh, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return scoped


def _run_all_sources(args):
    """--source all: every source with data on this machine, combined into ONE report with
    one score and one profile. The blend is honest per dimension: each dimension is scored
    over the merged data of only the sources that can observe it (see analyze_combined);
    per-source sub-scores ride along for the report's source-mix panel."""
    if args.path:
        print("--source all reads each source's standard location and can't be combined with "
              "an explicit path. Pass a specific --source together with the path.", file=sys.stderr)
        return 2
    entries = []
    archive_info = None
    for adapter in ADAPTERS.values():
        try:
            if not adapter.detect():
                continue
        except Exception:
            continue
        files = adapter.discover(None)
        if adapter.archive_enabled:
            files, ainfo = _merge_archive(files, adapter, args)
            if files or ainfo["archived_sessions"]:
                if archive_info is None:
                    archive_info = dict(ainfo)
                else:
                    for k in ("live_sessions", "archived_sessions", "merged_sessions", "new", "updated"):
                        archive_info[k] += ainfo[k]
        if not files:
            continue
        corpus = parse(files, adapter)
        if not corpus.real_prompts:
            continue
        caps = getattr(adapter, "capabilities_observed", None)
        caps = caps(corpus) if caps else adapter.capabilities
        r = analyze(corpus, caps)
        entries.append({"source": adapter.name, "corpus": corpus, "caps": caps,
                        "overall": r["overall"], "band": r["band"],
                        "dims": {k: round(r["shrunk"][k]) for k in r["measurable"]}})
    if not entries:
        print("No coding-agent transcripts found for any source on this machine.", file=sys.stderr)
        return 1

    merged, result = analyze_combined(entries)
    cards, strength = build_action_plan(merged, result)
    caps_union = {k: any(e["caps"].get(k, True) for e in entries)
                  for k in ("prompts", "edits", "verify", "reads", "delegation")}

    analysis, analysis_note, rc = _load_analysis(args, result.get("fingerprint"))
    if rc:
        return rc

    if args.evidence:
        bundle = build_evidence(merged, result, cards, archive_info,
                                source="combined", capabilities=caps_union)
        text = json.dumps(bundle, indent=2)
        if args.evidence == "-":
            print(text)
        else:
            ep = os.path.abspath(args.evidence)
            os.makedirs(os.path.dirname(ep) or ".", exist_ok=True)
            with open(ep, "w", encoding="utf-8") as f:
                f.write(text)
            if not args.quiet:
                print(f"  Evidence: {ep}", file=sys.stderr)

    if args.json:
        payload = {
            "source": "combined",
            "sources": result["sources"],
            "not_measurable": result["na_dims"],
            "overall": result["overall"], "overall_raw": result["overall_raw"],
            "band": result["band"], "archetype": result["archetype"]["label"],
            "dimensions_raw": result["raw"], "dimensions_adjusted": result["shrunk"],
            "confidence": result["conf"], "detail": result["detail"],
            "data_ingested": {
                "files": merged.files, "projects": len(merged.projects),
                "bytes": merged.total_bytes, "user_records": merged.user_records,
                "real_prompts": len(merged.real_prompts), "filtered": dict(merged.filtered),
                "active_hours": round(merged.active_seconds / 3600, 1),
                "prompt_distribution": result["dist"],
                "archive": archive_info,
            },
        }
        print(json.dumps(payload, indent=2))
        return 0

    hist_runs = _record_and_read_history(
        args.archive if not args.no_archive else None,
        "combined", result, merged,
        record=bool(not args.no_archive and not args.quiet),
    )
    html_doc = build_html(merged, result, cards, strength, archive_info, analysis, analysis_note,
                          source="all sources", history_runs=hist_runs)
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    if not args.quiet:
        print(terminal_summary(merged, result, "all sources"))
        for s in result["sources"]:
            na = f" · {', '.join(disp(n) for n in s['not_measurable'])} n/a" if s["not_measurable"] else ""
            print(f"    {s['source']}: {s['overall']}/100 ({s['band']}) · "
                  f"{s['real_prompts']} prompts{na}")
        if archive_info and archive_info["enabled"]:
            print(f"  Archive: {archive_info['merged_sessions']} sessions preserved at "
                  f"{archive_info['dir']} ({archive_info['new']} new, {archive_info['updated']} updated this run).")
        print(f"\n  Report: {out_path}\n")
    if not args.no_open:
        try:
            webbrowser.open(f"file://{out_path}")
        except Exception:
            pass
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="AI Fluency v2 — how skillfully you build with AI (one command, zero install).")
    ap.add_argument("path", nargs="?", help="transcript dir or .jsonl file (default: the source's standard location)")
    ap.add_argument("--source", default="auto", choices=["auto", "all"] + sorted(ADAPTERS),
                    help="which coding-agent logs to analyze (default: auto-detect; "
                         "'all' = every source on this machine combined into ONE report — "
                         "one score, one profile, each dimension blended only from the "
                         "sources that can observe it)")
    ap.add_argument("-o", "--out", default="ai_fluency_report.html", help="HTML output path")
    ap.add_argument("--json", action="store_true", help="print raw metrics as JSON and exit")
    ap.add_argument("--no-open", action="store_true", help="don't auto-open the report in a browser")
    ap.add_argument("--archive", default=os.environ.get("CLAUDE_INSIGHT_ARCHIVE", DEFAULT_ARCHIVE_DIR),
                    metavar="DIR",
                    help="persistent archive that preserves transcripts beyond Claude Code's "
                         "30-day cleanup so history accumulates (default ~/.claude/insight-archive; "
                         "keep it private to you — a folder shared between people mixes their data)")
    ap.add_argument("--no-archive", action="store_true",
                    help="don't copy this run's transcripts into the archive (still reads an existing one)")
    ap.add_argument("--evidence", metavar="PATH",
                    help="write the de-contaminated evidence bundle (JSON) for the two-model "
                         "analysis pipeline to PATH ('-' for stdout), then continue")
    ap.add_argument("--analysis", metavar="PATH",
                    help="merge an AI analysis (JSON from the Opus stage) into the report's skill map")
    ap.add_argument("--analysis-evidence", metavar="PATH", dest="analysis_evidence",
                    help="the evidence bundle the --analysis was produced from; its run_fingerprint "
                         "is checked against this run so a stale/foreign analysis can't be merged")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress the terminal summary (the skill's internal measure pass uses this "
                         "so the score isn't surfaced before the full AI report is ready)")
    args = ap.parse_args(argv)

    if args.source == "all":
        return _run_all_sources(args)

    # Single source: named, or auto (an explicit path defaults to Claude Code's format —
    # point another format at it with an explicit --source).
    if args.source == "auto":
        adapter = ADAPTERS["claude-code"] if args.path else detect_adapter()
    else:
        adapter = ADAPTERS[args.source]

    files = adapter.discover(args.path)

    # Default mode: maintain + read the persistent archive so we can analyze more than the
    # ~30 days Claude Code keeps on disk. Skipped when an explicit path is given, or for
    # sources whose store isn't subject to that churn.
    archive_info = None
    if not args.path and adapter.archive_enabled:
        files, archive_info = _merge_archive(files, adapter, args)

    if not files:
        where = args.path or f"the standard {adapter.name} location"
        print(f"No {adapter.name} transcripts found in {where}.\n"
              f"Point at your transcripts with:  python3 insight.py /path/to/dir", file=sys.stderr)
        return 1

    corpus = parse(files, adapter)
    if not corpus.real_prompts:
        print("Found transcripts but no real human-typed prompts to analyze.", file=sys.stderr)
        return 1

    caps = getattr(adapter, "capabilities_observed", None)
    caps = caps(corpus) if caps else adapter.capabilities
    result = analyze(corpus, caps)
    cards, strength = build_action_plan(corpus, result)

    if args.evidence:
        bundle = build_evidence(corpus, result, cards, archive_info,
                                source=adapter.name, capabilities=caps)
        text = json.dumps(bundle, indent=2)
        if args.evidence == "-":
            print(text)
        else:
            ep = os.path.abspath(args.evidence)
            os.makedirs(os.path.dirname(ep) or ".", exist_ok=True)
            with open(ep, "w", encoding="utf-8") as f:
                f.write(text)
            if not args.quiet:
                print(f"  Evidence: {ep}", file=sys.stderr)

    analysis, analysis_note, rc = _load_analysis(args, result.get("fingerprint"))
    if rc:
        return rc

    if args.json:
        payload = {
            "source": adapter.name,
            "not_measurable": result["na_dims"],
            "overall": result["overall"], "overall_raw": result["overall_raw"],
            "band": result["band"], "archetype": result["archetype"]["label"],
            "dimensions_raw": result["raw"], "dimensions_adjusted": result["shrunk"],
            "confidence": result["conf"], "detail": result["detail"],
            "data_ingested": {
                "files": corpus.files, "projects": len(corpus.projects),
                "bytes": corpus.total_bytes, "user_records": corpus.user_records,
                "real_prompts": len(corpus.real_prompts), "filtered": dict(corpus.filtered),
                "active_hours": round(corpus.active_seconds / 3600, 1),
                "prompt_distribution": result["dist"],
                "archive": archive_info,
            },
        }
        print(json.dumps(payload, indent=2))
        return 0

    # Render fully before touching the file, so a render error can't leave a 0-byte report.
    hist_runs = _record_and_read_history(
        args.archive if (not args.no_archive and not args.path) else None,
        adapter.name, result, corpus,
        record=bool(not args.no_archive and not args.path and not args.quiet),
    )
    html_doc = build_html(corpus, result, cards, strength, archive_info, analysis, analysis_note,
                          source=adapter.name, history_runs=hist_runs)
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)

    if not args.quiet:
        print(terminal_summary(corpus, result, adapter.name))
        if archive_info and archive_info["enabled"]:
            print(f"  Archive: {archive_info['merged_sessions']} sessions preserved at "
                  f"{archive_info['dir']} ({archive_info['new']} new, {archive_info['updated']} updated this run).")
        print(f"  Report: {out_path}\n")
    if not args.no_open:
        try:
            webbrowser.open(f"file://{out_path}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
