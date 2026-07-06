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
import statistics
import sys
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

# Commands where work leaves the machine (or becomes shared history). The Diligence
# competency's core observable is whether these are GATED by a verification that ran
# after the last edit — "did they check before it mattered".
SHIP_RE = re.compile(
    r"\b("
    r"git (commit|push|merge)|gh (pr|release) (create|merge)|"
    r"npm publish|yarn publish|twine upload|cargo publish|gem push|"
    r"terraform apply|kubectl apply|docker push|"
    r"fly deploy|vercel deploy|netlify deploy|eb deploy|cap deploy"
    r")\b",
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
# Description has three sub-skills in the AI Fluency framework. Product description is the
# artifact/constraint/intent cues above; these two catch the other legs:
#   process description — telling the agent HOW to get there (order, steps, gates)
#   performance description — the shape/format/style the output should take
PROCESS_CUE = re.compile(
    r"\b(first|then|after that|before (you|that)|start by|step \d|one at a time|"
    r"in (that|this) order|once (that|it|you)|finally)\b", re.I
)
PERFORMANCE_CUE = re.compile(
    r"\b(act as|as (json|markdown|a table)|be (concise|brief|thorough|specific)|"
    r"format (it|the output|as)|in the style of|output only|respond (with|in)|"
    r"show (me )?the diff|no (commentary|explanations?))\b", re.I
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

# ---- The 4D competency layer -------------------------------------------------
# The AI Fluency framework defines fluency as four competencies: Delegation,
# Description, Discernment, Diligence. The engine measures seven behavioral SIGNALS
# from transcripts and blends them into deterministic competency scores; the headline
# score is the weighted competency blend. (The AI stage adds judgment on top — it
# never changes these numbers.)
COMP_WEIGHTS = {"Delegation": 0.25, "Description": 0.30, "Discernment": 0.25, "Diligence": 0.20}

# How each competency is composed from the measured signals (each row sums to 1.0),
# following the mapping in reference/ai-fluency-framework.md. Note the agency logic is
# built in: Verification/Context (habits Claude largely drives) enter only through
# Discernment/Diligence at partial weight, while the user-driven signals (Direction,
# Delegation, Shipping, Iteration) carry the competencies they define.
COMP_MIX = {
    "Delegation":  {"Delegation": 0.45, "Toolcraft": 0.35, "Direction": 0.20},
    "Description": {"Direction": 0.80, "Iteration": 0.20},
    "Discernment": {"Verification": 0.40, "Context": 0.35, "Iteration": 0.25},
    "Diligence":   {"Shipping": 0.45, "Verification": 0.30, "Context": 0.25},
}
COMP_LEVEL_LABELS = ["Emerging", "Developing", "Proficient", "Advanced", "Expert"]

# Effective per-signal weights, DERIVED from the competency blend so the two views can
# never drift apart: overall = Σ COMP_WEIGHTS·competency = Σ WEIGHTS·signal.
WEIGHTS = {}
for _comp, _mix in COMP_MIX.items():
    for _sig, _w in _mix.items():
        WEIGHTS[_sig] = WEIGHTS.get(_sig, 0.0) + COMP_WEIGHTS[_comp] * _w
del _comp, _mix, _sig, _w

# Opportunity-count targets for per-signal confidence shrinkage.
TARGET_N = {"Direction": 60, "Verification": 15, "Context": 25, "Iteration": 12,
            "Toolcraft": 40, "Delegation": 25, "Shipping": 8}

# User-facing labels — plain English, readable by any human. Internal keys stay
# stable (evidence/JSON consumers see both via DISPLAY_NAMES).
DISPLAY_NAMES = {"Direction": "How you ask", "Verification": "Checking the work",
                 "Context": "Reading before changing", "Iteration": "Course-correcting",
                 "Toolcraft": "Using the right tools",
                 "Delegation": "Handing off whole jobs", "Shipping": "Checking before you ship"}

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
    "Delegation": {
        "what_it_is": "Handing the agent whole, outcome-shaped jobs — and reaching for planning, sub-agents and background runs when the work is big or parallel.",
        "why_it_matters": "Micro-stepping wastes the agent's autonomy: every hand-back costs you a round-trip, while a scoped whole job lets it search, edit and verify in one run.",
        "how_to_improve": "Bundle your next three micro-requests into one hand-off with a finish line: what done looks like, what not to touch, and tell the agent to keep going until it's verified.",
        "examples": [
            {"before": "open api.py", "after": "In api.py, add rate-limiting to the public endpoints (100 req/min per key), leave the admin routes alone, and run the tests — come back when they pass."},
            {"before": "now add the test", "after": "Implement the retry helper in http/client.py and add tests covering timeout and 5xx; run pytest and iterate until green, then summarize what changed."},
        ],
        "practice": "Once per session, hand off a whole task with a 'done when…' line instead of steering it step by step.",
        "good_looks_like": "You define outcomes and guardrails; the agent plans, executes and verifies the middle on its own.",
    },
    "Shipping": {
        "what_it_is": "Gating anything that leaves the machine — commits, pushes, deploys, publishes — behind a real check that ran after your last edit.",
        "why_it_matters": "A commit or deploy is the moment a mistake becomes everyone's problem; one test or build run right before it is the cheapest insurance there is.",
        "how_to_improve": "Make the check part of the ship request itself: 'run the tests, and only commit if they pass.' Never let a ship command be the first thing after an edit.",
        "examples": [
            {"before": "commit and push", "after": "Run the test suite; if it's green, commit with a message describing the rate-limit change and push. If anything fails, stop and show me the output."},
            {"before": "deploy it", "after": "Build, run the smoke tests against staging, then deploy. Paste the health-check response after so we know it's live and healthy."},
        ],
        "practice": "Put one verification between your last edit and your next commit, push or deploy — every time.",
        "good_looks_like": "Every commit, push or deploy sits right after a passing check — shipping is a verified act, not a hopeful one.",
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
    "Director":         {"emoji": "🎬", "vec": [66, 85, 70, 82, 70, 85],
        "blurb": "You hand over whole outcomes, steer at the level of intent, and hold every result to a hard acceptance test — you delegate like a lead and inspect like QA."},
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
        self.active_days = set()        # distinct calendar dates with any activity
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


def parse(files):
    c = Corpus()
    c.files = len(files)
    for path in files:
        project = os.path.basename(os.path.dirname(path)) or "default"
        c.projects.add(project)
        try:
            c.total_bytes += os.path.getsize(path)
        except OSError:
            pass
        session_id = os.path.splitext(os.path.basename(path))[0]
        timeline = []
        ts_in_file = []
        prompt_idx = 0
        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(e.get("timestamp"))
                if ts:
                    ts_in_file.append(ts)
                    c.active_days.add(ts.date())
                    c.first_ts = ts if c.first_ts is None or ts < c.first_ts else c.first_ts
                    c.last_ts = ts if c.last_ts is None or ts > c.last_ts else c.last_ts
                msg = e.get("message") if isinstance(e.get("message"), dict) else {}
                role = e.get("role") or msg.get("role") or e.get("type")
                content = msg.get("content", e.get("content"))

                if role == "assistant":
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                raw = b.get("name", "unknown")
                                name = _denamespace_tool(raw)
                                c.tool_usage[name] += 1
                                c.total_tool_calls += 1
                                inp = b.get("input", {}) if isinstance(b.get("input"), dict) else {}
                                if name.lower() in DELEGATION_TOOLS:
                                    c.delegation_events += 1
                                if name.lower() == "bash" and inp.get("run_in_background"):
                                    c.delegation_events += 1
                                fpath = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
                                cmd = inp.get("command") if name.lower() == "bash" else None
                                timeline.append({
                                    "kind": "tool", "name": name.lower(),
                                    "file": fpath, "cmd": cmd, "ts": ts,
                                })
                    continue

                if role != "user":
                    continue
                c.user_records += 1
                if _is_tool_result(content):
                    c.filtered["tool results"] += 1
                    continue
                if e.get("isSidechain") is True:
                    c.filtered["subagent turns"] += 1
                    continue
                if e.get("isMeta") is True:
                    c.filtered["meta-injected"] += 1
                    continue
                text = _text_of(content).strip()
                if not text:
                    c.filtered["empty"] += 1
                    continue
                if _looks_injected(text):
                    c.filtered["injected / pasted"] += 1
                    continue
                # A genuine, human-typed prompt.
                prompt_idx += 1
                rec = {"text": text, "project": project, "session": session_id, "idx": prompt_idx}
                c.real_prompts.append(rec)
                timeline.append({"kind": "prompt", "text": text, "rec": rec, "ts": ts})

        if len(ts_in_file) >= 2:
            ts_in_file.sort()
            c.active_seconds += sum(
                min((ts_in_file[i + 1] - ts_in_file[i]).total_seconds(), GAP_CAP_SECONDS)
                for i in range(len(ts_in_file) - 1)
            )
        if timeline:
            c.sessions[session_id] = {"project": project, "timeline": timeline}
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
    constraint = artifact = intent = process = performance = shaped = 0
    weak_examples = []
    for p in prompts:
        t = p["text"]
        has_artifact = bool(ARTIFACT_RE.search(t))
        has_constraint = bool(CONSTRAINT_CUE.search(t) and ACTION_VERB.search(t))
        has_intent = bool(INTENT_CUE.search(t))
        has_process = bool(PROCESS_CUE.search(t))
        has_performance = bool(PERFORMANCE_CUE.search(t))
        artifact += 1 if has_artifact else 0
        constraint += 1 if has_constraint else 0
        intent += 1 if has_intent else 0
        process += 1 if has_process else 0
        performance += 1 if has_performance else 0
        shaped += 1 if (has_process or has_performance) else 0
        if _is_action_prompt(t) and not (has_artifact or has_constraint or has_intent) and len(t) < 120:
            weak_examples.append(p)
    constraint_rate = constraint / n
    artifact_rate = artifact / n
    intent_rate = intent / n
    # process/performance description (the framework's other two Description legs):
    # saying HOW to get there (order, steps) or what SHAPE the output should take.
    shape_rate = shaped / n
    # front-loading: penalize rules first revealed via a high-info correction
    corr = _find_corrections(corpus)
    new_rule_corrections = sum(1 for x in corr if x["high_info"])
    action_prompts = max(1, sum(1 for p in prompts if _is_action_prompt(p["text"])))
    front_loading = 1 - clamp(new_rule_corrections / action_prompts, 0, 1)
    score = 100 * (
        0.25 * squash(constraint_rate, 0.45)
        + 0.20 * squash(artifact_rate, 0.45)
        + 0.20 * squash(intent_rate, 0.30)
        + 0.15 * squash(shape_rate, 0.25)
        + 0.20 * front_loading
    )
    detail = {
        "n": n, "constraint_rate": constraint_rate, "artifact_rate": artifact_rate,
        "intent_rate": intent_rate, "process_rate": process / n,
        "performance_rate": performance / n, "shape_rate": shape_rate,
        "front_loading": front_loading,
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
    # Confidence is keyed on prompt count n (the opportunity count), NOT correction count k:
    # a user with many clean prompts and zero corrections has STRONG evidence of good iteration,
    # so it must not be shrunk toward 50 as if it were "no data".
    detail = {"n": n, "corrections": k, "correction_rate": rate, "specificity": specificity}
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


def _tool_kind(name):
    if name in READ_TOOLS:
        return "look"
    if name in EDIT_TOOLS:
        return "change"
    if name == "bash":
        return "check"
    return "other"


def score_delegation(corpus):
    """Delegation as the framework defines it: handing the agent WHOLE jobs, not
    micro-steps. Three rate-based parts:
      * hand-off events per active hour (sub-agents, background runs, planning) —
        path awareness;
      * hand-off depth — the median number of agent actions each action-prompt buys
        before the user has to steer again;
      * whole-job rate — the share of hand-offs whose run covered a full
        look→change→check cycle. This credits delegation done in PROMPTS ("build X
        and verify it"), not just delegation done with tools — a director who never
        touches plan-mode still delegates.
    All rates, so doing MORE of the same never raises the score."""
    run_lengths = []
    whole_jobs = 0
    micro_examples = []
    for sid, project, timeline in _iter_sessions(corpus):
        cur = None      # the action prompt whose run we're counting
        count = 0
        kinds = set()

        def close_run():
            nonlocal whole_jobs
            run_lengths.append(count)
            if len(kinds - {"other"}) >= 3 or count >= 6:
                whole_jobs += 1
            if count <= 1:
                micro_examples.append({"session": sid, "project": project,
                                       "text": cur["text"]})

        for ev in timeline:
            if ev["kind"] == "prompt":
                if cur is not None:
                    close_run()
                cur = ev if _is_action_prompt(ev["text"]) else None
                count = 0
                kinds = set()
            elif cur is not None:
                count += 1
                kinds.add(_tool_kind(ev["name"]))
        if cur is not None:
            close_run()
    n = len(run_lengths)
    if n == 0:
        return 50.0, {"n": 0, "delegation_events": corpus.delegation_events,
                      "events_per_hour": 0.0, "median_run": 0, "whole_job_rate": None}, []
    active_hours = max(corpus.active_seconds / 3600, 0.5)
    eph = corpus.delegation_events / active_hours
    median_run = statistics.median(run_lengths)
    depth = squash(median_run, 6)
    whole_rate = whole_jobs / n
    score = 100 * (0.35 * squash(eph, 2.0) + 0.35 * depth + 0.30 * squash(whole_rate, 0.5))
    detail = {"n": n, "delegation_events": corpus.delegation_events,
              "events_per_hour": round(eph, 2), "median_run": round(median_run, 1),
              "whole_job_rate": round(whole_rate, 2)}
    return score, detail, micro_examples[:4]


def score_shipping(corpus):
    """Deployment diligence: when work leaves the machine (commit/push/deploy/publish),
    was it GATED by a verification that ran after the last edit? A ship command that is
    itself compound with a check (e.g. `npm test && git push`) counts as gated. With no
    ship events at all the signal is neutral (n=0 → fully hedged), never a penalty."""
    ships = gated = 0
    blind_examples = []
    for sid, project, timeline in _iter_sessions(corpus):
        dirty = False   # edits since the last verification
        for ev in timeline:
            if ev["kind"] != "tool":
                continue
            name = ev["name"]
            cmd = ev.get("cmd") or ""
            if name in EDIT_TOOLS:
                dirty = True
            elif name == "bash":
                if SHIP_RE.search(cmd):
                    ships += 1
                    if not dirty or VERIFY_RE.search(cmd):
                        gated += 1
                    else:
                        blind_examples.append({"session": sid, "project": project,
                                               "cmd": cmd[:100]})
                    dirty = False
                elif VERIFY_RE.search(cmd):
                    dirty = False
    if ships == 0:
        return 50.0, {"n": 0, "ships": 0, "gated": 0, "rate": None}, []
    rate = gated / ships
    score = 100 * squash(rate, 0.80)
    return score, {"n": ships, "ships": ships, "gated": gated, "rate": rate}, blind_examples[:4]


# --------------------------------------------------------------------------- #
# Episode mining — the specific moments that make the report feel personal.
# Scores stay untouched; episodes are EVIDENCE: they show where a habit actually
# cost (or saved) this person time, quoting their own prompts and files.
# --------------------------------------------------------------------------- #

_FIX_COMMIT_RE = re.compile(r"git commit.*\b(fix|revert|hotfix|oops|typo|broke|undo)", re.I)


def _minutes_between(a, b, fallback_turns):
    """Elapsed minutes between two timeline events, idle-capped per hop like active
    time; falls back to ~2 min per turn when timestamps are missing."""
    if a and b:
        secs = (b - a).total_seconds()
        if 0 <= secs <= 3600:
            return max(1, round(secs / 60))
    return fallback_turns * 2


def mine_episodes(corpus):
    """Concrete, quotable moments from this person's own sessions:
      * correction_loops — 2+ corrections in a row before the fix landed (with the
        real prompts and roughly what the loop cost in time);
      * blind_reedits — a file edited without being read, then edited AGAIN soon
        after (the first change didn't land clean);
      * ship_then_fix — an unverified commit/push followed by a fix/revert commit
        in the same session (the check that was skipped got paid for later);
      * best_brief / best_correction — their own strongest moments, quoted back.
    Everything is deterministic and drawn verbatim from the transcripts."""
    loops, blind_reedits, ship_fixes = [], [], []
    best_brief, best_brief_cues = None, -1
    best_corr, best_corr_info = None, -1

    for sid, project, timeline in _iter_sessions(corpus):
        # -- correction loops ------------------------------------------------
        run = []
        saw_tool = False
        for ev in timeline:
            if ev["kind"] == "tool":
                saw_tool = True
                continue
            head = ev["text"][:160]
            is_corr = bool(saw_tool and CORRECTION_CUE.search(head)
                           and not PRAISE_CUE.search(head))
            if is_corr:
                run.append(ev)
            else:
                if len(run) >= 2:
                    loops.append({
                        "session": sid, "project": project, "turns": len(run),
                        "prompts": [r["text"][:160] for r in run[:3]],
                        "minutes": _minutes_between(run[0].get("ts"), run[-1].get("ts"), len(run)),
                    })
                run = []
            saw_tool = False
        if len(run) >= 2:
            loops.append({
                "session": sid, "project": project, "turns": len(run),
                "prompts": [r["text"][:160] for r in run[:3]],
                "minutes": _minutes_between(run[0].get("ts"), run[-1].get("ts"), len(run)),
            })

        # -- blind edits that needed re-edits ---------------------------------
        read_paths, edited_once, blind_files = set(), set(), {}
        for ev in timeline:
            if ev["kind"] != "tool":
                continue
            name, fpath = ev["name"], ev.get("file")
            if not fpath:
                continue
            if name in READ_TOOLS:
                read_paths.add(fpath)
            elif name in EDIT_TOOLS:
                if fpath in blind_files:
                    blind_files[fpath] += 1
                elif name != "write" and fpath not in read_paths and fpath not in edited_once:
                    blind_files[fpath] = 0
                edited_once.add(fpath)
        for fpath, reedits in blind_files.items():
            if reedits >= 1:
                blind_reedits.append({"session": sid, "project": project,
                                      "file": os.path.basename(fpath), "reedits": reedits})

        # -- unverified ship, then a fix commit --------------------------------
        dirty = False
        pending_blind_ship = None
        for ev in timeline:
            if ev["kind"] != "tool":
                continue
            name, cmd = ev["name"], ev.get("cmd") or ""
            if name in EDIT_TOOLS:
                dirty = True
            elif name == "bash":
                if SHIP_RE.search(cmd):
                    if pending_blind_ship and _FIX_COMMIT_RE.search(cmd):
                        ship_fixes.append({"session": sid, "project": project,
                                           "ship_cmd": pending_blind_ship[:100],
                                           "fix_cmd": cmd[:100]})
                        pending_blind_ship = None
                    elif dirty and not VERIFY_RE.search(cmd):
                        pending_blind_ship = cmd
                    dirty = False
                elif VERIFY_RE.search(cmd):
                    dirty = False

    # -- their own best moments ------------------------------------------------
    for p in corpus.real_prompts:
        t = p["text"]
        if len(t) > 600:
            continue
        cues = (bool(ARTIFACT_RE.search(t)) + bool(CONSTRAINT_CUE.search(t))
                + bool(INTENT_CUE.search(t)) + bool(PROCESS_CUE.search(t))
                + bool(PERFORMANCE_CUE.search(t)))
        if cues > best_brief_cues and _is_action_prompt(t) and cues >= 2:
            best_brief_cues, best_brief = cues, p
    for x in _find_corrections(corpus):
        info = (bool(re.search(r"\d", x["text"])) + bool(ARTIFACT_RE.search(x["text"]))
                + min(len(x["text"].split()), 40) / 40)
        if x["high_info"] and info > best_corr_info and len(x["text"]) <= 400:
            best_corr_info, best_corr = info, x

    loops.sort(key=lambda L: (L["turns"], L["minutes"]), reverse=True)
    return {
        "correction_loops": loops[:4],
        "blind_reedits": blind_reedits[:4],
        "ship_then_fix": ship_fixes[:4],
        "best_brief": ({"text": best_brief["text"][:400], "project": best_brief["project"]}
                       if best_brief else None),
        "best_correction": ({"text": best_corr["text"][:400], "project": best_corr["project"]}
                            if best_corr else None),
        "loop_turns_total": sum(L["turns"] for L in loops),
        "loop_minutes_total": sum(L["minutes"] for L in loops),
    }


# --------------------------------------------------------------------------- #
# Driver share — owned habits vs borrowed ones.
#
# The fluency score deliberately rates the SYSTEM (you + Claude): setting up a
# collaboration where verification always happens IS the skill, no matter whose
# keystroke ran pytest. But a habit is only robust if the USER would keep it alive
# with a less diligent agent — so we measure, for every check and every read, whether
# the user's own prompt asked for it or the agent volunteered it. High agent share
# isn't penalized; it's surfaced ("borrowed discipline") and it feeds the archetype's
# agency weighting with MEASURED values instead of fixed constants.
# --------------------------------------------------------------------------- #

CHECK_DEMAND_RE = re.compile(
    r"\b(test|tests|verify|check|make sure|confirm|prove|run (it|the|them)|green|lint|build it)\b", re.I)
READ_DEMAND_RE = re.compile(
    r"\b(read|look at|open|inspect|first understand|explore|review|study|before (touching|changing|editing))\b", re.I)


def measure_driver_share(corpus):
    """For each verification command and each read, attribute it to the USER when the
    prompt steering that stretch of work asked for it, else to the agent. Coarse but
    honest: it distinguishes 'I demand checks' from 'Claude happens to check'."""
    v_user = v_total = r_user = r_total = 0
    for sid, project, timeline in _iter_sessions(corpus):
        last_prompt = None
        for ev in timeline:
            if ev["kind"] == "prompt":
                last_prompt = ev["text"]
                continue
            name = ev["name"]
            if name == "bash" and VERIFY_RE.search(ev.get("cmd") or ""):
                v_total += 1
                if last_prompt and CHECK_DEMAND_RE.search(last_prompt):
                    v_user += 1
            elif name in READ_TOOLS and ev.get("file"):
                r_total += 1
                if last_prompt and READ_DEMAND_RE.search(last_prompt):
                    r_user += 1
    return {
        "verification": {"user": v_user, "total": v_total,
                         "share": round(v_user / v_total, 2) if v_total else None},
        "reading": {"user": r_user, "total": r_total,
                    "share": round(r_user / r_total, 2) if r_total else None},
    }


def _measured_agency(driver):
    """Archetype agency weights, measured per-user where the data allows: the more of
    the checking/reading the user initiates, the more those axes describe THEM. Falls
    back to the fixed constants below ~5 observed events."""
    agency = dict(AGENCY)
    v = driver["verification"]
    if v["total"] >= 5:
        agency["Verification"] = round(clamp(0.15 + 0.85 * v["share"], 0.15, 1.0), 2)
    r = driver["reading"]
    if r["total"] >= 5:
        agency["Context"] = round(clamp(0.10 + 0.90 * r["share"], 0.10, 1.0), 2)
    return agency


def _driver_word(share):
    if share is None:
        return None
    if share >= 0.5:
        return "mostly you"
    if share >= 0.2:
        return "shared"
    return "mostly Claude"


# --------------------------------------------------------------------------- #
# Insight engine — condition → observation rules that only fire when THIS person's
# data shows the pattern, each carrying its own numbers. The written profile is
# composed from the strongest fired insights, so two different people get genuinely
# different reads instead of one template with swapped numbers.
# --------------------------------------------------------------------------- #

def derive_insights(corpus, result, driver, episodes):
    ins = []  # {"key","kind":"strength"|"tension","weight",html}

    def add(key, kind, weight, html):
        ins.append({"key": key, "kind": kind, "weight": weight, "html": html})

    det = result["detail"]
    shrunk = result["shrunk"]

    # 1/2 — owned vs borrowed discipline (the you-vs-you+Claude question, answered with data)
    v = driver["verification"]
    if v["total"] >= 5 and shrunk["Verification"] >= 55:
        if v["share"] is not None and v["share"] < 0.25:
            add("borrowed_checks", "tension", 3.0 + v["total"] / 20,
                f"The checking happens — but <b>Claude starts {100 - round(v['share']*100)}% of the checks itself</b> "
                f"({v['total'] - v['user']} of {v['total']}). That's <i>borrowed discipline</i>: it works today, and it "
                f"disappears silently the day you work with a tool that doesn't volunteer it. Say the check yourself "
                f"once per session (“…and run the tests before you finish”) and the habit becomes yours.")
        elif v["share"] is not None and v["share"] >= 0.5:
            add("owned_checks", "strength", 2.5 + v["total"] / 20,
                f"<b>You ask for the checks yourself</b> — {v['user']} of {v['total']} verifications happened because "
                f"your prompt demanded one. That habit is <i>owned</i>, not borrowed: it travels with you to any tool, "
                f"any model, any teammate.")
    r = driver["reading"]
    if r["total"] >= 8 and r["share"] is not None and r["share"] >= 0.4:
        add("owned_reading", "strength", 2.0,
            f"You point before you shoot: {r['user']} of {r['total']} reads happened because you said where to look first.")

    # 3 — front-loading: how session-openers compare to follow-ups
    firsts, rests = [], []
    for sid, project, timeline in _iter_sessions(corpus):
        seen_first = False
        for ev in timeline:
            if ev["kind"] != "prompt":
                continue
            (rests, firsts)[not seen_first].append(len(ev["text"]))
            seen_first = True
    if len(firsts) >= 3 and len(rests) >= 6:
        mf, mr = statistics.median(firsts), max(statistics.median(rests), 1)
        if mf >= 2 * mr:
            add("front_loader", "strength", 2.0,
                f"You brief once, then steer terse: your session-openers run <b>{int(mf)} characters</b> against "
                f"<b>{int(mr)}</b> after. That's an efficient shape — your loops start on the days the opener is thin.")
        elif mf <= mr and shrunk["Direction"] < 55:
            add("thin_openers", "tension", 2.2,
                f"Your session-openers are as thin as your follow-ups ({int(mf)} vs {int(mr)} characters, median) — "
                f"so the agent starts every session guessing. The single cheapest upgrade you have: make the first "
                f"message of a session the complete one.")

    # 4 — what happens after a miss: do you get more specific, or terser?
    # Baseline = your normal prompts, with the corrections themselves excluded —
    # otherwise heavy correctors drag their own baseline down and the signal vanishes.
    corr = _find_corrections(corpus)
    corr_texts = {x["text"] for x in corr}
    base_pool = [p for p in corpus.real_prompts if p["text"] not in corr_texts]
    base_words = statistics.median([len(p["text"].split()) for p in base_pool]) if base_pool else 0
    if len(corr) >= 3 and base_words:
        cw = statistics.median([len(x["text"].split()) for x in corr])
        if cw >= base_words * 1.5:
            add("adaptive_corrector", "strength", 2.4,
                f"When Claude misses, <b>you get more specific, not louder</b> — your corrections run {int(cw)} words "
                f"against a {int(base_words)}-word median prompt. That's textbook recovery; it's why your misses don't spiral.")
        elif cw <= base_words * 0.7:
            loops_n = len(episodes.get("correction_loops") or [])
            loop_ref = (f" — it's where your {loops_n} correction loop(s) came from" if loops_n else "")
            add("terse_corrector", "tension", 2.6,
                f"When Claude misses, you get <b>terser</b> ({int(cw)} words vs your usual {int(base_words)}). A short "
                f"“no” makes the agent guess again{loop_ref}. Spend one sentence on the symptom and the rule, and most "
                f"loops end in one turn.")

    # 5 — same person, different discipline across projects
    by_proj = defaultdict(list)
    for p in corpus.real_prompts:
        by_proj[p["project"]].append(p["text"])
    anchored = {}
    for proj, texts in by_proj.items():
        if len(texts) >= 8:
            k = sum(1 for t in texts if ARTIFACT_RE.search(t) or (CONSTRAINT_CUE.search(t) and ACTION_VERB.search(t)))
            anchored[proj] = k / len(texts)
    if len(anchored) >= 2:
        hi = max(anchored, key=anchored.get); lo = min(anchored, key=anchored.get)
        if anchored[hi] - anchored[lo] >= 0.25:
            add("project_split", "tension", 2.3,
                f"You already know how to brief — on <b>{_esc(_project_label(hi))}</b>, {round(anchored[hi]*100)}% of your "
                f"prompts carry an anchor (a file, a limit); on <b>{_esc(_project_label(lo))}</b> that falls to "
                f"{round(anchored[lo]*100)}%. Same person, different discipline. The skill exists — spend it everywhere.")

    # 6 — hand off and it lands
    if corpus.delegation_events >= 3 and det["Iteration"].get("correction_rate", 1) <= 0.10:
        add("trusted_handoffs", "strength", 2.0,
            f"You hand off and it lands: {corpus.delegation_events} delegations (sub-agents, background runs, planning) "
            f"with only {det['Iteration']['corrections']} corrections across {det['Iteration']['n']} prompts.")

    # 7 — the most expensive pattern, in minutes
    if episodes.get("loop_minutes_total", 0) >= 10:
        n = len(episodes["correction_loops"])
        add("loop_cost", "tension", 2.8,
            f"The most expensive pattern this period: <b>{n} correction loop(s)</b> burning roughly "
            f"<b>{episodes['loop_minutes_total']} minutes</b>. Every one of them starts the same way — a result rejected "
            f"without saying what rule it broke.")

    # 8 — clean shipper
    ship = det["Shipping"]
    if ship.get("rate") == 1.0 and ship.get("ships", 0) >= 3:
        add("clean_shipper", "strength", 2.2,
            f"Every ship was gated: {ship['gated']} of {ship['ships']} commits/pushes/deploys had a check run first. "
            f"Nothing left your machine on hope.")

    ins.sort(key=lambda x: x["weight"], reverse=True)
    return ins


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


def classify_archetype(dim_scores, delegation_score, agency=None):
    """Nearest-prototype over your DRIVING-STYLE vector, with a margin guard.

    The vector adds a Delegation axis and is AGENCY-WEIGHTED: axes you control
    (Direction, Iteration, Toolcraft, Delegation) count fully, while axes the agent
    mostly drives on its own (Verification, Context) are discounted. When enough
    events exist, the Verification/Context agency weights are MEASURED from driver
    share (who actually initiated the checks and reads) instead of the constants.
    """
    agency = agency or AGENCY
    scores = dict(dim_scores)
    scores["Delegation"] = delegation_score
    V = [scores[ax] for ax in ARCHETYPE_AXES]
    names = list(PROTOTYPES.keys())
    mat = [PROTOTYPES[n]["vec"] for n in names]
    # z-score each axis across prototypes + the user vector, then apply agency weights
    cols = list(zip(*(mat + [V])))
    means = [statistics.mean(col) for col in cols]
    stds = [statistics.pstdev(col) or 1.0 for col in cols]
    w = [agency[ax] for ax in ARCHETYPE_AXES]

    def zw(vec):
        return [w[i] * (v - means[i]) / stds[i] for i, v in enumerate(vec)]

    vz = zw(V)
    sims = sorted(((round(_cosine(vz, zw(PROTOTYPES[n]["vec"])), 3), n) for n in names), reverse=True)
    top_sim, top = sims[0]
    second_sim, second = sims[1]
    blended = (top_sim - second_sim) < ARCHETYPE_MARGIN
    second_short = second.replace("The ", "")
    article = "an" if second_short[:1] in "AEIOU" else "a"

    # Fit critique: the label is a nearest match, never a perfect one — read out the
    # residuals so the report can say what the label gets right and where this person
    # breaks it, with numbers. Weighted by agency so Claude-driven axes don't dominate.
    proto = PROTOTYPES[top]["vec"]
    gaps = [(agency[ax] * abs(V[i] - proto[i]), ax, round(V[i]), proto[i])
            for i, ax in enumerate(ARCHETYPE_AXES)]
    _, fit_ax, fit_you, fit_proto = min(gaps)
    miss_gap, miss_ax, miss_you, miss_proto = max(gaps)
    fit = {
        "right": {"axis": fit_ax, "you": fit_you, "proto": fit_proto},
        "miss": {"axis": miss_ax, "you": miss_you, "proto": miss_proto,
                 "big": miss_gap >= 20},
    }
    return {
        "primary": top, "primary_sim": top_sim, "secondary": second, "secondary_sim": second_sim,
        "blended": blended, "all": sims, "delegation_score": round(delegation_score),
        "axes": {ax: round(V[i]) for i, ax in enumerate(ARCHETYPE_AXES)}, "fit": fit,
        "label": f"{PROTOTYPES[top]['emoji']} {top}" + (f", with {article} {second_short} streak" if blended else ""),
        "blurb": PROTOTYPES[top]["blurb"],
    }


# --------------------------------------------------------------------------- #
# Analysis orchestration
# --------------------------------------------------------------------------- #

def compute_competencies(raw, shrunk, conf):
    """Blend the measured signals into the four AI-fluency competencies (the 4Ds),
    per COMP_MIX. Each competency carries a raw and confidence-adjusted score, a
    blended confidence, and a 1–5 level on the framework's rubric."""
    out = {}
    for comp, mix in COMP_MIX.items():
        s = sum(w * shrunk[d] for d, w in mix.items())
        r = sum(w * raw[d] for d, w in mix.items())
        c = sum(w * conf[d] for d, w in mix.items())
        lv = max(1, min(5, int(s // 20) + 1))
        out[comp] = {"score": s, "raw": r, "conf": c, "level": lv,
                     "label": COMP_LEVEL_LABELS[lv - 1]}
    return out


def analyze(corpus):
    raw, detail, evidence = {}, {}, {}
    for name, fn in (("Direction", score_direction), ("Verification", score_verification),
                     ("Context", score_context), ("Iteration", score_iteration),
                     ("Toolcraft", score_toolcraft), ("Delegation", score_delegation),
                     ("Shipping", score_shipping)):
        s, d, ev = fn(corpus)
        raw[name], detail[name], evidence[name] = s, d, ev

    shrunk, conf = {}, {}
    for name in raw:
        shrunk[name], conf[name] = shrink(raw[name], detail[name].get("n", 0), TARGET_N[name])

    # The headline score IS the 4D framework, computed: the weighted blend of the four
    # competencies (which, being linear, equals the effective per-signal WEIGHTS blend).
    competencies = compute_competencies(raw, shrunk, conf)
    overall_raw = round(sum(COMP_WEIGHTS[c] * competencies[c]["raw"] for c in COMP_WEIGHTS))
    overall = round(sum(COMP_WEIGHTS[c] * competencies[c]["score"] for c in COMP_WEIGHTS))
    band, band_meaning = band_for(overall)
    # Who drives the borrowed-vs-owned habits: measured, then fed to the archetype so
    # its agency weights describe THIS user rather than an assumed one.
    driver = measure_driver_share(corpus)
    agency = _measured_agency(driver)
    delegation_score = shrunk["Delegation"]
    archetype = classify_archetype(shrunk, delegation_score, agency)
    archetype["agency_used"] = agency

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

    episodes = mine_episodes(corpus)
    result = {
        "raw": raw, "shrunk": shrunk, "conf": conf, "detail": detail, "evidence": evidence,
        "competencies": competencies, "episodes": episodes, "driver": driver,
        "overall_raw": overall_raw, "overall": overall, "band": band, "band_meaning": band_meaning,
        "archetype": archetype, "dist": dist, "fingerprint": _run_fingerprint(corpus),
    }
    result["insights"] = derive_insights(corpus, result, driver, episodes)
    return result


def build_action_plan(corpus, result):
    """Growth cards ranked by impact = (target - score) * weight. The teaching copy
    comes from SKILL_TEACH; user-specific evidence comes from result['evidence']."""
    TARGET = 85
    cards = []
    for name in WEIGHTS:
        score = result["shrunk"][name]
        impact = (TARGET - score) * WEIGHTS[name]
        cards.append({"dim": name, "score": round(score), "impact": impact,
                      "weak": result["evidence"].get(name, []),
                      "detail": result["detail"][name]})
    cards.sort(key=lambda c: c["impact"], reverse=True)
    # strength callout = highest shrunk score
    strength = max(WEIGHTS, key=lambda n: result["shrunk"][n])
    return cards, strength


def _shortest_action_prompt(corpus):
    cands = [p["text"] for p in corpus.real_prompts if _is_action_prompt(p["text"]) and len(p["text"]) < 40]
    return min(cands, key=len) if cands else None


# Deterministic, honest personalization: wrap the person's OWN prompt in the shape
# it was missing, with ‹fill-in› blanks for what only they know. Clearly labeled as
# auto-suggested — the Opus stage replaces these with fully written rewrites.
_AUTO_REWRITE = {
    "Direction": "{t} — it lives in ‹file›. Don't break ‹the thing to protect›. Done when ‹the command you'd run› passes.",
    "Iteration": "Instead of another “{t}”: what I saw was ‹the symptom›; the rule it broke is ‹the rule›; do ‹the fix› instead, then rerun the check.",
    "Delegation": "Whole job, not a step: ‹the end goal this serves›. Along the way, {t}. Keep going until ‹your done-check› passes, then report back.",
}


def _auto_rewrite(dim, text):
    tpl = _AUTO_REWRITE.get(dim)
    if not tpl or not text:
        return None
    t = str(text).strip().rstrip(".!")
    return tpl.format(t=t[:160])


def build_evidence(corpus, result, cards, archive_info=None):
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
            if e.get("cmd"):
                c["cmd"] = _scrub_paths(str(e["cmd"])[:120])
            if e.get("project"):
                c["project"] = _project_label(e["project"])
            if c:
                out.append(c)
        return out

    # Distinct calendar days beats (last-first).days, which reads "0" for any
    # single-day history no matter how many hours it spans.
    span_days = len(corpus.active_days)
    a = result["archetype"]
    return {
        "schema": "claude-insight-evidence/1",
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
            # Deterministic 4D competency scores (the framework, computed). The analysis
            # stage reconciles its judgment with these — it does not replace them.
            "competencies": {
                k: {"score": round(v["score"]), "raw": round(v["raw"]), "level": v["level"],
                    "label": v["label"], "confidence": round(v["conf"], 2)}
                for k, v in result["competencies"].items()
            },
            "competency_weights": COMP_WEIGHTS,
            "competency_mix": COMP_MIX,
            "dimensions_raw": {k: round(v) for k, v in result["raw"].items()},
            "dimensions_adjusted": {k: round(v) for k, v in result["shrunk"].items()},
            "confidence": {k: round(v, 2) for k, v in result["conf"].items()},
            "dimension_names": DISPLAY_NAMES,
            # Owned vs borrowed: who initiated the checks/reads. The score rates the
            # collaboration on purpose; this says whether the habit is the user's.
            "driver_share": result.get("driver"),
        },
        # Fired pattern-observations (plain text, with their numbers) — the analyst
        # should build on these; they are already grounded in this person's data.
        "insights": [
            {"kind": i["kind"], "text": re.sub(r"<[^>]+>", "", i["html"])}
            for i in (result.get("insights") or [])[:6]
        ],
        "dimension_detail": result["detail"],
        "archetype": {"primary": a["primary"], "secondary": a["secondary"],
                      "blended": a.get("blended"), "similarities": a.get("all"),
                      "axes": a.get("axes"), "fit": a.get("fit"),
                      "prototype_definitions": {k: {"vec": v["vec"], "blurb": v["blurb"]}
                                                for k, v in PROTOTYPES.items()}},
        "behavior": {
            "sample_prompts": sample,
            "weak_examples": {c["dim"]: clean_ex(c["weak"]) for c in cards},
            "tool_usage": dict(corpus.tool_usage),
            "delegation_events": corpus.delegation_events,
            # Mined moments: correction loops (their real prompts + cost), blind
            # re-edits, unverified-ship-then-fix, and their own best brief/correction.
            # The analysis stage should cite THESE for its growth cards.
            "episodes": {
                "correction_loops": [
                    {"project": _project_label(L["project"]), "turns": L["turns"],
                     "minutes": L["minutes"],
                     "prompts": [_scrub_paths(p) for p in L["prompts"]]}
                    for L in result["episodes"]["correction_loops"]
                ],
                "blind_reedits": [
                    {"project": _project_label(b["project"]), "file": b["file"],
                     "reedits": b["reedits"]}
                    for b in result["episodes"]["blind_reedits"]
                ],
                "ship_then_fix": [
                    {"project": _project_label(s["project"]),
                     "ship_cmd": _scrub_paths(s["ship_cmd"]), "fix_cmd": _scrub_paths(s["fix_cmd"])}
                    for s in result["episodes"]["ship_then_fix"]
                ],
                "best_brief": ({"text": _scrub_paths(result["episodes"]["best_brief"]["text"]),
                                "project": _project_label(result["episodes"]["best_brief"]["project"])}
                               if result["episodes"]["best_brief"] else None),
                "best_correction": ({"text": _scrub_paths(result["episodes"]["best_correction"]["text"]),
                                     "project": _project_label(result["episodes"]["best_correction"]["project"])}
                                    if result["episodes"]["best_correction"] else None),
                "loop_turns_total": result["episodes"]["loop_turns_total"],
                "loop_minutes_total": result["episodes"]["loop_minutes_total"],
            },
        },
    }


def _analysis_section_html(analysis):
    """Render the AI-authored skill map (produced by the Opus analysis stage,
    grounded in reference/ai-fluency-framework.md). Falls back to nothing if absent."""
    if not analysis or not isinstance(analysis, dict):
        return ""
    parts = ['<section><h3>Skill map — analyzed against the AI Fluency framework</h3>']
    read = analysis.get("overall_read") or analysis.get("summary")
    if read:
        parts.append(f'<p class="assess">{_esc(read)}</p>')
    # Second opinion on the computed archetype: the analyst reads the actual prompts,
    # so it can say what the label gets right, where this person breaks it, and name
    # their real pattern in plain words.
    prof = analysis.get("profile")
    if isinstance(prof, dict) and prof.get("your_real_pattern"):
        verdict = str(prof.get("archetype_verdict", "partly")).lower()
        v_txt = {"agree": "the label fits", "partly": "the label is half right",
                 "disagree": "the label misses you"}.get(verdict, "the label is half right")
        parts.append(
            '<div class="dim"><div class="dim-h"><b>Second opinion on your archetype</b>'
            f'<span class="pill">{_esc(v_txt)}</span></div>'
            + (f'<p><b>What it gets right:</b> {_esc(prof.get("gets_right", ""))}</p>' if prof.get("gets_right") else "")
            + (f'<p><b>What it misses:</b> {_esc(prof.get("misses", ""))}</p>' if prof.get("misses") else "")
            + f'<p class="next"><b>Your real pattern:</b> {_esc(prof["your_real_pattern"])}'
            + (f' — {_esc(prof.get("pattern_why", ""))}' if prof.get("pattern_why") else "") + '</p>'
            + '</div>')
    for s in analysis.get("skill_map") or []:
        if not isinstance(s, dict):
            continue
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


def _growth_cards_html(analysis):
    """The 'how to grow' cards, written FOR THIS PERSON by the Opus analysis stage:
    each item names the habit, why it matters, how to grow it, and a before/after where
    the 'before' is one of THEIR real prompts and the 'after' is Opus's tailored rewrite.
    Returns '' when there is no analysis (the caller then falls back to the generic
    teaching examples), so the report only ever shows canned examples when no AI ran."""
    if not analysis or not isinstance(analysis, dict):
        return ""
    items = [g for g in (analysis.get("top_growth") or []) if isinstance(g, dict)]
    if not items:
        return ""
    out = []
    for i, g in enumerate(items[:3]):
        title = _esc(g.get("title", "Your next growth move"))
        why = _esc(g.get("why", ""))
        how = _esc(g.get("how", ""))
        before = g.get("example_before")
        after = g.get("example_after")
        ba = ""
        if before and after:
            ba = (f'<div class="ba"><div class="before"><span>A prompt you wrote</span>'
                  f'“{_esc(str(before)[:400])}”</div>'
                  f'<div class="after"><span>Tailored rewrite for you</span>'
                  f'“{_esc(str(after)[:600])}”</div></div>')
        out.append(
            f'<div class="card prio"><div class="ph">Priority {i + 1} · written for you</div>'
            f'<h4>{title}</h4>'
            + (f'<p class="why"><b>Why it matters.</b> {why}</p>' if why else "")
            + (f'<div class="wwh"><span class="lab">How to grow it</span>'
               + (f'<p class="how">{how}</p>' if how else "") + f'{ba}</div>'
               if (how or ba) else "")
            + '</div>')
    return "".join(out)


# --------------------------------------------------------------------------- #
# Progress loop — the report remembers what it told you to work on, and the next
# run opens with the deltas ("you were working on ship-gating: 40% -> 80%"). A
# diagnosis without follow-up is a poster; this is what makes it a coach.
# State is scores-only (no prompts), lives outside any repo, and is written only
# on default runs — explicit-path runs (tests, one-offs) never touch it.
# --------------------------------------------------------------------------- #

DEFAULT_STATE_PATH = "~/.claude/insight/progress.json"
PROGRESS_KEEP_RUNS = 24

# Per-signal follow-up metric: the one rate the "this week" practice targets,
# with the plain-English label the delta line uses.
_SIGNAL_METRIC = {
    "Direction": ("constraint_rate", "prompts that set a limit"),
    "Verification": ("rate", "edit rounds that got checked"),
    "Context": ("rate", "changes grounded in a prior read"),
    "Iteration": ("specificity", "corrections that said exactly what broke"),
    "Toolcraft": ("evenness", "tool spread"),
    "Delegation": ("whole_job_rate", "hand-offs that ran a full look→change→check cycle"),
    "Shipping": ("rate", "ships gated by a check"),
}


def _progress_snapshot(result, corpus, cards):
    signals = {}
    for name, (key, _label) in _SIGNAL_METRIC.items():
        v = result["detail"].get(name, {}).get(key)
        signals[name] = round(v, 3) if isinstance(v, (int, float)) else None
    return {
        "as_of": corpus.last_ts.isoformat() if corpus.last_ts else None,
        "overall": result["overall"], "band": result["band"],
        "competencies": {k: round(v["score"]) for k, v in result["competencies"].items()},
        "signals": signals,
        "top_moves": [c["dim"] for c in cards[:2]],
        "prompts": len(corpus.real_prompts),
        "fingerprint": result.get("fingerprint"),
    }


def load_progress(path):
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and isinstance(d.get("runs"), list) else None
    except (OSError, json.JSONDecodeError):
        return None


def save_progress(path, prev, snap):
    """Append this run's snapshot (skipping exact re-runs of the same data) and keep
    the last PROGRESS_KEEP_RUNS. Atomic write; failures never break the report."""
    runs = list((prev or {}).get("runs") or [])
    if runs and runs[-1].get("fingerprint") == snap.get("fingerprint"):
        return
    runs.append(snap)
    runs = runs[-PROGRESS_KEEP_RUNS:]
    p = os.path.expanduser(path)
    tmp = p + ".tmp"
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"schema": "claude-insight-progress/1", "runs": runs}, f, indent=1)
        os.replace(tmp, p)
    except OSError:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def build_progress_html(prev, result, corpus, cards):
    """The 'Since your last report' section: overall + competency deltas and, most
    importantly, follow-up on the exact habits the previous report told this person
    to practice. Empty when there is no prior run or no new data."""
    runs = (prev or {}).get("runs") or []
    if not runs:
        return ""
    last = runs[-1]
    if last.get("fingerprint") == result.get("fingerprint"):
        return ""    # same data as last time — nothing to compare
    cur = _progress_snapshot(result, corpus, cards)

    def arrow(delta):
        if delta > 0:
            return f'<b style="color:var(--good)">▲ +{delta}</b>'
        if delta < 0:
            return f'<b style="color:var(--bad)">▼ {delta}</b>'
        return '<b>· ±0</b>'

    when = _esc(str(last.get("as_of") or "")[:10]) or "your last run"
    d_overall = cur["overall"] - (last.get("overall") or 0)
    comp_bits = []
    for name in ("Delegation", "Description", "Discernment", "Diligence"):
        o, n = (last.get("competencies") or {}).get(name), cur["competencies"].get(name)
        if o is not None and n is not None and n != o:
            comp_bits.append(f"{name} {o}→{n}")
    comp_line = (" · ".join(comp_bits)) if comp_bits else "competency levels unchanged"

    # Follow-up on what the last report said to practice.
    target_lines = ""
    for dim in (last.get("top_moves") or [])[:2]:
        key_label = _SIGNAL_METRIC.get(dim)
        if not key_label:
            continue
        old = (last.get("signals") or {}).get(dim)
        new = cur["signals"].get(dim)
        if old is None or new is None:
            continue
        verdict = ("✓ that's the habit forming" if new > old + 0.02
                   else ("— slipping, worth one deliberate rep this week" if new < old - 0.02
                         else "— no change yet; one deliberate rep per session moves this"))
        target_lines += (f'<li>You were working on <b>{_esc(disp(dim))}</b>: '
                         f'{key_label[1]} went <b>{old*100:.0f}% → {new*100:.0f}%</b> {verdict}</li>')
    targets_html = (f'<ul class="facts" style="margin-top:8px">{target_lines}</ul>'
                    if target_lines else "")

    return (f'<section><h3>Since your last report ({when})</h3>'
            f'<div class="card keep"><p><b>Overall: {last.get("overall", "?")} → {cur["overall"]}</b> '
            f'{arrow(d_overall)} <span class="loc">({cur["prompts"] - (last.get("prompts") or 0):+d} new prompts '
            f'analyzed)</span></p>'
            f'<p class="receipt">{_esc(comp_line)}</p>'
            f'{targets_html}</div></section>')


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


def terminal_summary(corpus, result):
    a = result["archetype"]
    comp = " · ".join(f"{k} L{v['level']}" for k, v in result["competencies"].items())
    lines = [
        "",
        f"  AI Fluency Score: {result['overall']}/100  ({result['band']})",
        f"  Archetype: {a['label']}",
        f"  Competencies: {comp}",
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
    "Director": "You already delegate and verify like a lead — the next gain is briefing depth: one sentence of intent plus a 'done when…' per hand-off, and your acceptance passes get shorter.",
}

_SIG_DESC = {
    "Delegation": "how much you hand off — you give Claude whole jobs and trust it to run them end-to-end",
    "Toolcraft": "the range of tools you bring to bear — you reach past the shell for the right instrument",
    "Iteration": "how cleanly you change course — your corrections tend to name the fix, not just reject",
    "Direction": "how concretely you frame requests when it matters",
}

# The specific, evidence-grounded line that explains each dimension as a growth edge.
_GROWTH_LINE = {
    "Direction": "{s}s win on how sharply they frame the work they hand off — and right now yours are often one-liners like “{ex}”, so Claude fills gaps you could have decided.",
    "Verification": "Right now changes often move on without a test, build or run to confirm them — the cheapest reliability you can buy back.",
    "Context": "Right now some edits land before the file has been read that session — an easy blind-edit risk to remove.",
    "Iteration": "Right now corrections lean toward brief rejections; naming the symptom and the exact rule resolves loops in fewer turns.",
    "Toolcraft": "Right now most work funnels through one tool — reaching for search, planning and delegation widens what you can take on.",
    "Delegation": "Right now tasks are often steered step by step — bundling them into whole, outcome-shaped hand-offs buys back the round-trips.",
    "Shipping": "Right now some commits/pushes follow edits with no check in between — gate each ship behind one verification.",
}


def build_assessment(corpus, result, cards):
    """The written profile. Composed from the insight engine — observations that only
    fire when THIS person's data shows the pattern, each carrying its own numbers — so
    two people get genuinely different reads. Falls back to a sturdy generic paragraph
    only where no insight fired."""
    a = result["archetype"]
    arch = a["primary"]
    short = arch.replace("The ", "")
    art = "an" if short[:1] in "AEIOU" else "a"
    deleg = a["delegation_score"]
    median = result["dist"].get("median_chars", "?")
    ins = result.get("insights") or []
    strengths = [i for i in ins if i["kind"] == "strength"][:2]
    tensions = [i for i in ins if i["kind"] == "tension"][:2]

    # -- who you are: identity + the shape of your work -------------------------
    n_sessions = max(len(corpus.sessions), 1)
    per_session = round(len(corpus.real_prompts) / n_sessions, 1)
    p1 = (f"You drive Claude like <b>{_esc(a['label'])}</b>. {_esc(a['blurb'])} "
          f"The shape of your work: {len(corpus.real_prompts)} prompts over {n_sessions} session(s) "
          f"(≈{per_session} per session), median prompt {median} characters, "
          f"<b>{deleg}/100</b> on handing off whole jobs. Together that lands the collaboration at "
          f"<b>{result['overall']}/100 ({_esc(result['band'])})</b>.")

    # -- what's distinctly yours -------------------------------------------------
    if strengths:
        p2 = "<b>What's distinctly yours.</b> " + " ".join(s["html"] for s in strengths)
    else:
        user_signals = {
            "Direction": result["shrunk"]["Direction"], "Iteration": result["shrunk"]["Iteration"],
            "Toolcraft": result["shrunk"]["Toolcraft"], "Delegation": float(deleg),
        }
        sig = max(user_signals, key=user_signals.get)
        p2 = (f"<b>What's distinctly yours.</b> Your strongest self-driven habit is "
              f"{_esc(_SIG_DESC.get(sig, sig.lower()))}.")

    # -- the tension worth resolving ----------------------------------------------
    growth = cards[0]["dim"]
    if tensions:
        p3 = ("<b>The tension worth resolving.</b> " + " ".join(t["html"] for t in tensions)
              + " The move cards below start exactly there.")
    else:
        example = _shortest_action_prompt(corpus) or "run it"
        gline = _GROWTH_LINE.get(growth, "").format(s=_esc(short), ex=_esc(example))
        path_why = ARCH_PATHS.get(arch, "Keep building the habits below and your next run will show the gain.")
        p3 = (f"<b>The tension worth resolving.</b> Your lowest signal is <b>{_esc(disp(growth))}</b> — for "
              f"{art} {_esc(short)} that isn't a contradiction, it's the defining growth edge. {gline} "
              f"{_esc(path_why)}")

    return (f'<p class="assess">{p1}</p><p class="assess">{p2}</p><p class="assess">{p3}</p>')


def build_html(corpus, result, cards, strength, archive_info=None, analysis=None, analysis_note=None,
               progress_html=""):
    a = result["archetype"]
    d = result["dist"]
    eps = result.get("episodes") or {}
    analysis_section = _analysis_section_html(analysis)
    # When an AI analysis was expected but couldn't be used (no-op'd, empty, or from a
    # different run), say so plainly instead of letting the template-only report pass as
    # the full thing. Silent on a plain deterministic run (no --analysis was attempted).
    analysis_status_html = ""
    if not analysis_section and analysis_note:
        analysis_status_html = (
            '<section><div class="prov">ℹ️ <b>Deterministic report only.</b> '
            f'{_esc(analysis_note)} — the Sonnet&nbsp;+&nbsp;Opus skill map was not added on top. '
            'Every score below is still fully computed from your data; to add the AI-written '
            'skill map, re-run <code>/ai-fluency</code> inside Claude Code.'
            '</div></section>')
    # Distinct calendar days with activity — never "(last-first).days", which shows
    # "0 days" for a 20-hour single-day history and reads like a bug (it was one).
    days = len(corpus.active_days)
    active_h = corpus.active_seconds / 3600
    filtered_total = sum(corpus.filtered.values())
    provisional = len(corpus.real_prompts) < PROVISIONAL_MIN_PROMPTS

    DIM_BLURB = {
        "Direction": "Do your requests say what you want, where it lives, and how you'll know it worked?",
        "Verification": "Does the work get proven — tests, build, a real run — before moving on?",
        "Context": "Does a file get read before it gets changed?",
        "Iteration": "When something's wrong, does your correction say what broke and what to do instead?",
        "Toolcraft": "Is the right tool used for each step — search, run, background — not just chat?",
        "Delegation": "Do you hand over whole jobs, or steer one small step at a time?",
        "Shipping": "Does anything get checked between the last edit and the commit, push, or deploy?",
    }

    def dim_rate_line(name):
        det = result["detail"][name]
        if name == "Verification" and det.get("rate") is not None:
            return f"{det['verified']} of {det['episodes']} rounds of edits were checked ({det['rate']*100:.0f}%)"
        if name == "Context" and det.get("rate") is not None:
            return f"{det['grounded']} of {det['total_edits']} changes landed on a file that was read first ({det['rate']*100:.0f}%)"
        if name == "Direction":
            return (f"{det['constraint_rate']*100:.0f}% of prompts set a limit · "
                    f"{det['artifact_rate']*100:.0f}% point at a file or error · "
                    f"{det['intent_rate']*100:.0f}% say why · "
                    f"{det.get('shape_rate', 0)*100:.0f}% shape the steps or output")
        if name == "Iteration":
            return (f"{det['corrections']} corrections ({det['correction_rate']*100:.0f}% of prompts); "
                    f"{det['specificity']*100:.0f}% said exactly what was wrong")
        if name == "Toolcraft":
            return (f"{det.get('distinct', 0)} different tools · spread {det.get('evenness', 0.0):.2f} "
                    f"(1 = perfectly even) · {det.get('delegation_events', 0)} hand-offs")
        if name == "Delegation":
            wj = det.get("whole_job_rate")
            wj_txt = f" · {wj*100:.0f}% of hand-offs ran a full look→change→check cycle" if wj is not None else ""
            return (f"{det.get('delegation_events', 0)} hand-offs ({det.get('events_per_hour', 0.0)}/active hour) · "
                    f"a handed-off task runs {det.get('median_run', 0)} agent actions before you steer again{wj_txt}")
        if name == "Shipping":
            if det.get("rate") is None:
                return "nothing shipped in these sessions — nothing to grade, so this stays neutral"
            return f"{det['gated']} of {det['ships']} commits/pushes/deploys had a check run first ({det['rate']*100:.0f}%)"
        return ""

    # ---- signal rows (bars, direct-labeled) --------------------------------
    dim_html = ""
    order = sorted(WEIGHTS, key=lambda n: result["shrunk"][n], reverse=True)
    for name in order:
        sc = round(result["shrunk"][name])
        c = result["conf"][name]
        tag = ""
        if name == strength:
            tag = '<span class="tag s">strongest</span>'
        elif name == cards[0]["dim"]:
            tag = '<span class="tag w">biggest gain</span>'
        ld = '<span class="tag ld">low data</span>' if c < 0.75 else ""
        rate = dim_rate_line(name)
        dim_html += f"""
      <div class="sig" title="{_esc(disp(name))}: {sc}/100 — {_esc(rate)}">
        <div class="sig-top"><span class="sig-name">{_esc(disp(name))} {tag}{ld}</span><span class="sig-val">{sc}</span></div>
        <div class="bar"><i style="width:{sc}%"></i></div>
        <p class="sig-q">{_esc(DIM_BLURB[name])}</p>
        <p class="sig-rate">{_esc(rate)}<span class="wt"> · counts for {WEIGHTS[name]*100:.0f}% of the score</span></p>
      </div>"""

    # ---- the four competencies (skill map) ---------------------------------
    skill_levels = _skill_levels(result)
    skill_html = ""
    for sk in skill_levels:
        dots = "".join(
            f'<span class="dot {"on" if i < sk["level"] else ""}"></span>' for i in range(5)
        )
        lowdata = '<span class="tag ld">low data</span>' if sk["conf"] < 0.75 else ""
        skill_html += f"""
      <div class="comp" title="{_esc(sk['name'])}: {sk['score']}/100 — Level {sk['level']} of 5">
        <div class="comp-top">
          <span class="comp-name">{_esc(sk['name'])} {lowdata}</span>
          <span class="comp-lvl">Level {sk['level']} of 5 · {_esc(sk['label'])}</span>
        </div>
        <div class="comp-mid"><span class="sk-dots">{dots}</span>
          <div class="bar comp-bar"><i style="width:{sk['score']}%"></i></div>
          <span class="sig-val">{sk['score']}</span></div>
        <p class="comp-what">{_esc(sk['what'])}</p>
        <p class="comp-now"><b>You're here:</b> {_esc(sk['now'])}</p>
        {f'<p class="comp-driver">{sk["driver"]}</p>' if sk.get('driver') else ''}
        <p class="comp-next"><b>Next:</b> {_esc(sk['next'])}</p>
      </div>"""

    # ---- archetype affinity -------------------------------------------------
    aff = ""
    for sim, nm in a["all"]:
        pct = max(0, round((sim + 1) / 2 * 100))
        me = ' class="me"' if nm == a["primary"] else ""
        aff += f"""<div class="bar-item"{me}><div class="bl">{PROTOTYPES[nm]['emoji']} {_esc(nm)}</div>
          <div class="bt" title="{_esc(nm)}: similarity {sim:+.2f}"><i style="width:{pct}%"></i></div><div class="bv">{sim:+.2f}</div></div>"""

    # ---- data composition: what we read vs what actually counts -------------
    # A binary story on purpose: blue = the prompts that count, one neutral gray =
    # everything excluded. The per-category breakdown is carried by the legend text
    # (identity by label, never by shades of gray).
    noise = corpus.filtered.most_common()
    segs = (f'<i class="seg real" style="flex:{max(len(corpus.real_prompts), 1)}" '
            f'title="Prompts you typed: {len(corpus.real_prompts)}"></i>')
    if filtered_total:
        segs += (f'<i class="seg noise" style="flex:{filtered_total}" '
                 f'title="Excluded machinery: {filtered_total:,}"></i>')
    legend = f'<li><i class="sw real"></i><b>{len(corpus.real_prompts)}</b> prompts you actually typed</li>'
    if filtered_total:
        breakdown = " · ".join(f"{v:,} {_esc(k)}" for k, v in noise[:5])
        legend += (f'<li><i class="sw noise"></i><b>{filtered_total:,}</b> excluded, not yours '
                   f'<span class="loc">({breakdown})</span></li>')

    # Archive stat tile + the "why ~30 days / how to see more" callout.
    archive_tile = retention_note = ""
    arch_dir_disp = _esc(archive_info["dir"]) if archive_info else _esc(DEFAULT_ARCHIVE_DIR)
    if archive_info:
        archive_tile = (f'<div class="ing"><div class="n">{archive_info["archived_sessions"]:,}</div>'
                        f'<div class="l">sessions in your archive</div></div>')
    if days <= 32:
        grew = ""
        if archive_info and archive_info.get("enabled"):
            grew = (f' This run preserved <b>{archive_info["new"]:,}</b> new session(s) to your '
                    f'archive (<code>{arch_dir_disp}</code>), so from here your history keeps growing '
                    f'past the 30-day wall. Keep this archive private to you — sharing one folder '
                    f'between people would mix everyone\'s transcripts into a single report.')
        retention_note = (
            '<div class="honesty" style="margin-top:14px">'
            f'<b>Why only {days} day{"s" if days != 1 else ""} of history?</b> Claude Code deletes transcripts older than your '
            '<code>cleanupPeriodDays</code> setting (default <b>30</b>), so that is all that was '
            'left on disk to read — not a limit of this tool. To analyze more history: '
            '<b>(1)</b> raise <code>cleanupPeriodDays</code> in <code>~/.claude/settings.json</code> '
            '(e.g. <code>"cleanupPeriodDays": 365</code>); '
            f'<b>(2)</b> keep running Claude Insight.{grew}'
            '</div>')

    # ---- "what we saw" evidence + cost, per move card ------------------------
    proj_counts = Counter(p["project"] for p in corpus.real_prompts)

    def evidence_html(card):
        name = card["dim"]
        ev = card["weak"]
        items = ""
        for e in (ev or [])[:3]:
            if name in ("Direction", "Iteration", "Delegation"):
                proj = e["project"]; txt = _scrub_paths(e["text"])
                small = " <em>(small sample)</em>" if proj_counts.get(proj, 0) < 10 else ""
                items += f'<li>You typed “{_esc(txt[:140])}” <span class="loc">— {_esc(_project_label(proj))}{small}</span></li>'
            elif name == "Shipping":
                small = " <em>(small sample)</em>" if proj_counts.get(e["project"], 0) < 10 else ""
                items += f'<li>Shipped with <code>{_esc(_scrub_paths(e["cmd"]))}</code> right after edits, nothing run in between <span class="loc">— {_esc(_project_label(e["project"]))}{small}</span></li>'
            elif name == "Context":
                small = " <em>(small sample)</em>" if proj_counts.get(e["project"], 0) < 10 else ""
                items += f'<li><code>{_esc(os.path.basename(e["file"]))}</code> was changed without being read first <span class="loc">— {_esc(_project_label(e["project"]))}{small}</span></li>'
            elif name == "Verification":
                small = " <em>(small sample)</em>" if proj_counts.get(e["project"], 0) < 10 else ""
                items += f'<li>A round of edits to <code>{_esc(e["files"])}</code> ended with nothing run afterwards <span class="loc">— {_esc(_project_label(e["project"]))}{small}</span></li>'
        # episode receipts, matched to the habit they evidence
        if name in ("Iteration", "Direction"):
            for L in (eps.get("correction_loops") or [])[:1]:
                q = "” → “".join(_esc(_scrub_paths(p)[:80]) for p in L["prompts"][:2])
                items += (f'<li>A correction loop: “{q}” — {L["turns"]} tries in a row before it landed '
                          f'(~{L["minutes"]} min) <span class="loc">— {_esc(_project_label(L["project"]))}</span></li>')
        if name == "Context":
            for b in (eps.get("blind_reedits") or [])[:1]:
                items += (f'<li><code>{_esc(b["file"])}</code> was changed blind, then had to be re-edited '
                          f'{b["reedits"]} more time(s) — the first change didn\'t land clean '
                          f'<span class="loc">— {_esc(_project_label(b["project"]))}</span></li>')
        if name == "Shipping":
            for s2 in (eps.get("ship_then_fix") or [])[:1]:
                items += (f'<li>An unchecked ship (<code>{_esc(_scrub_paths(s2["ship_cmd"]))}</code>) was followed by a fix commit '
                          f'(<code>{_esc(_scrub_paths(s2["fix_cmd"]))}</code>) — the skipped check got paid for later '
                          f'<span class="loc">— {_esc(_project_label(s2["project"]))}</span></li>')
        if not items:
            return '<p class="ev-none">No clear misses in your transcripts — this is already a habit. ✓</p>'
        return f"<ul class='ev'>{items}</ul>"

    def cost_line(name):
        if name in ("Iteration", "Direction"):
            loops = eps.get("correction_loops") or []
            if loops:
                t = eps.get("loop_turns_total", 0); m = eps.get("loop_minutes_total", 0)
                return (f'<p class="cost">What it cost you: about <b>{t} extra turns</b> '
                        f'(~<b>{m} min</b>) went into correction loops in this period.</p>')
        if name == "Context":
            br = eps.get("blind_reedits") or []
            if br:
                n = sum(b["reedits"] for b in br)
                return (f'<p class="cost">What it cost you: <b>{n} repeat edit(s)</b> to files that were '
                        f'changed without being read first.</p>')
        if name == "Shipping":
            sf = eps.get("ship_then_fix") or []
            if sf:
                return (f'<p class="cost">What it cost you: <b>{len(sf)} fix-up commit(s)</b> after '
                        f'shipping without a check.</p>')
        return ""

    def say_html(card):
        """The 'what to type instead' block. Personalized when we have one of THEIR
        prompts to rewrite (auto-suggested shape, honestly labeled); the generic
        teaching pair only appears when there is nothing of theirs to build on."""
        name = card["dim"]
        t = SKILL_TEACH[name]
        their = next((e for e in (card["weak"] or []) if isinstance(e, dict) and e.get("text")), None)
        auto = _auto_rewrite(name, their["text"]) if their else None
        if auto:
            return (f'<p class="exgen">Your own prompt, reshaped — fill the ‹blanks›. '
                    f'Auto-suggested from a rule; run <code>/ai-fluency</code> and Opus writes it fully for you.</p>'
                    f'<div class="ba"><div class="before"><span>You typed</span>“{_esc(_scrub_paths(their["text"])[:200])}”</div>'
                    f'<div class="after"><span>Say it like this</span>“{_esc(auto)}”</div></div>')
        ex_html = "".join(
            f'<div class="ba"><div class="before"><span>Instead of</span>“{_esc(e["before"])}”</div>'
            f'<div class="after"><span>Stronger</span>“{_esc(e["after"])}”</div></div>'
            for e in t["examples"][:1]
        )
        return (f'<p class="exgen">A generic illustration of the habit — <b>not</b> from your sessions:</p>{ex_html}')

    cards_html = ""
    for i, card in enumerate(cards[:2]):
        name = card["dim"]
        t = SKILL_TEACH[name]
        cards_html += f"""
      <div class="card prio">
        <div class="ph">Move {i+1} · {_esc(disp(name))} <span class="pscore">now {card['score']}/100</span></div>
        <h4>{_esc(t['what_it_is'])}</h4>
        <p class="why"><b>Why it matters.</b> {_esc(t['why_it_matters'])}</p>
        <div class="wwh"><span class="lab">What we saw in your sessions</span>{evidence_html(card)}{cost_line(name)}</div>
        <div class="wwh"><span class="lab">What to type instead</span><p class="how">{_esc(t['how_to_improve'])}</p>
          {say_html(card)}
        </div>
        <p class="tgt">🎯 This week: {_esc(t['practice'])}</p>
      </div>"""

    # strength callout — receipts included (their own best moment when we have one)
    s_det = dim_rate_line(strength)
    strong_score = round(result["shrunk"][strength])
    receipt = ""
    if strength == "Direction" and eps.get("best_brief"):
        bb = eps["best_brief"]
        receipt = (f'<p class="receipt">Your best brief, for the record: “{_esc(_scrub_paths(bb["text"])[:220])}” '
                   f'<span class="loc">— {_esc(_project_label(bb["project"]))}</span></p>')
    elif strength == "Iteration" and eps.get("best_correction"):
        bc = eps["best_correction"]
        receipt = (f'<p class="receipt">Your sharpest correction: “{_esc(_scrub_paths(bc["text"])[:220])}” '
                   f'<span class="loc">— {_esc(_project_label(bc["project"]))}</span></p>')
    if strong_score >= 55:
        strength_head = "Keep doing this"
        strength_body = (f"{_esc(SKILL_TEACH[strength]['good_looks_like'])} The proof in your "
                         f"sessions: {_esc(s_det)}.")
    else:
        strength_head = "Your strongest area (for now)"
        strength_body = (f"Even your strongest habit has room ({strong_score}/100), but it's the most "
                         f"natural place to build from. The proof in your sessions: {_esc(s_det)}.")
    strength_html = f"""
      <div class="card keep">
        <div class="ph">{strength_head} · {_esc(disp(strength))} <span class="pscore">{strong_score}/100</span></div>
        <p>{strength_body}</p>
        {receipt}
      </div>"""

    # No label fits anyone perfectly — read the residuals out loud: the axis where
    # this person matches their archetype best, and the axis where they break it.
    arch_fit_html = ""
    fit = a.get("fit")
    if fit:
        r, m = fit["right"], fit["miss"]
        hold = (" So hold the label loosely — the written read below resolves the tension."
                if m.get("big") else "")
        arch_fit_html = (
            f'<p class="fine"><b>Where this label fits you:</b> {_esc(disp(r["axis"]))} — you measure '
            f'{r["you"]}/100 and the {_esc(a["primary"])} pattern sits at {r["proto"]}. '
            f'<b>Where you break it:</b> {_esc(disp(m["axis"]))} — the pattern expects {m["proto"]}, '
            f'you measure {m["you"]}.{hold}</p>')

    prov_banner = ""
    if provisional:
        prov_banner = (f'<div class="prov">⚠️ Provisional: only {len(corpus.real_prompts)} real prompts found — '
                       f'treat the score as a rough range (±10). It sharpens as you use Claude Code more.</div>')
    arch_hedge = ""
    if provisional:
        arch_hedge = ('<p class="hedge">Provisional — based on only '
                      f'{len(corpus.real_prompts)} prompt(s); the archetype can shift as more history accumulates.</p>')

    assessment_html = build_assessment(corpus, result, cards)

    # "What to improve": prefer the Opus analysis's tailored growth cards (grounded in this
    # person's real prompts). Fall back to the deterministic episode/auto-rewrite cards.
    growth_cards = _growth_cards_html(analysis)
    if growth_cards:
        improve_cards = growth_cards
        improve_intro = ('<p class="exgen" style="margin-bottom:14px">Written for you by Claude '
                         'Opus&nbsp;4.8 from your real prompts — your highest-leverage moves, each '
                         'with one of your prompts rewritten.</p>')
    else:
        improve_cards = cards_html
        improve_intro = ""

    ring_len = 2 * math.pi * 74 * result['overall'] / 100

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your AI Fluency Report</title>
<style>
:root{{
  --page:#f9f9f7; --card:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --mut:#898781;
  --grid:#e1e0d9; --base:#c3c2b7; --border:rgba(11,11,11,.10);
  --accent:#2a78d6; --good:#006300; --good-bg:rgba(12,163,12,.10);
  --bad:#d03b3b; --bad-bg:rgba(208,59,59,.08);
  --warn-bg:rgba(236,131,90,.12); --warn-bd:rgba(236,131,90,.55);
  --noise:#898781;
}}
@media (prefers-color-scheme: dark){{
  :root{{
    --page:#0d0d0d; --card:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7; --mut:#898781;
    --grid:#2c2c2a; --base:#383835; --border:rgba(255,255,255,.10);
    --accent:#3987e5; --good:#0ca30c; --good-bg:rgba(12,163,12,.14);
    --bad:#e66767; --bad-bg:rgba(230,103,103,.12);
    --warn-bg:rgba(236,131,90,.14); --warn-bd:rgba(236,131,90,.45);
    --noise:#898781;
  }}
}}
*{{box-sizing:border-box;margin:0;padding:0}}
html{{-webkit-text-size-adjust:100%}}
body{{background:var(--page);color:var(--ink);
font:16.5px/1.65 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;padding-bottom:80px}}
.wrap{{max-width:720px;margin:0 auto;padding:0 20px}}
header{{text-align:center;padding:52px 0 8px}}
.kick{{letter-spacing:.18em;text-transform:uppercase;font-size:12px;color:var(--mut)}}
h1{{font-size:30px;line-height:1.25;margin:10px 0 6px;letter-spacing:-.01em}}
.sub{{color:var(--ink2);max-width:560px;margin:4px auto 0;font-size:15.5px}}
.hero{{margin:28px auto 0;display:flex;gap:14px;align-items:stretch;flex-wrap:wrap;justify-content:center}}
.score-card{{background:var(--card);border:1px solid var(--border);border-radius:18px;
padding:24px 28px;text-align:center;min-width:230px}}
.ring{{position:relative;width:168px;height:168px;margin:0 auto}}
.ring .n{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}}
.ring .n b{{font-size:52px;line-height:1;letter-spacing:-.02em}}
.ring .n s{{text-decoration:none;color:var(--mut);font-size:13px;margin-top:2px}}
.band{{margin-top:12px;font-size:19px;font-weight:700}}
.rawnote{{color:var(--mut);font-size:12.5px;margin-top:4px}}
.arch{{flex:1;min-width:250px;background:var(--card);border:1px solid var(--border);border-radius:18px;padding:22px 24px;text-align:left}}
.arch .emoji{{font-size:36px}}
.arch h2{{font-size:22px;margin:6px 0 4px}}
.arch p{{color:var(--ink2);font-size:14.5px}}
.arch .fine{{margin-top:10px;font-size:12.5px;color:var(--mut)}}
.hedge{{margin-top:8px;font-size:12.5px;color:var(--bad)}}
.prov{{background:var(--warn-bg);border:1px solid var(--warn-bd);border-radius:12px;padding:12px 16px;margin:20px 0 0;font-size:14px}}
section{{margin:40px 0}}
h3{{font-size:12.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--mut);border-bottom:1px solid var(--grid);padding-bottom:9px;margin-bottom:16px;font-weight:600}}
.band-meaning{{background:var(--card);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:12px;padding:14px 18px;color:var(--ink2);margin-top:12px;font-size:15px}}
.band-meaning b{{color:var(--ink)}}
.assess{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:15px 18px;margin-bottom:10px;font-size:15.5px;line-height:1.7;color:var(--ink2)}}
.assess b,.assess i{{color:var(--ink)}}
.ingest{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
.ing{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:13px 15px}}
.ing .n{{font-size:23px;font-weight:700;font-variant-numeric:tabular-nums}}
.ing .l{{color:var(--mut);font-size:13px;margin-top:2px}}
.honesty{{margin-top:14px;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:15px 18px;font-size:14.5px;color:var(--ink2)}}
.honesty b{{color:var(--ink)}}
.stack{{display:flex;gap:2px;height:18px;border-radius:6px;overflow:hidden;margin:12px 0 8px}}
.seg{{display:block;min-width:3px}}
.seg.real{{background:var(--accent)}} .seg.noise{{background:var(--noise)}}
.stack-legend{{list-style:none;display:flex;flex-wrap:wrap;gap:6px 18px;font-size:13.5px;color:var(--ink2)}}
.stack-legend b{{color:var(--ink);font-variant-numeric:tabular-nums}}
.sw{{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:baseline}}
.sw.real{{background:var(--accent)}} .sw.noise{{background:var(--noise)}}
.comp,.sig,.dim{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:15px 18px;margin-bottom:10px}}
.comp:hover,.sig:hover{{border-color:var(--base)}}
.comp-top{{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap}}
.comp-name{{font-weight:700;font-size:17.5px}}
.comp-lvl{{font-size:13px;color:var(--ink2);white-space:nowrap}}
.comp-mid{{display:flex;align-items:center;gap:12px;margin:10px 0 8px}}
.comp-bar{{flex:1;margin:0}}
.comp-what{{color:var(--ink2);font-size:14.5px}}
.comp-now{{font-size:14px;color:var(--ink2);margin-top:6px}} .comp-now b{{color:var(--ink)}}
.comp-driver{{font-size:13.5px;color:var(--mut);margin-top:5px;border-left:2px solid var(--grid);padding-left:9px}} .comp-driver b{{color:var(--ink2)}}
.comp-next{{font-size:14px;margin-top:3px}} .comp-next b{{color:var(--good)}}
.sk-dots{{white-space:nowrap}}
.dot{{display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--grid);margin-right:3px}}
.dot.on{{background:var(--accent)}}
.sig-top{{display:flex;justify-content:space-between;align-items:baseline}}
.sig-name{{font-weight:650;font-size:16px}}
.sig-val{{font-size:19px;font-weight:750;font-variant-numeric:tabular-nums}}
.bar{{height:10px;background:var(--grid);border-radius:6px;overflow:hidden;margin:9px 0 8px}}
.bar>i{{display:block;height:100%;border-radius:0 4px 4px 0;background:var(--accent)}}
.sig-q{{color:var(--ink2);font-size:14px}}
.sig-rate{{color:var(--mut);font-size:13px;margin-top:3px;font-variant-numeric:tabular-nums}} .wt{{opacity:.85}}
.tag{{font-size:10.5px;padding:2px 8px;border-radius:99px;font-weight:700;margin-left:6px;vertical-align:middle}}
.tag.s{{background:var(--good-bg);color:var(--good)}} .tag.w{{background:var(--bad-bg);color:var(--bad)}}
.tag.ld{{background:var(--grid);color:var(--mut)}}
.bar-item{{display:flex;align-items:center;gap:12px;margin:7px 0}}
.bl{{min-width:170px;font-size:14px;color:var(--ink2)}}
.bar-item[class] .bl{{color:var(--ink2)}}
.bt{{flex:1;height:8px;background:var(--grid);border-radius:6px;overflow:hidden}}
.bt>i{{display:block;height:100%;background:var(--base);border-radius:0 4px 4px 0}}
.bar-item.me .bl{{color:var(--ink);font-weight:650}}
.bar-item.me .bt>i{{background:var(--accent)}}
.bv{{min-width:48px;text-align:right;color:var(--mut);font-size:13px;font-variant-numeric:tabular-nums}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:17px 20px;margin-bottom:12px}}
.prio{{border-left:3px solid var(--bad)}} .keep{{border-left:3px solid var(--good)}}
.ph{{font-size:12px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);font-weight:650}}
.pscore{{float:right;color:var(--ink2);letter-spacing:0;font-variant-numeric:tabular-nums}}
.card h4{{font-size:17.5px;margin:8px 0 10px;line-height:1.45}}
.wwh{{margin:12px 0}} .wwh .lab{{display:block;font-size:11.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin-bottom:6px;font-weight:650}}
ul.ev{{list-style:none;margin:8px 0 0;padding:0}}
ul.ev li{{background:var(--page);border:1px solid var(--border);border-radius:9px;padding:9px 12px;margin-bottom:7px;font-size:14px;color:var(--ink2)}}
.ev li code,.honesty code{{color:var(--ink)}}
.loc{{color:var(--mut);font-size:12.5px}} .ev-none{{color:var(--good);font-size:14px}}
.cost{{margin-top:8px;font-size:14px;color:var(--bad)}} .cost b{{font-variant-numeric:tabular-nums}}
.receipt{{margin-top:8px;font-size:14px;color:var(--ink2)}}
.ba{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px}}
.why{{color:var(--ink2);font-size:14px;margin:2px 0 4px}} .why b{{color:var(--ink)}}
.how{{font-size:14.5px;margin:0 0 4px}}
.exgen{{font-size:12.5px;color:var(--mut);margin:8px 0 2px;font-style:italic}}
.before,.after{{border-radius:10px;padding:10px 13px;font-size:14px;border:1px solid var(--border)}}
.before{{background:var(--bad-bg);color:var(--ink2)}} .after{{background:var(--good-bg);color:var(--ink)}}
.before span,.after span{{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.08em;opacity:.75;margin-bottom:3px;font-weight:650}}
.tgt{{margin-top:10px;color:var(--good);font-size:14px;font-weight:600}}
.dim-h{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:6px}}
.dim-h b{{font-size:17px}}
.dim p{{color:var(--ink2);font-size:14.5px}}
.pill{{font-size:12px;font-weight:700;color:var(--ink);background:var(--page);border:1px solid var(--border);border-radius:99px;padding:3px 11px;white-space:nowrap}}
.next{{margin-top:8px;font-size:14.5px}} .next b{{color:var(--ink)}}
.facts{{list-style:none}} .facts li{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 14px;margin-bottom:8px;font-size:14.5px;color:var(--ink2)}}
details{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 18px;margin-top:12px}}
summary{{cursor:pointer;color:var(--ink2);font-size:14.5px;font-weight:600}}
details p,details li{{color:var(--ink2);font-size:13.5px;margin-top:8px}}
details b{{color:var(--ink)}}
footer{{text-align:center;color:var(--mut);font-size:13px;margin-top:44px}}
code{{background:var(--grid);padding:1px 6px;border-radius:5px;font-size:13px}}
@media(max-width:620px){{.ba{{grid-template-columns:1fr}}.bl{{min-width:118px}}.comp-mid{{flex-wrap:wrap}}}}
</style></head><body><div class="wrap">

