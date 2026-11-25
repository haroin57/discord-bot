"""Admin entry points for durable requests; successful replies follow DB commit."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from typing import Callable

import discord
from discord import app_commands

from bot_commands._deps import CommandDeps
from bot_commands.guards import require_admin_channel
from bot_commands.responses import InteractionResponse
from services.work.store import (
    Work, WorkError, WorkNotFound, WorkStateConflict, WorkValidationError, get_work_store,
)

log = logging.getLogger(__name__)
_STATE_LABELS = {
    "queued": "受付済み", "running": "実行中", "waiting": "回答待ち", "paused": "休止中",
    "completed": "完了", "cancelled": "取消済み", "needs_reconciliation": "結果要確認",
}


def _policy():
    try:
        from services.memory_policy import get_memory_policy
    except ModuleNotFoundError as exc:
        if exc.name != "services.memory_policy":
            raise
        return None
    return get_memory_policy()


def _private(interaction) -> bool:
    recipient = getattr(interaction.channel, "recipient", None)
    return recipient is not None and str(recipient.id) == str(interaction.user.id)


def _safe_text(text: str, work: Work, interaction, policy) -> str:
    if policy is None:
        return text
    return policy.sanitize_context(text, work.owner_id, channel_id=str(interaction.channel_id),
                                   is_private=_private(interaction), case_id=work.id)


def _artifact_reusable(artifact: dict, work: Work, interaction, policy) -> bool:
    if artifact["invalidated"] or artifact["audience"] != work.audience:
        return False
    if policy is None:
        return True
    metadata = artifact.get("metadata") or {}
    return policy.can_reuse_context(metadata.get("memory_epoch"), owner_id=work.owner_id,
        channel_id=str(interaction.channel_id), is_private=_private(interaction),
        stored_audience=metadata.get("audience_key"))


async def _start_saved(work: Work, interaction) -> str:
    try:
        from services.work.runtime import get_work_runtime

        await get_work_runtime().start(work.id, str(interaction.user.id), interaction.channel)
    except Exception as exc:
        log.warning("Work start unavailable work_id=%s error_type=%s", work.id, type(exc).__name__)
        return "実行開始の受付は確認できませんでした。保存した用件を /work show で確認してください。"
    return "実行開始を受け付けました。"


def _interrupt_saved(work: Work) -> str:
    try:
        from services.work.runtime import get_work_runtime

        get_work_runtime().interrupt(work.id)
    except Exception as exc:
        log.warning("Work interrupt unavailable work_id=%s error_type=%s", work.id, type(exc).__name__)
        return "実行中処理への中断通知は確認できませんでした。保存済みの状態変更は有効です。"
    return "開始済みの外部操作の結果は別途照合が必要です。"


def register(tree: app_commands.CommandTree, deps: CommandDeps) -> None:
    group = app_commands.Group(name="work", description="継続する用件の受付・条件変更・再開を行います。")

    async def ready(interaction):
        response = InteractionResponse(interaction)
        context = await require_admin_channel(response, interaction, deps.is_admin)
        if context is None:
            return None
        await response.defer(ephemeral=True)
        return response

    async def send(response, text, **kwargs):
        return await response.send(text[:1950], ephemeral=True,
                                   allowed_mentions=discord.AllowedMentions.none(), **kwargs)

    async def failure(response, exc):
        if isinstance(exc, WorkError):
            # Ambiguity descriptions can contain old goal text; commands always use IDs.
            await send(response, f"処理できませんでした。{exc}")
        else:
            log.warning("Work command failed error_type=%s", type(exc).__name__)
            await send(response, "処理結果を確認できませんでした。用件IDを指定して /work show で確認してください。")

    async def run(interaction, operation: Callable):
        response = await ready(interaction)
        if response is None:
            return
        try:
            store = await asyncio.to_thread(get_work_store)
            await operation(store, response)
        except Exception as exc:
            await failure(response, exc)

    @group.command(name="create", description="新しい用件を保存して実行を開始します。")
    @app_commands.describe(goal="得たい結果です。", completion="完了条件です。省略できます。")
    async def create_cmd(interaction: discord.Interaction, goal: str, completion: str | None = None):
        async def operation(store, response):
            work = await asyncio.to_thread(store.create, str(interaction.user.id), str(interaction.channel_id),
                goal, f"discord:{interaction.id}:create", guild_id=None if interaction.guild_id is None else str(interaction.guild_id),
                completion_criteria=[completion] if completion else [])
            status = await _start_saved(work, interaction)
            await send(response, f"用件 {work.id} を保存しました。版は {work.revision} です。\n{status}")
        await run(interaction, operation)

    @group.command(name="list", description="自分が参加する用件を表示します。")
    async def list_cmd(interaction: discord.Interaction, all_channels: bool = False, include_closed: bool = False):
        async def operation(store, response):
            rows = await asyncio.to_thread(store.list_for, str(interaction.user.id),
                None if all_channels else str(interaction.channel_id), not include_closed)
            policy = await asyncio.to_thread(_policy)
            epoch = policy.epoch() if policy else None
            lines = []
            for work in rows[:15]:
                goal = _safe_text(work.goal, work, interaction, policy)[:70] or "内容の再確認が必要です。"
                lines.append(f"- {work.id} / {_STATE_LABELS[work.state]} / 版 {work.revision}。{goal}")
            if len(rows) > 15:
                lines.append(f"ほかに {len(rows) - 15} 件あります。チャンネルを絞って確認してください。")
            for work in rows[:15]:
                await validate_output(store, work, interaction, epoch)
            await send(response, "\n".join(lines) or "該当する用件はありません。")
        await run(interaction, operation)

    @group.command(name="show", description="用件IDで現在の条件・質問・成果物を確認します。")
    async def show_cmd(interaction: discord.Interaction, work_id: str):
        async def operation(store, response):
            def render():
                with store.transaction():
                    work = store.get(work_id, str(interaction.user.id))
                    policy = _policy()
                    epoch = policy.epoch() if policy else None
                    lines = [f"用件 {work.id}。{_STATE_LABELS[work.state]}。版 {work.revision}。共有範囲は {work.audience} です。"]
                    details = [f"目的: {work.goal}"]
                    for label, value in (("条件", work.requirements), ("決定", work.decisions),
                        ("進捗", work.progress), ("未確定事項", work.unknowns),
                        ("次の行動", work.next_action), ("完了条件", work.completion_criteria)):
                        if value:
                            details.append(f"{label}: {json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value}")
                    safe = _safe_text("\n".join(details), work, interaction, policy)
                    lines.append(safe[:1050] or "内容は現在の記憶・共有指定で再構成が必要です。")
                    for q in store.list_questions(work_id, str(interaction.user.id), open_only=True)[:4]:
                        text = _safe_text(q["text"], work, interaction, policy)[:130] or "質問内容は再確認が必要です。"
                        lines.append(f"質問 {q['id']}（{'必須' if q['required'] else '仮定可'}）: {text}")
                    artifacts = store.list_artifacts(work_id, str(interaction.user.id))
                    valid = [a for a in artifacts if _artifact_reusable(a, work, interaction, policy)]
                    lines.extend(f"成果物 {a['id']} / 第 {a['version']} 版。/work artifact で取得できます。" for a in valid[:4])
                    if len(valid) < len(artifacts):
                        lines.append("以前の成果物は記憶・共有指定の照合が必要なため表示を保留しています。")
                    return work, epoch, "\n".join(lines)
            work, epoch, content = await asyncio.to_thread(render)
            await validate_output(store, work, interaction, epoch)
            await send(response, content)
        await run(interaction, operation)

    async def validate_output(store, work, interaction, epoch):
        await asyncio.to_thread(store.assert_current, work.id, work.revision, str(interaction.user.id))
        policy = _policy()
        if policy is not None and not policy.is_current(epoch):
            raise WorkStateConflict("記憶または共有範囲が更新されています。表示を再取得してください。")

    @group.command(name="amend", description="用件の条件変更を保存します。")
    async def amend_cmd(interaction: discord.Interaction, work_id: str, text: str, expected_revision: int | None = None):
        async def operation(store, response):
            work = await asyncio.to_thread(store.amend, work_id, str(interaction.user.id), text,
                f"discord:{interaction.id}:amend", expected_revision=expected_revision)
            await send(response, f"用件 {work.id} の条件変更を保存しました。版は {work.revision} です。\n"
                       "実行中の処理は変更後の条件を照合します。休止中・受付済みの場合は /work resume で開始できます。")
        await run(interaction, operation)

    async def change_state(interaction, work_id, state):
        async def operation(store, response):
            def prepare():
                with store.transaction():
                    current = store.get(work_id, str(interaction.user.id))
                    if state == "paused" and current.state == "needs_reconciliation":
                        raise WorkStateConflict("外部操作の結果要確認状態です。/work reconcile で照合してください。")
                    return store.transition(work_id, str(interaction.user.id), state, current.revision)
            work = await asyncio.to_thread(prepare)
            note = _interrupt_saved(work)
            await send(response, f"用件 {work.id} を{_STATE_LABELS[state]}にしました。版は {work.revision} です。\n{note}")
        await run(interaction, operation)

    @group.command(name="pause", description="指定した用件を休止します。")
    async def pause_cmd(interaction: discord.Interaction, work_id: str):
        await change_state(interaction, work_id, "paused")

    @group.command(name="cancel", description="指定した用件を取り消します。")
    async def cancel_cmd(interaction: discord.Interaction, work_id: str):
        await change_state(interaction, work_id, "cancelled")

    @group.command(name="resume", description="保存した用件を再開します。")
    async def resume_cmd(interaction: discord.Interaction, work_id: str):
        async def operation(store, response):
            def prepare():
                with store.transaction():
                    work = store.get(work_id, str(interaction.user.id))
                    if work.state == "running":
                        return work
                    return store.transition(work.id, str(interaction.user.id), "queued", work.revision)
            work = await asyncio.to_thread(prepare)
            status = await _start_saved(work, interaction)
            await send(response, f"用件 {work.id} の再開要求を保存しました。版は {work.revision} です。\n{status}")
        await run(interaction, operation)

    @group.command(name="reconcile", description="保存した操作結果を照合します。不明な操作を再送しません。")
    async def reconcile_cmd(interaction: discord.Interaction, work_id: str):
        async def operation(store, response):
            await asyncio.to_thread(store.get, work_id, str(interaction.user.id))
            from services.work.runtime import get_work_runtime

            result = await get_work_runtime().reconcile(work_id, str(interaction.user.id), interaction.channel)
            if not isinstance(result, str) or not result.strip():
                raise WorkStateConflict("外部操作の照合結果を確認できませんでした。再送は行っていません。")
            await send(response, result)
        await run(interaction, operation)

    @group.command(name="answer", description="用件の質問IDを指定して回答します。")
    async def answer_cmd(interaction: discord.Interaction, work_id: str, question_id: str, answer: str):
        async def operation(store, response):
            work = await asyncio.to_thread(store.answer_question, work_id, question_id, str(interaction.user.id),
                                          answer, f"discord:{interaction.id}:answer")
            status = await _start_saved(work, interaction)
            await send(response, f"用件 {work.id} の質問 {question_id} への回答を保存しました。版は {work.revision} です。\n{status}")
        await run(interaction, operation)

    @group.command(name="artifact", description="成果物IDと版を指定して保存済み成果物を取得します。")
    async def artifact_cmd(interaction: discord.Interaction, work_id: str, artifact_id: str | None = None, version: int | None = None):
        async def operation(store, response):
            if version is not None and artifact_id is None:
                raise WorkValidationError("版を指定する場合は成果物IDも指定してください。")
            def collect():
                with store.transaction():
                    work = store.get(work_id, str(interaction.user.id))
                    policy = _policy()
                    epoch = policy.epoch() if policy else None
                    candidates = store.list_artifacts(work_id, str(interaction.user.id), history=version is not None)
                    candidates = [a for a in candidates if (artifact_id is None or a["id"] == artifact_id)
                                  and (version is None or a["version"] == version)]
                    reusable = [a for a in candidates if _artifact_reusable(a, work, interaction, policy)]
                    return work, epoch, reusable
            work, epoch, artifacts = await asyncio.to_thread(collect)
            if not artifacts:
                raise WorkNotFound("現在の条件・記憶・共有範囲で表示できる成果物がありません。再構成が必要です。")
            await validate_output(store, work, interaction, epoch)
            if artifact_id is None:
                await send(response, "\n".join(f"- {a['id']} / 第 {a['version']} 版。{str(a['title'])[:100]}" for a in artifacts[:15]))
                return
            artifact = artifacts[0]
            header = f"用件 {work.id}。成果物 {artifact['id']} の第 {artifact['version']} 版です。"
            body = artifact["body"]
            if body:
                encoded = body.encode("utf-8")
                if len(encoded) > 8 * 1024 * 1024:
                    raise WorkValidationError("成果物が添付可能なサイズを超えています。保存先から取得してください。")
                attachment = discord.File(io.BytesIO(encoded), filename=f"{artifact['id']}-v{artifact['version']}.md")
                try:
                    await send(response, header, file=attachment)
                finally:
                    attachment.close()
            else:
                await send(response, header + "\n保存先: " + json.dumps(artifact["storage_refs"], ensure_ascii=False))
        await run(interaction, operation)

    @group.command(name="share", description="用件の共有範囲と管理者の参加者を設定します。")
    @app_commands.choices(audience=[app_commands.Choice(name=label, value=value) for value, label in (
        ("shared", "共有可"), ("owner", "本人限定"), ("case", "案件内"), ("channel", "元のチャンネル内"))])
    async def share_cmd(interaction: discord.Interaction, work_id: str, audience: app_commands.Choice[str], participants: str | None = None):
        async def operation(store, response):
            fields = {"audience": audience.value}
            if participants is not None:
                ids = [part for part in re.split(r"[\s,、]+", participants.strip()) if part]
                if any(not uid.isdigit() for uid in ids):
                    raise WorkValidationError("参加者は数値のユーザーIDで指定してください。")
                for uid in ids:
                    user = deps.bot.get_user(int(uid))
                    if user is None or not deps.is_admin(user):
                        raise WorkValidationError("参加者は確認済みの管理者IDで指定してください。")
                fields["participants"] = ids
            work = await asyncio.to_thread(store.amend, work_id, str(interaction.user.id), "",
                                          f"discord:{interaction.id}:share", **fields)
            _interrupt_saved(work)
            await send(response, f"用件 {work.id} の共有範囲を {work.audience} に保存しました。版は {work.revision} です。\n"
                "以前の成果物は再利用を停止しました。再開には /work resume を使用します。既に投稿した内容の削除は別操作です。")
        await run(interaction, operation)

    tree.add_command(group)
