"""
Metrics Analyzer for Claude Code Sessions

Computes productivity and style metrics from parsed transcripts.
All calculations are local — no external APIs or services.
"""

from dataclasses import dataclass, field
from typing import Optional
from collections import Counter
import re
import math


@dataclass
class PromptMetrics:
    """Metrics about prompt quality and patterns."""
    total_prompts: int = 0
    avg_length: float = 0.0
    avg_words: float = 0.0
    specificity_score: float = 0.0  # 0-100, based on file paths, code references
    context_score: float = 0.0      # 0-100, based on background info provided
    iteration_patterns: list = field(default_factory=list)
    
    # Specificity indicators
    contains_file_paths: int = 0
    contains_code_snippets: int = 0
    contains_questions: int = 0
    contains_commands: int = 0
    contains_product_terms: int = 0


@dataclass
class SessionMetrics:
    """Metrics for a single session."""
    duration_minutes: float = 0.0
    message_count: int = 0
    prompt_count: int = 0
    tool_call_count: int = 0
    code_edits: int = 0
    file_reads: int = 0
    bash_commands: int = 0
    error_count: int = 0


@dataclass
class AggregateMetrics:
    """Aggregated metrics across all sessions."""
    total_sessions: int = 0
    total_prompts: int = 0
    total_messages: int = 0
    total_tool_calls: int = 0
    total_errors: int = 0
    
    # Time metrics
    total_coding_time_hours: float = 0.0
    avg_session_duration: float = 0.0
    longest_session: float = 0.0
    shortest_session: float = 0.0
    
    # Prompt metrics
    avg_prompt_length: float = 0.0
    avg_prompt_words: float = 0.0
    longest_prompt: int = 0
    shortest_prompt: int = 0
    
    # Tool usage breakdown
    tool_usage: dict = field(default_factory=dict)
    most_used_tool: str = ""
    tool_diversity: float = 0.0  # 0-100
    
    # Quality scores (0-100)
    steering_score: float = 0.0      # How well you direct the AI
    execution_score: float = 0.0     # Speed and iteration efficiency
    engineering_score: float = 0.0     # Systematic approach
    product_score: float = 0.0       # Focus on outcomes
    planning_score: float = 0.0      # Research-to-build ratio
    
    # Archetype detection
    archetype: str = "Unknown"
    archetype_scores: dict = field(default_factory=dict)

    # Growth edges
    growth_recommendations: list = field(default_factory=list)

    # Local-LLM qualitative analysis (empty when run in heuristic-only mode)
    llm_summary: str = ""
    llm_archetype_reason: str = ""
    llm_model: str = ""
    
    # Efficiency
    efficiency_score: float = 0.0
    
    # Patterns
    prompt_patterns: dict = field(default_factory=dict)
    work_patterns: dict = field(default_factory=dict)


