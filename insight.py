#!/usr/bin/env python3
"""
Claude Insight v2 — one-command, zero-install AI-fluency analyzer.

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

Pure Python standard library. No pip, no Ollama, no API key, no network. 100%
offline; your transcripts never leave your machine. The only thing it writes is
the HTML report and a local copy of your transcripts in an archive
(~/.claude/insight-archive) so history survives Claude Code's 30-day cleanup —
pass --no-archive to skip that and read your transcripts without copying them.
"""

import argparse
import glob
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
# Point it at a synced folder (Dropbox/iCloud) to keep it across machines.
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
    r"doesn't work|does not work|not working|unteligible|unteliggeble)\b", re.I
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


class Corpus:
    """Everything we measured from the transcripts, cleanly separated from scoring."""

    def __init__(self):
        self.files = 0
        self.projects = set()
        self.total_bytes = 0
        self.user_records = 0
        self.filtered = Counter()       # why user records were not counted as prompts
        self.signals = Counter()        # source-specific scrutiny signals (e.g. permission_denials)
        self.real_prompts = []          # list of dicts: text, project, session, idx
        self.tool_usage = Counter()     # de-namespaced tool name -> count
        self.total_tool_calls = 0
        self.delegation_events = 0
        self.first_ts = None
        self.last_ts = None
        self.active_seconds = 0.0
        # Per-session ordered timelines of {"kind": "prompt"|"tool", ...}
        self.sessions = {}              # session_id -> {"project","timeline":[...]}


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


def _dedupe_sessions(files):
    """When the same session shows up in more than one root (the live ~/.claude/projects dir
    AND the persistent archive — possibly under a since-renamed project folder, a different-case
    path, or a synced copy from another machine), keep a single copy of it: the largest one,
    since transcripts only ever grow, so the biggest file is the most complete. Claude Code
    session filenames are globally-unique IDs, so the filename alone identifies the session —
    keying on it (not the parent folder) is what makes the dedupe robust to all of the above."""
    best = {}
    for path in files:
        key = os.path.basename(path)
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
# --------------------------------------------------------------------------- #
#
# Every supported coding-agent tool plugs in as a SourceAdapter. An adapter's only
# job is to turn one tool's local logs into the normalized event stream that the
# generic `parse()` below consumes — after which scoring, the evidence bundle, the
# archive and the Sonnet->Opus analysis pipeline are entirely source-agnostic.
#
# iter_events(path) yields, in order, dicts of these shapes:
#   {"role": "session", "project": str, "session_id": str}   one header per file, first
#   {"role": "ts",      "ts": <iso str>}                     a timestamp (active-time only)
#   {"role": "user",    "text": str}                         a de-contaminated human prompt
#   {"role": "tool",    "name": str, "file": path|None,      an agent tool call; `name` is
#                       "cmd": str|None, "meta": {...}}        de-namespaced (case preserved for
#                                                              tool_usage); parse lowercases it
#                                                              for the canonical vocabulary
#   {"role": "drop",    "reason": str}                       a candidate prompt that was filtered
#
# Mapping `name` onto the canonical vocabulary (read/grep/glob, edit/write/multiedit/
# notebookedit, bash, agent/task/workflow/enter|exitplanmode) is what makes every scorer
# "just work". `meta` carries source extras (e.g. {"background": True} for a backgrounded
# shell -> a delegation signal; {"denied": True}, {"decision": "rejected"} for scrutiny).
# De-contamination happens inside iter_events: only real human prompts are emitted as
# `user`; everything filtered is emitted as `drop` with a reason (rolled into corpus.filtered).


def _normalize_path(p):
    """Strip machine-identifying home prefixes so nothing personal leaks into the evidence
    bundle/report. Handles a home path WITH or WITHOUT a trailing child segment:
    /Users/<name>/x -> ~/x, and bare /Users/<name> -> ~ (else basename would surface the username)."""
    if not p or not isinstance(p, str):
        return p
    p = re.sub(r"^(?:/Users/|/home/)[^/]+(?=/|$)", "~", p)
    p = re.sub(r"^[A-Za-z]:\\Users\\[^\\]+(?=\\|$)", "~", p)
    return p


# Match a home path's username segment (with OR without a trailing child); replacing the whole
# match with "~" turns /Users/jane/proj -> ~/proj and bare /Users/jane -> ~.
_HOME_PATH_RE = re.compile(r"(?:/Users/|/home/)[^/\s]+")
_WIN_HOME_RE = re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+")
# Shell-prompt user@host tokens (pasted terminal sessions leak the OS username + hostname).
# Also redacts e-mail-shaped tokens — fine for a privacy tool; the analysis doesn't need them.
_USER_AT_HOST_RE = re.compile(r"\b[A-Za-z0-9._-]+@[A-Za-z0-9._-]+\b")


def _scrub_paths(text):
    """Redact machine-identifying personal data (home paths + user@host tokens) anywhere in free
    text. Applied only at PRESENTATION (build_evidence/build_html), never to the scored corpus, so
    scores stay byte-identical. /Users/<name>/x -> ~/x ; bare /Users/<name> -> ~ ; user@host -> <user>@<host>."""
    if not isinstance(text, str):
        return text
    text = _HOME_PATH_RE.sub("~", text)
    text = _WIN_HOME_RE.sub("~", text)
    text = _USER_AT_HOST_RE.sub("<user>@<host>", text)
    return text


def _claude_tool_event(b):
    """Map one Claude-style tool_use block to a normalized tool event. Shared by the Claude
    Code and Claude Desktop adapters (identical message shape). `name` keeps its de-namespaced
    case for tool_usage; parse() lowercases it for the canonical vocabulary. mcp__workspace__bash
    de-namespaces to 'bash' and so its input.command is picked up as a shell command for free."""
    raw = b.get("name", "unknown")
    name = _denamespace_tool(raw)
    inp = b.get("input", {}) if isinstance(b.get("input"), dict) else {}
    fpath = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
    cmd = inp.get("command") if name.lower() == "bash" else None
    meta = {}
    if name.lower() == "bash" and inp.get("run_in_background"):
        meta["background"] = True
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
    def discover(explicit):
        return discover_files(explicit)

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

_DESKTOP_ROOT = "~/Library/Application Support/Claude/local-agent-mode-sessions"
_UPLOADED_RE = re.compile(r"<uploaded_files>.*?</uploaded_files>", re.S)


def _strip_uploaded_files(text):
    if not text:
        return text
    return _UPLOADED_RE.sub("", text)


