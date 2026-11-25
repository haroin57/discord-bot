"""Bounded immutable input artifacts for durable Work requests."""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
from pathlib import Path

_IMAGES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".tsx", ".html", ".xml", ".yaml", ".yml", ".toml", ".tex", ".log"}


async def capture(store, work, message, *, policy=None):
    """Persist supported content and explicit failures; never silently omit input."""
    errors = []
    private = getattr(getattr(message.channel, "recipient", None), "id", None) == message.author.id
    epoch = policy.epoch() if policy else 0
    audience = policy.audience_key(work.owner_id, str(message.channel.id), private) if policy else ""
    images = 0
    for index, attachment in enumerate(getattr(message, "attachments", ())):
        name = str(attachment.filename)
        mime = str(attachment.content_type or "").split(";", 1)[0].strip()
        metadata = {"memory_epoch": epoch, "audience_key": audience, "direct_input": True,
                    "source_message_id": str(message.id), "filename": name, "mime_type": mime}
        body = ""
        source_id = f"input:{message.id}:{getattr(attachment, 'id', index)}"
        store.add_artifact(work.id, expected_revision=work.revision, source_id=source_id,
            title=name, body="添付の取得は未完了です。", kind="input", actor_id=work.owner_id,
            metadata={**metadata, "encoding": "pending"})
        try:
            if index >= 10 or attachment.size > 8 * 1024 * 1024:
                raise ValueError("添付の件数または8MBの上限を超えています。")
            value = await asyncio.wait_for(attachment.read(), timeout=30)
            if len(value) > 8 * 1024 * 1024:
                raise ValueError("添付が8MBの上限を超えています。")
            if mime in _IMAGES:
                images += 1
                if images > 4:
                    raise ValueError("画像は4件までです。")
                body = base64.b64encode(value).decode("ascii")
                metadata["encoding"] = "base64-image"
            elif mime.startswith("text/") or Path(name).suffix.lower() in _TEXT_SUFFIXES:
                if len(value) > 200_000:
                    raise ValueError("テキスト添付は200KBまでです。")
                body = value.decode("utf-8-sig")
                metadata["encoding"] = "text"
            elif mime == "application/pdf" or name.lower().endswith(".pdf"):
                with tempfile.TemporaryDirectory(prefix="yui-work-pdf-") as directory:
                    path = Path(directory) / "input.pdf"
                    path.write_bytes(value)
                    process = await asyncio.create_subprocess_exec("pdftotext", "-layout", str(path), "-",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                    try:
                        output, _ = await asyncio.wait_for(process.communicate(), timeout=20)
                    except BaseException:
                        process.kill()
                        await process.wait()
                        raise
                    if process.returncode or len(output) > 200_000:
                        raise ValueError("PDFの本文を制限内で抽出できませんでした。")
                    body = output.decode("utf-8")
                    if not body.strip():
                        raise ValueError("PDFから文字を抽出できませんでした。画像として添付できます。")
                    metadata["encoding"] = "text"
            else:
                raise ValueError("この添付形式は用件の入力として解析できません。")
        except Exception as exc:
            error = f"{name}: {exc if isinstance(exc, ValueError) else '添付を取得できませんでした。'}"
            errors.append(error)
            body = error
            metadata["encoding"] = "unavailable"
        store.add_artifact(work.id, expected_revision=work.revision,
            source_id=source_id, title=name,
            body=body or "空のファイルです。", kind="input", actor_id=work.owner_id, metadata=metadata)
    return errors


def input_artifacts(store, work, *, policy=None, channel_id=None, private=False):
    # Condition edits invalidate result artifacts; original user attachments are
    # still input. Privacy changes independently invalidate their reuse.
    rows = [row for row in store.list_artifacts(work.id, work.owner_id, include_invalidated=True)
            if row.get("kind") == "input"]
    if policy:
        result = []
        for row in rows:
            if policy.can_reuse_context(row.get("metadata", {}).get("memory_epoch"),
                owner_id=work.owner_id, channel_id=channel_id or work.channel_id, is_private=private,
                stored_audience=row.get("metadata", {}).get("audience_key")):
                result.append(row)
            else:
                result.append({"id": row["id"], "kind": "input", "title": "再確認が必要な添付です。",
                    "body": "記憶または公開範囲の変更後、この添付は再利用できません。", "metadata": {
                        "encoding": "unavailable", "redacted": True, "memory_epoch": policy.epoch(),
                        "audience_key": policy.audience_key(work.owner_id, channel_id or work.channel_id, private)}})
        rows = result
    return rows
