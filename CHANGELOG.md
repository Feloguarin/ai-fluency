# Changelog

All notable changes to AI Fluency are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/), and the
project aims for [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Continuous integration: the test suite runs on every push to `main` and every
  pull request, across Python 3.8 / 3.10 / 3.12. Nothing merges red.
- `LICENSE` file (MIT) — the README already declared MIT; this makes it real.
- This changelog.

### Changed
- **Renamed: Claude Insight is now AI Fluency.** New name across the README, landing
  page, report, CLI help, and installer; the repo moved to `Feloguarin/ai-fluency`
  (old git and install URLs redirect). Data paths (`~/.claude/insight/`,
  `~/.claude/insight-archive`), the `CLAUDE_INSIGHT_ARCHIVE` env var, and the
  evidence schema id are intentionally unchanged, so existing installs and archives
  keep working.
- The installer now downloads the **latest tagged release** instead of bleeding-edge
  `main`, so a work-in-progress commit on `main` can never break a fresh install.
  It falls back to `main` only if no release exists.

## [1.0.0] — 2026-06-19

First tagged release — the known-good baseline existing users can pin to.

### Added
- Single-file, pure-stdlib engine (`insight.py`): discover → de-contaminate →
  score → report.
- `/ai-fluency` skill and the Sonnet 4.6 → Opus 4.8 workflow.
- 0–100 fluency score, builder archetype, 4-competency skill map, five measured
  dimensions, and personalized growth levers.
- Built-in private archive so analysis can see beyond Claude Code's 30-day window.
- 38 passing tests.

[Unreleased]: https://github.com/Feloguarin/ai-fluency/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Feloguarin/ai-fluency/releases/tag/v1.0.0