<header>
  <div class="kick">Claude Insight · AI Fluency Report</div>
  <h1>How you build with AI</h1>
  <p class="sub">Measured from your real prompts and Claude's real actions — analyzed entirely on your machine. Nothing left it.</p>
</header>

{prov_banner}

<div class="hero">
  <div class="score-card">
    <div class="ring">
      <svg width="168" height="168" style="transform:rotate(-90deg)" role="img" aria-label="Overall score {result['overall']} out of 100">
        <circle cx="84" cy="84" r="74" fill="none" stroke="var(--grid)" stroke-width="11"/>
        <circle cx="84" cy="84" r="74" fill="none" stroke="var(--accent)" stroke-width="11" stroke-linecap="round"
          stroke-dasharray="{ring_len:.0f} 999"/>
      </svg>
      <div class="n"><b>{result['overall']}</b><s>out of 100</s></div>
    </div>
    <div class="band">{_esc(result['band'])}</div>
    <div class="rawnote">what we measured: {result['overall_raw']} · shown: {result['overall']} (adjusted for how much data there is)</div>
  </div>
  <div class="arch">
    <div class="emoji">{PROTOTYPES[a['primary']]['emoji']}</div>
    <h2>{_esc(a['label'].replace(PROTOTYPES[a['primary']]['emoji'] + ' ', '', 1))}</h2>
    <p>{_esc(a['blurb'])}</p>
    {arch_fit_html}
    <p class="fine">This describes how <b>you</b> drive — your asks, corrections, tool choices and hand-offs ({a['delegation_score']}/100 on handing off). It discounts the habits Claude carries on its own (measured, not assumed). The score above rates the two of you together; this rates you.</p>
    {arch_hedge}
  </div>
