"""Explicit, owner-scoped memory and preference controls.

All responses are ephemeral, including deletion selectors. The initial rollout
retains the existing admin admission rule without coupling persona to authority.
"""
from __future__ import annotations

import discord
from discord import app_commands

from bot_commands._deps import CommandDeps
from services.memory_policy import ControlResult, get_memory_policy
from utils.discord_helpers import split_message


async def _admit(interaction, deps):
    if not deps.is_admin(interaction.user):
        await interaction.response.send_message("この記憶操作は現在管理者が利用できます。", ephemeral=True)
        return False
    await interaction.response.defer(ephemeral=True)
    return True


def _source(interaction):
    return dict(channel_id=str(interaction.channel_id or ""),
                is_private=interaction.guild_id is None,
                source_message_id=str(interaction.id), source_timestamp=interaction.created_at)


async def _send(interaction, result):
    for chunk in split_message(result.message):
        await interaction.followup.send(chunk, ephemeral=True)


async def _ensure_owned(policy, owner_id, record_id):
    try:
        return policy.get(owner_id, record_id)["id"]
    except ValueError:
        # A caller cannot supply a foreign backend id and bypass the namespace query.
        from services.mem0 import get_all_memories
        rows = await get_all_memories(str(owner_id), limit=None, strict=True)
        for row in rows or []:
            if str(row.get("id")) == str(record_id):
                return policy.adopt_legacy(row, owner_id)
    raise ValueError("本人の記憶IDが見つかりません。/recall で確認してください。")


async def _rows(policy, owner_id, *, query="", as_of=None, known_at=None, include_inactive=False):
    if as_of is not None or known_at is not None:
        local = policy.recall(owner_id, is_private=True, query=query, as_of=as_of,
                              known_at=known_at, include_inactive=include_inactive, inspection=True)
    else:
        local = policy.inspect_records(owner_id, query=query, include_inactive=include_inactive)
    warning = ""
    legacy = []
    try:
        from services.mem0 import get_all_memories
        legacy = await get_all_memories(str(owner_id), limit=None, strict=True) or []
    except Exception:
        warning = "Mem0の取得は未完了です。以下は台帳で確認できた記憶です。\n"
    # The ledger is authoritative for adopted entries. Do not display their stale projection.
    adopted = {r.get("external_id") for r in policy._all() if r["owner_id"] == str(owner_id)}
    legacy = [r for r in legacy if str(r.get("id")) not in adopted
              and (not query or query.casefold() in str(r.get("memory", "")).casefold())]
    legacy = policy.filter_memories(legacy, owner_id, is_private=True, as_of=as_of, known_at=known_at)
    return local + legacy, warning


def _render(row):
    m = row.get("metadata") or {}
    details = []
    for label, key in (("所有者", "owner_id"), ("話者", "speaker_id"), ("対象", "subject_id"),
                       ("種別", "fact_kind"), ("共有範囲", "scope"), ("状態", "status"),
                       ("発言時刻", "source_timestamp"), ("学習時刻", "learned_at"),
                       ("有効開始", "valid_from"), ("有効終了", "valid_to"),
                       ("出典ID", "source_message_ids")):
        if m.get(key) is not None:
            details.append(f"{label}: {m[key]}")
    if not m.get("scope"):
        details.append("共有範囲: 既存設定（追加指定なし）")
    return f"ID: {row['id']}\n{row.get('memory', '')}\n" + " / ".join(details)


