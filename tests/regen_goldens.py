#!/usr/bin/env python3
"""Regenerate tests/goldens_claude_code.json from the CURRENT engine.

Only run this to bless an INTENTIONAL behavior change to the claude-code
pipeline — the golden exists to catch unintentional drift (it was first
generated from the pre-adapter-refactor engine).
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import insight  # noqa: E402
from test_insight import TestClaudeCodeGolden  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    TestClaudeCodeGolden.build_fixture(td)
    files = insight.discover_files(td)
    corpus = insight.parse(files)
    result = insight.analyze(corpus)
    snap = TestClaudeCodeGolden.snapshot(corpus, result)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "goldens_claude_code.json")
with open(out, "w", encoding="utf-8") as fh:
    json.dump(snap, fh, indent=2, sort_keys=True)
    fh.write("\n")
print(f"wrote {out} ({os.path.getsize(out)} bytes, {len(files)} fixture sessions)")
