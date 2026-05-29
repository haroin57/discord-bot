"""
日経平均 × 日経VI proxy 月次散布図 (依頼ベース実行)

実行: python3 /home/ubuntu/discord-bot/scripts/monthly_nikkei_vi_proxy.py

データ取得状況:
- 日経VI 実値は無料API提供なし (Yahoo / Stooq / Investing.com 全滅)
- JPX 公式 (J-Quants) は有料、Free Plan は VI 含むか要検証
- 代替として HV22 (22日 Historical Volatility 年率化) + VRP 3% を proxy
- HV22 と日経VI の実証相関 0.75-0.85

出力: /tmp/discord_bot_attachments/nikkei_vi_scatter_YYYYMM.png
"""
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yfinance as yf
from matplotlib.collections import LineCollection
import japanize_matplotlib  # noqa: F401


def fetch_data(years_back: int = 1, extra_days: int = 35):
    """日経平均とVIXを取得。HV22計算のため1ヶ月余分に取得。"""
    end = datetime.now()
    start_extended = end - timedelta(days=365 * years_back + extra_days)
    start_display = end - timedelta(days=365 * years_back)

    nikkei = yf.download('^N225', start=start_extended, end=end, progress=False, auto_adjust=False)
    vix = yf.download('^VIX', start=start_extended, end=end, progress=False, auto_adjust=False)

    nikkei_close = nikkei['Close'].squeeze() if hasattr(nikkei['Close'], 'squeeze') else nikkei['Close']
    vix_close = vix['Close'].squeeze() if hasattr(vix['Close'], 'squeeze') else vix['Close']

    return nikkei_close, vix_close, start_display


def compute_vi_proxy(nikkei_close: pd.Series, window: int = 22, vrp: float = 3.0) -> pd.Series:
    log_returns = np.log(nikkei_close / nikkei_close.shift(1))
    hv22 = log_returns.rolling(window=window).std() * np.sqrt(252) * 100
    return hv22 + vrp, hv22


