# Multi-Source Plan v2 — Cowork, Codex, Cursor (+ optional ChatGPT export)

**Status (2026-07-11):** approved work order — Felipe green-lit P0→P3 (adapter refactor →
Cowork → Codex → Cursor, one green commit each) and **decided to skip Phase 4 (ChatGPT
chat export)**. §Phase 4 below is kept for the record but is not being built.
A complete v1 implementation exists on the `multi-source-plan` branch (built Jun 15 against
a pre-1.0 `main`, now unmergeable due to the June repo restructure) and a small, verified
Cowork-only patch exists on `feat/cowork-support` (Jun 19). This doc reconciles both with
fresh on-disk verification and defines the port onto current `main`.

**Goal:** AI Fluency analyzes four local sources through pluggable source adapters, without
touching the scorers: `claude-code` (today's baseline), `claude-desktop` (Cowork),
`codex` (OpenAI Codex CLI + desktop app), `cursor` (Cursor IDE). ChatGPT *chat* history is
an optional fifth source via the official export ZIP, clearly masked as chat-only.

---

## 1. Ground truth (verified on this machine, 2026-07-11)

| Source | Where | Volume here | Freshness | Format check |
|---|---|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | baseline, already supported | live | n/a |
| Cowork (Claude desktop) | `~/Library/Application Support/Claude/local-agent-mode-sessions/**/local_*/audit.jsonl` | 15 sessions | Jul 7 | **unchanged** vs June: `user/system/assistant/result` records, `_audit_timestamp`, `parent_tool_use_id`, `permission_denials`, `_audit_hmac` (read-only!) |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` (+ `archived_sessions`) | 49 rollouts | today | matches branch spec. Note: `/Applications/ChatGPT.app` here has bundle id `com.openai.codex` — "the ChatGPT app" on this Mac IS Codex and shares `~/.codex` |
| Cursor | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` (96 MB), table `cursorDiskKV` | 39 composers (26 agent / 8 chat / 3 plan / 1 multitask), 1,904 bubbles, 106 user prompts | Jul 10 | **June assumptions hold on current data**: `bubbleId:` type 1=user (text), type 2=assistant (`toolFormerData` on 1,123 bubbles). New since June: `composerHeaders` table (see §4), `agentKv:blob:*` family (mixed binary/JSON — supplementary state, ignore) |
| ChatGPT chat | nothing local. Context: on 2026-07-09 OpenAI made the Codex app THE ChatGPT desktop app (`com.openai.codex`); the old app is now "ChatGPT Classic" (`com.openai.chat`), whose local store is encrypted with a Keychain-held key (no read path — do not attempt). The new app's Chat tab is web-backed, no local plaintext | 0 | — | export-ZIP only (see §6) |

## 2. Coverage per source (honesty first — mask, never fake)

Weights today: Briefing 24 · Verification 22 · Context 22 · Iteration 18 · Toolcraft 14.
When a capability is absent, the dimension renders "not measurable from <source>" and the
overall score re-normalizes over measurable weights (machinery specced in v1 §7).

| Dimension | Claude Code | Cowork | Codex | Cursor (agent) | Cursor (chat-only) | ChatGPT export |
|---|---|---|---|---|---|---|
| Briefing / Direction | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Verification | ✅ | ✅ | ✅ (`exec_command`) | ✅ (`run_terminal_cmd`) | ❌ | ❌ |
| Context-setting | ✅ | ✅ | ⚠️ N/A (exec-based reads only) | ✅ (`read_file`) | ❌ | ❌ |
| Iteration | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Toolcraft | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| Bonus signals | — | `permission_denials` → Discernment | plan events → Delegation | `userDecision` approve/reject → Discernment; `isSubagent` excludable | — | — |
| Effective coverage | 100% | 100% | ~78% (Context masked) | ~100% | ~42% (Briefing+Iteration) | ~42% |

## 3. Strategy: port, don't merge

The `multi-source-plan` branch predates the June restructure (main has since deleted 5 test
files it touches, renamed the product, and shipped v1.0.0) — a merge is all conflicts. The
surgical path is a fresh commit series on `main`, using the branch as the reference
implementation and its `docs/MULTI_SOURCE_PLAN.md` (v1, on the branch) as the detailed
adapter spec. `feat/cowork-support` contributes one design correction (below).

Hard constraints carry over unchanged: single file, stdlib only (`sqlite3`, `zipfile`,
`json` are stdlib); read-only on all sources; existing tests stay green unmodified;
byte-identical claude-code output after the refactor; capability masking over faking.

## 4. Phases (each lands as its own green commit)

**Phase 0 — adapter refactor, no behavior change.** Extract discovery + per-record parsing
into `ClaudeCodeAdapter`; `parse()` becomes adapter-driven; add the regression test locking
claude-code output byte-identical. (Reference: branch implementation, v1 §2–3.)

**Phase 1 — Cowork adapter.** Port from the branch, with one correction from
`feat/cowork-support`: keep the archive ENABLED for Cowork by keying sessions on the
`local_<uuid>` parent folder (`_session_key`) instead of the colliding `audit.jsonl`
basename. (The v1 branch disabled desktop archiving to dodge this; the Jun 19 patch solved
it properly — verified 7 files → 7 sessions.) Never rewrite source files (`_audit_hmac`).

**Phase 2 — Codex adapter.** Port as-is from the branch (v1 §5 mapping table:
`exec_command`→bash, `apply_patch`→edit/write, `update_plan`→delegation; drop
`role:developer`, harness instructions, reasoning — chain-of-thought is ciphertext even
locally, by design). Context stays N/A-masked. **Discovery glob must be
`sessions/**/rollout-*.jsonl` only — never sweep `~/.codex/` broadly: `auth.json` holds
live OAuth tokens and `config.toml` can hold MCP credentials.** Optional enrichment later:
`state_5.sqlite` `threads` table carries per-session `model`, `tokens_used`, `git_branch`,
`cwd`, `title` (treat as alpha-cadence, additive-churn). README gets one line: since
2026-07-09 the ChatGPT desktop app IS Codex (`com.openai.codex`) — users who "use the
ChatGPT app" for coding have `~/.codex` data. (Validating nugget: that app itself imports
Claude Code transcripts — `~/.codex/external_agent_session_imports.json` — i.e. OpenAI
ships this exact ingestion pattern.)

