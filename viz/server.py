"""Visualization: collects pipeline events, then renders a static flowchart."""

import json
import os
import tempfile
import webbrowser

_events: list[dict] = []


def emit(event_type: str, **data) -> None:
    _events.append({"type": event_type, **data})


def start() -> None:
    pass


def render_and_open() -> None:
    """Write a self-contained HTML flowchart and open it in the browser."""
    html = _build_html(json.dumps(_events))
    path = os.path.join(tempfile.gettempdir(), "ai_scraper_viz.html")
    with open(path, "w") as f:
        f.write(html)
    webbrowser.open(f"file://{path}")


def _build_html(events_json: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>AI Event Scraper — Results</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"></script>
  <script src="https://unpkg.com/dagre@0.8.5/dist/dagre.min.js"></script>
  <script src="https://unpkg.com/cytoscape-dagre@2.5.0/cytoscape-dagre.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0d1117;
      color: #e6edf3;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", monospace;
      display: flex;
      flex-direction: column;
      height: 100vh;
      overflow: hidden;
    }}
    #header {{
      padding: 14px 20px;
      border-bottom: 1px solid #21262d;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-shrink: 0;
    }}
    #header h1 {{ font-size: 15px; font-weight: 600; }}
    #summary {{ font-size: 13px; color: #8b949e; }}
    #cy {{ flex: 1; }}
    #legend {{
      padding: 10px 20px;
      border-top: 1px solid #21262d;
      display: flex;
      gap: 24px;
      font-size: 12px;
      color: #8b949e;
      flex-shrink: 0;
    }}
    .legend-item {{ display: flex; align-items: center; gap: 7px; }}
    .legend-dot {{ width: 10px; height: 10px; border-radius: 3px; border: 1.5px solid transparent; }}
  </style>
</head>
<body>
<div id="header">
  <h1>⚡ AI Event Scraper — Run Results</h1>
  <span id="summary"></span>
</div>
<div id="cy"></div>
<div id="legend">
  <div class="legend-item"><div class="legend-dot" style="background:#1a3a2a;border-color:#238636"></div> Pipeline step</div>
  <div class="legend-item"><div class="legend-dot" style="background:#1a3a2a;border-color:#3fb950"></div> Score 4–5</div>
  <div class="legend-item"><div class="legend-dot" style="background:#3a2f1a;border-color:#d29922"></div> Score 2–3</div>
  <div class="legend-item"><div class="legend-dot" style="background:#3a1a1a;border-color:#da3633"></div> Score 1 / Failed</div>
</div>
<script>
const EVENTS = {events_json};

// ── Replay events into per-site state ──────────────────────────────────────
const sites = [];      // ordered list of {{ url, steps, events }}
const siteByUrl = {{}};

for (const e of EVENTS) {{
  if (e.type === 'site_queued') {{
    const site = {{ url: e.url, steps: {{}}, events: [] }};
    sites.push(site);
    siteByUrl[e.url] = site;

  }} else if (e.type === 'step_done') {{
    const site = siteByUrl[e.url];
    if (site) site.steps[e.step] = {{ state: 'done', detail: e.detail || null }};

  }} else if (e.type === 'step_failed') {{
    const site = siteByUrl[e.url];
    if (site) site.steps[e.step] = {{ state: 'failed' }};

  }} else if (e.type === 'event_result') {{
    const site = siteByUrl[e.url];
    if (site) site.events.push({{ title: e.title, score: e.score, reason: e.reason }});
  }}
}}

// ── Score → visual style ───────────────────────────────────────────────────
function scoreStyle(score) {{
  if (score >= 4) return {{ bg: '#1a3a2a', border: '#3fb950', color: '#3fb950' }};
  if (score >= 2) return {{ bg: '#3a2f1a', border: '#d29922', color: '#d29922' }};
  return {{ bg: '#3a1a1a', border: '#da3633', color: '#f85149' }};
}}

// ── Build cytoscape elements ───────────────────────────────────────────────
const els = [];

const PIPELINE_STEPS = ['fetch', 'reduce', 'extract'];
const STEP_LABEL = {{ fetch: 'Fetch', reduce: 'Reduce', extract: 'Extract' }};

