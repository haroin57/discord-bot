"""Persistent question choices backed by WorkStore, not interaction tokens."""

from __future__ import annotations

import discord


class WorkQuestionView(discord.ui.View):
    def __init__(self, work_id: str, question: dict, runtime):
        super().__init__(timeout=None)
        self.work_id, self.question, self.runtime = work_id, question, runtime
        choices = question.get("choices") or []
        if choices:
            select = discord.ui.Select(custom_id=f"work-answer:{question['id']}",
                placeholder="回答を選択します。", min_values=1, max_values=1,
                options=[discord.SelectOption(label=str(value)[:100], value=str(index))
                         for index, value in enumerate(choices[:25])])
            select.callback = self.answer
            self.add_item(select)

    async def answer(self, interaction: discord.Interaction):
        from services.work.store import WorkError

        await interaction.response.defer(ephemeral=True)
        try:
            index = int((interaction.data.get("values") or [""])[0])
            answer = self.question["choices"][index]
            work = self.runtime.store.answer_question(self.work_id, self.question["id"],
                str(interaction.user.id), str(answer), f"interaction:{interaction.id}")
            await interaction.followup.send(f"回答を第{work.revision}版に保存しました。", ephemeral=True)
            await self.runtime.start(work.id, str(interaction.user.id), interaction.channel)
        except (WorkError, ValueError, IndexError, PermissionError) as exc:
            await interaction.followup.send(f"回答を処理できませんでした。{exc}", ephemeral=True)


def restore_question_views(bot, runtime):
    # Owner IDs come from trusted saved work; the callback still reauthorizes
    # the user for every click and only accepts unresolved question IDs.
    with runtime.store.transaction() as db:
        rows = db.execute("SELECT id FROM works").fetchall()
    for row in rows:
        work = runtime.store.get(row["id"])
        if work.state in {"cancelled", "completed"}:
            continue
        for question in runtime.store.list_questions(work.id, open_only=True):
            if question.get("choices"):
                message_id = runtime._question_message(work.id, question["id"])
                if message_id:
                    bot.add_view(WorkQuestionView(work.id, question, runtime), message_id=int(message_id))