**Phase 3 — Cursor adapter.** Port from the branch (v1 §6) with three upgrades from
today's DB probe: (a) enumerate sessions from the new `composerHeaders` table
(`composerId, workspaceId, createdAt, isSubagent, isArchived`) instead of scanning
`composerData:` JSON — and EXCLUDE `isSubagent=1` composers from prompt counts (same
rationale as Claude Code sidechains); (b) per-session capability masking by
`composerData.unifiedMode` (`chat` → prompts-only ≈42% coverage; `agent`/`plan`/`multitask`
→ full) — v1 deferred this, `unifiedMode` makes it trivial; (c) ignore `agentKv:blob:*`
(supplementary, half binary) — bubbles remain the message store through current versions
(re-verified on Cursor 3.10, data written Jul 10); add a fail-soft probe so a future
migration degrades with a clear message, not a crash. **Copy `state.vscdb` AND its `-wal`
(+`-shm`) siblings together, then open the copy `mode=ro` — NOT `immutable=1`, which makes
SQLite ignore the WAL. Copying the .vscdb alone can read as empty/stale because recent rows
live in the WAL (verified failure mode).**
Read bubbles in `composerData.fullConversationHeadersOnly` order; take the project name
from `composerHeaders.value.workspaceIdentifier.uri.fsPath` (basename). Future enrichment,
out of v1: `~/.cursor/ai-tracking/ai-code-tracking.db` `scored_commits` has per-commit
AI-vs-human line attribution.

**Phase 4 — ChatGPT chat via official export. DECIDED 2026-07-11: SKIPPED.** Kept for
reference only.
No auto-discovery: `--source chatgpt --input ~/Downloads/chatgpt-export.zip`. Parse
`conversations.json`: an array of conversations, each with a `mapping` TREE (edits and
regenerations branch) — reconstruct the visible thread by walking backwards from
`current_node` via `parent`; keep `author.role == "user"` text parts with `weight != 0`;
drop system/tool nodes (`recipient != "all"` marks assistant→tool calls). Detect both the
single-file and the newer sharded export layouts. The tree skeleton has been stable ~3
years; `content_type`/`metadata` churn at the edges. Note: Codex sessions are NOT in this
export — they're `~/.codex` (Phase 2). Capabilities: prompts + session structure only →
Briefing + Iteration measurable, everything else masked (~42% coverage, like Cursor
chat-only). v1 explicitly deferred pure-chat sources for honesty; the masking machinery
makes inclusion defensible now, but it's a product call, not a technical one.

## 5. Sequencing vs in-flight work

`scoring-accuracy-fix` (Dropbox clone, dirty) and `feat/agency-scoring-fixes` (dirty) touch
the scorer layer; this plan touches the parse layer. Semantically disjoint but same file —
land or rebase the scoring branches around Phase 0 to keep conflicts trivial, and prefer
landing them FIRST (they're further along and affect reported numbers).

## 6. Test plan

Per adapter: a tiny synthetic fixture (`audit.jsonl`, `rollout-*.jsonl`, in-test-built
`state.vscdb`, minimal `conversations.json`) asserting: prompt count after
de-contamination, tool events land on the canonical vocabulary (`read/edit/write/bash/...`),
delegation counted, capabilities + masking correct, report renders, and no absolute home
path leaks into evidence (`_normalize_path` rule, v1 §9). Plus the Phase 0 byte-identical
regression test. Cursor gets one extra: `isSubagent` composers excluded; `unifiedMode:chat`
session scores Briefing/Iteration only.

## 6b. Privacy additions (beyond v1 §9)

- Never ingest `~/.codex/auth.json` (live OAuth tokens) or `config.toml` (possible MCP
  credentials); the Codex glob targets `sessions/**/rollout-*.jsonl` only.
- Cursor's Privacy Mode is server-side only: local transcripts embed file contents and
  terminal output regardless. We stay local-only as always, and the existing home-path
  scrub applies to everything that reaches the evidence bundle.
- ChatGPT Classic's encrypted store stays untouched — extracting its Keychain key would
  circumvent an intentional security control; the official export reaches the same data.

## 7. Out of scope (unchanged from v1)

Claude.ai web chat. Cursor Tab/autocomplete telemetry. Rewriting any source file, ever.

**Superseded 2026-07-11 (product decision by Felipe):** the v1 "never blend sources into
one score" rule. `--source all` now produces ONE combined report — one score, one profile —
assembled honestly: each dimension is scored over the merged data of only the sources that
can observe it (capability masks), evidence volume drives the usual shrinkage, and per-tool
sub-scores render in the report's source-mix panel. The blend never imputes an unobservable
signal, which is what the v1 rule actually protected. Implemented in the C1-C3 commit
series; the /ai-fluency skill runs combined by default.
