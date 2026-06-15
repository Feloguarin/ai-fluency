---
name: ai-fluency
description: Analyze how the developer collaborates with AI coding agents (Claude Code, Claude Desktop, Codex CLI, Cursor) and produce a Platzi-branded "AI fluency" skill map — overall score per tool, archetype, the four AI-fluency competencies (Delegation, Description, Discernment, Diligence), the measured dimensions, and clear what/where/how direction. Use when the user asks to analyze their AI-coding usage, AI fluency, builder profile, or prompting style, or runs /ai-fluency.
argument-hint: "[PATH | --source NAME | --no-open]"
allowed-tools: Bash(python3 *), Read, Write, Workflow
---

# AI Fluency Analysis — one command, multi-source, Platzi-branded

You produce a reliable AI-fluency **skill map** from the developer's real logs across every
coding agent on the machine. One command, three local stages:

1. **Measure (deterministic).** `insight.py` parses each source's logs, de-contaminates them,
   and computes rate-based, confidence-hedged numbers. It reads four sources via adapters:
   Claude Code, Claude Desktop (agent mode), Codex CLI, Cursor — auto-detecting what's present.
2. **Explore (Sonnet 4.6).** Parallel explorers read the evidence, one per AI-fluency competency.
3. **Analyze (Opus 4.8).** A senior assessor writes ONE cross-tool skill map, **grounded in the
   bundled AI Fluency framework**, then verifies it is evidence-grounded.

The final output is a **single Platzi-branded HTML report** (`ai_fluency_report.html`) with the
score per tool, the unified 4D skill map, and growth levers.

## Step 1 — Measure all sources + emit per-source evidence (one command)

From the repo root (pure standard library — no install, no API key, fully offline):

```bash
python3 insight.py --source all --evidence .insight/ev.json --no-open -o ai_fluency_report.html
```

This writes a deterministic combined report and one evidence bundle per detected source
(`.insight/ev.<source>.json`, de-contaminated, local, git-ignored). If the user passed a single
PATH or `--source <name>` in `$ARGUMENTS`, run that instead (single-source) — e.g.
`python3 insight.py --evidence .insight/ev.json --no-open -o ai_fluency_report.html $ARGUMENTS`.

List the evidence files and resolve absolute paths:

```bash
python3 -c "import glob,os;print('\n'.join(os.path.abspath(p) for p in sorted(glob.glob('.insight/ev*.json'))));print(os.path.abspath('reference/ai-fluency-framework.md'))"
```

## Step 2 — Run the two-model analysis workflow

Call the **Workflow** tool with:
- `name`: `ai-fluency`
- `args`: `{ "evidence": [<abs evidence paths>], "framework": "<abs framework path>" }`
  (pass the list of all `.insight/ev.<source>.json` paths; a single string also works for one source)

It uses **Sonnet 4.6** to explore and **Opus 4.8** to analyze + verify (models baked in) and
returns one unified analysis JSON (overall_read, a 4-competency skill_map, top_growth, strengths).
If `Workflow` resolves the name from the wrong directory, pass `{ scriptPath: "<abs path to
.claude/workflows/ai-fluency.js>" }` instead.

## Step 3 — Render the final Platzi-branded report

Write the workflow's returned JSON to `.insight/analysis.json`, then merge it:

```bash
python3 insight.py --source all --analysis .insight/analysis.json --no-open -o ai_fluency_report.html
```

The report now carries the Opus-authored, framework-grounded skill map on top of the deterministic
numbers. Point the user to `ai_fluency_report.html` (open it unless they passed `--no-open`).

## Step 4 — Narrate (don't re-derive)

In chat, give a short, encouraging read: the **score + archetype per tool** in one line, the
**single highest-leverage growth move** grounded in one of their real prompts, and their
**strongest competency** as the foundation. Keep it to a paragraph or two; the report has the depth.

## Fallbacks

- **No Workflow capability?** Step 1 alone is complete — the deterministic combined report stands
  on its own (it prints a "run /ai-fluency for the skill map" note). Narrate from it; skip 2–3.
- **One source / explicit path?** Pass it as `$ARGUMENTS` (e.g. `--source codex`, or a directory)
  in Steps 1 and 3 — the single-source report uses the same Platzi branding.

## Notes

- Everything runs locally; source logs are never modified (Cursor's DB is copied, opened read-only;
  Claude Desktop's `_audit_hmac` is never touched). Nothing is uploaded.
- **Nothing personal is committed:** the report, `.insight/`, and the archive are git-ignored, and
  absolute home paths + `user@host` tokens are scrubbed from the report and evidence.
- A source that can't observe a signal (e.g. Codex has no read tool → Context) is marked
  *not measurable* and excluded from its score, never faked. Thin signals are flagged "low data".
