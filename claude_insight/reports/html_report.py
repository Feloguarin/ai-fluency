"""
HTML Report Generator

Generates beautiful, self-contained HTML reports with charts.
No external dependencies — pure HTML/CSS/JS inline.
"""

import json
from html import escape


class HTMLReport:
    """Generate self-contained HTML reports."""
    
    def __init__(self, metrics):
        self.metrics = metrics
    
    def generate(self) -> str:
        """Generate full HTML report."""
        
        # Prepare chart data
        archetype_data = []
        for name, score in sorted(self.metrics.archetype_scores.items(), key=lambda x: x[1], reverse=True):
            archetype_data.append({"label": name, "value": round(score, 1)})
        
        dimension_data = [
            {"label": "Steering", "value": round(self.metrics.steering_score, 1), "color": "#10b981"},
            {"label": "Execution", "value": round(self.metrics.execution_score, 1), "color": "#3b82f6"},
            {"label": "Engineering", "value": round(self.metrics.engineering_score, 1), "color": "#8b5cf6"},
            {"label": "Product", "value": round(self.metrics.product_score, 1), "color": "#f59e0b"},
            {"label": "Planning", "value": round(self.metrics.planning_score, 1), "color": "#ef4444"},
        ]
        
        tool_data = []
        for tool, count in sorted(self.metrics.tool_usage.items(), key=lambda x: x[1], reverse=True)[:10]:
            tool_data.append({"label": tool, "value": count})

        # Archetype description: prefer the local model's reasoning if present.
        archetype_desc = (
            self._esc(self.metrics.llm_archetype_reason)
            if self.metrics.llm_archetype_reason
            else self._get_archetype_desc(self.metrics.archetype)
        )

        # Optional AI-generated profile summary card.
        summary_card = ""
        if self.metrics.llm_summary:
            summary_card = f"""
<div class="card" style="margin-bottom: 2rem;">
  <h2>📝 AI Profile Summary</h2>
  <p style="color: var(--text); line-height: 1.7;">{self._esc(self.metrics.llm_summary)}</p>
</div>"""

        privacy_badge = "🔒 Zero data uploaded • 100% offline"
        if self.metrics.llm_model:
            privacy_badge = f"🔒 Analyzed locally with {self._esc(self.metrics.llm_model)} • Zero data uploaded"

        # Build the HTML
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Insight — Builder Profile</title>
<style>
:root {{
  --bg: #0f0f1a;
  --card: #1a1a2e;
  --card-hover: #252540;
  --text: #e2e8f0;
  --text-muted: #94a3b8;
  --border: #2d2d44;
  --accent: #6366f1;
  --accent-glow: rgba(99, 102, 241, 0.3);
  --success: #10b981;
  --warning: #f59e0b;
  --danger: #ef4444;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  min-height: 100vh;
}}

.container {{
  max-width: 1200px;
  margin: 0 auto;
  padding: 2rem;
}}

header {{
  text-align: center;
  padding: 3rem 0;
  border-bottom: 1px solid var(--border);
  margin-bottom: 2rem;
}}

header h1 {{
  font-size: 2.5rem;
  font-weight: 800;
  background: linear-gradient(135deg, #6366f1, #8b5cf6, #ec4899);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  margin-bottom: 0.5rem;
}}

header p {{
  color: var(--text-muted);
  font-size: 1.1rem;
}}

.badge {{
  display: inline-block;
  background: var(--card);
  border: 1px solid var(--border);
  padding: 0.25rem 0.75rem;
  border-radius: 9999px;
  font-size: 0.75rem;
  color: var(--text-muted);
  margin-top: 1rem;
}}

.grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 1.5rem;
  margin-bottom: 2rem;
}}

.card {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 1rem;
  padding: 1.5rem;
  transition: transform 0.2s, border-color 0.2s;
}}

.card:hover {{
  border-color: var(--accent);
  transform: translateY(-2px);
}}

.card h2 {{
  font-size: 0.875rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  margin-bottom: 1rem;
}}

