"""
Tests for the v2 single-file engine (insight.py).

Focus: the accuracy guarantees that v1 violated — prompt de-contamination,
rate-based scoring that can't be inflated by volume, gap-capped active time,
and confidence shrinkage of thin signals. Pure stdlib unittest.
"""
import glob
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import insight  # noqa: E402


def _rec(**kw):
    return json.dumps(kw)


def write_session(dirpath, name, records):
    path = os.path.join(dirpath, name)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(r + "\n")
    return path


def user_text(text, **extra):
    e = {"type": "user", "timestamp": extra.pop("ts", "2026-01-01T00:00:00Z"),
         "message": {"role": "user", "content": text}}
    e.update(extra)
    return _rec(**e)


def user_tool_result(ts="2026-01-01T00:00:01Z"):
    return _rec(type="user", timestamp=ts,
                message={"role": "user", "content": [{"type": "tool_result", "content": "ok"}]})


def assistant_tool(name, ts="2026-01-01T00:00:02Z", **inp):
    return _rec(type="assistant", timestamp=ts,
                message={"role": "assistant",
                         "content": [{"type": "tool_use", "name": name, "input": inp}]})


class TestDecontamination(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_filters_noise_keeps_real_prompts(self):
        recs = [
            user_text("add a login endpoint to api.py, only touch that file"),  # real
            user_tool_result(),                                                 # tool result
            user_text("<task-notification>\n<task-id>abc</task-id>"),           # injection marker
            user_text("You are a senior engineer wiring a trading bot. " + "x" * 50),  # subagent leak
            _rec(type="user", isSidechain=True, timestamp="2026-01-01T00:00:03Z",
                 message={"role": "user", "content": "subagent internal prompt"}),  # sidechain
            _rec(type="user", isMeta=True, timestamp="2026-01-01T00:00:04Z",
                 message={"role": "user", "content": "meta injected"}),             # meta
            user_text("y" * 7000),                                              # > 6KB paste
            user_text("run the tests"),                                         # real
        ]
        write_session(self.tmp, "s1.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        texts = [p["text"] for p in corpus.real_prompts]
        self.assertEqual(len(texts), 2)
        self.assertIn("add a login endpoint to api.py, only touch that file", texts)
        self.assertIn("run the tests", texts)
        # everything else was filtered, and the breakdown is recorded
        self.assertGreaterEqual(corpus.filtered["tool results"], 1)
        self.assertGreaterEqual(corpus.filtered["subagent turns"], 1)
        self.assertGreaterEqual(corpus.filtered["meta-injected"], 1)
        self.assertGreaterEqual(corpus.filtered["injected / pasted"], 2)


class TestNoVolumeInflation(unittest.TestCase):
    """Doing MORE of the same must not raise the score (rate-based)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_repeating_a_weak_prompt_does_not_help(self):
        few = [user_text("do it")] * 3
        many = [user_text("do it")] * 60
        write_session(self.tmp, "few.jsonl", few)
        c1 = insight.parse(insight.discover_files(self.tmp))
        d1, _, _ = insight.score_direction(c1)

        tmp2 = tempfile.mkdtemp()
        write_session(tmp2, "many.jsonl", many)
        c2 = insight.parse(insight.discover_files(tmp2))
        d2, _, _ = insight.score_direction(c2)
        # 20x the volume of the same weak prompt -> not a higher score
        self.assertLessEqual(d2, d1 + 1.0)


class TestActiveTimeCapsIdle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_idle_gap_is_capped(self):
        recs = [
            user_text("start", ts="2026-01-01T00:00:00Z"),
            user_text("end after a week of idle", ts="2026-01-08T00:00:00Z"),
        ]
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        # one ~7-day gap must be capped at GAP_CAP_SECONDS, not counted as a week
        self.assertLessEqual(corpus.active_seconds, insight.GAP_CAP_SECONDS + 1)


class TestConfidenceShrinkage(unittest.TestCase):
    def test_thin_signal_pulled_toward_50(self):
        # a high raw score on tiny n must shrink toward 50
        shrunk, c = insight.shrink(90.0, n=3, target_n=12)
        self.assertLess(shrunk, 90.0)
        self.assertGreater(shrunk, 50.0)
        self.assertAlmostEqual(c, 0.25, places=3)
        # full data -> no shrink
        shrunk2, c2 = insight.shrink(90.0, n=60, target_n=12)
        self.assertEqual(round(shrunk2), 90)
        self.assertEqual(c2, 1.0)


class TestContextGrounding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_blind_edit_scores_lower_than_grounded(self):
        grounded = [
            user_text("fix the bug"),
            assistant_tool("Read", file_path="/x/a.py"),
            assistant_tool("Edit", file_path="/x/a.py"),
        ]
        blind = [
            user_text("fix the bug"),
            assistant_tool("Edit", file_path="/x/a.py"),  # edited without reading
        ]
        write_session(self.tmp, "g.jsonl", grounded)
        cg = insight.parse(insight.discover_files(self.tmp))
        sg, dg, _ = insight.score_context(cg)

        tmp2 = tempfile.mkdtemp()
        write_session(tmp2, "b.jsonl", blind)
        cb = insight.parse(insight.discover_files(tmp2))
        sb, db, _ = insight.score_context(cb)
        self.assertGreater(sg, sb)
        self.assertEqual(dg["rate"], 1.0)
        self.assertEqual(db["rate"], 0.0)


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_full_run_and_html(self):
        recs = [
            user_text("add a /health endpoint to server.py, only that file, so the LB can probe it"),
            assistant_tool("Read", file_path="/x/server.py"),
            assistant_tool("Edit", file_path="/x/server.py"),
            assistant_tool("Bash", command="python -m pytest -q"),
            user_text("run it"),
        ]
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        result = insight.analyze(corpus)
        self.assertIn(result["band"], [b[0] for b in insight.BANDS])
        self.assertTrue(0 <= result["overall"] <= 100)
        cards, strength = insight.build_action_plan(corpus, result)
        html = insight.build_html(corpus, result, cards, strength)
        self.assertIn("AI Fluency", html)
        self.assertIn("How much data this is based on", html)
        self.assertIn(result["band"], html)


class TestEdgeCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_real_prompts_but_zero_tool_calls_renders(self):
        # regression: zero tool calls used to crash build_html with KeyError: 'evenness'
        write_session(self.tmp, "chat.jsonl", [user_text("hi"), user_text("what can you do?")])
        rc = insight.main([self.tmp, "-o", os.path.join(self.tmp, "r.html"), "--no-open"])
        self.assertEqual(rc, 0)
        html = open(os.path.join(self.tmp, "r.html"), encoding="utf-8").read()
        self.assertIn("AI Fluency", html)
        self.assertNotIn("{", html.split("<style>")[0])  # no template leaks before CSS

    def test_self_authored_file_edit_is_grounded(self):
        # regression: editing a file the agent WROTE this session must count as grounded
        recs = [
            user_text("make a config"),
            assistant_tool("Write", file_path="/x/conf.py"),
            assistant_tool("Edit", file_path="/x/conf.py"),   # never Read — but we wrote it
            assistant_tool("Edit", file_path="/x/conf.py"),
        ]
        write_session(self.tmp, "s.jsonl", recs)
        corpus = insight.parse(insight.discover_files(self.tmp))
        _, detail, blind = insight.score_context(corpus)
        self.assertEqual(detail["rate"], 1.0)
        self.assertEqual(blind, [])

    def test_injected_head_allows_casual_youre(self):
        self.assertFalse(insight._looks_injected("you're right, fix the login bug in auth.py"))
        self.assertTrue(insight._looks_injected("You are a senior engineer. Your task is ..."))

    def test_archetype_reflects_user_not_claude(self):
        # A heavy delegator with terse prompts must read as the Autonomous Agent even when
        # Claude's read-before-edit / verify habits are maxed — those Claude-driven
        # dimensions are agency-discounted.
        dims = {"Direction": 48, "Verification": 100, "Context": 100, "Iteration": 62, "Toolcraft": 84}
        a = insight.classify_archetype(dims, delegation_score=100)
        self.assertEqual(a["primary"], "Autonomous Agent")
        # the same profile with NO delegation should NOT read as the Autonomous Agent
        b = insight.classify_archetype(dims, delegation_score=0)
        self.assertNotEqual(b["primary"], "Autonomous Agent")


class TestArchive(unittest.TestCase):
    """The archive is what lets analysis exceed Claude Code's 30-day on-disk retention."""

    def setUp(self):
        self.live = tempfile.mkdtemp()
        self.arch = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.live, "proj"), exist_ok=True)
        self.f = write_session(os.path.join(self.live, "proj"), "sess.jsonl",
                               [user_text("first prompt")])

    def test_copies_new_then_skips_unchanged_then_updates_on_growth(self):
        new, updated = insight.archive_transcripts([self.f], self.arch)
        self.assertEqual((new, updated), (1, 0))
        dest = os.path.join(self.arch, "proj", "sess.jsonl")
        self.assertTrue(os.path.exists(dest))
        # second run, unchanged -> no copy
        new, updated = insight.archive_transcripts([self.f], self.arch)
        self.assertEqual((new, updated), (0, 0))
        # the live file grows (a new turn) -> archive copy is refreshed
        with open(self.f, "a", encoding="utf-8") as fh:
            fh.write(user_text("second prompt") + "\n")
        new, updated = insight.archive_transcripts([self.f], self.arch)
        self.assertEqual((new, updated), (0, 1))
        self.assertEqual(os.path.getsize(dest), os.path.getsize(self.f))

    def test_archive_never_truncates_on_smaller_live(self):
        # if a fresh (smaller) file ever shadows an older richer archive copy, we keep the big one
        insight.archive_transcripts([self.f], self.arch)
        dest = os.path.join(self.arch, "proj", "sess.jsonl")
        big = os.path.getsize(dest)
        # archive holds the full history; a truncated live copy must NOT shrink it via dedupe
        merged = insight._dedupe_sessions([self.f, dest])
        self.assertEqual(len(merged), 1)

    def test_dedupe_prefers_largest_and_keeps_distinct_sessions(self):
        # same session in two roots, different sizes -> the larger (more complete) wins
        d2 = os.path.join(self.arch, "proj")
        os.makedirs(d2, exist_ok=True)
        small = write_session(d2, "sess.jsonl", [user_text("x")])  # smaller copy of same session
        merged = insight._dedupe_sessions([self.f, small])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0], self.f)  # the bigger live file, not the small archive copy
        # a genuinely different session is preserved
        other = write_session(os.path.join(self.live, "proj"), "other.jsonl", [user_text("y")])
        merged2 = insight._dedupe_sessions([self.f, small, other])
        self.assertEqual(len(merged2), 2)

    def test_main_merges_archive_so_old_sessions_still_count(self):
        # An "old" session that exists ONLY in the archive (Claude Code already deleted the live
        # copy) must still be analyzed. Live dir is empty; the archive supplies the history.
        empty_live = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.arch, "oldproj"), exist_ok=True)
        write_session(os.path.join(self.arch, "oldproj"), "old.jsonl",
                      [user_text("add a /health endpoint to server.py, only that file, so the LB can probe it"),
                       user_text("now run the tests to confirm it works")])
        out = os.path.join(empty_live, "r.html")
        os.environ["CLAUDE_PROJECTS_DIR"] = empty_live  # discover_files reads the empty live dir
        try:
            # no positional path -> archive logic engages; --archive supplies the old session
            rc = insight.main(["--archive", self.arch, "-o", out, "--no-open"])
        finally:
            del os.environ["CLAUDE_PROJECTS_DIR"]
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("AI Fluency", html)
        # the archive-only prompts were actually analyzed (live had none)
        self.assertIn("sessions in your archive", html)

    def test_smaller_live_never_truncates_larger_archive(self):
        # If the live file is SMALLER than the archive (corruption / truncation), the archive
        # must NOT be overwritten — the bigger copy is the more complete history.
        insight.archive_transcripts([self.f], self.arch)
        dest = os.path.join(self.arch, "proj", "sess.jsonl")
        with open(dest, "a", encoding="utf-8") as fh:        # grow the ARCHIVE past live
            fh.write(user_text("extra archived turn that live no longer has") + "\n")
        big = os.path.getsize(dest)
        new, updated = insight.archive_transcripts([self.f], self.arch)
        self.assertEqual((new, updated), (0, 0))             # skipped — archive already bigger
        self.assertEqual(os.path.getsize(dest), big)         # archive untouched

    def test_dedupe_survives_project_folder_rename(self):
        # Same session (same UUID filename) under two DIFFERENT project folders must dedupe to one.
        a = write_session(os.path.join(self.live, "proj"), "uuid-1.jsonl", [user_text("one"), user_text("two")])
        d2 = os.path.join(self.arch, "renamed-proj")
        os.makedirs(d2, exist_ok=True)
        b = write_session(d2, "uuid-1.jsonl", [user_text("one")])   # smaller copy, different folder
        merged = insight._dedupe_sessions([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0], a)                       # the larger one wins

    def test_no_archive_flag_does_not_write(self):
        live_dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(live_dir, "proj"), exist_ok=True)
        write_session(os.path.join(live_dir, "proj"), "s.jsonl",
                      [user_text("add a /health route to server.py and run the tests")])
        out = os.path.join(live_dir, "r.html")
        os.environ["CLAUDE_PROJECTS_DIR"] = live_dir
        try:
            rc = insight.main(["--no-archive", "--archive", self.arch, "-o", out, "--no-open"])
        finally:
            del os.environ["CLAUDE_PROJECTS_DIR"]
        self.assertEqual(rc, 0)
        self.assertEqual(glob.glob(os.path.join(self.arch, "**", "*.jsonl"), recursive=True), [])

    def test_explicit_path_does_not_touch_archive(self):
        # Seed an archive, then analyze an explicit dir: the archive must be neither written nor merged.
        os.makedirs(os.path.join(self.arch, "old"), exist_ok=True)
        write_session(os.path.join(self.arch, "old"), "old.jsonl", [user_text("archived only")])
        before = sorted(glob.glob(os.path.join(self.arch, "**", "*.jsonl"), recursive=True))
        explicit = tempfile.mkdtemp()
        write_session(explicit, "live.jsonl",
                      [user_text("add a /health route to server.py and run the tests")])
        out = os.path.join(explicit, "r.html")
        rc = insight.main([explicit, "--archive", self.arch, "-o", out, "--no-open"])
        self.assertEqual(rc, 0)
        after = sorted(glob.glob(os.path.join(self.arch, "**", "*.jsonl"), recursive=True))
        self.assertEqual(before, after)                      # archive untouched
        with open(out, encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn("sessions in your archive", html)   # archive not merged into analysis


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_subagent_transcripts_are_excluded(self):
        # Agent-to-agent transcripts under .../subagents/... are NOT the user's prompts and
        # must not be discovered — otherwise running workflows would inflate the analysis.
        proj = os.path.join(self.tmp, "proj")
        sub = os.path.join(proj, "uuid", "subagents")
        os.makedirs(sub, exist_ok=True)
        main_f = write_session(proj, "main.jsonl", [user_text("a real user prompt about server.py")])
        sub_f = write_session(sub, "agent-x.jsonl", [user_text("do the assigned subtask")])
        found = insight.discover_files(self.tmp)
        self.assertIn(main_f, found)
        self.assertNotIn(sub_f, found)

    def test_explicit_single_subagent_file_is_still_honored(self):
        sub = os.path.join(self.tmp, "uuid", "subagents")
        os.makedirs(sub, exist_ok=True)
        sub_f = write_session(sub, "agent-x.jsonl", [user_text("explicitly requested file")])
        self.assertEqual(insight.discover_files(sub_f), [sub_f])


class TestPipelineModes(unittest.TestCase):
    """The --evidence (pipeline input) and --analysis (Opus output → report) hooks."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        write_session(self.tmp, "s.jsonl", [
            user_text("add a /health endpoint to server.py, only that file, so the LB can probe it"),
            assistant_tool("Read", file_path="/x/server.py"),
            assistant_tool("Edit", file_path="/x/server.py"),
            assistant_tool("Bash", command="python -m pytest -q"),
            user_text("run it and tell me if it passes"),
        ])

    def test_evidence_bundle_is_valid_and_self_contained(self):
        ev = os.path.join(self.tmp, "ev.json")
        rc = insight.main([self.tmp, "--evidence", ev, "--no-open", "-o", os.path.join(self.tmp, "r.html")])
        self.assertEqual(rc, 0)
        with open(ev, encoding="utf-8") as fh:
            d = json.load(fh)
        self.assertEqual(d["schema"], "claude-insight-evidence/1")
        for k in ("meta", "scores", "dimension_detail", "behavior", "archetype"):
            self.assertIn(k, d)
        self.assertGreaterEqual(len(d["behavior"]["sample_prompts"]), 1)
        self.assertIn("Direction", d["behavior"]["weak_examples"])
        # evidence must carry file basenames, never absolute paths
        for items in d["behavior"]["weak_examples"].values():
            for e in items:
                self.assertNotIn("/", e.get("file", ""))

    def test_analysis_json_merges_into_report(self):
        analysis = {
            "overall_read": "You hand off whole jobs well; sharpen your briefs next.",
            "skill_map": [
                {"competency": "Delegation", "level": 4, "level_label": "Advanced",
                 "summary": "Hands off end to end.", "evidence": ["one scoped hand-off"],
                 "next_move": "add one sentence of intent per hand-off"},
                {"competency": "Description", "level": 2, "level_label": "Developing",
                 "summary": "Often terse.", "evidence": ["'run it'"],
                 "next_move": "name a file + a constraint"},
            ],
            "top_growth": [{"title": "Brief better", "why": "fewer rounds", "how": "front-load intent",
                            "example_before": "run it", "example_after": "run the server.py tests; report failures"}],
            "strengths": ["clear delegation"],
        }
        ap = os.path.join(self.tmp, "an.json")
        with open(ap, "w", encoding="utf-8") as fh:
            json.dump(analysis, fh)
        out = os.path.join(self.tmp, "r.html")
        rc = insight.main([self.tmp, "--analysis", ap, "--no-open", "-o", out])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("analyzed against the AI Fluency framework", html)
        self.assertIn("Delegation", html)
        self.assertIn("Advanced", html)
        self.assertIn("name a file + a constraint", html)

    def test_report_without_analysis_has_no_ai_section(self):
        out = os.path.join(self.tmp, "r.html")
        rc = insight.main([self.tmp, "--no-open", "-o", out])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn("analyzed against the AI Fluency framework", html)


# --------------------------------------------------------------------------- #
# Multi-source adapters
# --------------------------------------------------------------------------- #

def codex_line(typ, payload, ts="2026-02-01T00:00:00Z"):
    return json.dumps({"type": typ, "timestamp": ts, "payload": payload})


def desktop_line(**kw):
    return json.dumps(kw)


class TestAdapterContract(unittest.TestCase):
    def test_registry_and_capabilities(self):
        self.assertEqual(set(insight.ADAPTERS), {"claude-code", "claude-desktop", "codex", "cursor"})
        for name, ad in insight.ADAPTERS.items():
            self.assertTrue(callable(ad.detect))
            self.assertTrue(callable(ad.discover))
            self.assertTrue(callable(ad.iter_events))
            self.assertEqual(set(ad.capabilities), {"prompts", "edits", "verify", "reads", "delegation"})
        # only Claude Code archives (others would collide on non-unique filenames / are cumulative)
        self.assertTrue(insight.ADAPTERS["claude-code"].archive_enabled)
        for n in ("claude-desktop", "codex", "cursor"):
            self.assertFalse(insight.ADAPTERS[n].archive_enabled)

    def test_default_parse_is_claude_code_and_unchanged(self):
        # parse(files) with no adapter must behave exactly like the Claude Code adapter
        tmp = tempfile.mkdtemp()
        recs = [user_text("add a /health route to server.py"),
                assistant_tool("Read", file_path="/x/server.py"),
                assistant_tool("Edit", file_path="/x/server.py"),
                assistant_tool("Bash", command="pytest -q")]
        write_session(tmp, "s.jsonl", recs)
        files = insight.discover_files(tmp)
        a = insight.parse(files)
        b = insight.parse(files, insight.ClaudeCodeAdapter)
        self.assertEqual(len(a.real_prompts), len(b.real_prompts))
        self.assertEqual(dict(a.tool_usage), dict(b.tool_usage))
        self.assertEqual(a.delegation_events, b.delegation_events)
        # tool_usage preserves original case (Bash/Read/Edit), timeline lowercases
        self.assertIn("Bash", a.tool_usage)
        self.assertEqual(len(a.real_prompts), 1)


class TestPhase0Lock(unittest.TestCase):
    """Lock the refactor: a rich Claude Code fixture must parse to exact, known values."""

    def test_rich_corpus_values(self):
        tmp = tempfile.mkdtemp()
        recs = [
            user_text("add a /health endpoint to server.py, only that file", ts="2026-01-01T00:00:00Z"),
            user_tool_result(ts="2026-01-01T00:00:05Z"),                       # filtered
            _rec(type="assistant", timestamp="2026-01-01T00:00:10Z",          # multi-tool record
                 message={"role": "assistant", "content": [
                     {"type": "text", "text": "ok"},
                     {"type": "tool_use", "name": "Read", "input": {"file_path": "/x/server.py"}},
                     {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/server.py"}}]}),
            _rec(type="assistant", timestamp="2026-01-01T00:00:20Z",          # assistant text-only (ts only)
                 message={"role": "assistant", "content": [{"type": "text", "text": "done"}]}),
            assistant_tool("Bash", ts="2026-01-01T00:00:30Z", command="pytest -q"),
            assistant_tool("Task", ts="2026-01-01T00:00:40Z"),                # delegation
            user_text("run it", ts="2026-01-01T00:00:50Z"),
        ]
        write_session(tmp, "s.jsonl", recs)
        c = insight.parse(insight.discover_files(tmp))
        self.assertEqual(len(c.real_prompts), 2)
        self.assertEqual(c.user_records, 3)                  # 2 real + 1 tool-result
        self.assertEqual(c.filtered["tool results"], 1)
        self.assertEqual(c.total_tool_calls, 4)              # Read, Edit, Bash, Task
        self.assertEqual(c.delegation_events, 1)             # Task
        self.assertEqual(c.tool_usage["Read"], 1)
        self.assertEqual(len(c.sessions), 1)
        # active time spans 50s of small gaps, never the idle cap
        self.assertGreater(c.active_seconds, 0)
        self.assertLessEqual(c.active_seconds, 50)


class TestCodexAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        lines = [
            codex_line("session_meta", {"id": "sess1", "cwd": "/Users/x/proj",
                                        "git": {"repository_url": "https://github.com/me/myrepo.git"}}),
            codex_line("response_item", {"type": "message", "role": "user",
                                         "content": [{"type": "input_text", "text": "build a parser in parser.py"}]}),
            codex_line("response_item", {"type": "message", "role": "developer",
                                         "content": [{"type": "input_text", "text": "<permissions instructions> ..."}]}),
            codex_line("response_item", {"type": "message", "role": "user",
                                         "content": [{"type": "input_text", "text": "<environment_context>\ncwd: /Users/x\n</environment_context>"}]}),
            codex_line("response_item", {"type": "function_call", "name": "exec_command",
                                         "arguments": json.dumps({"cmd": "pytest -q", "workdir": "/Users/x/proj"})}),
            codex_line("response_item", {"type": "custom_tool_call", "name": "apply_patch",
                                         "input": "*** Begin Patch\n*** Add File: /Users/x/proj/new.py\n+print(1)\n*** Update File: parser.py\n@@\n-old\n+new\n*** End Patch"}),
            codex_line("response_item", {"type": "function_call", "name": "update_plan",
                                         "arguments": json.dumps({"plan": [{"step": "a", "status": "completed"}]})}),
            codex_line("response_item", {"type": "web_search_call", "action": {"type": "search", "query": "python json"}}),
            codex_line("response_item", {"type": "reasoning", "summary": [], "encrypted_content": "gAAAA"}),
        ]
        write_session(self.tmp, "rollout-2026-02-01T00-00-00-sess1.jsonl", lines)

    def test_parses_prompts_tools_and_decontaminates(self):
        files = insight.CodexAdapter.discover(self.tmp)
        self.assertEqual(len(files), 1)
        c = insight.parse(files, insight.CodexAdapter)
        # only the one clean user message survives; developer is skipped, env-context dropped
        self.assertEqual(len(c.real_prompts), 1)
        self.assertEqual(c.real_prompts[0]["text"], "build a parser in parser.py")
        self.assertGreaterEqual(c.filtered["injected / pasted"], 1)
        merged = {k.lower(): v for k, v in c.tool_usage.items()}
        self.assertEqual(merged.get("bash"), 1)        # exec_command
        self.assertEqual(merged.get("write"), 1)       # Add File
        self.assertEqual(merged.get("edit"), 1)        # Update File
        self.assertEqual(merged.get("web_search"), 1)
        self.assertGreaterEqual(c.delegation_events, 1)  # update_plan -> enterplanmode
        self.assertEqual(sorted(c.projects), ["myrepo"])

    def test_codex_context_is_not_measurable(self):
        files = insight.CodexAdapter.discover(self.tmp)
        c = insight.parse(files, insight.CodexAdapter)
        result = insight.analyze(c, insight.CodexAdapter.capabilities)
        self.assertIn("Context", result["na_dims"])
        self.assertNotIn("Context", result["measurable"])
        cards, strength = insight.build_action_plan(c, result)
        self.assertNotIn("Context", [card["dim"] for card in cards])
        html = insight.build_html(c, result, cards, strength, source="codex",
                                  capabilities=insight.CodexAdapter.capabilities)
        self.assertIn("not measurable", html.lower())
        self.assertIn("OpenAI Codex CLI", html)


class TestDesktopAdapter(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        sess = os.path.join(self.root, "sess1")
        os.makedirs(sess, exist_ok=True)
        lines = [
            desktop_line(type="system", subtype="init", cwd="/Users/x/proj",
                         session_id="s1", timestamp="2026-03-01T00:00:00Z"),
            desktop_line(type="system", subtype="status", timestamp="2026-03-01T00:00:01Z"),
            desktop_line(type="user", timestamp="2026-03-01T00:00:02Z",
                         message={"role": "user", "content": "add a /health route to server.py"}),
            desktop_line(type="user", timestamp="2026-03-01T00:00:03Z", tool_use_result={"x": 1},
                         message={"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}),
            desktop_line(type="user", timestamp="2026-03-01T00:00:04Z", isReplay=True,
                         message={"role": "user", "content": "add a /health route to server.py"}),
            desktop_line(type="assistant", timestamp="2026-03-01T00:00:05Z",
                         message={"role": "assistant", "content": [
                             {"type": "tool_use", "name": "mcp__workspace__bash", "input": {"command": "pytest -q"}}]}),
            desktop_line(type="assistant", timestamp="2026-03-01T00:00:06Z",
                         message={"role": "assistant", "content": [
                             {"type": "tool_use", "name": "Edit", "input": {"file_path": "/Users/x/server.py"}}]}),
            desktop_line(type="result", timestamp="2026-03-01T00:00:07Z",
                         permission_denials=[{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {}}]),
            desktop_line(type="tool_use_summary", summary="ran tests", _audit_timestamp="2026-03-01T00:00:08Z"),
        ]
        write_session(sess, "audit.jsonl", lines)

    def test_parses_and_decontaminates(self):
        files = insight.ClaudeDesktopAdapter.discover(self.root)
        self.assertEqual(len(files), 1)
        c = insight.parse(files, insight.ClaudeDesktopAdapter)
        self.assertEqual(len(c.real_prompts), 1)            # replay + tool_result dropped
        self.assertEqual(c.real_prompts[0]["text"], "add a /health route to server.py")
        self.assertGreaterEqual(c.filtered["tool results"], 1)
        self.assertGreaterEqual(c.filtered["replays"], 1)
        merged = {k.lower(): v for k, v in c.tool_usage.items()}
        self.assertEqual(merged.get("bash"), 1)             # mcp__workspace__bash -> bash
        self.assertEqual(merged.get("edit"), 1)
        self.assertEqual(c.signals["permission_denials"], 1)
        self.assertEqual(sorted(c.projects), ["claude-desktop"])


class TestCursorAdapter(unittest.TestCase):
    def _make_db(self):
        import sqlite3
        d = tempfile.mkdtemp()
        db = os.path.join(d, "state.vscdb")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
        composer = {"composerId": "c1", "isAgentic": True,
                    "fullConversationHeadersOnly": [
                        {"bubbleId": "b1", "type": 1}, {"bubbleId": "b2", "type": 2},
                        {"bubbleId": "b3", "type": 2}, {"bubbleId": "b4", "type": 2}]}
        rows = {
            "composerData:c1": composer,
            "bubbleId:c1:b1": {"type": 1, "text": "refactor utils.py to add caching"},
            "bubbleId:c1:b2": {"type": 2, "toolFormerData": {
                "name": "run_terminal_cmd", "params": {"command": "pytest -q"}, "userDecision": "approved"}},
            "bubbleId:c1:b3": {"type": 2, "toolFormerData": {
                "name": "read_file", "params": {"target_file": "/Users/x/utils.py"}}},
            "bubbleId:c1:b4": {"type": 2, "toolFormerData": {
                "name": "edit_file", "params": {"target_file": "/Users/x/utils.py"}, "userDecision": "rejected"}},
        }
        for k, v in rows.items():
            conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (k, json.dumps(v)))
        conn.commit()
        conn.close()
        return db

    def test_parses_composer_bubbles(self):
        db = self._make_db()
        before = os.path.getsize(db)
        files = insight.CursorAdapter.discover(db)
        self.assertEqual(files, [db])
        c = insight.parse(files, insight.CursorAdapter)
        self.assertEqual(len(c.real_prompts), 1)
        self.assertEqual(c.real_prompts[0]["text"], "refactor utils.py to add caching")
        merged = {k.lower(): v for k, v in c.tool_usage.items()}
        self.assertEqual(merged.get("bash"), 1)             # run_terminal_cmd
        self.assertEqual(merged.get("read"), 1)
        self.assertEqual(merged.get("edit"), 1)
        self.assertEqual(c.signals["tool_rejections"], 1)   # userDecision == rejected
        self.assertEqual(sorted(c.projects), ["cursor"])
        # read-only: the source DB must be untouched (we copy before reading)
        self.assertEqual(os.path.getsize(db), before)

    def test_reads_live_wal_mode_db(self):
        # A live Cursor DB is WAL-mode with uncheckpointed data in the -wal sidecar; the adapter
        # must copy the sidecar and read it (regression for "copy main only + immutable=1" which
        # silently returned zero data).
        import sqlite3
        d = tempfile.mkdtemp()
        db = os.path.join(d, "state.vscdb")
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")     # keep data in the -wal sidecar
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
        rows = {
            "composerData:c1": {"fullConversationHeadersOnly": [
                {"bubbleId": "b1", "type": 1}, {"bubbleId": "b2", "type": 2}]},
            "bubbleId:c1:b1": {"type": 1, "text": "add caching to the API client"},
            "bubbleId:c1:b2": {"type": 2, "toolFormerData": {
                "name": "run_terminal_cmd", "params": {"command": "pytest -q"}}},
        }
        for k, v in rows.items():
            conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (k, json.dumps(v)))
        conn.commit()
        try:
            # do NOT close: simulate a running Cursor with an uncheckpointed WAL
            self.assertTrue(os.path.exists(db + "-wal"))
            c = insight.parse(insight.CursorAdapter.discover(db), insight.CursorAdapter)
            self.assertEqual(len(c.real_prompts), 1)
            self.assertIn("bash", {k.lower() for k in c.tool_usage})
        finally:
            conn.close()


class TestPathScrub(unittest.TestCase):
    def test_scrub_paths_strips_username(self):
        self.assertEqual(insight._scrub_paths("see /Users/jane/proj/a.py please"), "see ~/proj/a.py please")
        self.assertEqual(insight._scrub_paths("/home/bob/x/y.txt"), "~/x/y.txt")
        self.assertEqual(insight._normalize_path("/Users/jane/proj/a.py"), "~/proj/a.py")
        self.assertEqual(insight._normalize_path("relative/a.py"), "relative/a.py")

    def test_scrub_bare_home_and_user_at_host(self):
        # bare home dir (no trailing child) must still be scrubbed
        self.assertEqual(insight._scrub_paths("my home is /Users/jane and repo /home/bob too"),
                         "my home is ~ and repo ~ too")
        self.assertEqual(insight._normalize_path("/Users/jane"), "~")
        self.assertEqual(insight._normalize_path("/home/bob"), "~")
        # pasted shell prompts leak user@host — redact it
        self.assertEqual(insight._scrub_paths("(env) jane@Janes-MBP repo % ls"),
                         "(env) <user>@<host> repo % ls")
        # home-root project label must not echo the username
        self.assertEqual(insight._project_label("-Users-jane"), "home")

    def test_no_home_path_leaks_into_evidence(self):
        # A Codex session whose prompt text AND patch embed absolute home paths must not leak them.
        tmp = tempfile.mkdtemp()
        lines = [
            codex_line("session_meta", {"id": "s", "cwd": "/Users/jane/proj"}),
            codex_line("response_item", {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "fix the bug, see logs in /Users/jane/proj/run.log"}]}),
            codex_line("response_item", {"type": "custom_tool_call", "name": "apply_patch",
                                         "input": "*** Begin Patch\n*** Update File: /Users/jane/proj/a.py\n@@\n-x\n+y\n*** End Patch"}),
        ]
        write_session(tmp, "rollout-x.jsonl", lines)
        c = insight.parse(insight.CodexAdapter.discover(tmp), insight.CodexAdapter)
        result = insight.analyze(c, insight.CodexAdapter.capabilities)
        cards, _ = insight.build_action_plan(c, result)
        ev = insight.build_evidence(c, result, cards, source="codex",
                                    capabilities=insight.CodexAdapter.capabilities)
        blob = json.dumps(ev)
        self.assertNotIn("/Users/", blob)
        self.assertNotIn("/home/", blob)


class TestMultiSourceCLI(unittest.TestCase):
    def test_explicit_source_routes_to_adapter(self):
        tmp = tempfile.mkdtemp()
        lines = [
            codex_line("session_meta", {"id": "s", "cwd": "/Users/x/proj"}),
            codex_line("response_item", {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "add a parser to parser.py and run pytest"}]}),
            codex_line("response_item", {"type": "function_call", "name": "exec_command",
                                         "arguments": json.dumps({"cmd": "pytest -q"})}),
        ]
        write_session(tmp, "rollout-x.jsonl", lines)
        out = os.path.join(tmp, "r.html")
        rc = insight.main(["--source", "codex", tmp, "-o", out, "--no-open"])
        self.assertEqual(rc, 0)
        html = open(out, encoding="utf-8").read()
        self.assertIn("AI Fluency", html)
        self.assertIn("OpenAI Codex CLI", html)

    def test_source_all_rejects_explicit_path(self):
        # 'all' reads each source's standard location; combining it with a path is ambiguous
        rc = insight.main(["--source", "all", "/tmp", "--no-open"])
        self.assertEqual(rc, 2)

    def test_json_carries_source_and_capabilities(self):
        tmp = tempfile.mkdtemp()
        lines = [
            codex_line("session_meta", {"id": "s", "cwd": "/Users/x/proj"}),
            codex_line("response_item", {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "add a parser to parser.py"}]}),
        ]
        write_session(tmp, "rollout-x.jsonl", lines)
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = insight.main(["--source", "codex", tmp, "--json", "--no-open"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["source"], "codex")
        self.assertIn("Context", payload["not_measurable"])
        self.assertFalse(payload["capabilities"]["reads"])


class TestCombinedReport(unittest.TestCase):
    def _entry(self, files, adapter):
        c = insight.parse(files, adapter)
        r = insight.analyze(c, adapter.capabilities)
        cards, strength = insight.build_action_plan(c, r)
        return {"source": adapter.name, "corpus": c, "result": r, "cards": cards, "strength": strength}

    def test_combined_renders_all_sources_and_skill_map(self):
        tmp = tempfile.mkdtemp()
        cc = os.path.join(tmp, "cc"); os.makedirs(cc)
        write_session(cc, "s.jsonl", [
            user_text("add a /health endpoint to server.py, only that file"),
            assistant_tool("Read", file_path="/x/server.py"),
            assistant_tool("Edit", file_path="/x/server.py"),
            assistant_tool("Bash", command="pytest -q"),
            user_text("run it")])
        e1 = self._entry(insight.ClaudeCodeAdapter.discover(cc), insight.ClaudeCodeAdapter)
        cx = os.path.join(tmp, "cx"); os.makedirs(cx)
        write_session(cx, "rollout-x.jsonl", [
            codex_line("session_meta", {"id": "s", "cwd": "/Users/x/p"}),
            codex_line("response_item", {"type": "message", "role": "user",
                                         "content": [{"type": "input_text", "text": "build a parser in parser.py"}]}),
            codex_line("response_item", {"type": "function_call", "name": "exec_command",
                                         "arguments": json.dumps({"cmd": "pytest -q"})})])
        e2 = self._entry(insight.CodexAdapter.discover(cx), insight.CodexAdapter)
        analysis = {
            "overall_read": "You delegate fluently; sharpen your briefs.",
            "skill_map": [
                {"competency": "Delegation", "level": 4, "level_label": "Advanced",
                 "summary": "Hands off whole jobs.", "evidence": ["e2e plans"], "next_move": "add a success line"},
                {"competency": "Description", "level": 2, "level_label": "Developing",
                 "summary": "Terse.", "evidence": ["'run it'"], "next_move": "name a file + a constraint"},
                {"competency": "Discernment", "level": 4, "level_label": "Advanced",
                 "summary": "Verifies.", "evidence": ["pytest"], "next_move": "carry into Desktop"},
                {"competency": "Diligence", "level": 3, "level_label": "Proficient",
                 "summary": "Owns it.", "evidence": ["teardown"], "next_move": "verify before live"}],
            "top_growth": [{"title": "Front-load one anchor", "why": "fewer rounds", "how": "name a file",
                            "example_before": "run it", "example_after": "run pytest -q and paste output"}],
            "strengths": ["elite delegation"]}
        html = insight.build_combined_html([e1, e2], analysis)
        for tok in ("Claude Code", "OpenAI Codex CLI", "Per-tool breakdown", "Platzi",
                    "analyzed against the AI Fluency framework", "Front-load one anchor", "Delegation"):
            self.assertIn(tok, html)
        self.assertNotIn("/Users/", html)
        # deterministic fallback (no analysis) still renders and points to /ai-fluency
        html2 = insight.build_combined_html([e1, e2], None)
        self.assertIn("/ai-fluency", html2)
        self.assertIn("Per-tool breakdown", html2)


if __name__ == "__main__":
    unittest.main()
