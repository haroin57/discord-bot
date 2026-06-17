"""VI発火通知 scheduler (Phase 1: 引け後確報判定)

bot のメインループに常駐するコルーチンとして実行され、
毎営業日 18:00 JST に scripts/vi_signal_alert.py --stage final を
サブプロセスで起動する。

設計背景 (2026-06-15):
  - サーバー cron は `no new privileges` で sudo 封じ、ubuntu の
    crontab 編集も /var/spool/cron/ 権限なしで不可。
  - 既存 mem0-backup cron は /etc/cron.d/ 配下にあるが sudo 必要。
  - → bot 自身に scheduler を組み込む方式を採用。

仕様 (b0nchichi 確定):
  - 通知タイミング: 毎営業日 18:00 JST
  - 実行内容: scripts/vi_signal_alert.py --stage final
  - 営業日判定: jpholiday (土日 + 日本の祝日を除外)
  - Phase 1 は確報通知のみ。Phase 2 で 11:30 / 14:30 のザラ場速報を追加予定。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import jpholiday

    _HAS_JPHOLIDAY = True
except ImportError:
    _HAS_JPHOLIDAY = False
    logger.warning(
        "jpholiday not installed; business day check will be weekend-only"
    )


REPO_ROOT = Path(__file__).resolve().parents[1]
VI_SCRIPT = REPO_ROOT / "scripts" / "vi_signal_alert.py"
VI_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
LOG_FILE = REPO_ROOT / ".state" / "vi_signal_alert.log"

# 実行時刻: 18:00 JST (b0nchichi 確定, 2026-06-15)
# 16:00 引け → 16:00 前後で日経公式CSV確報反映 → 18:00 で確実に取れる。
# ⚠️ エントリーは「発火日終値 = 当日引け 15:00」が正。
# 18:00 通知では当日エントリーには間に合わない。
# この scheduler は「事後確認・記録用」の位置づけ。本命の引け前 14:30
# ザラ場速報通知は別途 Phase 1B として実装する (2026-06-15 認識訂正)。
SCHEDULE_HOUR = 18
SCHEDULE_MINUTE = 0


def is_business_day(d: date) -> bool:
    """土日と日本の祝日を非営業日として除外する。"""
    if d.weekday() >= 5:  # 土日
        return False
    if _HAS_JPHOLIDAY and jpholiday.is_holiday(d):
        return False
    return True


def compute_next_run(now: datetime) -> datetime:
    """次の営業日 SCHEDULE_HOUR:SCHEDULE_MINUTE を計算する。"""
    target = now.replace(
        hour=SCHEDULE_HOUR,
        minute=SCHEDULE_MINUTE,
        second=0,
        microsecond=0,
    )
    # 今日の目標時刻を過ぎてる場合は翌日へ進める
    if now >= target:
        target = target + timedelta(days=1)
    # 営業日まで進める
    while not is_business_day(target.date()):
        target = target + timedelta(days=1)
    return target


async def run_vi_check() -> int:
    """vi_signal_alert.py をサブプロセスで実行し、結果をログ追記。

    戻り値: 終了コード (0=正常)
    """
    if not VI_SCRIPT.exists():
        logger.error(f"VI script not found: {VI_SCRIPT}")
        return 127
    if not VI_PYTHON.exists():
        logger.error(f"VI python not found: {VI_PYTHON}")
        return 127

    logger.info("VI signal check: starting (stage=final)")
    proc = await asyncio.create_subprocess_exec(
        str(VI_PYTHON),
        str(VI_SCRIPT),
        "--stage",
        "final",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(REPO_ROOT),
    )
    stdout, stderr = await proc.communicate()
    exit_code = proc.returncode or 0
    logger.info(f"VI signal check: exit={exit_code}")
    stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
    stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
    if stdout_text:
        logger.info(f"VI signal check stdout:\n{stdout_text}")
    if stderr_text:
        logger.warning(f"VI signal check stderr:\n{stderr_text}")

    # 永続ログにも追記 (cron時代の .state/vi_signal_alert.log と互換)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().isoformat()
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"=== {timestamp} (exit={exit_code}) ===\n")
            if stdout_text:
                f.write(stdout_text)
            if stderr_text:
                f.write(stderr_text)
            f.write("\n")
    except OSError as e:
        logger.warning(f"VI signal check: failed to append log file: {e}")

    return exit_code


async def vi_signal_daemon() -> None:
    """毎営業日 18:00 に VI 発火判定を走らせる常駐タスク。

    bot の on_ready から `asyncio.create_task(vi_signal_daemon())` で起動する。
    例外は内部で捕捉して継続、CancelledError は伝播。
    """
    logger.info(
        f"VI signal daemon started "
        f"(schedule={SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} JST, "
        f"script={VI_SCRIPT.name}, python={VI_PYTHON})"
    )
    while True:
        try:
            now = datetime.now()
            next_run = compute_next_run(now)
            wait_seconds = (next_run - now).total_seconds()
            wait_hours = wait_seconds / 3600
            logger.info(
                f"VI daemon: next run at {next_run.isoformat()} "
                f"(wait {wait_hours:.2f}h)"
            )
            await asyncio.sleep(wait_seconds)

            # 実行直前に再判定 (営業日でなかったらスキップ)
            today = date.today()
            if is_business_day(today):
                await run_vi_check()
            else:
                logger.info(f"VI daemon: skip (non-business day: {today})")

            # 連続実行・スピンループ防止のため最低60秒待つ
            await asyncio.sleep(60)

        except asyncio.CancelledError:
            logger.info("VI signal daemon cancelled")
            raise
        except Exception as exc:
            logger.exception(f"VI signal daemon error: {exc}")
            # エラー時は1分待って再試行 (無限エラーループ防止)
            await asyncio.sleep(60)


def main() -> None:
    """独立 systemd service (`python -m services.vi_signal_scheduler`) のエントリ。

    bot 本体には結合させず、専用の vi-signal-scheduler.service として常駐させる
    (機能8, 2026-06-17)。bot が落ちても scheduler は生き続ける。営業日・時刻判定が
    `datetime.now()` に依存するため、service 側で `Environment=TZ=Asia/Tokyo` を必須に
    する (deploy/systemd/vi-signal-scheduler.service)。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(vi_signal_daemon())
    except KeyboardInterrupt:
        logger.info("VI signal daemon stopped (SIGINT/SIGTERM)")


if __name__ == "__main__":
    main()
