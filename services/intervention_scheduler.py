"""為替介入アラート scheduler (ADR-0027)

30分ごとに scripts/intervention_alert.py をサブプロセスで起動する常駐タスク。
VIの vi_signal_scheduler.py (18:00定時, ADR-0026) とは別系統の30分ループ。

起動方法:
  - systemd: python -m services.intervention_scheduler (intervention-scheduler.service)
  - bot常駐: on_ready から asyncio.create_task(intervention_daemon())
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
ALERT_SCRIPT = REPO_ROOT / "scripts" / "intervention_alert.py"
VI_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
LOG_FILE = REPO_ROOT / ".state" / "intervention_alert.log"

INTERVAL_MINUTES = 30


async def run_intervention_check() -> int:
    """intervention_alert.py をサブプロセス実行し、結果をログ追記。"""
    if not ALERT_SCRIPT.exists():
        logger.error(f"intervention script not found: {ALERT_SCRIPT}")
        return 127
    if not VI_PYTHON.exists():
        logger.error(f"python not found: {VI_PYTHON}")
        return 127

    logger.info("intervention check: starting")
    proc = await asyncio.create_subprocess_exec(
        str(VI_PYTHON),
        str(ALERT_SCRIPT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(REPO_ROOT),
    )
    stdout, stderr = await proc.communicate()
    exit_code = proc.returncode or 0
    stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
    stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
    logger.info(f"intervention check: exit={exit_code}")
    if stdout_text:
        logger.info(f"intervention stdout:\n{stdout_text}")
    if stderr_text:
        logger.warning(f"intervention stderr:\n{stderr_text}")

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"=== {datetime.now().isoformat()} (exit={exit_code}) ===\n")
            if stdout_text:
                f.write(stdout_text)
            if stderr_text:
                f.write(stderr_text)
            f.write("\n")
    except OSError as e:
        logger.warning(f"intervention check: log append failed: {e}")

    return exit_code


async def intervention_daemon() -> None:
    """30分境界(00分/30分)ごとに介入アラートを走らせる常駐タスク。

    例外は内部で捕捉して継続、CancelledError は伝播。
    """
    logger.info(f"intervention daemon started (every {INTERVAL_MINUTES}min)")
    while True:
        try:
            now = datetime.now()
            minutes_to_next = INTERVAL_MINUTES - (now.minute % INTERVAL_MINUTES)
            next_run = (now + timedelta(minutes=minutes_to_next)).replace(
                second=0, microsecond=0
            )
            wait = (next_run - now).total_seconds()
            logger.info(
                f"intervention daemon: next run at {next_run:%H:%M} ({wait:.0f}s)"
            )
            await asyncio.sleep(wait)
            try:
                await run_intervention_check()
            except Exception as exc:
                logger.exception(f"run_intervention_check failed: {exc}")
            await asyncio.sleep(60)  # スピンループ防止
        except asyncio.CancelledError:
            logger.info("intervention daemon: cancelled")
            raise
        except Exception as exc:
            logger.exception(f"intervention daemon error: {exc}")
            await asyncio.sleep(300)


def main() -> None:
    """systemd から -m services.intervention_scheduler で起動。"""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    asyncio.run(intervention_daemon())


if __name__ == "__main__":
    main()
