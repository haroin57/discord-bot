"""Durable research records and selective dependency invalidation.

The orchestrator owns live agents; this module owns provider-independent facts
about their outputs. No background process is required while waiting for input.
"""

from __future__ import annotations

import json
import re
import io
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


class ResearchInterrupted(Exception):
    """Research reached a durable waiting/paused boundary."""


def parse_record(text: str) -> dict:
    """Read a JSON record without interpreting model output as executable code."""
    raw = (text or "").strip()
    for candidate in (raw, *re.findall(r"```(?:json)?\s*(.*?)```", raw, re.S)):
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def requirement_values(requirements: Any) -> dict:
    if isinstance(requirements, dict):
        return requirements.copy()
    if isinstance(requirements, list):
        return {
            str(value.get("id", index)) if isinstance(value, dict) else str(index): value
            for index, value in enumerate(requirements)
        }
    return {}


def refresh_dependencies(state, requirements: Any, *, now: datetime | None = None) -> list[str]:
    """Invalidate only evaluations whose inputs changed or expired.

Unknown requirement dependencies use ``*`` and are conservatively refreshed.
Unknown publication timestamps are never filled with the retrieval timestamp.
"""
    current = requirement_values(requirements)
    def semantic(key, value):
        if key == "answers" and isinstance(value, dict):
            return {qid: answer for qid, answer in value.items()
                    if str(answer).strip().lower() not in {"承認", "approve"}}
        return value
    changed = {
        key for key in set(current) | set(state.requirements)
        if semantic(key, current.get(key, {} if key == "answers" else None))
        != semantic(key, state.requirements.get(key, {} if key == "answers" else None))
    }
    now = now or datetime.now(timezone.utc)
    by_sid = {ref.sid: ref for ref in state.sources.values()}
    stale: list[str] = []
    for subq, evaluation in state.evaluations.items():
        keys = set(evaluation.get("requirement_keys", ["*"]))
        reasons = []
        if changed and ("amendments" in changed or "*" in keys or keys & changed):
            reasons.append("conditions_changed:" + ",".join(sorted(changed)))
        versions = evaluation.get("source_versions", {})
        for sid in evaluation.get("evidence_sids", []):
            ref = by_sid.get(sid)
            if ref is None:
                reasons.append(f"source_missing:{sid}")
                continue
            if versions.get(sid, ref.version) != ref.version:
                reasons.append(f"source_changed:{sid}")
            if ref.fresh_for_s is not None:
                try:
                    fetched = datetime.fromisoformat(ref.fetched_at)
                    if fetched.tzinfo is None:
                        fetched = fetched.replace(tzinfo=timezone.utc)
                    if (now - fetched).total_seconds() >= ref.fresh_for_s:
                        reasons.append(f"source_expired:{sid}")
                except (ValueError, TypeError):
                    reasons.append(f"source_time_unknown:{sid}")
        if reasons:
            evaluation["stale"] = True
            evaluation["stale_reasons"] = reasons
            stale.append(subq)
    state.requirements = current
    if changed or stale:
        state.approval_revision = None
        state.completion_status = "in_progress"
        state.report = ""
        state.lens_outputs = []
        state.agent_results = {}
    return stale


def record_evaluation(state, subq: str, result, metadata: dict | None = None) -> None:
    from services.deep_research.sources import extract_cited_sids
    metadata = metadata or {}
    keys = metadata.get("requirement_keys", ["*"])
    if not isinstance(keys, list) or any(key not in state.requirements and key != "*" for key in keys):
        keys = ["*"]
    sids = extract_cited_sids(result.summary_md)
    by_sid = {ref.sid: ref for ref in state.sources.values()}
    state.evaluations[subq] = {
        "requirement_keys": keys,
        "evidence_sids": sids,
        "source_versions": {sid: by_sid[sid].version for sid in sids if sid in by_sid},
        "stale": not result.ok,
        "stale_reasons": [] if result.ok else [result.error or "search_failed"],
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "revision": state.work_revision,
    }