def render(df: pd.DataFrame, output_path: Path, corr_hv_vix: float):
    fig, axes = plt.subplots(1, 2, figsize=(16, 8), dpi=120,
                             gridspec_kw={'width_ratios': [1.4, 1]})

    # --- 左: 散布図 (時系列接続) ---
    ax = axes[0]
    points = np.array([df['nikkei'].values, df['vi_proxy'].values]).T.reshape(-1, 1, 2)
    segments = np.concatenate([points[:-1], points[1:]], axis=1)
    n = len(df) - 1
    colors = plt.cm.viridis(np.linspace(0, 1, n))
    lc = LineCollection(segments, colors=colors, linewidth=1.2, alpha=0.7)
    ax.add_collection(lc)

    sc = ax.scatter(df['nikkei'].values, df['vi_proxy'].values,
                    c=range(len(df)), cmap='viridis', s=12, alpha=0.6, edgecolor='none')

    start_point = df.iloc[0]
    end_point = df.iloc[-1]
    ax.plot(start_point['nikkei'], start_point['vi_proxy'], 'o',
            color='blue', markersize=14, markeredgecolor='white', markeredgewidth=1.5,
            label=f'開始 {df.index[0].strftime("%Y-%m-%d")} (日経 {start_point["nikkei"]:.0f}, VI {start_point["vi_proxy"]:.1f})',
            zorder=5)
    ax.plot(end_point['nikkei'], end_point['vi_proxy'], 's',
            color='red', markersize=14, markeredgecolor='white', markeredgewidth=1.5,
            label=f'最新 {df.index[-1].strftime("%Y-%m-%d")} (日経 {end_point["nikkei"]:.0f}, VI {end_point["vi_proxy"]:.1f})',
            zorder=5)

    # 極値マーカー
    for label, idx_key, color, bgcolor, offset in [
        ('VI最高', df['vi_proxy'].idxmax(), 'red', '#ffe6e6', (40, -5)),
        ('VI最低', df['vi_proxy'].idxmin(), 'green', '#e6ffe6', (50, 30)),
        ('日経最高', df['nikkei'].idxmax(), 'orange', '#fff5e6', (-130, -60)),
    ]:
        row = df.loc[idx_key]
        ax.annotate(
            f'{label}\n{idx_key.strftime("%Y-%m-%d")}\n日経 {row["nikkei"]:.0f}\nVI {row["vi_proxy"]:.1f}',
            xy=(row['nikkei'], row['vi_proxy']),
            xytext=offset, textcoords='offset points',
            fontsize=9, ha='left' if offset[0] > 0 else 'right',
            bbox=dict(boxstyle='round,pad=0.4', facecolor=bgcolor, edgecolor=color, alpha=0.85),
            arrowprops=dict(arrowstyle='->', color=color))

    # 回帰線
    z = np.polyfit(df['nikkei'].values, df['vi_proxy'].values, 1)
    xx = np.linspace(df['nikkei'].min(), df['nikkei'].max(), 50)
    yy = z[0] * xx + z[1]
    ax.plot(xx, yy, '--', color='gray', alpha=0.5, linewidth=1,
            label=f'回帰線 (傾き {z[0]*1000:.3f} %/千円)')

    cbar = plt.colorbar(sc, ax=ax, pad=0.02, shrink=0.6)
    n_ticks = 5
    tick_positions = np.linspace(0, len(df) - 1, n_ticks).astype(int)
    cbar.set_ticks(tick_positions)
    cbar.set_ticklabels([df.index[i].strftime('%Y-%m') for i in tick_positions])
    cbar.set_label('日付', fontsize=9)

    corr = df['nikkei'].corr(df['vi_proxy'])
    ax.set_xlabel('日経平均株価 (円)', fontsize=11)
    ax.set_ylabel('日経VI proxy (HV22 + VRP補正, %)', fontsize=11)
    ax.set_title(
        f'日経平均 × 日経VI proxy\n'
        f'({df.index[0].strftime("%Y-%m-%d")} 〜 {df.index[-1].strftime("%Y-%m-%d")}, n={len(df)}, 相関 {corr:+.3f})',
        fontsize=12)
    ax.legend(loc='upper left', fontsize=8.5, framealpha=0.9)
    ax.grid(True, alpha=0.3)
    ax.autoscale_view()

    # --- 右: 時系列推移 ---
    ax2 = axes[1]
    ax2_twin = ax2.twinx()
    l1 = ax2.plot(df.index, df['nikkei'], color='steelblue', linewidth=1.3, label='日経平均')
    l2 = ax2_twin.plot(df.index, df['vi_proxy'], color='crimson', linewidth=1.0, alpha=0.7, label='VI proxy')
    l3 = ax2_twin.plot(df.index, df['vix'], color='darkorange', linewidth=0.8, alpha=0.6, linestyle='--', label='VIX (米)')

    ax2.set_ylabel('日経平均 (円)', color='steelblue', fontsize=10)
    ax2_twin.set_ylabel('VI proxy / VIX (%)', color='crimson', fontsize=10)
    ax2.tick_params(axis='y', labelcolor='steelblue')
    ax2_twin.tick_params(axis='y', labelcolor='crimson')
    lines = l1 + l2 + l3
    ax2.legend(lines, [l.get_label() for l in lines], loc='upper left', fontsize=9, framealpha=0.9)
    ax2.set_title('時系列確認 (日経 vs VI proxy vs VIX)', fontsize=11)
    ax2.grid(True, alpha=0.3)
    fig.autofmt_xdate(rotation=45)

    fig.suptitle(f'日経平均 × 日経VI 1年間散布図 ({datetime.now().strftime("%Y-%m")} 月次版)',
                 fontsize=14, fontweight='bold', y=1.00)
    fig.text(0.02, -0.02,
             '※ 日経VI 実値は無料API未提供のため、HV22 + VRP補正 3% を proxy として使用。'
             f' US VIX との相関: {corr_hv_vix:.2f}',
             fontsize=8, color='gray', style='italic')

    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches='tight', facecolor='white')
    plt.close()


def main():
    output_dir = Path('/tmp/discord_bot_attachments')
    output_dir.mkdir(parents=True, exist_ok=True)
    yyyymm = datetime.now().strftime('%Y%m')
    output_path = output_dir / f'nikkei_vi_scatter_{yyyymm}.png'

    print('Fetching Nikkei 225 and VIX...')
    nikkei_close, vix_close, start_display = fetch_data(years_back=1, extra_days=35)
    vi_proxy, hv22 = compute_vi_proxy(nikkei_close)

    df = pd.DataFrame({
        'nikkei': nikkei_close,
        'hv22': hv22,
        'vi_proxy': vi_proxy,
        'vix': vix_close.reindex(nikkei_close.index, method='ffill'),
    }).dropna()
    df = df.loc[start_display:]

    corr_hv_vix = df['hv22'].corr(df['vix'])
    print(f"Date range: {df.index[0].date()} 〜 {df.index[-1].date()} (n={len(df)})")
    print(f"Nikkei: {df['nikkei'].min():.0f} 〜 {df['nikkei'].max():.0f}")
    print(f"VI proxy: {df['vi_proxy'].min():.1f} 〜 {df['vi_proxy'].max():.1f}")
    print(f"Correlation Nikkei vs VI proxy: {df['nikkei'].corr(df['vi_proxy']):+.3f}")
    print(f"Correlation HV22 vs VIX: {corr_hv_vix:+.3f}")

    render(df, output_path, corr_hv_vix)
    print(f"\nSaved: {output_path}")


if __name__ == '__main__':
    main()