class ClaudeDesktopAdapter:
    """Claude Desktop agent-mode sessions — .../local-agent-mode-sessions/**/audit.jsonl.
    The records share Claude Code's message/tool_use shape, so tool extraction is reused; the
    differences are the harness envelopes (system/result/rate_limit/tool_use_summary), the
    isReplay/isSynthetic de-contamination, and the real shell tool being mcp__workspace__bash
    (which de-namespaces to 'bash')."""

    name = "claude-desktop"
    archive_enabled = False   # every file is named audit.jsonl -> basename-keyed archive can't dedupe it
    capabilities = {"prompts": True, "edits": True, "verify": True,
                    "reads": True, "delegation": True}

    @staticmethod
    def detect():
        return os.path.isdir(os.path.expanduser(_DESKTOP_ROOT))

    @staticmethod
    def discover(explicit):
        if explicit:
            p = os.path.expanduser(explicit)
            if os.path.isfile(p) and p.endswith(".jsonl"):
                return [p]
            if os.path.isdir(p):
                return sorted(glob.glob(os.path.join(p, "**", "audit.jsonl"), recursive=True))
            return []
        root = os.path.expanduser(_DESKTOP_ROOT)
        if not os.path.isdir(root):
            return []
        return sorted(glob.glob(os.path.join(root, "**", "audit.jsonl"), recursive=True))

    @staticmethod
    def iter_events(path):
        # One audit.jsonl == one session. The session id is the per-session directory name
        # (globally unique); project is just the source (desktop agent mode has no project tree).
        session_id = os.path.basename(os.path.dirname(path)) or os.path.splitext(os.path.basename(path))[0]
        yield {"role": "session", "project": "claude-desktop", "session_id": session_id}
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
                        # user scrutiny of agent actions — a Discernment signal
                        yield {"role": "signal", "name": "permission_denials", "value": len(pd)}
                    continue
                if typ in ("system", "rate_limit_event", "tool_use_summary"):
                    continue  # init/status/permission envelopes & summaries — never prompts/actions
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
                if e.get("isSidechain") or e.get("parent_tool_use_id") or e.get("subagent_type"):
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


# --- OpenAI Codex CLI ----------------------------------------------------------------------- #

_CODEX_ROOTS = ["~/.codex/sessions", "~/.codex/archived_sessions"]
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
            return [{"role": "drop", "reason": "system"}]   # harness/system prose; counted for transparency
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
    return []   # reasoning / *_output / unknown payloads -> ignored


class CodexAdapter:
    """OpenAI Codex CLI — ~/.codex/sessions/**/rollout-*.jsonl. We read the `response_item`
    stream only (the `event_msg` stream is parallel telemetry — using both double-counts).
    `reads` is False: Codex has no read tool (files are read via shell), so read-before-edit
    grounding (Context) is not reliably observable and is honestly marked not-measurable."""

    name = "codex"
    archive_enabled = False
    capabilities = {"prompts": True, "edits": True, "verify": True,
                    "reads": False, "delegation": True}

    @staticmethod
    def detect():
        return any(os.path.isdir(os.path.expanduser(r)) for r in _CODEX_ROOTS)

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
                    for ev in pending:     # records seen before the header belong to this session
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

_CURSOR_GLOBAL = "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb"
_CURSOR_WORKSPACES = "~/Library/Application Support/Cursor/User/workspaceStorage"
# Linux:   ~/.config/Cursor/User/{globalStorage,workspaceStorage}/...
# Windows: %APPDATA%\Cursor\User\{globalStorage,workspaceStorage}\...


class CursorAdapter:
    """Cursor IDE — SQLite state.vscdb. The global DB's cursorDiskKV table holds composer
    sessions (composerData:<id>) and messages (bubbleId:<composerId>:<bubbleId>); older
    per-workspace DBs use ItemTable/aiService.*. The live DB can be tens of GB and WAL-mode
    while Cursor is open, so we COPY it first and open the copy strictly read-only."""

    name = "cursor"
    archive_enabled = False
    capabilities = {"prompts": True, "edits": True, "verify": True,
                    "reads": True, "delegation": True}

    @staticmethod
    def detect():
        if os.path.exists(os.path.expanduser(_CURSOR_GLOBAL)):
            return True
        ws = os.path.expanduser(_CURSOR_WORKSPACES)
        return bool(glob.glob(os.path.join(ws, "*", "state.vscdb")))

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
        g = os.path.expanduser(_CURSOR_GLOBAL)
        if os.path.exists(g):
            out.append(g)
        out.extend(sorted(glob.glob(os.path.join(os.path.expanduser(_CURSOR_WORKSPACES),
                                                  "*", "state.vscdb"))))
        return out

    @staticmethod
    def iter_events(path):
        tmpdir = tempfile.mkdtemp(prefix="insight-cursor-")
        tmpdb = os.path.join(tmpdir, "state.vscdb")
        conn = None
        try:
            try:
                shutil.copyfile(path, tmpdb)
                # The live DB is WAL-mode while Cursor is open: recently-committed rows live in the
                # -wal sidecar, not the main file. Copy the sidecars too (and DON'T use immutable=1,
                # which makes SQLite ignore the WAL) so a running Cursor's history is still readable.
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
                yield from CursorAdapter._iter_disk_kv(conn)
            elif "ItemTable" in tables:
                yield from CursorAdapter._iter_item_table(conn, path)
        finally:
            if conn is not None:
                conn.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def _iter_disk_kv(conn):
        # Stream the composer rows lazily (the DB can be tens of GB — don't fetchall); the per-bubble
        # lookups run on a SECOND cursor so they don't reset the outer result set mid-iteration.
        outer = conn.cursor()
        inner = conn.cursor()
        try:
            outer.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'")
        except sqlite3.Error:
            return
        while True:
            try:
                row = outer.fetchone()
            except sqlite3.Error:
                return
            if row is None:
                break
            key, value = row
            try:
                data = json.loads(value)
            except (ValueError, TypeError):
                continue
            composer_id = key.split(":", 1)[1] if ":" in key else key
            yield {"role": "session", "project": "cursor", "session_id": composer_id}
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
        # Gap-capped active time is computed per session window; a new `session` header flushes
        # the prior one. Single-header sources (Claude Code/Desktop/Codex) flush once at EOF,
        # so their active time and session map are byte-identical to a per-file computation.
        ts_in = cur["ts"]
        if len(ts_in) >= 2:
            ts_in.sort()
            c.active_seconds += sum(
                min((ts_in[i + 1] - ts_in[i]).total_seconds(), GAP_CAP_SECONDS)
                for i in range(len(ts_in) - 1)
            )
        if cur["timeline"]:
            c.sessions[cur["session_id"]] = {"project": cur["project"], "timeline": cur["timeline"]}

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
                out.append({"session": sid, "project": project, "text": t, "high_info": high_info})
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
    detail = {"n": k, "corrections": k, "correction_rate": rate, "specificity": specificity}
    return score, detail, low_info[:4]


def score_context(corpus):
    total_edits = 0
    grounded = 0
    blind_examples = []
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
                    blind_examples.append({"session": sid, "project": project, "file": fpath})
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
            unverified_examples.append({"session": sid, "project": project,
                                        "files": ", ".join(sorted(set(ep_files))[:3]) or "files"})
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

# Which dimensions a source can actually observe. A capability that is False (e.g. Codex has
# no read tool -> can't observe read-before-edit grounding) makes its dimension *not measurable*;
# such dimensions are excluded from the overall score (weights renormalized) rather than scored
# as 0 — honesty over coverage. capabilities=None means "all measurable" (Claude Code default),
# which leaves the overall score byte-identical to the original single-source engine.
def _measurable_dims(capabilities):
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
    out.append("Toolcraft")  # tool breadth is observable whenever any tool is used
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
        "archetype": archetype, "dist": dist,
        "measurable": measurable, "na_dims": na_dims,
    }