def assessment_briefing(state, report: str) -> str:
    """Request public decisions and unmet criteria, never hidden reasoning."""
    return (
        "調査の公開可能な判断記録をJSONで返してください。推論過程は不要です。"
        "複数エージェントの合意や文量は根拠に数えません。未確認事項を隠さないでください。\n"
        "schema: {answered: bool, criteria: [{criterion: str, met: bool, evidence_sids: [str], reason: str}], "
        "unknowns: [str], decision: {options: [str], mandatory_conditions: [str], evaluation_axes: [str], "
        "selected: str, reasons: [str], change_conditions: [str]}, "
        "learning: {known: [str], unknown: [str], check_questions: [str], next_explanation: str}, "
        "evidence_quality: {primary_sids: [str], secondary_sids: [str], counterevidence: [str], unverified: [str]}}\n"
        "一次資料・二次資料の区分は資料内容から判断し、断定できなければunverifiedに残します。\n"
        f"目的: {state.mode}。decisionならdecisionを、learningならlearningを埋めます。\n"
        f"問い: {state.clarified_query}\n必須条件: {json.dumps(state.requirements, ensure_ascii=False)}\n"
        f"完了条件: {json.dumps(state.completion_criteria, ensure_ascii=False)}\n"
        f"登録済出典: {json.dumps([ref.sid for ref in state.sources.values()])}\n"
        f"成果物:\n{report[:50000]}"
    )


def apply_assessment(state, report: str, assessment: dict) -> None:
    from services.deep_research.evidence_registry import EvidenceRegistry
    from services.deep_research.sources import extract_cited_sids, verify_report_citations
    raw_unknowns = assessment.get("unknowns", [])
    unknowns = [str(value) for value in raw_unknowns if value] if isinstance(raw_unknowns, list) else ["未確認事項の記録形式を検証できていません。"]
    if state.incomplete_reason:
        unknowns.append(state.incomplete_reason)
    if not assessment or assessment.get("answered") is not True:
        unknowns.append("問いへの回答の充足を確認できていません。")
    if any(question.get("required") and question.get("status") not in {"answered", "superseded"} for question in state.questions.values()):
        unknowns.append("必須質問への回答が未取得です。")
    for subq in state.plan:
        evaluation = state.evaluations.get(subq)
        if evaluation is None or evaluation.get("stale"):
            unknowns.append(f"再確認が必要です: {subq}")
    narrative = re.split(r"(?m)^\*\*情報ソース(?:（自動収集）)?\*\*", report, maxsplit=1)[0]
    present, missing = verify_report_citations(narrative, state)
    if not present or missing:
        unknowns.append("引用可能な根拠が不足しています。")
    if any(not check.supported for check in EvidenceRegistry(state).verify(narrative)):
        unknowns.append("一部の記述と引用資料の支持関係を確認できていません。")
    criteria = assessment.get("criteria", [])
    criteria = criteria if isinstance(criteria, list) else []
    by_criterion = {str(value.get("criterion")): value for value in criteria if isinstance(value, dict)}
    known = {ref.sid for ref in state.sources.values()}
    for criterion in state.completion_criteria:
        item = by_criterion.get(criterion, {})
        cited = item.get("evidence_sids")
        valid = isinstance(cited, list) and bool(cited) and all(isinstance(sid, str) and sid in known for sid in cited)
        if item.get("met") is not True or not valid:
            unknowns.append(f"完了条件が未確認です: {criterion}")
    state.decision_record = assessment.get("decision", {}) if state.mode == "decision" and isinstance(assessment.get("decision"), dict) else {}
    state.learning_record = assessment.get("learning", {}) if state.mode == "learning" and isinstance(assessment.get("learning"), dict) else {}
    quality = assessment.get("evidence_quality", {})
    state.evidence_quality = quality if isinstance(quality, dict) else {}
    for name in ("primary_sids", "secondary_sids"):
        supplied = state.evidence_quality.get(name, [])
        if isinstance(supplied, list) and any(not isinstance(sid, str) or sid not in known for sid in supplied):
            unknowns.append("資料の一次・二次区分に未登録の出典が含まれています。")
        state.evidence_quality[name] = [sid for sid in supplied if isinstance(sid, str) and sid in known] if isinstance(supplied, list) else []
    unverified = state.evidence_quality.get("unverified", [])
    if isinstance(unverified, list):
        unknowns.extend(str(value) for value in unverified if value)
    state.unknowns = list(dict.fromkeys(unknowns))
    state.evidence_quality["classification"] = "model_assessed"
    state.completion_status = "partial" if state.unknowns else "complete"
    state.incomplete_reason = " / ".join(state.unknowns)
    state.report = report