class _ForgetSelect(discord.ui.Select):
    def __init__(self, rows, owner_id):
        self.owner_id = str(owner_id)
        self._by_value = {str(i): str(row["id"]) for i, row in enumerate(rows[:25]) if row.get("id")}
        options = [discord.SelectOption(label=(str(row.get("memory", ""))[:95] or "記憶"), value=str(i))
                   for i, row in enumerate(rows[:25]) if row.get("id")]
        super().__init__(placeholder="保存先を確認して削除する記憶を選択します。", min_values=1,
                         max_values=max(1, len(options)), options=options)

    async def callback(self, interaction):
        if str(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("本人のみ操作できます。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        policy = get_memory_policy()
        messages = []
        for value in self.values:
            record_id = self._by_value.get(value)
            if not record_id:
                continue
            try:
                owned_id = await _ensure_owned(policy, self.owner_id, record_id)
                result = await policy.delete(owned_id, self.owner_id, source_message_id=str(interaction.id), source_timestamp=interaction.created_at)
                messages.append(result.message)
            except Exception:
                messages.append("削除の完了を確認できません。記憶IDを指定して再試行してください。")
        for chunk in split_message("\n".join(messages) or "削除対象がありません。"):
            await interaction.followup.send(chunk, ephemeral=True)


class _ForgetView(discord.ui.View):
    def __init__(self, rows, owner_id):
        super().__init__(timeout=120)
        self.add_item(_ForgetSelect(rows, owner_id))


def register(tree: app_commands.CommandTree, deps: CommandDeps) -> None:
    @tree.command(name="remember", description="出典・対象・共有範囲を付けて明示的に覚えます。")
    @app_commands.describe(text="保存内容", subject="対象人物（省略で本人）", scope="auto/shared/private/case",
                           kind="fact/plan/hearsay/inference", case_id="案件内の場合の案件ID",
                           valid_from="有効開始（タイムゾーン付きISO日時）", valid_to="有効終了（同形式）")
    async def remember_cmd(interaction: discord.Interaction, text: str, subject: str | None = None,
                           scope: str = "auto", kind: str = "fact", case_id: str | None = None,
                           valid_from: str | None = None, valid_to: str | None = None):
        if not await _admit(interaction, deps):
            return
        try:
            result = get_memory_policy().remember(text, str(interaction.user.id), subject_id=subject,
                scope=None if scope == "auto" else scope, fact_kind=kind, case_id=case_id,
                valid_from=valid_from, valid_to=valid_to, **_source(interaction))
            await _send(interaction, result)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            await interaction.followup.send("保存を確認できません。保存完了として扱いません。", ephemeral=True)

    @tree.command(name="recall", description="本人の記憶・出典・有効期間・共有範囲を確認します。")
    @app_commands.describe(query="検索語（省略で一覧）", as_of="いつの事実か（タイムゾーン付きISO日時）",
                           known_at="いつ把握していた内容か（同形式）", history="失効記録も本人だけに確認表示します。")
    async def recall_cmd(interaction: discord.Interaction, query: str | None = None,
                         as_of: str | None = None, known_at: str | None = None, history: bool = False):
        if not await _admit(interaction, deps):
            return
        try:
            rows, warning = await _rows(get_memory_policy(), str(interaction.user.id), query=query or "",
                                        as_of=as_of, known_at=known_at, include_inactive=history)
            message = warning + ("\n\n".join(_render(r) for r in rows[:30]) if rows else "該当する記憶はありません。")
            for chunk in split_message(message):
                await interaction.followup.send(chunk, ephemeral=True)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            await interaction.followup.send("記憶の取得を完了できません。空の一覧として扱いません。", ephemeral=True)

    @tree.command(name="correction", description="誤記録・実際の変化・予定取消・使用停止を区別して反映します。")
    @app_commands.describe(record_id="本人の記憶ID", kind="wrong/change/cancel/stop/delete",
                           replacement="訂正後の内容（wrong/changeで必須）", subject="訂正後の対象人物",
                           effective_at="変化・取消の有効日時（タイムゾーン付きISO形式）")
    async def correction_cmd(interaction: discord.Interaction, record_id: str, kind: str,
                             replacement: str | None = None, subject: str | None = None,
                             effective_at: str | None = None):
        if not await _admit(interaction, deps):
            return
        try:
            policy = get_memory_policy()
            owner = str(interaction.user.id)
            owned_id = await _ensure_owned(policy, owner, record_id)
            if kind == "delete":
                result = await policy.delete(owned_id, owner, source_message_id=str(interaction.id), source_timestamp=interaction.created_at)
            else:
                result = policy.correct(owned_id, owner, mode=kind, replacement=replacement,
                    subject_id=subject, effective_at=effective_at, **_source(interaction))
            await _send(interaction, result)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            await interaction.followup.send("訂正処理の完了を確認できません。/recall で状態を確認してください。", ephemeral=True)

    @tree.command(name="share", description="本人の記憶を本人限定・共有可・指定案件内に変更します。")
    @app_commands.describe(record_id="本人の記憶ID", scope="private/shared/case", case_id="案件内の場合の案件ID")
    async def share_cmd(interaction: discord.Interaction, record_id: str, scope: str, case_id: str | None = None):
        if not await _admit(interaction, deps):
            return
        try:
            policy = get_memory_policy()
            owner = str(interaction.user.id)
            owned_id = await _ensure_owned(policy, owner, record_id)
            await _send(interaction, policy.share(owned_id, owner, scope, case_id=case_id,
                source_message_id=str(interaction.id), source_timestamp=interaction.created_at))
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            await interaction.followup.send("共有指定の保存を確認できません。", ephemeral=True)

    @tree.command(name="preferences", description="本人の明示した呼称・説明量・話題の好みを保存します。")
    @app_commands.describe(key="address/hide_name/avoid_topic/verbosity/tone/listening（省略で一覧）",
                           value="値（verbosityはshort/normal/detailed、真偽はtrue/false）",
                           here="このチャンネルだけに適用します。", audience="auto/all/private/public", clear="指定した設定を解除します。")
    async def preferences_cmd(interaction: discord.Interaction, key: str | None = None,
                              value: str | None = None, here: bool = True, audience: str = "auto", clear: bool = False):
        if not await _admit(interaction, deps):
            return
        policy = get_memory_policy()
        owner = str(interaction.user.id)
        channel = str(interaction.channel_id or "") if here else None
        private = interaction.guild_id is None
        if audience == "auto":
            audience = "private" if private else "public"
        try:
            if not key:
                prefs = policy.preferences(owner, str(interaction.channel_id or ""), private)
                await interaction.followup.send("\n".join(f"{k}: {v}" for k, v in prefs.items()) or "明示された好みはありません。", ephemeral=True)
            elif clear:
                await _send(interaction, policy.clear_preference(owner, key, channel_id=channel, audience=audience))
            else:
                result = policy.set_preference(owner, key, value, channel_id=channel, audience=audience,
                    source_message_id=str(interaction.id), source_timestamp=interaction.created_at)
                await _send(interaction, result)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            await interaction.followup.send("応答の好みの保存を確認できません。", ephemeral=True)

    @tree.command(name="forget", description="本人の記憶を削除し、保存先ごとの完了範囲を表示します。")
    @app_commands.describe(query="削除候補を絞る検索語", record_id="記憶ID（削除待ちの再試行にも使用します。）")
    async def forget_cmd(interaction: discord.Interaction, query: str | None = None, record_id: str | None = None):
        if not await _admit(interaction, deps):
            return
        policy = get_memory_policy()
        owner = str(interaction.user.id)
        try:
            if record_id:
                owned_id = await _ensure_owned(policy, owner, record_id)
                await _send(interaction, await policy.delete(owned_id, owner, source_message_id=str(interaction.id), source_timestamp=interaction.created_at))
                return
            rows, warning = await _rows(policy, owner, query=query or "", include_inactive=True)
            if not rows:
                await interaction.followup.send(warning + "削除対象がありません。", ephemeral=True)
                return
            await interaction.followup.send(warning + "削除する記憶を選択してください。会話での使用停止のみは /correction の stop を指定します。",
                                            view=_ForgetView(rows[:25], owner), ephemeral=True)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            await interaction.followup.send("削除の完了を確認できません。記憶IDを指定して再試行してください。", ephemeral=True)
