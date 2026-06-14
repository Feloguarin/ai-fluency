"""
Tests for the v2 single-file engine (insight.py).

Focus: the accuracy guarantees that v1 violated — prompt de-contamination,
rate-based scoring that can't be inflated by volume, gap-capped active time,
and confidence shrinkage of thin signals. Pure stdlib unittest.
"""
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
            user_text("You are a senior engineer wiring a Kalshi bot. " + "x" * 50),  # subagent leak
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
        # A heavy delegator with terse prompts must read as a delegator (Director/
        # Orchestrator) even when Claude's read-before-edit / verify habits are maxed —
        # those Claude-driven dimensions are agency-discounted.
        dims = {"Direction": 48, "Verification": 100, "Context": 100, "Iteration": 62, "Toolcraft": 84}
        a = insight.classify_archetype(dims, delegation_score=100)
        self.assertIn(a["primary"], ("The Director", "The Orchestrator"))
        self.assertNotEqual(a["primary"], "The Craftsman")
        # the same profile with NO delegation should NOT be a delegator archetype
        b = insight.classify_archetype(dims, delegation_score=0)
        self.assertNotIn(b["primary"], ("The Director", "The Orchestrator"))


if __name__ == "__main__":
    unittest.main()