def completion_note(state) -> str:
    lines = [f"調査記録: {state.mode} / 条件版 {state.work_revision}。"]
    if state.completion_status != "complete":
        lines.append("暫定結果です。未確定事項が残っています。")
        lines.extend(f"- {item}" for item in state.unknowns)
    record = state.learning_record if state.mode == "learning" else state.decision_record
    if record:
        labels = {
            "options": "選択肢", "mandatory_conditions": "必須条件", "evaluation_axes": "評価軸",
            "selected": "採用案", "reasons": "採用理由", "change_conditions": "結論を変える条件",
            "known": "理解済み", "unknown": "未理解", "check_questions": "確認問題", "next_explanation": "次の説明",
        }
        for key, value in record.items():
            if key in labels and value:
                text = " / ".join(map(str, value)) if isinstance(value, list) else str(value)
                lines.append(f"- {labels[key]}: {text}")
    quality = state.evidence_quality
    if quality.get("primary_sids"):
        lines.append("- 内容から一次資料と判定した出典: " + ", ".join(quality["primary_sids"]))
    for label, name in (("反証", "counterevidence"), ("根拠の未確認事項", "unverified")):
        values = quality.get(name)
        if isinstance(values, list) and values:
            lines.append(f"- {label}: " + " / ".join(map(str, values)))
    return "\n".join(lines)


def is_research_work(store, work_id: str, actor_id=None) -> bool:
    """Identify a research Work even after its current checkpoint was invalidated."""
    store.get(work_id, actor_id)
    with store.transaction() as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_snapshots'").fetchone() is None:
            return False
        return connection.execute("SELECT 1 FROM research_snapshots WHERE work_id=?", (work_id,)).fetchone() is not None


def unresolved_publications(store, work_id: str) -> list[dict]:
    pending = {}
    after = 0
    while True:
        events = store.list_events(work_id, after_id=after, limit=500)
        for event in events:
            payload = event["payload"]
            if payload.get("operation") != "publish":
                continue
            if event["kind"] == "effect_dispatching":
                pending[payload.get("artifact_id")] = event
            elif event["kind"] in {"effect_confirmed", "effect_reconciled"}:
                pending.pop(payload.get("artifact_id"), None)
        if len(events) < 500:
            return list(pending.values())
        after = events[-1]["id"]


