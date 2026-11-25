"""Explicit durable /remind and /watch requests; never invoke a model to authorize."""

from __future__ import annotations

from datetime import datetime
import inspect
from typing import Callable
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from bot_commands._deps import CommandDeps
from services.work.effects import ScopeError
from services.work.schedules import ScheduleService


_service: ScheduleService | None = None
_authorize_watch: Callable | None = None
_JST = ZoneInfo("Asia/Tokyo")


def configure(service: ScheduleService, authorize_watch: Callable | None = None) -> None:
    """Root supplies work owner/audience checks; registration is import-I/O free."""
    global _service, _authorize_watch
    _service = service
    _authorize_watch = authorize_watch
    if authorize_watch is not None:
        service.register_authorizer("work", authorize_watch)


def parse_time(text: str) -> float:
    """Require a calendar date and time; a missing timezone means Japan time."""
    if "T" not in text and " " not in text:
        raise ValueError("日時は 2026-09-10 08:00 の形式で指定してください。")
    try:
        value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("日時は 2026-09-10 08:00 の形式で指定してください。") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=_JST)
    return value.timestamp()


def _date(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, _JST).strftime("%Y-%m-%d %H:%M JST")


def register(tree: app_commands.CommandTree, deps: CommandDeps) -> None:
    async def ready(interaction):
        if not deps.is_admin(interaction.user):
            await interaction.response.send_message("管理者のみ利用できます。", ephemeral=True)
            return False
        if _service is None:
            await interaction.response.send_message("将来通知の保存機構を利用できません。登録していません。", ephemeral=True)
            return False
        if interaction.channel_id is None:
            await interaction.response.send_message("通知先のチャンネルを確認できません。", ephemeral=True)
            return False
        await interaction.response.defer(ephemeral=True)
        return True

    @tree.command(name="remind", description="日時を指定し、このチャンネルへの通知を保存します。")
    @app_commands.describe(when="通知日時です。例: 2026-09-10 08:00。時差省略時は日本時間です。",
                           text="通知する内容です。", expires="未送信時の有効期限です。省略時は指定時刻の24時間後です。")
    async def remind_cmd(interaction: discord.Interaction, when: str, text: str, expires: str | None = None):
        if not await ready(interaction):
            return
        try:
            schedule = _service.add_reminder(
                str(interaction.user.id), str(interaction.channel_id), text,
                run_at=parse_time(when), expires_at=parse_time(expires) if expires else None,
                request_id=f"discord:interaction:{interaction.id}:remind",
            )
        except (ValueError, ScopeError) as exc:
            await interaction.followup.send(f"登録できませんでした。{exc}", ephemeral=True)
            return
        if schedule.status != "active":
            await interaction.followup.send(
                f"この依頼の通知記録は `{schedule.id}` です。現在の状態は `{schedule.status}` です。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"通知を保存しました。IDは `{schedule.id}` です。\n"
            f"通知日時は {_date(schedule.next_due)}、有効期限は {_date(schedule.expires_at)} です。\n"
            "このチャンネルへのメッセージ投稿を行います。端末の鳴動や既読は確認しません。\n"
            "停止には `/watch cancel` を使用します。", ephemeral=True,
        )

    group = app_commands.Group(name="watch", description="依頼した監視と通知の保存・照会・停止を行います。")

    @group.command(name="add", description="用件の状態または変更を、このチャンネルに通知します。")
    @app_commands.describe(target="監視対象の用件IDです。", expires="監視の終了日時です。例: 2026-09-30 18:00。",
                           condition="通知する条件です。", interval_minutes="確認間隔です。",
                           cooldown_minutes="通知と次の通知との最短間隔です。",
                           differences_only="前回と異なる結果だけを通知します。", once="最初の通知で監視を終了します。")
    @app_commands.choices(condition=[
        app_commands.Choice(name="変更があった時", value="changed"),
        app_commands.Choice(name="完了した時", value="completed"),
        app_commands.Choice(name="回答待ちになった時", value="waiting"),
        app_commands.Choice(name="結果確認が必要になった時", value="needs_reconciliation"),
    ])
    async def add_cmd(interaction: discord.Interaction, target: str, expires: str,
                      condition: app_commands.Choice[str],
                      interval_minutes: app_commands.Range[int, 1, 1440] = 5,
                      cooldown_minutes: app_commands.Range[int, 1, 10080] = 5,
                      differences_only: bool = True, once: bool = False):
        if not await ready(interaction):
            return
        try:
            if _authorize_watch is None:
                raise ScopeError("用件の閲覧範囲を確認できません。")
            permitted = _authorize_watch(str(interaction.user.id), target, str(interaction.channel_id))
            if inspect.isawaitable(permitted):
                permitted = await permitted
            if not permitted:
                raise ScopeError("対象の用件をこのチャンネルへ通知する権限がありません。")
            schedule = _service.add_watch(
                str(interaction.user.id), str(interaction.channel_id), observer="work", target=target,
                criteria={} if condition.value == "changed" else {"status": condition.value},
                expires_at=parse_time(expires), interval_seconds=interval_minutes * 60,
                cooldown_seconds=cooldown_minutes * 60, diff_only=differences_only,
                stop_rule="first_match" if once else "expiry",
                request_id=f"discord:interaction:{interaction.id}:watch",
            )
        except (ValueError, ScopeError) as exc:
            await interaction.followup.send(f"登録できませんでした。{exc}", ephemeral=True)
            return
        if schedule.status != "active":
            await interaction.followup.send(
                f"この依頼の監視記録は `{schedule.id}` です。現在の状態は `{schedule.status}` です。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"監視を保存しました。IDは `{schedule.id}` です。\n"
            f"対象は `{target}`、終了日時は {_date(schedule.expires_at)} です。\n"
            f"確認間隔は {interval_minutes} 分、通知間隔は最低 {cooldown_minutes} 分です。\n"
            f"差分のみの通知は {'有効' if differences_only else '無効'}、"
            f"最初の通知での終了は {'有効' if once else '無効'} です。", ephemeral=True,
        )

    @group.command(name="list", description="自分が保存した監視と通知、その実行状態を確認します。")
    async def list_cmd(interaction: discord.Interaction, include_inactive: bool = False):
        if not await ready(interaction):
            return
        schedules = _service.list(str(interaction.user.id), include_inactive=include_inactive)
        if not schedules:
            await interaction.followup.send("該当する監視と通知はありません。", ephemeral=True)
            return
        states = {"active": "有効", "completed": "通知済み", "cancelled": "停止済み", "expired": "期限終了"}
        lines = []
        for schedule in schedules:
            lines.append(
                f"- `{schedule.id}` は{states.get(schedule.status, schedule.status)}です。"
                f"対象は `{schedule.target}`、通知先は <#{schedule.recipient}>、"
                f"終了は {_date(schedule.expires_at)}、確認済み投稿は {schedule.notifications} 件です。"
                + (f"未確認事項は {schedule.error} です。" if schedule.error else "")
            )
        from utils.discord_helpers import split_message
        for chunk in split_message("\n".join(lines)):
            await interaction.followup.send(chunk, ephemeral=True)

    @group.command(name="cancel", description="指定した監視または通知を停止します。")
    async def cancel_cmd(interaction: discord.Interaction, schedule_id: str):
        if not await ready(interaction):
            return
        try:
            schedule = _service.cancel(schedule_id, str(interaction.user.id))
        except ScopeError:
            await interaction.followup.send("本人が登録した対象がありません。", ephemeral=True)
            return
        message = f"`{schedule.id}` を停止しました。新しい投稿は開始しません。"
        if schedule.pending:
            message += "既に開始済みの投稿については、操作記録の結果確認が必要です。"
        await interaction.followup.send(message, ephemeral=True)

    @group.command(name="show", description="監視の条件と投稿の受付・確認状態を確認します。")
    async def show_cmd(interaction: discord.Interaction, schedule_id: str):
        if not await ready(interaction):
            return
        try:
            schedule = _service.get(schedule_id, str(interaction.user.id))
        except ScopeError:
            await interaction.followup.send("本人が登録した対象がありません。", ephemeral=True)
            return
        states = {
            "accepted": "依頼受付済み", "submitted": "実行要求送信段階", "remote_accepted": "連携先受付済み",
            "confirmed": "メッセージ作成確認済み", "needs_reconciliation": "結果要確認",
            "cancelled": "取消済み", "rejected": "範囲変更により未実行",
        }
        lines = [
            f"`{schedule.id}` の状態は `{schedule.status}` です。",
            f"対象は `{schedule.target}`、通知先は <#{schedule.recipient}> です。",
            f"終了日時は {_date(schedule.expires_at)} です。",
            f"確認間隔は {schedule.interval_seconds:g} 秒、通知間隔は最低 {schedule.cooldown_seconds:g} 秒です。",
            "操作の確認はDiscordのメッセージ作成までです。端末の鳴動や既読を含みません。",
        ]
        effects = [receipt for receipt in _service.executor.list_effects(str(interaction.user.id))
                   if receipt.schedule_id == schedule.id]
        for receipt in effects[:20]:
            lines.append(f"- `{receipt.id}` は{states.get(receipt.state, receipt.state)}です。"
                         + (f"投稿IDは `{receipt.remote_id}` です。" if receipt.remote_id else ""))
        if not effects:
            lines.append("投稿操作はまだ受け付けていません。")
        from utils.discord_helpers import split_message
        for chunk in split_message("\n".join(lines)):
            await interaction.followup.send(chunk, ephemeral=True)

    @group.command(name="reconcile", description="結果が不明な投稿を照合します。新しい投稿は送信しません。")
    async def reconcile_cmd(interaction: discord.Interaction, schedule_id: str):
        if not await ready(interaction):
            return
        try:
            schedule = _service.get(schedule_id, str(interaction.user.id))
        except ScopeError:
            await interaction.followup.send("本人が登録した対象がありません。", ephemeral=True)
            return
        pending = [receipt for receipt in _service.executor.list_effects(str(interaction.user.id))
                   if receipt.schedule_id == schedule.id
                   and receipt.state in {"submitted", "remote_accepted", "needs_reconciliation"}]
        confirmed = 0
        for receipt in pending:
            updated = await _service.executor.execute(receipt.id)
            confirmed += updated.state == "confirmed"
        await interaction.followup.send(
            f"投稿記録を照合しました。{confirmed} 件のメッセージ作成を確認し、"
            f"{len(pending) - confirmed} 件の結果が未確認です。新しい投稿は送信していません。",
            ephemeral=True,
        )

    tree.add_command(group)
