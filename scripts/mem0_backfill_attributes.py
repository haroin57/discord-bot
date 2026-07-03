#!/usr/bin/env python3
"""Phase M (Mem0 v4): 既存 fact に attribute をバックフィル（再抽出方式）。

3社検算の一致指摘「新規適用だけでは既存の汎用 fact が誤注入され続ける →
lazy migration or 再抽出が必要」への対応。既存 fact のテキスト単体を Haiku で
分類し、単一属性の現在値を述べている fact に attribute メタデータを付与する。
決定的 conflict ルート(Phase A-3)が既存 fact も照合対象にできるようになる。

- dry-run がデフォルト。--apply で初めて mem.update を実行する
- attribute が既にある fact は skip
- 無停止・バッチ（--batch-sleep で ChromaDB/Haiku への負荷を絞る）

T6 硬化（gpt-5.5-pro 指摘「LLM backfill の汚染リスク」対応, 2026-07-03）:
- Haiku に confidence(high/low) を自己申告させ、**low は apply しない**（dry/shadow には出す）
- apply 時は metadata に schema_version=1 と backfill_confidence を残す（後から
  backfill 由来の行を特定・巻き戻しできるように）
- **condition は推定しない**: 既存 fact の condition は unknown であって
  unconditional ではない（gpt-5.5-pro リスク2）。condition 付与は新規 ingest のみ
- --shadow: metadata を一切書かず判定結果を JSONL に出力 → 人が確認して --apply

使い方:
    python scripts/mem0_backfill_attributes.py                 # dry-run（先輩の記憶）
    python scripts/mem0_backfill_attributes.py --limit 50       # 先頭50件だけ試算
    python scripts/mem0_backfill_attributes.py --shadow out.jsonl  # 判定を書き出すだけ
    python scripts/mem0_backfill_attributes.py --apply          # 実適用（high のみ）
"""

import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

# 先輩(haroin)の Mem0 namespace（mcp_mem0_server.py と同じ）
DEFAULT_USER_ID = "573489206240870410"

_INFER_PROMPT = """以下は先輩(haroin)について長期記憶に保存された1文です。
この fact が「先輩のある一つの属性の"現在値"」を述べているなら属性キーを、
単発の出来事・経験・感想・知識・その場限りの発言ならば空を返してください。

fact: {text}

属性キー語彙:
- 単一値(新しい値が来たら旧値が無効になる類): weight / height / residence / occupation / affiliation / relationship_status / current_focus
- ドメイン付き: habit.<名詞> / preference.<領域>
- どれにも当てはまらなければ ""
- **他者(母・兄・猫等)の属性には付けない**(先輩本人のみ)

confidence: この判定にどれだけ自信があるか
- "high": fact が明確に単一属性の現在値を述べている
- "low": 属性かもしれないが、出来事/経験/一時的状態とも読める

JSONのみ返してください: {{"attribute":"<語彙 or 空文字列>","confidence":"<high|low>"}}"""