class MetricsAnalyzer:
    """Analyzes Claude Code sessions and computes metrics."""
    
    # Keywords that indicate archetypes
    ARCHITECT_KEYWORDS = [
        "architecture", "design", "structure", "pattern", "refactor",
        "plan", "organize", "abstract", "interface", "component",
        "before implementing", "explore", "compare", "approaches"
    ]
    
    SPRINTER_KEYWORDS = [
        "quick", "fast", "add", "simple", "just", "now", "quickly",
        "implement", "create", "write", "generate", "build"
    ]
    
    DEBUGGER_KEYWORDS = [
        "fix", "debug", "error", "bug", "issue", "broken",
        "why", "what's wrong", "not working", "trace", "log"
    ]
    
    COLLABORATOR_KEYWORDS = [
        "should we", "what do you think", "recommend", "suggest",
        "which", "best practice", "opinion", "guidance", "review"
    ]
    
    AUTONOMOUS_KEYWORDS = [
        "build a", "create a", "implement", "deploy", "end-to-end",
        "full", "complete", "entire", "workflow", "automation"
    ]

    # Words that signal outcome/product focus rather than pure mechanics.
    PRODUCT_KEYWORDS = [
        "user", "users", "feature", "outcome", "goal", "deliver",
        "ship", "product", "customer", "experience", "value"
    ]
    
    def analyze_prompts(self, session) -> PromptMetrics:
        """Analyze prompt quality in a session."""
        user_msgs = session.user_messages
        if not user_msgs:
            return PromptMetrics()
        
        prompts = [m.content for m in user_msgs if m.content]
        if not prompts:
            return PromptMetrics()
        
        lengths = [len(p) for p in prompts]
        words = [len(p.split()) for p in prompts]
        
        # Specificity analysis
        file_paths = sum(1 for p in prompts if re.search(r'[\w/]+\.\w+', p))
        code_snippets = sum(1 for p in prompts if '`' in p or '```' in p)
        questions = sum(1 for p in prompts if '?' in p)
        commands = sum(1 for p in prompts if any(cmd in p.lower() for cmd in ['run', 'execute', 'test', 'build', 'deploy']))
        product_terms = sum(
            1 for p in prompts
            if any(re.search(r'\b' + kw + r'\b', p.lower()) for kw in self.PRODUCT_KEYWORDS)
        )
        
        # Calculate specificity score (0-100)
        specificity = min(100, (file_paths / len(prompts) * 40) + 
                                (code_snippets / len(prompts) * 30) +
                                (commands / len(prompts) * 30))
        
        # Calculate context score (0-100)
        context = min(100, sum(len(p) for p in prompts) / len(prompts) / 10)
        
        return PromptMetrics(
            total_prompts=len(prompts),
            avg_length=sum(lengths) / len(lengths),
            avg_words=sum(words) / len(words),
            specificity_score=specificity,
            context_score=min(100, context),
            contains_file_paths=file_paths,
            contains_code_snippets=code_snippets,
            contains_questions=questions,
            contains_commands=commands,
            contains_product_terms=product_terms
        )
    
    def analyze_session(self, session) -> SessionMetrics:
        """Analyze a single session."""
        metrics = SessionMetrics()
        
        metrics.message_count = session.total_messages
        metrics.prompt_count = session.total_prompts
        metrics.tool_call_count = session.total_tool_calls

        # Real wall-clock duration from timestamps (falls back to an estimate
        # of 2.5 min/message when the transcript has no usable timestamps).
        metrics.duration_minutes = session.duration_minutes
        
        # Count specific tool types
        tool_usage = session.tool_usage
        metrics.code_edits = tool_usage.get("Edit", 0) + tool_usage.get("Write", 0)
        metrics.file_reads = tool_usage.get("Read", 0) + tool_usage.get("Glob", 0) + tool_usage.get("Grep", 0)
        metrics.bash_commands = tool_usage.get("Bash", 0) + tool_usage.get("Shell", 0)
        
        # Estimate errors (based on retry keywords in assistant responses)
        error_indicators = ["error", "failed", "sorry", "cannot", "unable", "exception"]
        for msg in session.messages:
            if msg.is_assistant and msg.content:
                if any(err in msg.content.lower() for err in error_indicators):
                    metrics.error_count += 1
        
        return metrics
    
    def detect_archetype(self, sessions: list) -> tuple[str, dict]:
        """Detect builder archetype across all sessions."""
        # Collect all user prompts
        all_prompts = []
        for session in sessions:
            all_prompts.extend([m.content.lower() for m in session.user_messages if m.content])
        
        if not all_prompts:
            return "Unknown", {}
        
        prompt_text = " ".join(all_prompts)

        def count_keywords(keywords: list) -> int:
            # Whole-word/phrase matching so "add" doesn't match "address"
            # or "build" inside "building".
            return sum(
                1 for kw in keywords
                if re.search(r'\b' + re.escape(kw) + r'\b', prompt_text)
            )

        # Score each archetype
        scores = {
            "🏗️ Architect": count_keywords(self.ARCHITECT_KEYWORDS),
            "⚡ Sprinter": count_keywords(self.SPRINTER_KEYWORDS),
            "🐛 Debugger": count_keywords(self.DEBUGGER_KEYWORDS),
            "🤝 Collaborator": count_keywords(self.COLLABORATOR_KEYWORDS),
            "🤖 Autonomous Agent": count_keywords(self.AUTONOMOUS_KEYWORDS),
        }
        
        # Normalize scores
        total_keywords = sum(scores.values())
        if total_keywords > 0:
            scores = {k: (v / total_keywords * 100) for k, v in scores.items()}
        
        # Determine primary archetype
        archetype = max(scores, key=scores.get) if scores else "Unknown"
        
        return archetype, scores
    
    def compute_efficiency_score(self, metrics: AggregateMetrics) -> float:
        """Compute overall efficiency score (0-100)."""
        if metrics.total_sessions == 0:
            return 0.0
        
        # Base score from prompt quality
        base = 50.0

        # Add points for specificity
        base += min(20, metrics.avg_prompt_length / 50)

        # Add points for tool diversity
        base += min(15, metrics.tool_diversity / 6)

        # Subtract for a high error rate (errors per message), not for simply
        # having analyzed more sessions.
        if metrics.total_messages > 0:
            error_rate = metrics.total_errors / metrics.total_messages
            base -= min(20, error_rate * 100)

        return min(100, max(0, base))
    
    def generate_recommendations(self, metrics: AggregateMetrics) -> list[str]:
        """Generate personalized growth recommendations."""
        recs = []
        
        if metrics.avg_prompt_length < 100:
            recs.append("🎯 Add more context to your prompts. Include file paths, expected behavior, and constraints.")
        
        if metrics.tool_diversity < 30:
            recs.append("🛠️ Try using more tool types. Explore Glob, Grep, and Task agents for broader exploration.")
        
        if metrics.planning_score < 50:
            recs.append("📋 Spend more time in planning phase. Ask the AI to explore patterns before implementing.")
        
        if metrics.execution_score < 50:
            recs.append("⚡ Use more direct action prompts. 'Add X to file Y' is faster than open-ended requests.")
        
        if metrics.product_score < 50:
            recs.append("🎯 Connect tasks to outcomes. Start prompts with 'So that users can...' to stay product-focused.")
        
        if not recs:
            recs.append("✨ You're doing great! Try adding automated tests via Bash tool to level up.")
        
        return recs[:3]  # Top 3 recommendations
    
    def analyze_all(self, sessions: list) -> AggregateMetrics:
        """Analyze all sessions and compute aggregate metrics."""
        if not sessions:
            return AggregateMetrics()
        
        metrics = AggregateMetrics()
        metrics.total_sessions = len(sessions)
        
        # Collect per-session data
        session_metrics = [self.analyze_session(s) for s in sessions]
        prompt_metrics = [self.analyze_prompts(s) for s in sessions]
        
        # Aggregate prompt data
        all_prompt_lengths = []
        all_prompt_words = []
        total_tool_usage = Counter()
        
        for pm in prompt_metrics:
            metrics.total_prompts += pm.total_prompts
            all_prompt_lengths.append(pm.avg_length)
            all_prompt_words.append(pm.avg_words)
        
        for sm in session_metrics:
            metrics.total_messages += sm.message_count
            metrics.total_tool_calls += sm.tool_call_count
            metrics.total_coding_time_hours += sm.duration_minutes / 60
            metrics.total_errors += sm.error_count
        
        # Tool usage across all sessions
        for session in sessions:
            for tool, count in session.tool_usage.items():
                total_tool_usage[tool] += count
        
        metrics.tool_usage = dict(total_tool_usage)
        if total_tool_usage:
            metrics.most_used_tool = total_tool_usage.most_common(1)[0][0]
            metrics.tool_diversity = len(total_tool_usage) * 10  # Rough score
        
        # Compute averages
        if all_prompt_lengths:
            metrics.avg_prompt_length = sum(all_prompt_lengths) / len(all_prompt_lengths)
            metrics.avg_prompt_words = sum(all_prompt_words) / len(all_prompt_words)
        
        # Session duration stats
        durations = [sm.duration_minutes for sm in session_metrics]
        if durations:
            metrics.avg_session_duration = sum(durations) / len(durations)
            metrics.longest_session = max(durations)
            metrics.shortest_session = min(durations)
        
        # Compute dimension scores
        metrics.steering_score = self._compute_steering_score(prompt_metrics)
        metrics.execution_score = self._compute_execution_score(session_metrics, prompt_metrics)
        metrics.engineering_score = self._compute_engineering_score(session_metrics)
        metrics.product_score = self._compute_product_score(prompt_metrics)
        metrics.planning_score = self._compute_planning_score(prompt_metrics)
        
        # Detect archetype
        metrics.archetype, metrics.archetype_scores = self.detect_archetype(sessions)
        
        # Compute efficiency
        metrics.efficiency_score = self.compute_efficiency_score(metrics)
        
        # Generate recommendations
        metrics.growth_recommendations = self.generate_recommendations(metrics)
        
        # Work patterns
        metrics.work_patterns = {
            "sessions_per_day": metrics.total_sessions / max(1, metrics.total_coding_time_hours / 24),
            "tools_per_session": metrics.total_tool_calls / max(1, metrics.total_sessions),
            "messages_per_session": metrics.total_messages / max(1, metrics.total_sessions),
        }
        
        return metrics
    
    def _compute_steering_score(self, prompt_metrics: list) -> float:
        """How well the user directs the AI (0-100)."""
        if not prompt_metrics:
            return 0.0
        
        avg_specificity = sum(pm.specificity_score for pm in prompt_metrics) / len(prompt_metrics)
        avg_context = sum(pm.context_score for pm in prompt_metrics) / len(prompt_metrics)
        
        return min(100, (avg_specificity * 0.6) + (avg_context * 0.4))
    
    def _compute_execution_score(self, session_metrics: list, prompt_metrics: list) -> float:
        """Speed and iteration efficiency (0-100)."""
        if not session_metrics:
            return 0.0
        
        # Shorter sessions with high tool usage = efficient
        avg_duration = sum(sm.duration_minutes for sm in session_metrics) / len(session_metrics)
        avg_tools = sum(sm.tool_call_count for sm in session_metrics) / len(session_metrics)
        
        # Normalize: ideal session ~30 min with 10+ tools
        duration_score = max(0, 100 - (avg_duration - 30) * 2)
        tool_score = min(100, avg_tools * 10)
        
        return min(100, (duration_score * 0.5) + (tool_score * 0.5))
    
    def _compute_engineering_score(self, session_metrics: list) -> float:
        """Systematic approach (0-100)."""
        if not session_metrics:
            return 0.0
        
        # High ratio of reads to edits = systematic
        total_reads = sum(sm.file_reads for sm in session_metrics)
        total_edits = sum(sm.code_edits for sm in session_metrics)
        
        if total_edits == 0:
            return 50.0
        
        ratio = total_reads / total_edits
        return min(100, ratio * 20)
    
    def _compute_product_score(self, prompt_metrics: list) -> float:
        """Focus on outcomes (0-100).

        Based on the share of prompts that actually mention product/outcome
        terms (user, feature, ship, customer, ...).
        """
        if not prompt_metrics:
            return 0.0

        total_prompts = sum(pm.total_prompts for pm in prompt_metrics)
        if total_prompts == 0:
            return 50.0

        product_prompts = sum(pm.contains_product_terms for pm in prompt_metrics)
        # Share of product-focused prompts, scaled to 0-100.
        return min(100, (product_prompts / total_prompts) * 100)
    
    def _compute_planning_score(self, prompt_metrics: list) -> float:
        """Research-to-build ratio (0-100)."""
        if not prompt_metrics:
            return 0.0
        
        # Longer prompts with questions = more planning
        avg_words = sum(pm.avg_words for pm in prompt_metrics) / len(prompt_metrics)
        avg_questions = sum(pm.contains_questions for pm in prompt_metrics) / max(1, len(prompt_metrics))
        
        # Normalize: ~200 words avg with questions = good planning
        word_score = min(100, avg_words / 2)
        question_score = min(100, avg_questions * 20)
        
        return (word_score * 0.7) + (question_score * 0.3)
