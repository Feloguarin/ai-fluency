"""
Local LLM Analyzer (Ollama)

Uses a small, locally-run model (default: Gemma 3 4B) to produce the
*qualitative* part of the builder profile — archetype, a written summary,
and personalized recommendations.

This keeps Claude Insight 100% local and private: the model runs on your own
machine via Ollama, so transcripts never leave it and no API key is needed.
The numeric metrics are still computed deterministically in metrics.py — small
models aren't reliable at inventing consistent scores, so we only ask the model
for judgement and prose.

Talks to Ollama over its local HTTP API using the standard library only
(no extra pip dependencies). If Ollama isn't running or the model isn't
installed, callers fall back to the heuristic analysis.
"""

import json
import os
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional


# Archetype names the model must choose from, mapped to their display labels.
ARCHETYPE_LABELS = {
    "Architect": "🏗️ Architect",
    "Sprinter": "⚡ Sprinter",
    "Debugger": "🐛 Debugger",
    "Collaborator": "🤝 Collaborator",
    "Autonomous Agent": "🤖 Autonomous Agent",
}

DEFAULT_MODEL = "gemma3:4b"
DEFAULT_HOST = "http://localhost:11434"

# Keep the prompt small so a 4B model stays fast and in-context.
MAX_SAMPLE_PROMPTS = 40
MAX_SAMPLE_CHARS = 6000


@dataclass
class LLMInsights:
    """Qualitative analysis produced by the local model."""
    archetype: str = ""             # display label, e.g. "🏗️ Architect"
    archetype_reason: str = ""
    summary: str = ""
    recommendations: list = field(default_factory=list)
    model: str = ""


# JSON schema Ollama constrains the model's output to (structured outputs).
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "archetype": {"type": "string", "enum": list(ARCHETYPE_LABELS.keys())},
        "archetype_reason": {"type": "string"},
        "summary": {"type": "string"},
        "recommendations": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["archetype", "archetype_reason", "summary", "recommendations"],
}


class LocalLLMAnalyzer:
    """Analyzes builder behavior with a local Ollama model."""

    def __init__(self, model: Optional[str] = None, host: Optional[str] = None,
                 timeout: float = 120.0):
        self.model = model or os.environ.get("CLAUDE_INSIGHT_MODEL", DEFAULT_MODEL)
        self.host = (host or os.environ.get("OLLAMA_HOST", DEFAULT_HOST)).rstrip("/")
        # OLLAMA_HOST is sometimes just "host:port" — normalize to a URL.
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.timeout = timeout

    def is_available(self) -> bool:
        """True if Ollama is reachable and the model is installed."""
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return False

        installed = {m.get("name", "") for m in data.get("models", [])}
        # Accept an exact match or the same model without an explicit ":latest".
        return any(
            name == self.model or name.split(":")[0] == self.model.split(":")[0]
            for name in installed
        )

    def analyze(self, metrics, sample_prompts: list) -> Optional[LLMInsights]:
        """Ask the local model for a qualitative profile. Returns None on failure."""
        user_prompt = self._build_prompt(metrics, sample_prompts)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": _RESPONSE_SCHEMA,
            "options": {"temperature": 0.3},
        }

        try:
            req = urllib.request.Request(
                f"{self.host}/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body.get("message", {}).get("content", "")
            parsed = json.loads(content)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError, KeyError):
            return None

        archetype_key = parsed.get("archetype", "")
        recs = parsed.get("recommendations", [])
        if not isinstance(recs, list):
            recs = []

        return LLMInsights(
            archetype=ARCHETYPE_LABELS.get(archetype_key, archetype_key),
            archetype_reason=str(parsed.get("archetype_reason", "")).strip(),
            summary=str(parsed.get("summary", "")).strip(),
            recommendations=[str(r).strip() for r in recs if str(r).strip()][:3],
            model=self.model,
        )

    def _system_prompt(self) -> str:
        return (
            "You are an analyst that profiles how a developer collaborates with "
            "AI coding tools. You are given aggregate statistics and a sample of "
            "the developer's prompts. Classify them into exactly one builder "
            "archetype, justify it briefly, write a concise 2-3 sentence profile "
            "summary, and give up to 3 specific, actionable recommendations to "
            "improve how they work with AI. Be concrete and reference their actual "
            "behavior. Respond only with JSON matching the requested schema."
        )

    def _build_prompt(self, metrics, sample_prompts: list) -> str:
        # Trim the prompt sample to keep the request small and fast.
        sample = []
        total = 0
        for p in sample_prompts:
            p = p.strip()
            if not p:
                continue
            if total + len(p) > MAX_SAMPLE_CHARS or len(sample) >= MAX_SAMPLE_PROMPTS:
                break
            sample.append(p)
            total += len(p)

        top_tools = sorted(
            metrics.tool_usage.items(), key=lambda x: x[1], reverse=True
        )[:6]
        tools_str = ", ".join(f"{t} ({c})" for t, c in top_tools) or "none recorded"

        stats = (
            f"Sessions analyzed: {metrics.total_sessions}\n"
            f"Total prompts: {metrics.total_prompts}\n"
            f"Total tool calls: {metrics.total_tool_calls}\n"
            f"Avg prompt length: {int(metrics.avg_prompt_length)} chars "
            f"({metrics.avg_prompt_words:.0f} words)\n"
            f"Avg session duration: {int(metrics.avg_session_duration)} min\n"
            f"Most used tools: {tools_str}\n"
            f"Heuristic dimension scores (0-100): "
            f"Steering {int(metrics.steering_score)}, "
            f"Execution {int(metrics.execution_score)}, "
            f"Engineering {int(metrics.engineering_score)}, "
            f"Product {int(metrics.product_score)}, "
            f"Planning {int(metrics.planning_score)}"
        )

        archetype_help = (
            "Archetype definitions:\n"
            "- Architect: plans and designs extensively before building.\n"
            "- Sprinter: high velocity, rapid iteration, direct action.\n"
            "- Debugger: methodical problem-solving and error-hunting.\n"
            "- Collaborator: seeks alignment, asks for opinions and reviews.\n"
            "- Autonomous Agent: delegates end-to-end workflows."
        )

        prompts_block = "\n".join(f"- {p}" for p in sample) or "(no prompts available)"

        return (
            f"{archetype_help}\n\n"
            f"=== Aggregate statistics ===\n{stats}\n\n"
            f"=== Sample of the developer's prompts ===\n{prompts_block}\n"
        )
