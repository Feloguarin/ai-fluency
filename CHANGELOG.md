# Changelog

All notable changes to Claude Insight are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/), and the
project aims for [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- **The score now actually measures the AI Fluency framework.** The four 4D
  competencies — Delegation, Description, Discernment, Diligence — are computed
  deterministically from measured signals, and the headline score is their weighted
  blend (25/30/25/20). Previously the 4Ds were only estimated by the optional LLM
  stage, Delegation was absent from the score entirely, and Diligence was a
  5-point teardown bonus.
- Two new measured signals feed the competencies: **Delegation** (hand-offs per
  active hour plus hand-off *depth* — the median number of agent actions each
  action-prompt buys before the user steers again, so whole-job hand-offs beat
  micro-stepping) and **Ship-gating** (commits/pushes/deploys gated by a
  verification that ran after the last edit; no ship events → neutral and hedged).
- Briefing/Direction now also measures the framework's process- and
  performance-description sub-skills (ordering the steps, shaping the output),
  not just product cues.
- The report's skill map, the evidence bundle (`scores.competencies`), `--json`,
  and the terminal summary all carry the measured competency levels; the AI
  analysis stage is told to reconcile with them instead of inventing levels.

### Added
- Continuous integration: the test suite runs on every push to `main` and every
  pull request, across Python 3.8 / 3.10 / 3.12. Nothing merges red.
- `LICENSE` file (MIT) — the README already declared MIT; this makes it real.
- This changelog.

### Changed
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

[Unreleased]: https://github.com/Feloguarin/claude-insight/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Feloguarin/claude-insight/releases/tag/v1.0.0