for (let si = 0; si < sites.length; si++) {{
  const site = sites[si];
  const p = `s${{si}}`;  // id prefix
  const short = site.url.replace(/^https?:\\/\\//, '').replace(/\\/$/, '');

  // Site (root) node
  els.push({{ data: {{
    id: `${{p}}_url`, label: short, kind: 'url',
    parent_group: p
  }} }});

  // Pipeline step nodes
  let prevId = `${{p}}_url`;
  for (const step of PIPELINE_STEPS) {{
    const info = site.steps[step] || {{}};
    const failed = info.state === 'failed';
    const id = `${{p}}_${{step}}`;
    const detail = info.detail ? `\\n${{info.detail}}` : '';
    els.push({{ data: {{
      id, label: STEP_LABEL[step] + detail,
      kind: failed ? 'failed' : 'pipeline',
      parent_group: p
    }} }});
    els.push({{ data: {{ source: prevId, target: id }} }});
    prevId = id;
    if (failed) break;
  }}

  // Event nodes branching from extract
  if (site.events.length > 0) {{
    for (let ei = 0; ei < site.events.length; ei++) {{
      const ev = site.events[ei];
      const evId = `${{p}}_ev${{ei}}`;
      const scoreId = `${{p}}_sc${{ei}}`;
      const truncTitle = ev.title.length > 45 ? ev.title.slice(0, 43) + '…' : ev.title;

      els.push({{ data: {{
        id: evId,
        label: truncTitle,
        kind: 'event',
        parent_group: p
      }} }});
      els.push({{ data: {{ source: prevId, target: evId }} }});

      els.push({{ data: {{
        id: scoreId,
        label: `${{ev.score}}/5`,
        score: ev.score,
        kind: 'score',
        parent_group: p
      }} }});
      els.push({{ data: {{ source: evId, target: scoreId }} }});
    }}
  }}
}}

// ── Cytoscape ─────────────────────────────────────────────────────────────
const cy = cytoscape({{
  container: document.getElementById('cy'),
  elements: els,
  style: [
    // default node
    {{
      selector: 'node',
      style: {{
        'label': 'data(label)',
        'text-valign': 'center',
        'text-halign': 'center',
        'border-width': 2,
        'font-size': 11,
        'font-family': 'monospace',
        'width': 'label',
        'height': 'label',
        'padding': '10px',
        'shape': 'roundrectangle',
        'text-wrap': 'wrap',
        'text-max-width': 200,
        'background-color': '#1c2128',
        'border-color': '#484f58',
        'color': '#8b949e',
      }},
    }},
    // site URL node
    {{
      selector: 'node[kind = "url"]',
      style: {{
        'font-size': 12,
        'font-weight': 'bold',
        'background-color': '#1c2128',
        'border-color': '#58a6ff',
        'color': '#e6edf3',
        'shape': 'roundrectangle',
      }},
    }},
    // pipeline step nodes
    {{
      selector: 'node[kind = "pipeline"]',
      style: {{
        'background-color': '#1a3a2a',
        'border-color': '#238636',
        'color': '#3fb950',
      }},
    }},
    // failed step
    {{
      selector: 'node[kind = "failed"]',
      style: {{
        'background-color': '#3a1a1a',
        'border-color': '#da3633',
        'color': '#f85149',
      }},
    }},
    // event title node
    {{
      selector: 'node[kind = "event"]',
      style: {{
        'background-color': '#161b22',
        'border-color': '#30363d',
        'color': '#c9d1d9',
        'text-max-width': 220,
        'font-size': 11,
      }},
    }},
    // score badge
    {{
      selector: 'node[kind = "score"][score >= 4]',
      style: {{ 'background-color': '#1a3a2a', 'border-color': '#3fb950', 'color': '#3fb950' }},
    }},
    {{
      selector: 'node[kind = "score"][score >= 2][score < 4]',
      style: {{ 'background-color': '#3a2f1a', 'border-color': '#d29922', 'color': '#d29922' }},
    }},
    {{
      selector: 'node[kind = "score"][score < 2]',
      style: {{ 'background-color': '#3a1a1a', 'border-color': '#da3633', 'color': '#f85149' }},
    }},
    // edges
    {{
      selector: 'edge',
      style: {{
        'width': 1.5,
        'line-color': '#30363d',
        'target-arrow-color': '#30363d',
        'target-arrow-shape': 'triangle',
        'curve-style': 'bezier',
        'arrow-scale': 0.7,
      }},
    }},
  ],
  layout: {{
    name: 'dagre',
    rankDir: 'LR',
    nodeSep: 30,
    rankSep: 60,
    padding: 40,
  }},
  userZoomingEnabled: true,
  userPanningEnabled: true,
  minZoom: 0.1,
  maxZoom: 2,
}});

cy.fit(cy.elements(), 50);

// ── Summary ────────────────────────────────────────────────────────────────
const totalEvents = sites.reduce((n, s) => n + s.events.length, 0);
const failed = sites.filter(s => Object.values(s.steps).some(st => st.state === 'failed')).length;
document.getElementById('summary').textContent =
  `${{sites.length}} site(s) · ${{totalEvents}} events extracted · ${{failed}} failed`;
</script>
</body>
</html>"""