def build_action_plan(corpus, result):
    """Growth cards ranked by impact = (target - score) * weight. The teaching copy
    comes from SKILL_TEACH; user-specific evidence comes from result['evidence']."""
    TARGET = 85
    dims = result.get("measurable") or list(WEIGHTS)
    cards = []
    for name in dims:
        score = result["shrunk"][name]
        impact = (TARGET - score) * WEIGHTS[name]
        cards.append({"dim": name, "score": round(score), "impact": impact,
                      "weak": result["evidence"].get(name, []),
                      "detail": result["detail"][name]})
    cards.sort(key=lambda c: c["impact"], reverse=True)
    # strength callout = highest shrunk score (among measurable dimensions)
    strength = max(dims, key=lambda n: result["shrunk"][n])
    return cards, strength


def _shortest_action_prompt(corpus):
    cands = [p["text"] for p in corpus.real_prompts if _is_action_prompt(p["text"]) and len(p["text"]) < 40]
    return min(cands, key=len) if cands else None


def build_evidence(corpus, result, cards, archive_info=None, source=None, capabilities=None):
    """Serialize a local, de-contaminated EVIDENCE bundle for the optional two-model
    analysis pipeline (Sonnet 4.6 explores it; Opus 4.8 analyzes it against the bundled
    AI-fluency framework). It contains your real prompts/behavior — it stays on your
    machine and is git-ignored. Deterministic (no randomness) so runs are reproducible."""
    prompts = corpus.real_prompts
    sample, seen = [], set()

    def add(p):
        k = (p["session"], p["idx"])
        if k in seen:
            return
        seen.add(k)
        sample.append({"text": _scrub_paths(p["text"][:600]), "project": _project_label(p["project"]),
                       "chars": len(p["text"])})

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
            if c:
                out.append(c)
        return out

    span_days = (corpus.last_ts - corpus.first_ts).days if corpus.first_ts and corpus.last_ts else 0
    a = result["archetype"]
    return {
        "schema": "claude-insight-evidence/1",
        "source": source or "claude-code",
        "capabilities": capabilities or {"prompts": True, "edits": True, "verify": True,
                                         "reads": True, "delegation": True},
        "not_measurable": result.get("na_dims", []),
        "meta": {
            "sessions": corpus.files, "projects": len(corpus.projects),
            "real_prompts": len(prompts), "user_records": corpus.user_records,
            "filtered_noise": dict(corpus.filtered),
            "span_days": span_days,
            "active_hours": round(corpus.active_seconds / 3600, 1),
            "archive": ({**archive_info, "dir": _scrub_paths(archive_info["dir"])}
                        if archive_info else None),
            "prompt_distribution": result["dist"],
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
            "weak_examples": {c["dim"]: clean_ex(c["weak"]) for c in cards},
            "tool_usage": dict(corpus.tool_usage),
            "delegation_events": corpus.delegation_events,
            "signals": dict(corpus.signals),
        },
    }


# The engine dimensions that feed each 4D competency (see the framework mapping table). A
# competency is only suppressed from the AI skill map when ALL its feeder dimensions are
# not-measurable for the source — so e.g. Codex (only Context N/A) keeps Discernment/Diligence,
# which are still observable via Verification.
_COMPETENCY_FEEDERS = {
    "delegation": {"Direction", "Toolcraft"},
    "description": {"Direction", "Iteration"},
    "discernment": {"Verification", "Context", "Iteration"},
    "diligence": {"Verification", "Context"},
}


def _analysis_section_html(analysis, na_dims=None):
    """Render the optional AI-authored skill map (produced by the Opus analysis stage,
    grounded in reference/ai-fluency-framework.md). Falls back to nothing if absent. Drops any
    competency whose signals are entirely not-measurable from the source (capability-aware honesty,
    enforced deterministically rather than relying on the model to omit it)."""
    if not analysis or not isinstance(analysis, dict):
        return ""
    na = set(na_dims or [])
    parts = ['<section><h3>Skill map — analyzed against the AI Fluency framework</h3>']
    read = analysis.get("overall_read") or analysis.get("summary")
    if read:
        parts.append(f'<p class="assess">{_esc(read)}</p>')
    for s in analysis.get("skill_map") or []:
        feeders = _COMPETENCY_FEEDERS.get(str(s.get("competency", "")).strip().lower())
        if feeders and na and feeders <= na:
            continue   # every signal behind this competency is not-measurable from this source
        comp = _esc(s.get("competency", "?"))
        lvl = s.get("level", "?")
        label = _esc(s.get("level_label", ""))
        summ = _esc(s.get("summary", ""))
        nxt = _esc(s.get("next_move", ""))
        ev = "".join(f"<li>“{_esc(str(x)[:200])}”</li>" for x in (s.get("evidence") or [])[:3])
        parts.append(
            f'<div class="dim"><div class="dim-h"><b>{comp}</b>'
            f'<span class="pill">Level {_esc(lvl)}/5 · {label}</span></div>'
            f'<p>{summ}</p>'
            + (f'<ul class="ev">{ev}</ul>' if ev else "")
            + (f'<p class="next"><b>Your next move:</b> {nxt}</p>' if nxt else "")
            + '</div>')
    strengths = analysis.get("strengths") or []
    if strengths:
        items = "".join(f"<li>{_esc(s)}</li>" for s in strengths[:5])
        parts.append(f'<p style="margin-top:14px"><b>What you already do well:</b></p><ul class="facts">{items}</ul>')
    parts.append('<p style="color:var(--mut);font-size:13px;margin-top:10px">'
                 'This section is written by Claude Opus 4.8 from your de-contaminated evidence '
                 '(explored by Claude Sonnet 4.6), grounded in the bundled AI Fluency framework. '
                 'The numbers above are computed deterministically and independently.</p>')
    parts.append('</section>')
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

def _project_label(name):
    """Claude encodes an absolute path with '-' for '/', so we can't perfectly
    recover hyphenated names. Drop the home/boilerplate prefix and show the rest.
    '-Users-me-Dropbox-AI-platzi-executive-assistant' -> 'AI platzi executive assistant'."""
    s = re.sub(r"^-?Users-[^-]+(?:-|$)", "", name)   # strip -Users-<user>- (or a bare -Users-<user>)
    s = re.sub(r"^Dropbox-", "", s)                  # strip a common cloud-folder prefix
    s = s.replace("-", " ").strip()
    # If nothing is left, the session ran in $HOME itself ('-Users-<name>') — never echo the raw
    # name back (it still holds the username); label it neutrally instead.
    if not s:
        return "home" if re.match(r"^-?Users-", name) else name
    return s


def terminal_summary(corpus, result):
    a = result["archetype"]
    lines = [
        "",
        f"  AI Fluency Score: {result['overall']}/100  ({result['band']})",
        f"  Archetype: {a['label']}",
        f"  Based on {len(corpus.real_prompts)} real prompts across {len(corpus.projects)} projects, "
        f"{corpus.files} sessions ({corpus.total_bytes/1e6:.1f} MB).",
        "",
    ]
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
    "Delegation": "how much you hand off — you give Claude whole jobs and trust it to run them end-to-end",
    "Toolcraft": "the range of tools you bring to bear — you reach past the shell for the right instrument",
    "Iteration": "how cleanly you change course — your corrections tend to name the fix, not just reject",
    "Briefing": "how concretely you frame requests when it matters",
}

# The specific, evidence-grounded line that explains each dimension as a growth edge.
_GROWTH_LINE = {
    "Direction": "{s}s win on how sharply they frame the work they hand off — and right now yours are often one-liners like “{ex}”, so Claude fills gaps you could have decided.",
    "Verification": "Right now changes often move on without a test, build or run to confirm them — the cheapest reliability you can buy back.",
    "Context": "Right now some edits land before the file has been read that session — an easy blind-edit risk to remove.",
    "Iteration": "Right now corrections lean toward brief rejections; naming the symptom and the exact rule resolves loops in fewer turns.",
    "Toolcraft": "Right now most work funnels through one tool — reaching for search, planning and delegation widens what you can take on.",
}


