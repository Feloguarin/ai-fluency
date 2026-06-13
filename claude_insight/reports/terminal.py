"""
Terminal Report Generator

Generates beautiful ASCII/text reports of Claude Code analysis.
"""

import textwrap
from typing import Optional


class TerminalReport:
    """Generate terminal-friendly reports."""
    
    def __init__(self, metrics):
        self.metrics = metrics
    
    def generate(self) -> str:
        """Generate full terminal report."""
        lines = []
        
        # Header
        lines.append("")
        lines.append("╔══════════════════════════════════════════════════════════════╗")
        lines.append("║                    🧠 CLAUDE INSIGHT                         ║")
        lines.append("║         Private AI Builder Profile — Your Data, Local        ║")
        lines.append("╚══════════════════════════════════════════════════════════════╝")
        lines.append("")
        
        # Archetype
        lines.append(f"   Your Builder Archetype: {self.metrics.archetype}")
        if self.metrics.llm_archetype_reason:
            for wrapped in self._wrap(self.metrics.llm_archetype_reason):
                lines.append(f"   {wrapped}")
        lines.append("")

        # AI profile summary (only when a local model produced one)
        if self.metrics.llm_summary:
            lines.append("   📝 Profile Summary")
            lines.append("   " + "─" * 50)
            for wrapped in self._wrap(self.metrics.llm_summary):
                lines.append(f"   {wrapped}")
            lines.append("")
        
        # Efficiency Score Box
        score = int(self.metrics.efficiency_score)
        bar = self._make_bar(score, 30)
        lines.append(f"   Efficiency Score: {score}/100")
        lines.append(f"   {bar}")
        lines.append("")
        
        # Stats Grid
        lines.append("   ┌─────────────────────────────────────────────────────┐")
        lines.append("   │  SESSIONS           │  PROMPTS           │  TOOLS    │")
        lines.append(f"   │  {self.metrics.total_sessions:<18} │  {self.metrics.total_prompts:<18} │  {self.metrics.total_tool_calls:<8} │")
        lines.append("   ├─────────────────────────────────────────────────────┤")
        lines.append("   │  AVG PROMPT LEN     │  SESSION TIME      │  DIVERSITY│")
        lines.append(f"   │  {int(self.metrics.avg_prompt_length):<18} │  {int(self.metrics.avg_session_duration)} min{'':<12} │  {int(self.metrics.tool_diversity):<8} │")
        lines.append("   └─────────────────────────────────────────────────────┘")
        lines.append("")
        
        # 5 Dimensions
        lines.append("   📊 Five Dimensions of Building")
        lines.append("   " + "─" * 50)
        
        dims = [
            ("🎯 Steering", self.metrics.steering_score),
            ("⚡ Execution", self.metrics.execution_score),
            ("🔧 Engineering", self.metrics.engineering_score),
            ("🎨 Product", self.metrics.product_score),
            ("📋 Planning", self.metrics.planning_score),
        ]
        
        for label, score in dims:
            bar = self._make_bar(int(score), 25)
            lines.append(f"   {label:<15} │{bar}│ {int(score)}")
        
        lines.append("")
        
        # Tool Usage
        if self.metrics.tool_usage:
            lines.append("   🛠️ Tool Usage Breakdown")
            lines.append("   " + "─" * 50)
            sorted_tools = sorted(self.metrics.tool_usage.items(), key=lambda x: x[1], reverse=True)[:8]
            for tool, count in sorted_tools:
                pct = count / max(1, self.metrics.total_tool_calls) * 100
                bar = self._make_bar(int(pct), 20)
                lines.append(f"   {tool:<15} │{bar}│ {count}")
            lines.append("")
        
        # Archetype Scores
        if self.metrics.archetype_scores:
            lines.append("   🎭 Archetype Affinity")
            lines.append("   " + "─" * 50)
            for archetype, score in sorted(self.metrics.archetype_scores.items(), key=lambda x: x[1], reverse=True):
                bar = self._make_bar(int(score), 25)
                lines.append(f"   {archetype:<18} │{bar}│ {int(score)}%")
            lines.append("")
        
        # Growth Edge
        lines.append("   🌱 Growth Recommendations")
        lines.append("   " + "─" * 50)
        for rec in self.metrics.growth_recommendations:
            lines.append(f"   {rec}")
        lines.append("")
        
        # Work Patterns
        if self.metrics.work_patterns:
            lines.append("   📈 Work Patterns")
            lines.append("   " + "─" * 50)
            for key, value in self.metrics.work_patterns.items():
                lines.append(f"   • {key.replace('_', ' ').title()}: {value:.1f}")
            lines.append("")
        
        # Footer
        lines.append("   ─────────────────────────────────────────────────────")
        lines.append("   💡 Tip: Run with --report to generate an HTML report")
        if self.metrics.llm_model:
            lines.append(f"   🔒 Analyzed locally with {self.metrics.llm_model} — no data uploaded")
        else:
            lines.append("   🔒 All analysis performed locally — no data uploaded")
        lines.append("")

        return "\n".join(lines)

    def _wrap(self, text: str, width: int = 60) -> list:
        """Wrap prose to fit the terminal report indent."""
        return textwrap.wrap(text, width=width) or [""]

    def _make_bar(self, value: int, width: int = 20) -> str:
        """Create an ASCII progress bar."""
        filled = int((value / 100) * width)
        filled = min(filled, width)
        empty = width - filled
        return "█" * filled + "░" * empty
