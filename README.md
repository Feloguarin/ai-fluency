# Claude Insight

See how you *actually* build with AI. Claude Insight reads your local Claude Code
transcripts and turns them into one self-contained HTML report: a fluency score, your
builder archetype, a 4-competency skill map, and the few highest-leverage things to change
next — with the "before/after" rewrites drawn from your own prompts.

It all runs on your machine. Your transcripts never leave it, and the originals are never
touched.

## ⚡ Install in 5 seconds

```bash
curl -fsSL https://raw.githubusercontent.com/Feloguarin/claude-insight/main/install.sh | bash
```

That drops the **`/ai-fluency`** skill into Claude Code. Then, inside Claude Code (any
folder), just run:

```text
/ai-fluency
```

One command, one finished report at `~/.claude/insight/ai_fluency_report.html`. Requires
Python 3.8+ and Claude Code.

## 🚀 What you get

- **A fluency score (0–100)** with a band — Operator → Developing → Proficient → Advanced → Expert — and what it means. The score **is** the AI Fluency framework, computed: the weighted blend of the four competencies below.
- **Your builder archetype** — Autonomous Agent, Architect, Debugger, Collaborator, or Sprinter — picked from *your* behavior, not from keywords.
- **A 4-competency skill map** — **Delegation · Description · Discernment · Diligence** (the AI Fluency framework) — each **measured deterministically** on a 1–5 level with one concrete next move. The AI stage adds judgment on top; it never invents the levels.
- **Seven measured signals** behind the map — shown in plain English: *how you ask, checking the work, reading before changing, course-correcting, using the right tools, handing off whole jobs, checking before you ship* — each a defensible rate, not a vanity count.
- **Mined moments, not generic advice.** The report finds the specific episodes where a habit cost you — correction loops (with your real prompts and roughly the minutes they burned), files edited blind that needed re-editing, unchecked pushes followed by fix commits — and quotes them back. Your "what to type instead" is **your own prompt, reshaped** (rule-suggested with ‹blanks›; the `/ai-fluency` Opus stage writes it fully).
- **A report any human can read.** Plain-English everywhere, one accent color, big readable type, light *and* dark mode, and charts that carry the data (score ring, competency levels, signal bars, a real-vs-noise composition bar).
- **What / Where / How** — your top growth levers, each tied to real moments in your transcripts and (when you run the full skill) a rewrite of one of *your own* prompts.
- **Full transparency** on the data: how many prompts you really typed (vs. tool output, subagent turns, and injected noise), projects, MB, and hands-on time — across **more than the 30 days Claude Code keeps on disk** (see below).

## 🎯 How the score works (and what it won't do)

The whole point is to measure *skill*, not activity — so a few things are deliberate:

- **The score is the 4D framework, computed.** Seven behavioral signals are blended into
  the four AI-fluency competencies — **Delegation 25% · Description 30% · Discernment 25%
  · Diligence 20%** — and the headline score is that weighted competency blend. Delegation
  is measured from real hand-off behavior (sub-agents / background runs / planning, plus
  how many agent actions each hand-off buys before you have to steer again), and Diligence
  from **ship-gating**: whether commits, pushes and deploys are gated by a check that ran
  after the last edit. Neither is guessed by an LLM — both are counted.
- **Everything is a rate, then saturated.** Each dimension is a per-prompt or
  per-opportunity rate run through `min(1, rate / target)`. Doing *more* of the same
  thing doesn't move the number — only doing it *better* does.
- **Thin data is hedged, not faked.** When you have little history, each dimension is
  pulled toward a neutral 50 in proportion to how few opportunities it had. So your first
  runs read conservatively and then **firm up over your first few dozen prompts as the tool
  gets confident, and settle** (each dimension reaches full confidence at its own count —
  e.g. ~60 briefing-prompts, ~15 edit-bursts). If an early score creeps up run-over-run,
  that's the hedge lifting toward your real level — it plateaus once there's enough data,
  and from then on only changing your habits moves it. Thin dimensions are flagged **low data**.