def build_assessment(corpus, result, cards):
    """A coherent, professional written read — synthesizes the numbers into one story
    and explicitly resolves the archetype-vs-weakest-dimension tension."""
    a = result["archetype"]
    arch = a["primary"]
    short = arch.replace("The ", "")
    art = "an" if short[:1] in "AEIOU" else "a"
    deleg = a["delegation_score"]
    n_deleg = corpus.delegation_events
    median = result["dist"].get("median_chars", "?")

    # signature strength = your strongest USER-driven signal (not Claude's defaults)
    user_signals = {
        "Briefing": result["shrunk"]["Direction"], "Iteration": result["shrunk"]["Iteration"],
        "Toolcraft": result["shrunk"]["Toolcraft"], "Delegation": float(deleg),
    }
    sig = max(user_signals, key=user_signals.get)

    growth = cards[0]["dim"]
    growth_disp = disp(growth)
    example = _scrub_paths(_shortest_action_prompt(corpus) or "run it")
    path_why = ARCH_PATHS.get(arch, "Keep building the habits below and your next run will show the gain.")

    p1 = (f"You drive Claude like <b>{_esc(a['label'])}</b>. {_esc(a['blurb'])} "
          f"The clearest signal is your delegation rate — <b>{deleg}/100</b>, from {n_deleg} hand-offs to "
          f"subagents, background jobs and planning — paired with fast, terse prompts (median "
          f"{median} characters).")

    p2 = (f"Your strongest <i>self-driven</i> habit is {_esc(_SIG_DESC.get(sig, sig.lower()))}. "
          f"That, plus the disciplined read→edit→verify loop your sessions show, is why your overall "
          f"score lands at <b>{result['overall']}/100 ({_esc(result['band'])})</b>.")

    gline = _GROWTH_LINE.get(growth, "").format(s=_esc(short), ex=_esc(example))
    p3 = (f"And the apparent tension, resolved: your lowest dimension is <b>{_esc(growth_disp)}</b> — but for "
          f"{art} {_esc(short)} that isn't a contradiction, it's the <i>defining</i> growth edge. {gline} "
          f"{_esc(path_why)}")

    return (f'<p class="assess">{p1}</p><p class="assess">{p2}</p><p class="assess">{p3}</p>')


_SOURCE_LABELS = {
    "claude-code": "Claude Code", "claude-desktop": "Claude Desktop (agent mode)",
    "codex": "OpenAI Codex CLI", "cursor": "Cursor",
}