.big-number {{
  font-size: 3rem;
  font-weight: 800;
  background: linear-gradient(135deg, #6366f1, #8b5cf6);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}}

.label {{
  color: var(--text-muted);
  font-size: 0.875rem;
}}

.score-circle {{
  width: 160px;
  height: 160px;
  margin: 0 auto;
  position: relative;
}}

.score-circle svg {{
  transform: rotate(-90deg);
}}

.score-circle .score-text {{
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  font-size: 2.5rem;
  font-weight: 800;
}}

.score-circle .score-label {{
  position: absolute;
  bottom: 20%;
  left: 50%;
  transform: translateX(-50%);
  font-size: 0.75rem;
  color: var(--text-muted);
  text-transform: uppercase;
}}

.archetype {{
  text-align: center;
  padding: 2rem;
}}

.archetype-icon {{
  font-size: 4rem;
  margin-bottom: 1rem;
}}

.archetype-name {{
  font-size: 1.5rem;
  font-weight: 700;
  margin-bottom: 0.5rem;
}}

.archetype-desc {{
  color: var(--text-muted);
  font-size: 0.875rem;
  max-width: 300px;
  margin: 0 auto;
}}

.bar-chart {{
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}}

.bar-item {{
  display: flex;
  align-items: center;
  gap: 1rem;
}}

.bar-label {{
  min-width: 120px;
  font-size: 0.875rem;
  color: var(--text-muted);
}}

.bar-track {{
  flex: 1;
  height: 8px;
  background: rgba(255,255,255,0.05);
  border-radius: 9999px;
  overflow: hidden;
}}

.bar-fill {{
  height: 100%;
  border-radius: 9999px;
  transition: width 1s ease;
}}

.bar-value {{
  min-width: 40px;
  text-align: right;
  font-size: 0.875rem;
  font-weight: 600;
}}

.recommendations {{
  list-style: none;
}}

.recommendations li {{
  padding: 1rem;
  background: rgba(99, 102, 241, 0.05);
  border-left: 3px solid var(--accent);
  border-radius: 0 0.5rem 0.5rem 0;
  margin-bottom: 0.75rem;
}}

.dimension-grid {{
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 1rem;
  text-align: center;
}}

.dimension-item {{
  padding: 1rem;
}}

.dimension-score {{
  font-size: 1.5rem;
  font-weight: 700;
  margin-bottom: 0.25rem;
}}

.dimension-name {{
  font-size: 0.75rem;
  color: var(--text-muted);
  text-transform: uppercase;
}}

footer {{
  text-align: center;
  padding: 3rem 0;
  color: var(--text-muted);
  font-size: 0.875rem;
  border-top: 1px solid var(--border);
  margin-top: 2rem;
}}

@keyframes pulse {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: 0.7; }}
}}

.pulse {{
  animation: pulse 2s ease-in-out infinite;
}}

@media (max-width: 768px) {{
  .dimension-grid {{ grid-template-columns: repeat(2, 1fr); }}
  header h1 {{ font-size: 1.75rem; }}
  .container {{ padding: 1rem; }}
}}
</style>
</head>
<body>
<div class="container">

<header>
  <h1>🧠 Claude Insight</h1>
  <p>Your AI Builder Profile — Generated Locally, Kept Private</p>
  <div class="badge">{privacy_badge}</div>
</header>
{summary_card}
<!-- Top Stats -->
<div class="grid">
  <div class="card">
    <h2>Efficiency Score</h2>
    <div class="score-circle">
      <svg width="160" height="160" viewBox="0 0 160 160">
        <circle cx="80" cy="80" r="70" fill="none" stroke="rgba(255,255,255,0.05)" stroke-width="10"/>
        <circle cx="80" cy="80" r="70" fill="none" stroke="url(#gradient)" stroke-width="10"
          stroke-dasharray="{440 * self.metrics.efficiency_score / 100:.1f} 440"
          stroke-linecap="round"/>
        <defs>
          <linearGradient id="gradient" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stop-color="#6366f1"/>
            <stop offset="100%" stop-color="#ec4899"/>
          </linearGradient>
        </defs>
      </svg>
      <div class="score-text">{int(self.metrics.efficiency_score)}</div>
      <div class="score-label">/100</div>
    </div>
  </div>
  
  <div class="card archetype">
    <h2>Your Archetype</h2>
    <div class="archetype-icon">
      {self._get_archetype_emoji(self.metrics.archetype)}
    </div>
    <div class="archetype-name">{self.metrics.archetype.replace('🏗️ ', '').replace('⚡ ', '').replace('🐛 ', '').replace('🤝 ', '').replace('🤖 ', '')}</div>
    <div class="archetype-desc">{archetype_desc}</div>
  </div>
  
  <div class="card">
    <h2>Sessions Analyzed</h2>
    <div class="big-number">{self.metrics.total_sessions}</div>
    <div class="label">{self.metrics.total_prompts} prompts • {self.metrics.total_messages} messages</div>
  </div>
</div>

<!-- Five Dimensions -->
<div class="card">
  <h2>📊 Five Dimensions of Building</h2>
  <div style="margin-top: 1.5rem;">
    {self._render_dimension_bars(dimension_data)}
  </div>
</div>

<!-- Stats Grid -->
<div class="grid">
  <div class="card">
    <h2>Avg Prompt Length</h2>
    <div class="big-number">{int(self.metrics.avg_prompt_length)}</div>
    <div class="label">characters per prompt</div>
  </div>
  
  <div class="card">
    <h2>Tool Diversity</h2>
    <div class="big-number">{int(self.metrics.tool_diversity)}</div>
    <div class="label">unique tools used</div>
  </div>
  
  <div class="card">
    <h2>Most Used Tool</h2>
    <div class="big-number" style="font-size: 1.5rem; margin-top: 0.5rem;">{self.metrics.most_used_tool or "N/A"}</div>
    <div class="label">primary tool of choice</div>
  </div>
  
  <div class="card">
    <h2>Est. Coding Time</h2>
    <div class="big-number">{int(self.metrics.total_coding_time_hours)}h</div>
    <div class="label">across all sessions</div>
  </div>