async def infer_attribute_for_fact(fact_text: str) -> tuple[str, str]:
    """fact テキスト単体から (attribute, confidence) を推定（Haiku）。

    attribute は正規化済み。失敗時は ("", "low")。
    """
    from services.mem0.client import get_anthropic_client
    from services.mem0.cli_compat import cli_messages_create, extract_text
    from services.mem0.extract import _normalize_attribute
    client = await get_anthropic_client()
    if client is None:
        return "", "low"
    safe = fact_text[:300].replace("{", "{{").replace("}", "}}")
    try:
        resp = await cli_messages_create(
            client,
            model=os.environ.get("MEM0_GATE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=150,
            user_message=_INFER_PROMPT.format(text=safe),
            task_prompt=(
                "You classify whether a memory fact states a single attribute "
                "current-value, with a confidence self-report. Return JSON only."
            ),
        )
        text = extract_text(resp).strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        data = json.loads(text)
        raw = data.get("attribute", "")
        conf = data.get("confidence", "low")
    except Exception:
        return "", "low"
    conf = conf if conf in ("high", "low") else "low"
    return _normalize_attribute(raw), conf


BACKFILL_SCHEMA_VERSION = 1


async def main(
    user_id: str, apply: bool, limit, batch_sleep: float,
    shadow_path: str | None = None,
) -> None:
    from services.mem0.client import _get_mem0, get_all_memories
    mode = "APPLY" if apply else ("SHADOW" if shadow_path else "DRY-RUN")
    mem = _get_mem0()
    if mem is None:
        print("Mem0 init failed — ChromaDB 未起動か OAuth 切れ", file=sys.stderr)
        return
    facts = await get_all_memories(user_id, limit=limit)
    targets = [
        f for f in facts
        if not (f.get("metadata") or {}).get("attribute")
        and (f.get("memory") or f.get("text"))
    ]
    print(f"total={len(facts)} targets(no attribute)={len(targets)} mode={mode}")
    shadow_f = open(shadow_path, "w", encoding="utf-8") if shadow_path else None
    inferred = updated = skipped_low = 0
    try:
        for i, f in enumerate(targets):
            text = f.get("memory") or f.get("text") or ""
            fid = f.get("id")
            if not fid:
                continue
            attr, conf = await infer_attribute_for_fact(text)
            if not attr:
                continue
            inferred += 1
            print(f"[{i + 1}/{len(targets)}] {mode} attribute={attr} "
                  f"conf={conf} id={fid} text={text[:60]!r}")
            if shadow_f:
                shadow_f.write(json.dumps({
                    "id": fid, "text": text, "attribute": attr,
                    "confidence": conf,
                }, ensure_ascii=False) + "\n")
            if apply:
                # T6: low confidence は書かない（誤付与は決定ルートの誤 archive に
                # 直結する。low は shadow/dry の出力で人が見る）。
                if conf != "high":
                    skipped_low += 1
                    continue
                try:
                    await asyncio.to_thread(
                        mem.update, memory_id=fid, metadata={
                            "attribute": attr,
                            "schema_version": BACKFILL_SCHEMA_VERSION,
                            "backfill_confidence": conf,
                        })
                    updated += 1
                except Exception as e:
                    print(f"  update failed: {e}", file=sys.stderr)
                    continue
                # T8a: fact_store が有効なら導出インデックスにも初期投入
                # (best-effort — 失敗しても Chroma 側 metadata は入っている)。
                try:
                    from config import MEM0_FACT_STORE_ENABLED
                    if MEM0_FACT_STORE_ENABLED:
                        from services.mem0 import fact_store
                        await asyncio.to_thread(
                            fact_store.record_fact, fid, user_id, attr,
                            value_text=text,
                            extractor_version=f"backfill-v{BACKFILL_SCHEMA_VERSION}",
                        )
                except Exception as e:
                    print(f"  fact_store record failed: {e}", file=sys.stderr)
            if (i + 1) % 20 == 0 and batch_sleep > 0:
                await asyncio.sleep(batch_sleep)
    finally:
        if shadow_f:
            shadow_f.close()
    print(f"done. inferred={inferred} updated={updated} "
          f"skipped_low_conf={skipped_low} (mode={mode})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Mem0 attribute backfill (Phase M)")
    ap.add_argument("--user-id", default=DEFAULT_USER_ID)
    ap.add_argument("--apply", action="store_true",
                    help="実適用（未指定なら dry-run。low confidence は書かない）")
    ap.add_argument("--shadow", metavar="OUT_JSONL", default=None,
                    help="判定結果を JSONL に書き出すだけ（metadata 変更なし）")
    ap.add_argument("--limit", type=int, default=None,
                    help="処理する最大 fact 数（試算用）")
    ap.add_argument("--batch-sleep", type=float, default=1.0,
                    help="20件ごとの sleep 秒（負荷絞り）")
    args = ap.parse_args()
    if args.apply and args.shadow:
        ap.error("--apply と --shadow は同時指定できません")
    asyncio.run(main(
        args.user_id, args.apply, args.limit, args.batch_sleep,
        shadow_path=args.shadow,
    ))