def build_html(corpus, result, cards, strength, archive_info=None, analysis=None,
               source=None, capabilities=None):
    a = result["archetype"]
    d = result["dist"]
    analysis_section = _analysis_section_html(analysis, result.get("na_dims"))
    days = (corpus.last_ts - corpus.first_ts).days if corpus.first_ts and corpus.last_ts else 0
    active_h = corpus.active_seconds / 3600
    filtered_total = sum(corpus.filtered.values())
    provisional = len(corpus.real_prompts) < PROVISIONAL_MIN_PROMPTS
    na = [n for n in WEIGHTS if n in set(result.get("na_dims") or [])]
    src_label = _SOURCE_LABELS.get(source or "claude-code", source or "Claude Code")

    DIM_BLURB = {
        "Direction": "How clearly you tell the agent what you want before it acts.",
        "Verification": "Whether changes get checked (tests / build / app) before moving on.",
        "Context": "Reading a file before editing it — grounded, not blind, changes.",
        "Iteration": "Correcting precisely instead of thrashing with vague rejections.",
        "Toolcraft": "Using a healthy range of tools — not forcing everything through one.",
    }

    def dim_rate_line(name):
        det = result["detail"][name]
        if name == "Verification" and det.get("rate") is not None:
            return f"{det['verified']} of {det['episodes']} edit-bursts verified ({det['rate']*100:.0f}%)"
        if name == "Context" and det.get("rate") is not None:
            return f"{det['grounded']} of {det['total_edits']} edits were grounded in a prior read ({det['rate']*100:.0f}%)"
        if name == "Direction":
            return (f"{det['constraint_rate']*100:.0f}% carry a constraint · "
                    f"{det['artifact_rate']*100:.0f}% name a file/error · {det['intent_rate']*100:.0f}% state a why")
        if name == "Iteration":
            return f"{det['corrections']} correction turns ({det['correction_rate']*100:.0f}% of prompts); {det['specificity']*100:.0f}% were specific"
        if name == "Toolcraft":
            return f"{det.get('distinct', 0)} distinct tools, evenness {det.get('evenness', 0.0):.2f}, {det.get('delegation_events', 0)} delegations"
        return ""

    # dimension bars (not-measurable dimensions are excluded from the score and shown as a note)
    dim_html = ""
    order = sorted([n for n in WEIGHTS if n not in na], key=lambda n: result["shrunk"][n], reverse=True)
    for name in order:
        sc = round(result["shrunk"][name])
        raw_sc = round(result["raw"][name])
        c = result["conf"][name]
        lowdata = c < 0.75
        tag = ""
        if name == strength:
            tag = '<span class="tag s">Strength</span>'
        elif name == cards[0]["dim"]:
            tag = '<span class="tag w">Top growth lever</span>'
        ld = '<span class="tag ld">low data</span>' if lowdata else ""
        dim_html += f"""
      <div class="dim">
        <div class="top"><span class="name">{_esc(disp(name))} {tag}{ld}</span><span class="sval">{sc}<span class="hint">/100</span></span></div>
        <div class="bar"><i style="width:{sc}%"></i></div>
        <p class="def">{_esc(DIM_BLURB[name])}</p>
        <p class="rate">{_esc(dim_rate_line(name))}<span class="wt"> · weight {int(WEIGHTS[name]*100)}%</span></p>
      </div>"""

    for name in na:
        dim_html += f"""
      <div class="dim" style="opacity:.72">
        <div class="top"><span class="name">{_esc(disp(name))} <span class="tag ld">not measurable</span></span><span class="sval" style="font-size:14px;color:var(--mut)">N/A</span></div>
        <p class="def">{_esc(DIM_BLURB[name])}</p>
        <p class="rate">Not observable from {_esc(src_label)} — this source doesn't emit the needed signal, so it's excluded from the score (the remaining weights are renormalized) rather than guessed at.</p>
      </div>"""

    # archetype affinity
    aff = ""
    for sim, nm in a["all"]:
        pct = max(0, round((sim + 1) / 2 * 100))
        aff += f"""<div class="bar-item"><div class="bl">{PROTOTYPES[nm]['emoji']} {_esc(nm)}</div>
          <div class="bt"><i style="width:{pct}%"></i></div><div class="bv">{sim:+.2f}</div></div>"""

    # data-ingested filter breakdown
    filt = "".join(
        f"<li><b>{v:,}</b> {_esc(k)}</li>" for k, v in corpus.filtered.most_common()
    )

    # Archive stat tile + the "why ~30 days / how to see more" callout.
    archive_tile = retention_note = ""
    arch_dir_disp = _esc(_scrub_paths(archive_info["dir"])) if archive_info else _esc(DEFAULT_ARCHIVE_DIR)
    if archive_info:
        archive_tile = (f'<div class="ing"><div class="n">{archive_info["archived_sessions"]:,}</div>'
                        f'<div class="l">sessions in your archive</div></div>')
    # Show the explainer whenever the visible history is short — that's the 30-day cleanup biting.
    # (Claude Code only; other sources don't have the same cleanupPeriodDays retention model.)
    if days <= 32 and (source in (None, "claude-code")):
        grew = ""
        if archive_info and archive_info.get("enabled"):
            grew = (f' This run preserved <b>{archive_info["new"]:,}</b> new session(s) to your '
                    f'archive (<code>{arch_dir_disp}</code>), so from here your history keeps growing '
                    f'past the 30-day wall — point <code>--archive</code> at a Dropbox/iCloud folder to '
                    f'keep it across machines and reinstalls.')
        retention_note = (
            '<div class="honesty" style="margin-top:14px">'
            f'<b>Why only ~{days} days?</b> Claude Code deletes transcripts older than your '
            '<code>cleanupPeriodDays</code> setting (default <b>30</b>), so that is all that was '
            'left on disk to read — not a limit of this tool. To analyze more history: '
            '<b>(1)</b> raise <code>cleanupPeriodDays</code> in <code>~/.claude/settings.json</code> '
            '(e.g. <code>"cleanupPeriodDays": 365</code>) to stop the deletion; '
            f'<b>(2)</b> keep running Claude Insight.{grew}'
            '</div>')

    # action cards (what/where/how)
    def evidence_html(card):
        name = card["dim"]
        ev = card["weak"]
        if not ev:
            return '<p class="ev-none">No clear examples in your transcripts — this is already a habit. ✓</p>'
        items = ""
        # small-sample guard per project
        proj_counts = Counter(p["project"] for p in corpus.real_prompts)
        for e in ev[:3]:
            if name == "Direction" or name == "Iteration":
                proj = e["project"]; txt = _scrub_paths(e["text"])
                small = " <em>(illustrative, small sample)</em>" if proj_counts.get(proj, 0) < 10 else ""
                items += f'<li>“{_esc(txt[:140])}” <span class="loc">— {_esc(_project_label(proj))}{small}</span></li>'
            elif name == "Context":
                small = " <em>(illustrative)</em>" if proj_counts.get(e["project"], 0) < 10 else ""
                items += f'<li>Edited <code>{_esc(os.path.basename(e["file"]))}</code> without reading it first <span class="loc">— {_esc(_project_label(e["project"]))}{small}</span></li>'
            elif name == "Verification":
                small = " <em>(illustrative)</em>" if proj_counts.get(e["project"], 0) < 10 else ""
                items += f'<li>A burst of edits to <code>{_esc(e["files"])}</code> with nothing run afterwards <span class="loc">— {_esc(_project_label(e["project"]))}{small}</span></li>'
        return f"<ul class='ev'>{items}</ul>"

    cards_html = ""
    for i, card in enumerate(cards[:2]):
        name = card["dim"]
        t = SKILL_TEACH[name]
        ex_html = "".join(
            f'<div class="ba"><div class="before"><span>Instead of</span>“{_esc(e["before"])}”</div>'
            f'<div class="after"><span>Stronger</span>“{_esc(e["after"])}”</div></div>'
            for e in t["examples"]
        )
        cards_html += f"""
      <div class="card prio">
        <div class="ph">Priority {i+1} · {_esc(disp(name))} <span class="pscore">now {card['score']}/100</span></div>
        <h4>{_esc(t['what_it_is'])}</h4>
        <p class="why"><b>Why it matters.</b> {_esc(t['why_it_matters'])}</p>
        <div class="wwh"><span class="lab">Where this shows up in your sessions</span>{evidence_html(card)}</div>
        <div class="wwh"><span class="lab">How to grow it</span><p class="how">{_esc(t['how_to_improve'])}</p>
          {ex_html}
        </div>
        <p class="tgt">🎯 Try this next session: {_esc(t['practice'])}</p>
      </div>"""

    # strength callout — lead with the user's signature (self-driven) strength
    s_det = dim_rate_line(strength)
    strength_html = f"""
      <div class="card keep">
        <div class="ph">Keep doing this · {_esc(disp(strength))} <span class="pscore">{round(result['shrunk'][strength])}/100</span></div>
        <p>{_esc(SKILL_TEACH[strength]['good_looks_like'])} The evidence in your sessions: {_esc(s_det)}. This is your foundation — build on it.</p>
      </div>"""

    # skill map (levels)
    skill_levels = _skill_levels(result)
    skill_html = ""
    for sk in skill_levels:
        dots = "".join(
            f'<span class="dot {"on" if i < sk["level"] else ""}"></span>' for i in range(5)
        )
        skill_html += f"""<div class="skill">
          <div class="sk-top"><span class="sk-name">{_esc(sk['name'])} <span class="lvl">Level {sk['level']}/5</span></span><span class="sk-dots">{dots}</span></div>
          <p class="sk-what">{_esc(sk['what'])}</p>
          <p class="sk-now"><b>You're here:</b> {_esc(sk['now'])}</p>
          <p class="sk-next"><b>Next move:</b> {_esc(sk['next'])}</p></div>"""

    prov_banner = ""
    if provisional:
        prov_banner = (f'<div class="prov">⚠️ Provisional: only {len(corpus.real_prompts)} real prompts found — '
                       f'treat the score as a rough range (±10). It sharpens as you use Claude Code more.</div>')

    # fun facts
    facts = [
        f"{len(corpus.real_prompts)} prompts you actually typed (out of {corpus.user_records:,} user records — the rest were tool output, subagent turns or system text)",
        f"median prompt is {d.get('median_chars','?')} characters ({d.get('median_words','?')} words); {d.get('under_80_pct','?')}% are under 80 chars",
        f"{active_h:.0f} hours of hands-on time (idle gaps over 5 min excluded)",
        f"{result['detail']['Toolcraft']['distinct']} distinct tools used; {corpus.total_tool_calls:,} tool calls in total",
        f"most-used tool: {corpus.tool_usage.most_common(1)[0][0] if corpus.tool_usage else 'n/a'}",
        f"{corpus.delegation_events} delegations (subagents / background jobs / planning)",
    ]
    facts_html = "".join(f"<li>{_esc(f)}</li>" for f in facts)
    assessment_html = build_assessment(corpus, result, cards)

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your AI Fluency Report</title>
<style>
:root{{--bg:#0c0d18;--p:#15172a;--p2:#1d2040;--ink:#eef0ff;--mut:#a4a8cc;--line:#2a2d52;
--ac:#7c5cff;--ac2:#3ad6c9;--good:#3ad68a;--warn:#ffb454;--bad:#ff6b8b;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:radial-gradient(1100px 640px at 72% -12%,#262a55 0%,var(--bg) 55%);color:var(--ink);
font:16px/1.65 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;padding-bottom:80px}}
.wrap{{max-width:880px;margin:0 auto;padding:0 22px}}
header{{text-align:center;padding:60px 0 12px}}
.kick{{letter-spacing:.22em;text-transform:uppercase;font-size:12px;color:var(--mut)}}
h1{{font-size:34px;margin:10px 0 4px}}
.sub{{color:var(--mut);max-width:620px;margin:6px auto 0;font-size:15px}}
.hero{{margin:30px auto 0;display:flex;gap:22px;align-items:stretch;flex-wrap:wrap;justify-content:center}}
.score-card{{background:linear-gradient(135deg,var(--p2),var(--p));border:1px solid var(--line);border-radius:22px;
padding:26px 30px;text-align:center;min-width:240px;box-shadow:0 18px 50px rgba(0,0,0,.4)}}
.ring{{position:relative;width:170px;height:170px;margin:0 auto}}
.ring .n{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}}
.ring .n b{{font-size:50px;line-height:1}}
.ring .n s{{text-decoration:none;color:var(--mut);font-size:13px}}
.band{{margin-top:12px;font-size:19px;font-weight:700;color:var(--ac2)}}
.rawnote{{color:var(--mut);font-size:12px;margin-top:4px}}
.arch{{flex:1;min-width:260px;background:var(--p);border:1px solid var(--line);border-radius:22px;padding:24px 26px;text-align:left}}
.arch .emoji{{font-size:40px}}
.arch h2{{font-size:23px;margin:6px 0}}
.arch p{{color:var(--mut);font-size:15px}}
.prov{{background:rgba(255,180,84,.1);border:1px solid rgba(255,180,84,.35);color:#ffe6c2;border-radius:12px;padding:12px 16px;margin:22px 0 0;font-size:14px}}
section{{margin:42px 0}}
h3{{font-size:13px;letter-spacing:.16em;text-transform:uppercase;color:var(--mut);border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:18px}}
.band-meaning{{background:var(--p);border:1px solid var(--line);border-left:4px solid var(--ac);border-radius:12px;padding:16px 20px;color:#dfe2ff}}
.assess{{background:var(--p);border:1px solid var(--line);border-radius:14px;padding:16px 20px;margin-bottom:12px;font-size:15.5px;line-height:1.7;color:#e8eaff}}
.assess b{{color:#fff}}
.ingest{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
.ing{{background:var(--p);border:1px solid var(--line);border-radius:14px;padding:14px 16px}}
.ing .n{{font-size:24px;font-weight:700;color:var(--ac2)}}
.ing .l{{color:var(--mut);font-size:13px;margin-top:2px}}
.honesty{{margin-top:16px;background:var(--p);border:1px solid var(--line);border-radius:14px;padding:16px 20px}}
.honesty b{{color:var(--ink)}}
.honesty ul{{list-style:none;display:flex;flex-wrap:wrap;gap:8px 22px;margin-top:8px}}
.honesty li{{color:var(--mut);font-size:14px}}
.dim{{background:var(--p);border:1px solid var(--line);border-radius:14px;padding:16px 20px;margin-bottom:12px}}
.dim .top{{display:flex;justify-content:space-between;align-items:baseline}}
.dim .name{{font-weight:700;font-size:17px}}
.dim .sval{{font-size:22px;font-weight:800}} .dim .hint{{color:var(--mut);font-size:12px;font-weight:400}}
.dim-h{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:6px}}
.dim-h b{{font-size:17px}}
.pill{{font-size:12px;font-weight:700;color:var(--ink);background:var(--p2);border:1px solid var(--line);border-radius:99px;padding:3px 11px;white-space:nowrap}}
.ev{{margin:8px 0 0 0;padding-left:18px}} .ev li{{color:var(--mut);font-size:14px;margin:3px 0}}
.next{{margin-top:8px;font-size:14.5px}} .next b{{color:#fff}}
.bar{{height:9px;background:#23264a;border-radius:99px;overflow:hidden;margin:11px 0 9px}}
.bar>i{{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--ac),var(--ac2))}}
.def{{color:var(--ink);font-size:14.5px}} .rate{{color:var(--mut);font-size:13px;margin-top:3px}} .wt{{opacity:.7}}
.tag{{font-size:10.5px;padding:2px 8px;border-radius:99px;font-weight:700;margin-left:6px;vertical-align:middle}}
.tag.s{{background:rgba(58,214,138,.16);color:var(--good)}} .tag.w{{background:rgba(255,107,139,.16);color:var(--bad)}}
.tag.ld{{background:rgba(164,168,204,.16);color:var(--mut)}}
.bar-item{{display:flex;align-items:center;gap:12px;margin:7px 0}}
.bl{{min-width:160px;font-size:14px}} .bt{{flex:1;height:7px;background:#23264a;border-radius:99px;overflow:hidden}}
.bt>i{{display:block;height:100%;background:linear-gradient(90deg,var(--ac),var(--ac2))}} .bv{{min-width:46px;text-align:right;color:var(--mut);font-size:13px}}
.card{{background:var(--p);border:1px solid var(--line);border-radius:16px;padding:18px 22px;margin-bottom:14px}}
.prio{{border-left:4px solid var(--warn)}} .keep{{border-left:4px solid var(--good)}}
.ph{{font-size:12px;text-transform:uppercase;letter-spacing:.1em;color:var(--mut)}}
.pscore{{float:right;color:var(--ac2);letter-spacing:0}}
.card h4{{font-size:18px;margin:8px 0 12px}}
.wwh{{margin:12px 0}} .wwh .lab{{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin-bottom:6px}}
ul.ev{{list-style:none}} ul.ev li{{background:var(--p2);border-radius:9px;padding:9px 12px;margin-bottom:7px;font-size:14px}}
.loc{{color:var(--mut);font-size:12.5px}} .ev-none{{color:var(--good);font-size:14px}}
.ba{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px}}
.why{{color:var(--mut);font-size:14px;margin:2px 0 4px}} .why b{{color:var(--ink)}}
.how{{font-size:14.5px;margin:0 0 4px}}
.sk-what{{color:var(--ink);font-size:13.5px;margin-top:5px}}
.lvl{{font-size:11px;color:var(--ac2);font-weight:600;margin-left:6px}}
.before,.after{{border-radius:10px;padding:10px 13px;font-size:14px}}
.before{{background:rgba(255,107,139,.08);color:#ffd0da}} .after{{background:rgba(58,214,138,.08);color:#cfeede}}
.before span,.after span{{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.08em;opacity:.7;margin-bottom:3px}}
.tgt{{margin-top:10px;color:var(--ac2);font-size:14px}}
.skill{{background:var(--p);border:1px solid var(--line);border-radius:14px;padding:14px 18px;margin-bottom:10px}}
.sk-top{{display:flex;justify-content:space-between;align-items:center}} .sk-name{{font-weight:700}}
.dot{{display:inline-block;width:11px;height:11px;border-radius:50%;background:#2a2d52;margin-left:4px}}
.dot.on{{background:linear-gradient(135deg,var(--ac),var(--ac2))}}
.sk-now{{color:var(--mut);font-size:13.5px;margin-top:6px}} .sk-next{{font-size:13.5px;margin-top:3px}}
.facts{{list-style:none}} .facts li{{background:var(--p);border:1px solid var(--line);border-radius:10px;padding:11px 15px;margin-bottom:8px;font-size:14.5px}}
.facts li::before{{content:"›";color:var(--ac2);font-weight:800;margin-right:9px}}
details{{background:var(--p);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin-top:14px}}
summary{{cursor:pointer;color:var(--mut);font-size:14px}} details p,details li{{color:var(--mut);font-size:13px;margin-top:8px}}
footer{{text-align:center;color:var(--mut);font-size:13px;margin-top:46px}}
code{{background:#23264a;padding:1px 6px;border-radius:5px;font-size:13px}}
@media(max-width:640px){{.ba{{grid-template-columns:1fr}}.bl{{min-width:120px}}}}
</style></head><body><div class="wrap">

<header>
  <div class="kick">Claude Insight · AI Fluency Report</div>
  <h1>How skillfully you build with AI</h1>
  <p class="sub">A read of how you actually drive {_esc(src_label)} — measured from your real prompts and the agent's real actions, analyzed entirely on your machine.</p>
</header>

{prov_banner}

<div class="hero">
  <div class="score-card">
    <div class="ring">
      <svg width="170" height="170" style="transform:rotate(-90deg)">
        <circle cx="85" cy="85" r="74" fill="none" stroke="#23264a" stroke-width="12"/>
        <circle cx="85" cy="85" r="74" fill="none" stroke="url(#g)" stroke-width="12" stroke-linecap="round"
          stroke-dasharray="{2*math.pi*74*result['overall']/100:.0f} 999"/>
        <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#7c5cff"/><stop offset="1" stop-color="#3ad6c9"/></linearGradient></defs>
      </svg>
      <div class="n"><b>{result['overall']}</b><s>/ 100</s></div>
    </div>
    <div class="band">{_esc(result['band'])}</div>
    <div class="rawnote">raw {result['overall_raw']} · confidence-adjusted {result['overall']}</div>
  </div>
  <div class="arch">
    <div class="emoji">{PROTOTYPES[a['primary']]['emoji']}</div>
    <h2>{_esc(a['label'])}</h2>
    <p>{_esc(a['blurb'])}</p>
    <p style="margin-top:10px;font-size:13px">Closest match {a['primary_sim']:+.2f}, next is {_esc(a['secondary'].replace('The ',''))} {a['secondary_sim']:+.2f}{' — close, so this is a blend' if a['blended'] else ''}. Built from how <b>you</b> drive — your briefs, corrections, tool choices and how much you hand off ({a['delegation_score']}/100 delegation) — and it deliberately discounts the read-before-edit and run-the-tests habits Claude does on its own, so it reflects you, not the agent.</p>
    <p style="margin-top:8px;font-size:12.5px;color:var(--mut)">Your <b>score</b> measures the quality of the collaboration (you + Claude); your <b>archetype</b> measures your driving style alone — so they can differ on purpose.</p>
  </div>
</div>

<section>
  <h3>Professional assessment</h3>
  {assessment_html}
</section>

<section>
  <h3>What your score means</h3>
  <div class="band-meaning"><b>{_esc(result['band'])} ({result['overall']}/100).</b> {_esc(result['band_meaning'])}</div>
</section>

<section>
  <h3>How much data this is based on</h3>
  <p style="color:var(--mut);font-size:14px;margin:-8px 0 14px">Source: <b>{_esc(src_label)}</b>{(' · ' + ', '.join(_esc(disp(n)) for n in na) + ' not measurable from this source (excluded from the score)') if na else ''}</p>
  <div class="ingest">
    <div class="ing"><div class="n">{corpus.files}</div><div class="l">sessions scanned</div></div>
    <div class="ing"><div class="n">{len(corpus.projects)}</div><div class="l">projects</div></div>
    <div class="ing"><div class="n">{corpus.total_bytes/1e6:.1f} MB</div><div class="l">transcript data parsed</div></div>
    <div class="ing"><div class="n">{days} days</div><div class="l">span of activity</div></div>
    <div class="ing"><div class="n">{len(corpus.real_prompts)}</div><div class="l">real prompts you typed</div></div>
    <div class="ing"><div class="n">{active_h:.0f} h</div><div class="l">hands-on active time</div></div>
    {archive_tile}
  </div>
  {retention_note}
  <div class="honesty">
    <b>The honest part:</b> we found {corpus.user_records:,} “user” records but only <b>{len(corpus.real_prompts)}</b> are prompts <b>you</b> typed. We filtered out {filtered_total:,} that the old tool wrongly counted:
    <ul>{filt}</ul>
    <p style="color:var(--mut);font-size:13px;margin-top:10px">Your real prompts: median {d.get('median_chars','?')} chars · {d.get('under_80_pct','?')}% under 80 chars · {active_h:.0f} h hands-on active time (idle gaps over 5 min are excluded — not raw wall-clock). Analyzed 100% on your machine; nothing was uploaded.</p>
  </div>
</section>

{analysis_section}

<section>
  <h3>The five dimensions</h3>
  {dim_html}
</section>

<section>
  <h3>What to improve — and exactly how</h3>
  {cards_html}
  {strength_html}
</section>

<section>
  <h3>Your skill map</h3>
  {skill_html}
</section>

<section>
  <h3>Archetype affinity</h3>
  {aff}
</section>

<section>
  <h3>Honest numbers at a glance</h3>
  <ul class="facts">{facts_html}</ul>
</section>

<section>
  <h3>Methodology &amp; honesty</h3>
  <details><summary>How every number was computed (click to expand)</summary>
    <p><b>Only real prompts are scored.</b> A “user” record counts as a prompt only if it is not a tool-result, not a subagent (sidechain) turn, not meta/injected, not a slash-command stub, and not a paste/system-prompt over {MAX_HUMAN_PROMPT_CHARS:,} chars or opening with “You are …”. This removes the contamination that made the old tool report a {d.get('mean_chars','?')}-vs-real average.</p>
    <p><b>Everything is a rate, then squashed.</b> Each dimension is a per-prompt or per-opportunity rate run through min(1, rate/target), so doing more work never raises the score — only doing it better does. Weights: Briefing 24%, Verification 22%, Context-setting 22%, Iteration 18%, Toolcraft 14%.</p>
    <p><b>Thin signals are hedged, not faked.</b> Each dimension is pulled toward a neutral 50 in proportion to how many opportunities it had (e.g. Iteration had only {result['detail']['Iteration']['corrections']} corrections, so it is flagged “low data”). Both raw and confidence-adjusted scores are shown.</p>
    <p><b>Archetype</b> describes your <b>driving style</b>, not the collaboration's quality, so it is built on a separate <b>agency-weighted</b> vector: Briefing, Iteration, Toolcraft and Delegation (handoffs to subagents/background jobs/planning) count fully, while Verification and Context — habits Claude largely does on its own — are discounted ({int(AGENCY['Verification']*100)}% and {int(AGENCY['Context']*100)}% weight). It is the nearest prototype by cosine on z-scored values; if the top two are within {ARCHETYPE_MARGIN} we show a blend. <b>Active time</b> caps idle gaps at {GAP_CAP_SECONDS//60} min. <b>Fixes vs v1:</b> prompt mis-count, length inflation, idle-time over-count, random archetype, uncapped tool-diversity, and keyword “error” false-positives.</p>
    <p><b>Limits:</b> this measures observable behavior, not intent; detectors are heuristic and English-biased; it's a single snapshot, not a trend. Terse prompts that carry intent from the prior turn can under-score Direction.</p>
  </details>
</section>

<footer>Generated locally by Claude Insight v2 · your transcripts never left this machine.</footer>
</div></body></html>"""


def _skill_levels(result):
    """Map dimension scores to L1-L5 skill levels with now/next text."""
    def lvl(score):
        return max(1, min(5, int(score // 20) + 1))
    s = result["shrunk"]
    na = set(result.get("na_dims") or [])
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
        if dim in na:
            continue
        L = lvl(s[dim])
        out.append({"name": name, "dim": dim, "level": L, "now": rub[L],
                    "what": SKILL_TEACH[dim]["what_it_is"],
                    "next": nxt if L < 5 else "maintain this — it's a real strength."})
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _source_out_path(out, source):
    """Per-source report path for --source all: report.html -> report.codex.html."""
    root, ext = os.path.splitext(out)
    return f"{root}.{source}{ext or '.html'}"


def _run_source(adapter, args, out_path, analysis, multi=False):
    """Full pipeline for ONE source: discover -> (archive) -> parse -> analyze -> render.
    Returns 0 on a produced report, 1 if there was nothing to analyze."""
    files = adapter.discover(args.path)

    # Persistent archive (Claude Code only): preserve transcripts past the 30-day cleanup so
    # history accumulates. Skipped for sources whose logs aren't subject to it, and when an
    # explicit path is given.
    archive_info = None
    if adapter.archive_enabled and not args.path:
        archive_dir = os.path.expanduser(args.archive)
        new = updated = 0
        if not args.no_archive:
            new, updated = archive_transcripts(files, archive_dir)
        arch_files = _filter_transcripts(glob.glob(os.path.join(archive_dir, "**", "*.jsonl"), recursive=True))
        merged = _dedupe_sessions(files + arch_files)
        archive_info = {
            "dir": args.archive, "enabled": not args.no_archive,
            "live_sessions": len(files), "archived_sessions": len(arch_files),
            "merged_sessions": len(merged), "new": new, "updated": updated,
        }
        files = merged

    if not files:
        if not multi:
            where = args.path or f"the {adapter.name} default location"
            print(f"No {adapter.name} data found in {where}.\n"
                  f"Point at your logs with:  python3 insight.py --source {adapter.name} /path", file=sys.stderr)
        return 1

    corpus = parse(files, adapter)
    if not corpus.real_prompts:
        if not multi:
            print(f"Found {adapter.name} logs but no real human-typed prompts to analyze.", file=sys.stderr)
        return 1

    result = analyze(corpus, adapter.capabilities)
    cards, strength = build_action_plan(corpus, result)

    if args.evidence:
        bundle = build_evidence(corpus, result, cards, archive_info,
                                source=adapter.name, capabilities=adapter.capabilities)
        text = json.dumps(bundle, indent=2)
        if args.evidence == "-":
            print(text)
        else:
            ep = os.path.abspath(_source_out_path(args.evidence, adapter.name) if multi else args.evidence)
            os.makedirs(os.path.dirname(ep) or ".", exist_ok=True)
            with open(ep, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"  Evidence: {ep}", file=sys.stderr)

    if args.json:
        payload = {
            "source": adapter.name, "capabilities": adapter.capabilities,
            "not_measurable": result["na_dims"],
            "overall": result["overall"], "overall_raw": result["overall_raw"],
            "band": result["band"], "archetype": result["archetype"]["label"],
            "dimensions_raw": result["raw"], "dimensions_adjusted": result["shrunk"],
            "confidence": result["conf"], "detail": result["detail"],
            "data_ingested": {
                "files": corpus.files, "projects": len(corpus.projects),
                "bytes": corpus.total_bytes, "user_records": corpus.user_records,
                "real_prompts": len(corpus.real_prompts), "filtered": dict(corpus.filtered),
                "signals": dict(corpus.signals),
                "active_hours": round(corpus.active_seconds / 3600, 1),
                "prompt_distribution": result["dist"],
                "archive": archive_info,
            },
        }
        print(json.dumps(payload, indent=2))
        return 0

    # Render fully before touching the file, so a render error can't leave a 0-byte report.
    html_doc = build_html(corpus, result, cards, strength, archive_info, analysis,
                          source=adapter.name, capabilities=adapter.capabilities)
    out_path = os.path.abspath(out_path)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)

    print(terminal_summary(corpus, result))
    if archive_info and archive_info["enabled"]:
        print(f"  Archive: {archive_info['merged_sessions']} sessions preserved at "
              f"{_scrub_paths(archive_info['dir'])} ({archive_info['new']} new, {archive_info['updated']} updated this run).")
    print(f"  Report ({adapter.name}): {out_path}\n")
    if not args.no_open:
        try:
            webbrowser.open(f"file://{out_path}")
        except Exception:
            pass
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Claude Insight v2 — AI fluency analyzer (one command, zero install).")
    ap.add_argument("path", nargs="?", help="transcript dir / file / SQLite DB (default: the source's standard location)")
    ap.add_argument("-o", "--out", default="ai_fluency_report.html", help="HTML output path")
    ap.add_argument("--source", default="auto",
                    choices=["auto", "all", "claude-code", "claude-desktop", "codex", "cursor"],
                    help="which coding-agent logs to analyze (default: auto-detect; 'all' = one "
                         "report per available source)")
    ap.add_argument("--json", action="store_true", help="print raw metrics as JSON and exit")
    ap.add_argument("--no-open", action="store_true", help="don't auto-open the report in a browser")
    ap.add_argument("--archive", default=os.environ.get("CLAUDE_INSIGHT_ARCHIVE", DEFAULT_ARCHIVE_DIR),
                    metavar="DIR",
                    help="persistent archive that preserves Claude Code transcripts beyond its "
                         "30-day cleanup so history accumulates (default ~/.claude/insight-archive; "
                         "point at a Dropbox/iCloud folder to keep it across machines)")
    ap.add_argument("--no-archive", action="store_true",
                    help="don't copy this run's transcripts into the archive (still reads an existing one)")
    ap.add_argument("--evidence", metavar="PATH",
                    help="write the de-contaminated evidence bundle (JSON) for the two-model "
                         "analysis pipeline to PATH ('-' for stdout), then continue")
    ap.add_argument("--analysis", metavar="PATH",
                    help="merge an AI analysis (JSON from the Opus stage) into the report's skill map")
    args = ap.parse_args(argv)

    analysis = None
    if args.analysis:
        try:
            with open(os.path.expanduser(args.analysis), encoding="utf-8") as f:
                analysis = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"Could not read --analysis {args.analysis}: {e}", file=sys.stderr)
            return 1

    # --source all: one report per source that has data on this machine.
    if args.source == "all":
        if args.path:
            print("--source all reads each source's standard location and can't be combined with an "
                  "explicit path (a path implies a single source). Pass a specific --source with the path.",
                  file=sys.stderr)
            return 2
        if analysis:
            print("  Note: --analysis applies to a single source; ignoring it for --source all.", file=sys.stderr)
        produced = 0
        for adapter in ADAPTERS.values():
            if not adapter.detect():
                continue
            rc = _run_source(adapter, args, _source_out_path(args.out, adapter.name), None, multi=True)
            produced += 1 if rc == 0 else 0
        if not produced:
            print("No coding-agent logs found for any source on this machine.", file=sys.stderr)
            return 1
        return 0

    # Single source: explicit, or auto-detect (a positional path defaults to Claude Code's format).
    if args.source == "auto":
        adapter = ADAPTERS["claude-code"] if args.path else detect_adapter()
    else:
        adapter = ADAPTERS[args.source]
    return _run_source(adapter, args, args.out, analysis, multi=False)


if __name__ == "__main__":
    raise SystemExit(main())
