---
name: ai-fluency
description: Analyze how the developer collaborates with Claude Code and produce an "AI fluency" builder profile — overall score, archetype, a skill map, the five dimensions, and clear what/where/how direction. Use when the user asks to analyze their Claude Code usage, AI fluency, builder profile, prompting style, or "how do I use Claude / AI", or runs /ai-fluency.
argument-hint: "[PATH | --no-open]"
allowed-tools: Bash(python3 *), Read, Write
---

# AI Fluency Analysis

You are profiling how this developer collaborates with AI coding tools, using
their real Claude Code session transcripts. The v2 engine (`insight.py`) computes
an accurate, deep, self-contained report; your job is to run it and give the
developer a short, human, plain-English read on top of it.

## Step 1 — Run the one command

From the repo root (the engine is pure standard library — no install, no Ollama,
no API key, fully offline):

```bash
python3 insight.py --no-open $ARGUMENTS
```

This writes `ai_fluency_report.html` and prints a 3-line summary (score, band,
archetype). Pass `--no-open` inside Claude Code so it doesn't try to launch a
browser. To point at a non-default location, pass a path as `$ARGUMENTS`
(default search: `~/.claude/projects`).

Then read the machine-readable metrics for your narration:

```bash
python3 insight.py --json $ARGUMENTS
```

If it reports no transcripts, tell the user to pass their transcript directory
as an argument (default is `~/.claude/projects`).

## Step 2 — Narrate (don't re-derive)

The engine already did the measurement accurately — every score is a RATE over
de-contaminated real prompts, so DON'T recompute numbers or second-guess the
filtering. Read the `--json` output and:

1. Lead with the headline: **overall score + band + archetype**, in one sentence.
2. Explain the **single top growth lever** (the first WHAT/WHERE/HOW priority)
   in plain English, grounded in one of their real prompts.
3. Call out their **strongest dimension** as the foundation to build on.
4. Mention the **data the report is based on** (real prompts, projects, span) so
   they trust it — and note that the headline length/time numbers exclude
   tool-output, subagent turns and idle time (the things v1 wrongly counted).

Keep it to a short, encouraging paragraph or two. The HTML report has the full
depth (five dimensions, skill map, archetype affinity, methodology); point the
user to `ai_fluency_report.html` for the deep dive.

## Notes

- Everything runs locally and read-only. Transcripts are analyzed on this
  machine; nothing is uploaded and no API key is involved.
- The scores measure observable behavior, not intent; thin signals are flagged
  "low data" and pulled toward neutral, so don't over-claim on those.
- The legacy package (`python -m claude_insight`) and its local-Ollama path still
  exist, but `insight.py` is the recommended, most accurate entry point.