</div>

<!-- Archetype Affinity -->
<div class="card">
  <h2>🎭 Archetype Affinity</h2>
  <div class="bar-chart" style="margin-top: 1rem;">
    {self._render_archetype_bars(archetype_data)}
  </div>
</div>

<!-- Tool Usage -->
<div class="card">
  <h2>🛠️ Tool Usage Breakdown</h2>
  <div class="bar-chart" style="margin-top: 1rem;">
    {self._render_tool_bars(tool_data)}
  </div>
</div>

<!-- Recommendations -->
<div class="card">
  <h2>🌱 Growth Recommendations</h2>
  <ul class="recommendations" style="margin-top: 1rem;">
    {self._render_recommendations()}
  </ul>
</div>

<!-- Work Patterns -->
<div class="card">
  <h2>📈 Work Patterns</h2>
  <div class="grid" style="margin-top: 1rem; grid-template-columns: repeat(3, 1fr);">
    {self._render_work_patterns()}
  </div>
</div>

<footer>
  <p>Generated by <strong>Claude Insight</strong> — Private, Local, Open Source</p>
  <p style="margin-top: 0.5rem; opacity: 0.6;">
    <a href="https://github.com/Feloguarin/claude-insight" style="color: var(--accent); text-decoration: none;">github.com/Feloguarin/claude-insight</a>
  </p>
</footer>

</div>
</body>
</html>"""
        
        return html
    
    def _esc(self, text: str) -> str:
        """HTML-escape text interpolated into the report."""
        return escape(str(text))

    def _get_archetype_emoji(self, archetype: str) -> str:
        """Get emoji for archetype."""
        emojis = {
            "🏗️ Architect": "🏗️",
            "⚡ Sprinter": "⚡",
            "🐛 Debugger": "🐛",
            "🤝 Collaborator": "🤝",
            "🤖 Autonomous Agent": "🤖",
            "Unknown": "❓",
        }
        return emojis.get(archetype, "❓")
    
    def _get_archetype_desc(self, archetype: str) -> str:
        """Get description for archetype."""
        descs = {
            "🏗️ Architect": "You plan extensively before building. Low code churn, high quality output.",
            "⚡ Sprinter": "You move fast and iterate rapidly. Quick to ship, quick to learn.",
            "🐛 Debugger": "You excel at systematic problem-solving. Methodical and persistent.",
            "🤝 Collaborator": "You seek alignment and ask great questions. Team multiplier.",
            "🤖 Autonomous Agent": "You delegate end-to-end workflows. Focus on outcomes, not mechanics.",
            "Unknown": "Keep building — your archetype will emerge as you code more.",
        }
        return descs.get(archetype, "Keep building — your archetype will emerge.")
    
    def _render_dimension_bars(self, data: list) -> str:
        """Render dimension bar chart."""
        html = '<div class="bar-chart">'
        for item in data:
            html += f'''
            <div class="bar-item">
              <div class="bar-label">{item['label']}</div>
              <div class="bar-track">
                <div class="bar-fill" style="width: {item['value']}%; background: {item['color']};"></div>
              </div>
              <div class="bar-value">{item['value']}</div>
            </div>'''
        html += '</div>'
        return html
    
    def _render_archetype_bars(self, data: list) -> str:
        """Render archetype affinity bars."""
        colors = ["#6366f1", "#8b5cf6", "#ec4899", "#f59e0b", "#10b981"]
        html = ''
        for i, item in enumerate(data):
            color = colors[i % len(colors)]
            html += f'''
            <div class="bar-item">
              <div class="bar-label">{item['label']}</div>
              <div class="bar-track">
                <div class="bar-fill" style="width: {item['value']}%; background: {color};"></div>
              </div>
              <div class="bar-value">{item['value']}%</div>
            </div>'''
        return html
    
    def _render_tool_bars(self, data: list) -> str:
        """Render tool usage bars."""
        total = sum(item['value'] for item in data) if data else 1
        html = ''
        for item in data:
            pct = (item['value'] / total) * 100
            html += f'''
            <div class="bar-item">
              <div class="bar-label">{item['label']}</div>
              <div class="bar-track">
                <div class="bar-fill" style="width: {pct}%; background: #6366f1;"></div>
              </div>
              <div class="bar-value">{item['value']}</div>
            </div>'''
        return html
    
    def _render_recommendations(self) -> str:
        """Render recommendations list."""
        html = ''
        for rec in self.metrics.growth_recommendations:
            html += f'<li>{rec}</li>\n'
        return html
    
    def _render_work_patterns(self) -> str:
        """Render work pattern cards."""
        html = ''
        for key, value in self.metrics.work_patterns.items():
            label = key.replace('_', ' ').title()
            html += f'''
            <div style="text-align: center;">
              <div style="font-size: 1.5rem; font-weight: 700; color: var(--accent);">{value:.1f}</div>
              <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.25rem;">{label}</div>
            </div>'''
        return html