- **The score rates the *collaboration*; the archetype rates *you*.** The fluency score
  is the quality of you-and-Claude together — and that includes habits Claude often does
  on its own, like reading a file before editing (Context-setting) or running tests
  (Verification), which are ~30% of the effective weight (they enter only through the
  Discernment and Diligence competencies, at partial weight). Your **archetype** is built from a
  separate, *agency-weighted* vector that discounts those Claude-driven habits, so it
  reflects how *you* drive. The two can differ on purpose: a thorough agent lifts the
  collaboration score more than it lifts the archetype.
- **Noise is stripped before anything is scored.** Tool results, subagent (sidechain)
  turns, slash-command stubs, injected system text, and pasted walls of text (over ~6k
  chars) don't count as your prompts. Idle gaps longer than 5 minutes are excluded from
  "active time," so it's hands-on time, not wall-clock.

Both the raw and the confidence-adjusted scores are shown in the report, and the full
methodology is in an appendix at the bottom of it.

## 🧠 One command, the full analysis

`/ai-fluency` runs the complete pass as **one pass that ends in a single finished report**
— it won't flash a score first and a report later:

1. **Measure** — `insight.py` de-contaminates and scrubs your transcripts and computes
   every number (rate-based, confidence-hedged, archive-backed so it sees more than the
   30-day window). This step is silent on purpose.
2. **Explore — Claude Sonnet 4.6** — four explorers run in parallel, one per AI-fluency
   competency, reading only your de-contaminated evidence.
3. **Analyze — Claude Opus 4.8** — a senior assessor writes the skill map and your growth
   levers grounded in [`reference/ai-fluency-framework.md`](reference/ai-fluency-framework.md),
   then a verifier checks every claim against your evidence and repairs anything ungrounded.

The numbers are always computed deterministically; the models add judgement and direction
on top and never change the math. It runs on your existing Claude Code session — **no
separate API key** — and the models are pinned per stage in
[`.claude/workflows/ai-fluency.js`](.claude/workflows/ai-fluency.js).

Two guarantees worth calling out:

- **Your "how to grow" cards are written from your real prompts** — the "before" is
  something you actually typed and the "after" is Opus's tailored rewrite of it, not a
  stock example.
- **An analysis can't leak across runs or people.** The evidence bundle carries a
  fingerprint of the exact run it came from; the report engine refuses to merge an
  analysis whose fingerprint doesn't match, and falls back to the deterministic report
  (and says so) instead.

If the Workflow capability isn't available, the skill still produces the complete
deterministic report — scores, archetype, dimensions, and skill levels — with generic,
clearly-labeled growth examples instead of the Opus-written ones.

### Data export & flags

```bash
python3 insight.py --json                                          # metrics + data breakdown as JSON
python3 insight.py --evidence ev.json                              # write the de-contaminated evidence bundle (carries a run fingerprint)
python3 insight.py --analysis an.json --analysis-evidence ev.json  # merge an Opus analysis, bound to this run
python3 insight.py /path/to/transcripts                            # analyze a specific directory or .jsonl file
python3 insight.py --quiet                                         # suppress the terminal summary (the skill's measure step uses this)
python3 insight.py --no-open                                       # don't auto-open the browser
python3 insight.py --archive ~/my-archive                          # keep history in a PRIVATE, per-person durable folder
python3 insight.py --no-archive                                    # analyze without copying anything new
```

## ⏳ Analyzing more than 30 days

By default Claude Code **deletes transcripts older than its `cleanupPeriodDays` setting
(default `30`)**, so only ~30 days of history is ever on disk — that's a limit of the
*data*, not of this tool, which reads everything available. Two things let it see more:

1. **Stop the deletion.** Add this to `~/.claude/settings.json` so Claude Code keeps a
   full year (set it before more history ages out):
   ```json
   { "cleanupPeriodDays": 365 }
   ```
