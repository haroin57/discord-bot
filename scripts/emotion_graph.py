#!/usr/bin/env python3
"""Generate emotion timeseries graphs from JSONL log.

Usage:
    python scripts/emotion_graph.py                    # last 24h
    python scripts/emotion_graph.py --days 7           # last 7 days
    python scripts/emotion_graph.py --hours 6 --output /tmp/graph.png
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

JST = timezone(timedelta(hours=9))

COLORS = {
    "valence": "#4CAF50",
    "arousal": "#FF9800",
    "loneliness": "#2196F3",
    "affection": "#E91E63",
    "curiosity": "#9C27B0",
    "anxiety": "#F44336",
}
COMPONENTS = list(COLORS.keys())

DARK_THEME = {
    "figure.facecolor": "#2C2F33",
    "axes.facecolor": "#23272A",
    "axes.edgecolor": "#40444B",
    "text.color": "#DCDDDE",
    "axes.labelcolor": "#DCDDDE",
    "xtick.color": "#DCDDDE",
    "ytick.color": "#DCDDDE",
    "grid.color": "#40444B",
    "grid.alpha": 0.3,
}


def load_as_dataframe(state_dir: Path, since: datetime, until: datetime):
    import pandas as pd
    from services.emotion.timeseries import TIMESERIES_FILENAME

    ts_path = state_dir / TIMESERIES_FILENAME
    if not ts_path.is_file() or ts_path.stat().st_size == 0:
        return pd.DataFrame()

    records = []
    with ts_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df[(df["ts"] >= since) & (df["ts"] <= until)]
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def generate_graph(
    df,
    output: Path,
    *,
    components: list[str] | None = None,
    dpi: int = 150,
) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    components = components or COMPONENTS

    plt.rcParams.update(DARK_THEME)
    try:
        plt.rcParams["font.family"] = ["Noto Sans CJK JP", "sans-serif"]
    except Exception:
        plt.rcParams["font.family"] = ["sans-serif"]

    fig, (ax_main, ax_events) = plt.subplots(
        2, 1,
        figsize=(1600 / dpi, 900 / dpi),
        dpi=dpi,
        gridspec_kw={"height_ratios": [3, 1]},
        sharex=True,
    )

    for comp in components:
        if comp not in df.columns:
            continue
        mask_conv = df["source"] == "conversation"
        mask_silence = df["source"] == "silence"

        ax_main.plot(
            df["ts"], df[comp],
            color=COLORS[comp], label=comp, linewidth=1.2, alpha=0.9,
        )
        if mask_conv.any():
            ax_main.scatter(
                df.loc[mask_conv, "ts"], df.loc[mask_conv, comp],
                color=COLORS[comp], marker="o", s=20, alpha=0.7, zorder=5,
            )
        if mask_silence.any():
            ax_main.scatter(
                df.loc[mask_silence, "ts"], df.loc[mask_silence, comp],
                color=COLORS[comp], marker="^", s=20, alpha=0.7, zorder=5,
            )

    ax_main.set_ylabel("Value")
    ax_main.set_ylim(-1.05, 1.05)
    ax_main.legend(loc="upper left", fontsize=8, framealpha=0.5)
    ax_main.grid(True, alpha=0.3)

    triggers = df[df["trigger"].fillna("").str.len() > 0].copy()
    if len(triggers) > 20:
        step = max(1, len(triggers) // 20)
        triggers = triggers.iloc[::step].head(20)

    ax_events.set_ylabel("Events")
    ax_events.set_yticks([])
    for _, row in triggers.iterrows():
        ax_events.axvline(row["ts"], color="#DCDDDE", alpha=0.3, linewidth=0.5)
        ax_events.annotate(
            row["trigger"],
            xy=(row["ts"], 0.5),
            xycoords=("data", "axes fraction"),
            rotation=90,
            fontsize=6,
            color="#DCDDDE",
            alpha=0.8,
            ha="center",
            va="bottom",
        )

    ax_events.xaxis.set_major_formatter(
        mdates.DateFormatter("%m/%d %H:%M", tz=JST)
    )
    plt.xticks(rotation=45, ha="right")
    fig.tight_layout()

    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return output


def main():
    parser = argparse.ArgumentParser(description="Generate emotion timeseries graph")
    parser.add_argument(
        "--state-dir", type=Path, default=REPO_DIR / ".state",
        help="State directory (default: .state)",
    )
    parser.add_argument("--hours", type=int, default=24, help="Last N hours")
    parser.add_argument("--days", type=int, default=None, help="Last N days (overrides --hours)")
    parser.add_argument("--output", type=Path, default=None, help="Output PNG path")
    parser.add_argument(
        "--components", type=str, default=None,
        help="Comma-separated components to plot",
    )
    parser.add_argument("--dpi", type=int, default=150, help="DPI (default: 150)")
    args = parser.parse_args()

    now = datetime.now(JST)
    if args.days is not None:
        since = now - timedelta(days=args.days)
    else:
        since = now - timedelta(hours=args.hours)
    until = now

    if args.output is None:
        ts_str = now.strftime("%Y%m%d_%H%M%S")
        att_dir = Path(tempfile.gettempdir()) / "discord_bot_attachments"
        att_dir.mkdir(parents=True, exist_ok=True)
        output = att_dir / f"emotion_graph_{ts_str}.png"
    else:
        output = args.output

    components = None
    if args.components:
        components = [c.strip() for c in args.components.split(",")]

    df = load_as_dataframe(args.state_dir, since, until)
    if df.empty:
        print("No data found in the specified time range.", file=sys.stderr)
        sys.exit(1)

    result = generate_graph(df, output, components=components, dpi=args.dpi)
    print(f"Graph saved to: {result}")


if __name__ == "__main__":
    main()
