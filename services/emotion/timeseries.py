"""Append-only emotion time-series logger.

Records every emotion state transition as a JSONL line for long-term
analysis. The bot process uses only stdlib (json, pathlib); pandas is
lazily imported only by the graph script.

Also auto-regenerates an interactive HTML graph (Plotly.js via CDN) in
the state directory. The HTML is self-contained and can be opened in
any browser for stock-chart-style zoom/scroll/hover inspection.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.emotion.state import EmotionState

log = logging.getLogger(__name__)

TIMESERIES_FILENAME = "emotion_ts.jsonl"
GRAPH_HTML_FILENAME = "emotion_graph.html"
_GRAPH_COOLDOWN_S = 300
JST = timezone(timedelta(hours=9))

_NUMERIC_FIELDS = (
    "valence", "arousal", "loneliness", "affection", "curiosity", "anxiety",
)


def append_timeseries(
    state: EmotionState,
    state_dir: Path,
    *,
    source: str,
) -> None:
    """Append one JSONL record. Best-effort; never raises."""
    try:
        journal = state.emotion_journal
        trigger = ""
        if journal:
            trigger = journal[-1].get("trigger", "")

        record: dict = {
            "ts": state.updated_at or datetime.now(JST).isoformat(),
            "valence": state.valence,
            "arousal": state.arousal,
            "loneliness": state.loneliness,
            "affection": state.affection,
            "curiosity": state.curiosity,
            "anxiety": state.anxiety,
            "source": source,
            "trigger": trigger,
        }
        if state.derived_labels:
            record["derived_labels"] = state.derived_labels
        if state.labels:
            record["labels"] = state.labels

        line = json.dumps(record, ensure_ascii=False) + "\n"

        ts_path = Path(state_dir) / TIMESERIES_FILENAME
        ts_path.parent.mkdir(parents=True, exist_ok=True)
        with ts_path.open("a", encoding="utf-8") as f:
            f.write(line)
        _maybe_regenerate_html(Path(state_dir))
        _log_drasticness(Path(state_dir))
    except Exception:
        log.warning("append_timeseries failed", exc_info=True)


def _log_drasticness(state_dir: Path) -> None:
    """Emit one INFO line with rolling std across the last 25 records.

    Lets us grep ``bot.log | grep drasticness:`` to track whether v2 is
    actually widening the dynamic range over time. Best-effort.
    """
    if not log.isEnabledFor(logging.INFO):
        return
    try:
        from services.emotion.dynamics import rolling_stats
        records = load_timeseries(state_dir)[-25:]
        if len(records) < 5:
            return
        faux = [
            {"v": r["valence"], "ar": r["arousal"], "lo": r["loneliness"],
             "af": r["affection"], "cu": r["curiosity"], "ax": r["anxiety"]}
            for r in records
        ]
        s = rolling_stats(faux, n=25)
        log.info(
            "drasticness: v_std=%.3f ar_std=%.3f lo_std=%.3f "
            "af_std=%.3f cu_std=%.3f ax_std=%.3f",
            s["valence"]["std"], s["arousal"]["std"], s["loneliness"]["std"],
            s["affection"]["std"], s["curiosity"]["std"], s["anxiety"]["std"],
        )
    except Exception:
        pass


def load_timeseries(
    state_dir: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """Load time-series as list of dicts. Skips corrupted lines."""
    ts_path = Path(state_dir) / TIMESERIES_FILENAME
    if not ts_path.is_file():
        return []
    records: list[dict] = []
    with ts_path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                log.warning("timeseries: corrupt line %d, skipping", lineno)
                continue
            if since or until:
                try:
                    ts = datetime.fromisoformat(rec["ts"])
                except (KeyError, ValueError):
                    continue
                if since and ts < since:
                    continue
                if until and ts > until:
                    continue
            records.append(rec)
    return records


def timeseries_size_bytes(state_dir: Path) -> int:
    """Return current file size for monitoring."""
    ts_path = Path(state_dir) / TIMESERIES_FILENAME
    try:
        return ts_path.stat().st_size
    except FileNotFoundError:
        return 0


# ---------------------------------------------------------------------------
# Interactive HTML graph (auto-regenerated with cooldown)
# ---------------------------------------------------------------------------

def _maybe_regenerate_html(state_dir: Path) -> None:
    """Regenerate the HTML graph if cooldown has elapsed."""
    try:
        html_path = state_dir / GRAPH_HTML_FILENAME
        if html_path.is_file():
            age = time.time() - html_path.stat().st_mtime
            if age < _GRAPH_COOLDOWN_S:
                return
        regenerate_html_graph(state_dir)
    except Exception:
        log.warning("html graph regeneration failed", exc_info=True)


def regenerate_html_graph(state_dir: Path) -> Path:
    """Build a self-contained Plotly.js HTML graph from JSONL data."""
    records = load_timeseries(state_dir)
    html_path = Path(state_dir) / GRAPH_HTML_FILENAME

    ts_list = [r.get("ts", "") for r in records]
    traces_data: dict[str, list] = {k: [] for k in _NUMERIC_FIELDS}
    sources: list[str] = []
    triggers: list[str] = []

    for r in records:
        for k in _NUMERIC_FIELDS:
            traces_data[k].append(r.get(k, 0))
        sources.append(r.get("source", ""))
        triggers.append(r.get("trigger", ""))

    labels_list: list[str] = []
    for r in records:
        dl = r.get("derived_labels", [])
        labels_list.append(", ".join(dl) if dl else "")

    data_json = json.dumps({
        "ts": ts_list,
        "traces": traces_data,
        "sources": sources,
        "triggers": triggers,
        "labels": labels_list,
    }, ensure_ascii=False)

    html = _GRAPH_HTML_TEMPLATE.replace("__DATA_JSON__", data_json)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")
    log.info("emotion graph HTML regenerated: %s (%d records)", html_path, len(records))
    return html_path


_GRAPH_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>かなえ感情グラフ</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #2C2F33; color: #DCDDDE; font-family: 'Noto Sans CJK JP', sans-serif; }
  #chart { width: 100vw; height: 100vh; }
</style>
</head>
<body>
<div id="chart"></div>
<script>
const D = __DATA_JSON__;
const COLORS = {
  valence: '#4CAF50', arousal: '#FF9800', loneliness: '#2196F3',
  affection: '#E91E63', curiosity: '#9C27B0', anxiety: '#F44336'
};
const traces = [];
for (const [key, vals] of Object.entries(D.traces)) {
  const hoverText = D.ts.map((t, i) => {
    let s = '<b>' + key + '</b>: ' + vals[i].toFixed(3);
    s += '<br>source: ' + D.sources[i];
    if (D.triggers[i]) s += '<br>trigger: ' + D.triggers[i];
    if (D.labels[i]) s += '<br>派生: ' + D.labels[i];
    return s;
  });
  traces.push({
    x: D.ts, y: vals, name: key, type: 'scatter', mode: 'lines+markers',
    line: { color: COLORS[key], width: 2 },
    marker: {
      color: COLORS[key], size: 6,
      symbol: D.sources.map(s => s === 'silence' ? 'triangle-up' : 'circle')
    },
    hovertext: hoverText, hoverinfo: 'text+x',
  });
}
const layout = {
  paper_bgcolor: '#2C2F33', plot_bgcolor: '#23272A',
  font: { color: '#DCDDDE', family: 'Noto Sans CJK JP, sans-serif' },
  legend: { bgcolor: 'rgba(35,39,42,0.8)', font: { size: 12 } },
  xaxis: {
    type: 'date', gridcolor: '#40444B', gridwidth: 1,
    rangeslider: { visible: true, bgcolor: '#2C2F33',
                   bordercolor: '#40444B', thickness: 0.08 },
    rangeselector: {
      bgcolor: '#40444B', activecolor: '#5865F2',
      font: { color: '#DCDDDE' },
      buttons: [
        { count: 1, label: '1h', step: 'hour', stepmode: 'backward' },
        { count: 6, label: '6h', step: 'hour', stepmode: 'backward' },
        { count: 1, label: '1d', step: 'day', stepmode: 'backward' },
        { count: 7, label: '7d', step: 'day', stepmode: 'backward' },
        { count: 1, label: '1M', step: 'month', stepmode: 'backward' },
        { step: 'all', label: 'ALL' }
      ]
    }
  },
  yaxis: { range: [-1.05, 1.05], gridcolor: '#40444B', zeroline: true,
           zerolinecolor: '#40444B', zerolinewidth: 1 },
  hovermode: 'x unified',
  margin: { t: 30, b: 10, l: 50, r: 20 },
};
const config = {
  scrollZoom: true, displaylogo: false, responsive: true,
  modeBarButtonsToRemove: ['lasso2d', 'select2d'],
};
Plotly.newPlot('chart', traces, layout, config);
</script>
</body>
</html>
"""