2. **The built-in archive (automatic).** On every default run, Claude Insight copies your
   transcripts into a persistent archive (`~/.claude/insight-archive` by default) *before*
   the cleanup can remove them, then analyzes **live + archive**, de-duplicated. From your
   first run on, your history accumulates indefinitely. It only ever grows files, copies
   atomically, and stays 100% on your machine.

   Keep the archive **private to you**. A single archive folder shared between people
   (e.g. a synced team Dropbox) would merge everyone's transcripts into one report — so
   point `--archive` at your own location, not a shared one:
   ```bash
   python3 insight.py --archive ~/Dropbox/claude-archive   # your own, private folder — survives reinstalls
   # or set CLAUDE_INSIGHT_ARCHIVE in your shell. Use --no-archive to skip a run.
   ```

## 📦 Run from source (no install)

`insight.py` is a single, pure-standard-library file — clone and run it, nothing to
`pip install`:

```bash
git clone https://github.com/Feloguarin/claude-insight.git
cd claude-insight
python3 insight.py                 # analyze ~/.claude/projects, then write + open the report
```

> Requires Python 3.8+. On its own, `python3 insight.py` produces the complete
> deterministic report (scores, archetype, dimensions, growth levers with generic
> examples). The Opus-personalized rewrites come from running `/ai-fluency` inside Claude
> Code.

## 📊 Example output

```
  AI Fluency Score: 78/100  (Advanced)
  Archetype: 🤖 Autonomous Agent
  Competencies: Delegation L4 · Description L3 · Discernment L4 · Diligence L4
  Based on 156 real prompts across 16 projects, 156 sessions (53.8 MB).
  Archive: 156 sessions preserved at ~/.claude/insight-archive (0 new, 1 updated this run).
  Report: ai_fluency_report.html
```

*(Illustrative — your numbers will differ.)* The HTML report adds the headline score ring,
the measured four-competency skill map (your level and next move for each), the seven signals,
your top growth levers with before/after rewrites, archetype affinity, a "how much data
this is based on" breakdown, and a methodology appendix.

## 🏗️ Architecture

```
insight.py                       # the whole engine: parse → de-contaminate → score → report
                                 #   (pure stdlib, zero install; --evidence / --analysis hooks)
reference/
└── ai-fluency-framework.md      # the 4D framework the Opus analysis stage is grounded in
.claude/
├── skills/ai-fluency/SKILL.md   # /ai-fluency — orchestrates the one-command pipeline
└── workflows/ai-fluency.js      # Sonnet 4.6 explore → Opus 4.8 analyze → verify
tests/                           # stdlib unittest (de-contamination, scoring, archive,
                                 #   analysis-provenance, personalized growth)
```

## 📈 What's measured

### The four AI-fluency competencies (the skill map — and the score)
Adapted from Anthropic's *AI Fluency: Frameworks & Foundations* (the 4 Ds). Each is
**computed deterministically** from the signals below, gets a 1–5 level on the framework's
rubric (Emerging → Expert), and the headline score is their weighted blend:

1. **Delegation** *(25%)* — deciding what to hand to the agent, and how to split the work.
   Composed from the Delegation signal (45%), Toolcraft (35%), Briefing (20%).
2. **Description** *(30%)* — telling the agent what you want (goal + constraint + acceptance test).
   Composed from Briefing (80%) and Iteration (20%).
3. **Discernment** *(25%)* — evaluating what comes back (verify, ground edits, correct precisely).
   Composed from Verification (40%), Context-setting (35%), Iteration (25%).
4. **Diligence** *(20%)* — being responsible: verify before it ships, tear down, own the result.
   Composed from Ship-gating (45%), Verification (30%), Context-setting (25%).

### The seven measured signals behind the competencies
(Report label — internal name.)
1. **How you ask** *(Direction)* — how concretely you frame requests (constraint / artifact /
   intent rates, plus process- and performance-description cues: ordering the steps,
   shaping the output).
2. **Checking the work** *(Verification)* — running tests / build / app after a burst of edits.
3. **Reading before changing** *(Context)* — grounding edits in a prior read, instead of blind edits.
4. **Course-correcting** *(Iteration)* — correcting precisely instead of vague rejection.
5. **Using the right tools** *(Toolcraft)* — reaching for a healthy range of tools, not forcing
   everything through one.