</div>

{progress_html}

<section>
  <h3>The short version</h3>
  {assessment_html}
  <div class="band-meaning"><b>{_esc(result['band'])} ({result['overall']}/100).</b> {_esc(result['band_meaning'])}</div>
</section>

<section>
  <h3>Your skill map — the four competencies, measured</h3>
  <p class="exgen" style="margin-bottom:12px">The four skills of working with AI — Delegation · Description · Discernment · Diligence — each counted from what actually happened in your sessions. The score ring above is the weighted blend of these four.</p>
  {skill_html}
</section>

{analysis_section}
{analysis_status_html}

<section>
  <h3>Do these next — your clearest path up</h3>
  {improve_intro}
  {improve_cards}
  {strength_html}
</section>

<section>
  <h3>The seven signals behind the scores</h3>
  {dim_html}
</section>

<section>
  <h3>Which builder you're closest to</h3>
  {aff}
</section>

<section>
  <h3>How much data this is based on</h3>
  <div class="ingest">
    <div class="ing"><div class="n">{corpus.files}</div><div class="l">sessions read</div></div>
    <div class="ing"><div class="n">{len(corpus.projects)}</div><div class="l">projects</div></div>
    <div class="ing"><div class="n">{corpus.total_bytes/1e6:.1f} MB</div><div class="l">of transcripts (mostly tool output)</div></div>
    <div class="ing"><div class="n">{days} day{"s" if days != 1 else ""}</div><div class="l">with activity</div></div>
    <div class="ing"><div class="n">{len(corpus.real_prompts)}</div><div class="l">prompts you typed</div></div>
    <div class="ing"><div class="n">{active_h:.0f} h</div><div class="l">hands-on time</div></div>
    {archive_tile}
  </div>
  <div class="honesty">
    <b>The honest part:</b> your transcripts contain {corpus.user_records:,} “user” records, but only <b>{len(corpus.real_prompts)}</b> are things <b>you</b> actually typed. The rest ({filtered_total:,}) is machinery — tool output, subagent chatter, injected system text — and none of it was scored:
    <div class="stack" role="img" aria-label="Of {corpus.user_records:,} user records, {len(corpus.real_prompts)} were real prompts">{segs}</div>
    <ul class="stack-legend">{legend}</ul>
    <p style="font-size:13px;margin-top:10px;color:var(--mut)">Your real prompts: median {d.get('median_chars','?')} characters · {d.get('under_80_pct','?')}% under 80 · {active_h:.0f} h hands-on (breaks over 5 min don't count).</p>
  </div>
  {retention_note}
</section>

<section>
  <h3>How the numbers are made</h3>
  <details><summary>The full method, in plain English (click to open)</summary>
    <p><b>Only what you typed gets scored.</b> Tool results, subagent turns, slash-command stubs, injected system text and giant pastes (over {MAX_HUMAN_PROMPT_CHARS:,} characters) are thrown out first.</p>
    <p><b>The score is the 4D framework, computed.</b> Seven measured signals blend into the four competencies — Delegation {int(COMP_WEIGHTS['Delegation']*100)}%, Description {int(COMP_WEIGHTS['Description']*100)}%, Discernment {int(COMP_WEIGHTS['Discernment']*100)}%, Diligence {int(COMP_WEIGHTS['Diligence']*100)}% — and the headline number is that blend. Levels 1–5 follow the framework's rubric (Emerging → Expert).</p>
    <p><b>Doing more never raises a score.</b> Every signal is a rate — “how often, when it mattered” — with a target; past the target, more of the same adds nothing. Only doing it <i>better</i> moves the number.</p>
    <p><b>Thin evidence is hedged, not faked.</b> A signal with few chances to show up gets pulled toward a neutral 50 and flagged “low data”. Both the measured and the adjusted numbers are shown under the ring.</p>
    <p><b>The “what we saw” moments are real.</b> Correction loops, blind re-edits and unchecked ships are found in your transcripts and quoted verbatim (home folder paths are masked). The ‹blank› rewrites are rule-made shapes of <i>your own</i> prompts; the fully written rewrites come from the Opus stage of <code>/ai-fluency</code>.</p>
    <p><b>Archetype ≠ score.</b> The archetype uses only signals you control (asks, corrections, tools, hand-offs — Verification at {int(AGENCY['Verification']*100)}% and reading-first at {int(AGENCY['Context']*100)}% weight), so it reflects you, not Claude's defaults. Nearest match by cosine on standardized values; a gap under {ARCHETYPE_MARGIN} shows as a blend.</p>
    <p><b>Limits:</b> this measures observable behavior, not intent; the detectors are keyword-based and English-biased; terse prompts that lean on earlier context can under-score “How you ask”. It's a snapshot, not a verdict.</p>
  </details>
</section>

<footer>Generated locally by Claude Insight · your transcripts never left this machine.</footer>
</div></body></html>"""


# The framework's four competencies, with a level rubric each. The engine now scores
# these DETERMINISTICALLY (see COMP_MIX); the AI stage layers judgment on top.
_COMPETENCY_DEFS = [
    ("Delegation", "Deciding what to hand to the AI and how — whole jobs, plans, sub-agents — versus doing it yourself.",
     {1: "Work is mostly micro-stepped; the agent rarely gets a whole job or a plan.",
      2: "Occasional whole hand-offs; sub-agents/planning appear but aren't habits.",
      3: "Regularly hands the agent complete tasks; reaches for planning or background runs.",
      4: "Deliberately splits work — whole jobs delegated, parallel/background where it pays.",
      5: "Orchestrates fleets: plans, delegates and supervises multi-step work as a reflex."}),
    ("Description", "Communicating what you want: the goal, the constraints, and what a good result looks like.",
     {1: "Terse nudges; the agent guesses at scope, constraints and success.",
      2: "Some prompts carry a file or a constraint; intent is stated occasionally.",
      3: "Most action prompts name the goal plus an anchor (file, constraint or 'done when…').",
      4: "Goal, constraints and acceptance criteria are routine; process/format stated when it matters.",
      5: "Briefs read like tickets: product, process and performance all specified up front."}),
    ("Discernment", "Evaluating what comes back — verifying results, grounding edits, correcting precisely.",
     {1: "Output is accepted as-is; edits land blind and little is re-checked.",
      2: "Checks happen sometimes; corrections are often just 'no, try again'.",
      3: "Most edit-bursts get verified and corrections usually name what broke.",
      4: "Verification is near-automatic; corrections carry the symptom and the rule.",
      5: "Layered evaluation as a reflex — tests, grounding and surgical feedback."}),
    ("Diligence", "Being responsible with what you ship — checking before it matters and cleaning up after.",
     {1: "Work ships unchecked; commits/deploys follow edits with nothing run between.",
      2: "Ships are sometimes gated by a check; cleanup is inconsistent.",
      3: "Most commits/pushes happen after a verification; teardown usually happens.",
      4: "Shipping is consistently gated; live systems are torn down as a habit.",
      5: "Nothing leaves unverified — checks, cleanup and ownership are systematic."}),
]


def _skill_levels(result):
    """The measured 4D skill map: per competency, its deterministic level, where the
    person is now, and the next move (the practice line of the weakest signal feeding
    that competency — so the advice targets what actually held the level down).

    Discernment and Diligence also carry a measured 'who drives it today' line: the
    score deliberately rates the collaboration (you + Claude), and this line says
    whether the habit is OWNED (you initiate it) or BORROWED (Claude volunteers it) —
    a borrowed habit works today and disappears with a less diligent agent."""
    driver = result.get("driver") or {}

    def driver_line(*keys):
        tot = sum((driver.get(k) or {}).get("total", 0) for k in keys)
        usr = sum((driver.get(k) or {}).get("user", 0) for k in keys)
        if tot < 5:
            return None
        word = _driver_word(usr / tot)
        if word == "mostly you":
            return f"Who starts these habits today: <b>mostly you</b> ({usr} of {tot}) — owned, it travels with you."
        if word == "shared":
            return f"Who starts these habits today: <b>shared</b> — you initiate {usr} of {tot}; Claude covers the rest."
        return (f"Who starts these habits today: <b>mostly Claude</b> (you asked for {usr} of {tot}) — "
                f"borrowed discipline. It works, but it vanishes with a less diligent agent; "
                f"ask for it yourself and it becomes yours.")

    drivers = {
        "Discernment": driver_line("verification", "reading"),
        "Diligence": driver_line("verification"),
    }
    out = []
    for name, what, rub in _COMPETENCY_DEFS:
        comp = result["competencies"][name]
        L = comp["level"]
        weakest = min(COMP_MIX[name], key=lambda d: result["shrunk"][d])
        nxt = (SKILL_TEACH[weakest]["practice"] if L < 5
               else "maintain this — it's a real strength.")
        out.append({"name": name, "level": L, "label": comp["label"],
                    "score": round(comp["score"]), "conf": comp["conf"],
                    "now": rub[L], "what": what, "next": nxt,
                    "driver": drivers.get(name)})
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description="Claude Insight v2 — AI fluency analyzer (one command, zero install).")
    ap.add_argument("path", nargs="?", help="transcript dir or .jsonl file (default: ~/.claude/projects)")
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

    files = discover_files(args.path)

    # Default mode: maintain + read the persistent archive so we can analyze more than the
    # ~30 days Claude Code keeps on disk. Skipped when an explicit path is given.
    archive_info = None
    if not args.path:
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
        # If most of what we're analyzing comes only from the archive (not this machine's
        # live transcripts), a shared/synced archive could be feeding in someone else's data.
        archive_only = archive_info["merged_sessions"] - archive_info["live_sessions"]
        if archive_only > max(25, 2 * archive_info["live_sessions"]):
            print(f"  Note: {archive_only} of {archive_info['merged_sessions']} analyzed sessions exist "
                  f"only in the archive ({args.archive}), not in your live transcripts. If that archive "
                  f"is shared or synced across people/machines, this report may mix in data that isn't "
                  f"yours — point --archive at a private, per-person path.", file=sys.stderr)

    if not files:
        where = args.path or "~/.claude/projects"
        print(f"No Claude Code transcripts found in {where}.\n"
              f"Point at your transcripts with:  python3 insight.py /path/to/dir", file=sys.stderr)
        return 1

    corpus = parse(files)
    if not corpus.real_prompts:
        print("Found transcripts but no real human-typed prompts to analyze.", file=sys.stderr)
        return 1

    result = analyze(corpus)
    cards, strength = build_action_plan(corpus, result)

    # Progress loop: only on default runs (an explicit path is a one-off analysis and
    # must never write into your personal progress history).
    progress_html = ""
    if not args.path:
        state_path = os.environ.get("CLAUDE_INSIGHT_STATE", DEFAULT_STATE_PATH)
        prev_state = load_progress(state_path)
        progress_html = build_progress_html(prev_state, result, corpus, cards)
        save_progress(state_path, prev_state, _progress_snapshot(result, corpus, cards))

    if args.evidence:
        bundle = build_evidence(corpus, result, cards, archive_info)
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

    analysis = None
    analysis_note = None
    if args.analysis:
        try:
            with open(os.path.expanduser(args.analysis), encoding="utf-8") as f:
                analysis = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"Could not read --analysis {args.analysis}: {e}", file=sys.stderr)
            return 1
        # Don't blindly trust the analysis file: it lives at a fixed, reused path, so it
        # may be empty (the AI stage no-op'd) or left over from a different run/person.
        # Validate shape + provenance; on any failure render the deterministic report
        # only, and say so, rather than pasting someone else's verdict into this report.
        current_fp = result.get("fingerprint")
        if not isinstance(analysis, dict) or not analysis.get("skill_map"):
            print("  Note: --analysis had no usable skill map (the AI stage may not have run); "
                  "rendering the deterministic report only.", file=sys.stderr)
            analysis_note = "the AI skill-map stage returned no usable output"
            analysis = None
        elif args.analysis_evidence:
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
                analysis_note = ("the saved AI analysis was produced from a different run / "
                                 "dataset, so it was not used")
                analysis = None
        else:
            # Manual --analysis with no evidence binding: if the file itself happens to carry a
            # run_fingerprint, honor it; otherwise merge (back-compat with hand-written analyses).
            supplied_fp = analysis.get("run_fingerprint")
            if supplied_fp and current_fp and supplied_fp != current_fp:
                print(f"  Note: the supplied --analysis was produced from a DIFFERENT run "
                      f"(fingerprint {supplied_fp} != {current_fp}). Ignoring it so it can't "
                      f"leak into this report; rendering the deterministic report only.",
                      file=sys.stderr)
                analysis_note = ("the saved AI analysis was produced from a different run / "
                                 "dataset, so it was not used")
                analysis = None

    if args.json:
        payload = {
            "overall": result["overall"], "overall_raw": result["overall_raw"],
            "band": result["band"], "archetype": result["archetype"]["label"],
            "competencies": {
                k: {"score": round(v["score"], 1), "raw": round(v["raw"], 1),
                    "level": v["level"], "label": v["label"], "confidence": round(v["conf"], 2)}
                for k, v in result["competencies"].items()
            },
            "competency_weights": COMP_WEIGHTS,
            "driver_share": result.get("driver"),
            "insights": [{"kind": i["kind"], "key": i["key"],
                          "text": re.sub(r"<[^>]+>", "", i["html"])}
                         for i in (result.get("insights") or [])],
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
    html_doc = build_html(corpus, result, cards, strength, archive_info, analysis, analysis_note,
                          progress_html=progress_html)
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)

    if not args.quiet:
        print(terminal_summary(corpus, result))
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
