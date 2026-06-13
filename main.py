#!/usr/bin/env python3
"""
Claude Insight — Private AI Builder Profiler

Convenience launcher. The real entry point lives in claude_insight.cli,
which is also exposed via `python -m claude_insight` and the
`claude-insight` console script.
"""

from claude_insight.cli import main

if __name__ == "__main__":
    main()