6. **Handing off whole jobs** *(Delegation)* — hand-offs per active hour (sub-agents /
   background runs / planning) and hand-off *depth*: the median number of agent actions each
   action-prompt buys before you steer again. Whole jobs run long; micro-steps hand back
   after one tool call.
7. **Checking before you ship** *(Shipping)* — when work leaves the machine
   (`git commit/push`, deploys, publishes), was it gated by a check that ran after the last
   edit? No ship events → neutral and fully hedged, never a penalty.

### You, Claude, or both? (owned vs borrowed habits)
The score rates the **collaboration** on purpose — if you'll always build with an agent,
setting up a partnership where verification always happens *is* the skill. But a habit is
only robust if it's **yours**: for every check and every read, the engine measures whether
*your prompt asked for it* or Claude volunteered it. The skill map then says, per
competency, "who starts these habits today: mostly you / shared / mostly Claude" — and a
Claude-carried habit is named **borrowed discipline**: real today, gone silently with a
less diligent agent. The same measurement replaces the archetype's hardcoded agency
constants with values measured from your own behavior.

### The insight engine (why no two reports read alike)
The written profile is composed from **pattern observations that only fire when your data
shows them**, each carrying its own numbers: owned vs borrowed checks, front-loaded vs
thin session openers, whether you get *more specific* or *terser* after a miss, projects
where your briefing discipline drops, hand-offs that land without correction, what your
correction loops cost in minutes, and ships that were always gated.

### The progress loop (the report follows up on itself)
Every default run saves a scores-only snapshot (no prompt text) to a private progress file
(`~/.claude/insight/progress.json`, override with `CLAUDE_INSIGHT_STATE`). The next run
opens with **"Since your last report"**: your overall and competency deltas, and — the
point — follow-up on the exact habits the last report told you to practice: *"You were
working on Checking before you ship: ships gated by a check went 40% → 80% ✓ that's the
habit forming."* Re-running on unchanged data never duplicates a snapshot, and explicit-path
runs never touch your progress history.

### Mined moments (the "it watched me work" layer)
Deterministically found in your own transcripts and quoted verbatim in the report and the
evidence bundle: **correction loops** (2+ corrections in a row, with the prompts and the
approximate minutes they cost), **blind re-edits** (a file changed without being read, then
changed again), **ship-then-fix** (an unchecked push followed by a fix/revert commit), and
your **best brief / sharpest correction** as proof you already know the target shape.

(Verification and Context-setting are largely habits Claude drives on its own — they enter
the score only through Discernment/Diligence at partial weight, and are further discounted
in the archetype.)

### Archetypes (from *your* behavior, not keywords)
- **🤖 Autonomous Agent** — delegates whole, end-to-end jobs and trusts the agent to run them.
- **🏗️ Architect** — plans and explores before building; reads and designs first.
- **🐛 Debugger** — methodical: read to diagnose, change, verify, repeat.
- **🤝 Collaborator** — works with the agent like a teammate: asks for options, gives feedback.
- **⚡ Sprinter** — fast and direct, terse prompts, low ceremony; verification is the growth edge.
- **🎬 Director** — hands over whole outcomes, steers at the level of intent, and holds every result to a hard acceptance test.

The archetype is the nearest match to your **agency-weighted** behavior vector — it counts
what *you* do (briefing, correcting, tool choice, delegation) and discounts the
read-before-edit and run-the-tests habits Claude does on its own, so it reflects you, not
the agent. Near-ties are reported as a blend, never a coin-flip.

## 🔒 Privacy

Everything is local. Transcripts are read from `~/.claude/projects` (and your archive),
analyzed on your machine, and written only to the report path you choose (the `/ai-fluency`
skill keeps everything under `~/.claude/insight/`). Nothing is uploaded; there's no API key
and no telemetry. The Sonnet/Opus stages run inside your own Claude Code session. Your
original transcripts are never modified, and the working files that hold your real prompts
(`evidence.json`, `analysis.json`, the report) are git-ignored so they can't be committed.

## 🔧 Development

```bash
# Run the test suite — standard library only, no pytest required
python3 -m unittest discover -s tests
```

## 📄 License

MIT License — see [LICENSE](LICENSE).