class ResearchJournal:
    """Atomic WorkStore research checkpoint with revision and audience fences."""

    def __init__(self, store, work_id: str, actor_id, *, channel_id=None, is_private=False, memory_policy=None) -> None:
        self.store, self.work_id, self.actor_id = store, work_id, actor_id
        self.context_invalidated = False
        self.requirements_changed = False
        self.channel_id, self.is_private = channel_id, is_private
        self.memory_policy = memory_policy
        if memory_policy is None:
            from services.memory_policy import get_memory_policy
            self.memory_policy = get_memory_policy()
        with store.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS research_snapshots ("
                "work_id TEXT PRIMARY KEY, payload TEXT NOT NULL, audience TEXT NOT NULL, "
                "updated_at TEXT NOT NULL)"
            )

    @staticmethod
    def _audience(work) -> str:
        return json.dumps({
            "audience": work.audience,
            "participants": sorted(str(value) for value in work.participants),
            "owner_id": str(work.owner_id),
        }, sort_keys=True)

    def restore(self):
        from services.deep_research.types import ResearchState
        with self.store.transaction() as connection:
            work = self.store.get(self.work_id, self.actor_id)
            row = connection.execute(
                "SELECT payload, audience FROM research_snapshots WHERE work_id=?", (self.work_id,)
            ).fetchone()
            if row is None:
                return None
            if row["audience"] != self._audience(work):
                self.context_invalidated = True
                return None
            state = ResearchState.restore(json.loads(row["payload"]))
            # A shared -> owner -> shared round trip still invalidates the old
            # derived context. Comparing only the current audience is insufficient.
            for event in connection.execute(
                "SELECT payload FROM work_events WHERE work_id=? AND revision>? AND kind='amended'",
                (self.work_id, state.work_revision),
            ):
                changed_fields = json.loads(event["payload"]).get("fields", {})
                if {"audience", "participants"} & changed_fields.keys():
                    self.context_invalidated = True
                    return None
            if not self.memory_policy.can_reuse_context(
                state.memory_epoch, owner_id=self.actor_id,
                channel_id=self.channel_id, is_private=self.is_private,
                stored_audience=state.audience_key,
            ):
                self.context_invalidated = True
                return None
            current_requirements = self.requirements(work)
            self.requirements_changed = (
                {key: value for key, value in state.requirements.items() if key != "answers"}
                != {key: value for key, value in current_requirements.items() if key != "answers"}
            )
            refresh_dependencies(state, current_requirements)
            state.work_revision = work.revision
            state.questions = {
                q["id"]: {**state.questions.get(q["id"], {}), **q,
                          "kind": state.questions.get(q["id"], {}).get("kind", "clarification")}
                for q in self.store.list_questions(self.work_id, self.actor_id)
                if q.get("status") != "superseded"
            }
            answers = [str(q["answer"]) for q in state.questions.values() if q.get("status") == "answered"]
            if answers:
                state.clarified_query = state.query + "\n\n[保存済み回答]\n" + "\n".join(answers)
            return state

    def requirements(self, work) -> dict:
        values = requirement_values(work.requirements)
        obsolete = {q["id"] for q in self.store.list_questions(self.work_id, self.actor_id)
                    if q.get("status") == "superseded"}
        if isinstance(values.get("answers"), dict):
            values["answers"] = {qid: value for qid, value in values["answers"].items() if qid not in obsolete}
        return values

    def reset_invalid_context(self, state) -> None:
        """Supersede derived questions, never turn invalidated questions into answers."""
        if not self.context_invalidated:
            return
        with self.store.transaction() as connection:
            row = connection.execute("SELECT payload FROM research_snapshots WHERE work_id=?", (self.work_id,)).fetchone()
            previous_ids = set(json.loads(row["payload"]).get("questions", {})) if row else set()
            superseded = []
            for question in self.store.list_questions(self.work_id, self.actor_id):
                if question["id"] not in previous_ids or question["status"] == "superseded":
                    continue
                question.update(status="superseded", superseded_at=datetime.now(timezone.utc).isoformat(),
                                invalidation_reason="research_context_changed")
                connection.execute("UPDATE work_questions SET data=? WHERE work_id=? AND id=?",
                                   (json.dumps(question, ensure_ascii=False), self.work_id, question["id"]))
                superseded.append(question["id"])
            work = self.store.amend(
                self.work_id, self.actor_id, "", f"research-context-reset:{self.work_id}:{state.work_revision}",
                expected_revision=state.work_revision,
                next_action="現行の記憶と公開範囲に合わせて調査条件を再確認します。",
            )
            self.store.record_event(self.work_id, "research_context_reset", {"superseded_questions": superseded}, actor_id=self.actor_id)
            state.work_revision = work.revision
            state.requirements = self.requirements(work)

    def supersede_stale_questions(self, state) -> None:
        """A new condition may resolve or replace an old question; re-clarify it."""
        if not self.requirements_changed:
            return
        obsolete = [question for question in state.questions.values() if question.get("status") == "open"]
        if not obsolete:
            return
        with self.store.transaction() as connection:
            self.store.assert_current(self.work_id, state.work_revision, self.actor_id)
            for question in obsolete:
                question.update(status="superseded", superseded_at=datetime.now(timezone.utc).isoformat(),
                                invalidation_reason="research_requirements_changed")
                connection.execute("UPDATE work_questions SET data=? WHERE work_id=? AND id=?",
                                   (json.dumps(question, ensure_ascii=False), self.work_id, question["id"]))
                state.questions.pop(question["id"], None)
            work = self.store.amend(self.work_id, self.actor_id, "", f"research-question-refresh:{self.work_id}:{state.work_revision}",
                expected_revision=state.work_revision, next_action="変更された条件に合わせて不足事項を再確認します。")
            state.work_revision = work.revision
            state.requirements = self.requirements(work)
            self.store.record_event(self.work_id, "research_questions_superseded", {"question_ids": [q["id"] for q in obsolete]}, actor_id=self.actor_id)

    def attach_context(self, state) -> None:
        state.memory_epoch = self.memory_policy.epoch()
        state.audience_key = self.memory_policy.audience_key(self.actor_id, self.channel_id, self.is_private)

    def assert_current(self, state) -> None:
        from services.work.store import WorkRevisionConflict, WorkStateConflict
        try:
            self.store.assert_current(self.work_id, state.work_revision, self.actor_id,
                                      allowed_states={"queued", "running"})
        except (WorkRevisionConflict, WorkStateConflict) as exc:
            raise ResearchInterrupted("条件または状態が変更されました。最新の条件で再開します。") from exc
        if not self.memory_policy.can_reuse_context(
            state.memory_epoch, owner_id=self.actor_id, channel_id=self.channel_id,
            is_private=self.is_private, stored_audience=state.audience_key,
        ):
            raise ResearchInterrupted("記憶または公開範囲が変更されました。再開時に調査内容を再構成します。")

    def save(self, state, phase: str | None = None) -> None:
        if phase:
            state.phase = phase
        with self.store.transaction() as connection:
            work = self.store.assert_current(self.work_id, state.work_revision, self.actor_id)
            payload = state.snapshot()
            self.store.save_checkpoint(self.work_id, "research", payload, state.work_revision)
            connection.execute(
                "INSERT INTO research_snapshots(work_id,payload,audience,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(work_id) DO UPDATE SET payload=excluded.payload, audience=excluded.audience, updated_at=excluded.updated_at",
                (self.work_id, json.dumps(payload, ensure_ascii=False), self._audience(work), datetime.now(timezone.utc).isoformat()),
            )

    def transition(self, state, status: str) -> None:
        work = self.store.transition(self.work_id, self.actor_id, status, expected_revision=state.work_revision)
        state.work_revision = work.revision
        self.save(state)

    def question(self, state, text: str, *, required=True, default=None, kind="clarification") -> dict:
        existing = next((q for q in state.questions.values() if q.get("kind") == kind and q.get("status") not in {"answered", "superseded"}), None)
        if existing:
            return existing
        question = self.store.add_question(
            self.work_id, text, actor_id=self.actor_id, required=required, default=default,
            expected_revision=state.work_revision,
            request_id=f"research-question:{self.work_id}:{kind}:{state.work_revision}",
        )
        question["kind"] = kind
        question["requirement_values"] = {key: value for key, value in state.requirements.items() if key != "answers"}
        state.questions[question["id"]] = question
        state.work_revision = self.store.get(self.work_id, self.actor_id).revision
        self.save(state)
        return question

    def bind_question(self, state, question_id: str, message_id, channel_id) -> None:
        with self.store.transaction():
            self.store.bind_message(self.work_id, str(message_id), str(channel_id))
            self.store.record_event(self.work_id, "question_displayed", {
                "question_id": question_id, "message_id": str(message_id),
                "memory_epoch": state.memory_epoch,
                "audience_key": self.memory_policy.audience_key(self.actor_id, str(channel_id), self.is_private),
            }, actor_id=self.actor_id)

    def answer(self, state, question_id: str, answer: str, *, request_id=None) -> None:
        work = self.store.answer_question(
            self.work_id, question_id, self.actor_id, answer,
            request_id or f"research-answer:{uuid4().hex}", expected_revision=state.work_revision,
        )
        state.work_revision = work.revision
        question = state.questions[question_id]
        question.update(answer=answer, status="answered")
        state.requirements = self.requirements(work)
        self.save(state)

    def pause(self, state, reason: str, *, waiting=True) -> None:
        state.incomplete_reason = reason
        state.completion_status = "partial"
        if reason not in state.unknowns:
            state.unknowns.append(reason)
        state.phase = "waiting_input" if waiting else "paused"
        self.transition(state, "waiting" if waiting else "paused")

    def artifact(self, state, report: str) -> dict:
        self.assert_current(state)
        return self.store.add_artifact(
            self.work_id, expected_revision=state.work_revision,
            source_id="research-report", title=state.query[:150], body=report,
            kind="research", actor_id=self.actor_id,
            metadata={"completion_status": state.completion_status,
                      "unknowns": state.unknowns, "mode": state.mode,
                      "decision": state.decision_record, "learning": state.learning_record,
                      "evidence_quality": state.evidence_quality,
                      "memory_epoch": state.memory_epoch, "audience_key": state.audience_key,
                      "sources": [vars(ref) for ref in state.sources.values()]},
        )

    async def publish(self, state, thread, artifact: dict, report: str):
        """One acknowledged complete file is the publication receipt.

        The event format is shared with WorkRuntime.reconcile. A missing or
        ambiguous Discord receipt is never interpreted as permission to resend.
        """
        import discord
        from services.response_preferences import ResponsePreferences, prepare_presentation
        work = self.store.get(self.work_id, self.actor_id)
        presentation = prepare_presentation(
            report, ResponsePreferences.from_dict(work.preferences),
            uncertainties=state.unknowns, provisional=state.completion_status != "complete",
            revision=state.work_revision,
        )
        header = f"用件 {self.work_id}。成果物 {artifact['id']} 第{artifact['version']}版。"
        attachment = discord.File(io.BytesIO(report.encode("utf-8")),
                                  filename=f"{self.work_id}-research-v{artifact['version']}.md")
        with self.store.transaction():
            self.assert_current(state)
            self.store.record_event(self.work_id, "effect_dispatching", {
                "operation": "publish", "artifact_id": artifact["id"], "revision": state.work_revision,
                "channel_id": str(thread.id), "header": header, "filename": attachment.filename,
            }, actor_id=self.actor_id)
        try:
            sent = await thread.send(f"{header}\n{presentation.display_text[:1600]}",
                                     file=attachment, allowed_mentions=discord.AllowedMentions.none())
        except BaseException:
            self.store.record_event(self.work_id, "effect_outcome_unknown", {
                "operation": "publish", "artifact_id": artifact["id"],
            }, actor_id=self.actor_id)
            current = self.store.get(self.work_id)
            if current.state in {"running", "queued", "waiting", "paused"}:
                self.store.transition(self.work_id, None, "needs_reconciliation", expected_revision=current.revision)
            raise
        finally:
            attachment.close()
        self.store.record_event(self.work_id, "effect_confirmed", {
            "operation": "publish", "artifact_id": artifact["id"], "message_id": str(sent.id),
        }, actor_id=self.actor_id)
        self.store.bind_message(self.work_id, str(sent.id), str(thread.id))
        self.assert_current(state)
        return sent

    def update_summary(self, state) -> None:
        """Expose the research decision and unmet criteria in ordinary Work views."""
        self.assert_current(state)
        record = state.learning_record if state.mode == "learning" else state.decision_record
        work = self.store.update_execution(
            self.work_id, self.actor_id, state.work_revision,
            decisions=[{"kind": state.mode, "record": record}],
            unknowns=state.unknowns,
            progress=[f"{subq}: {'再確認が必要です。' if item.get('stale') else '根拠を収集しました。'}"
                      for subq, item in state.evaluations.items()],
            next_action="未確認事項を再調査します。" if state.completion_status != "complete" else "",
        )
        state.work_revision = work.revision
        self.save(state)
