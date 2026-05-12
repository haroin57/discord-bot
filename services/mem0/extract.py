"""Haiku-based fact extraction & metadata classification for ingestion."""

import logging
import os

from services.oauth_manager import ensure_valid_token, get_access_token, init as oauth_init

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ingestion-time LLM classification (P2)
# ---------------------------------------------------------------------------
# Taxonomy categories — see /tmp/mem0_taxonomy.md for full definitions.
_TAXONOMY_CATEGORIES = "personal | project | tech | science | hobby | invest | ops | meta"

_CLASSIFY_PROMPT = """以下の会話から、長期記憶として保存される際のメタデータを推定してください。

会話:
{conv}

以下のJSON形式だけを返してください(説明や前置きなし、コードフェンスも不要):
{{"category":"<one of: """ + _TAXONOMY_CATEGORIES + """>","subcategory":"<short kebab-case label>","importance":<1-5>,"tier":<0-3>,"tags":["tag1","tag2"]}}

カテゴリ定義:
- personal: 個人・関係性・健康・進路・家族
- project: 具体的開発プロジェクト(ashitabo/kanae-bot/free-code/happy-stack/chatqa等の固有名詞)
- tech: 技術関心・学習(言語/コンパイラ/Async/LLMアーキ/逆解析)
- science: 物理/数学/思想/哲学(コード書かない世界観)
- hobby: 趣味・音楽・読書・アニメ・酒・グルメ
- invest: 投資・株・暗号資産・経済テーマ
- ops: 運用・Git安全・設定・VPS・dev環境
- meta: LLM対話メタ・好み・スタイル・断片ログ

importance: 1=trivial雑談, 2=断片ログ, 3=通常関心, 4=主要事実, 5=コア事実(失うと再構築不能)
tier: 0=Live(進行中), 1=Hot(継続参照), 2=Warm(過去事実), 3=Cold(archive候補)"""


async def _classify_for_metadata(messages: list[dict]) -> dict:
    """Use Haiku 4.5 (OAuth) to attach taxonomy metadata to a new memory event.

    Returns empty dict on any failure — callers must treat it as best-effort.
    """
    try:
        from services.mem0.client import get_anthropic_client
        client = await get_anthropic_client()
        if client is None:
            return {}
        conv_parts = []
        for m in messages or []:
            role = m.get("role", "?")
            content = m.get("content", "")
            if not isinstance(content, str):
                content = str(content)
            if content:
                conv_parts.append(f"{role}: {content[:400]}")
        if not conv_parts:
            return {}
        conv = "\n".join(conv_parts)
        resp = await client.messages.create(
            model=os.environ.get("MEM0_CLASSIFY_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=200,
            messages=[{"role": "user", "content": _CLASSIFY_PROMPT.format(conv=conv)}],
        )
        text = resp.content[0].text.strip()
        # strip codeblock if any
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        import json as _json
        meta = _json.loads(text)
        # keep only the keys we care about
        return {
            k: meta[k]
            for k in ("category", "subcategory", "importance", "tier", "tags")
            if k in meta
        }
    except Exception as exc:
        log.warning("classify_for_metadata failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# P2: Extraction gate — Haiku extracts self-only facts, then we raw-insert
# them with infer=False. This replaces Mem0's built-in (English `Prefers ~`)
# extractor for the senpai user_id, fixing dedup bloat + multi-speaker mixing.
# ---------------------------------------------------------------------------
# XML-block closing tags an attacker could embed in user_text/assistant_text/context_hint
# to "break out" of the fenced block in _EXTRACT_PROMPT below. We strip these before format().
_EXTRACT_XML_BREAKOUT_TAGS = (
    "</context_hint>",
    "</user_text>",
    "</assistant_text>",
)


def _strip_extract_breakout_tags(s: str) -> str:
    """Remove XML closing tags an attacker might use to escape the prompt template.

    Case-insensitive; preserves all other content. Safe to apply to legitimate
    user text because these exact tag literals are not natural Japanese/English.
    """
    if not s:
        return s
    out = s
    for tag in _EXTRACT_XML_BREAKOUT_TAGS:
        # Case-insensitive replace via re for robustness
        import re as _re
        out = _re.sub(_re.escape(tag), "", out, flags=_re.IGNORECASE)
    return out


_EXTRACT_PROMPT = """以下の会話ターンから、長期記憶として保存すべき「先輩(本人)に関する事実」だけを抽出してください。

<context_hint>
{context_hint}
</context_hint>

<user_text>
{user_text}
</user_text>

<assistant_text>
{assistant_text}
</assistant_text>

抽出ルール:
- 抽出対象は <user_text>(送信者本人の発話) と <assistant_text>(bot応答内に出てくる本人状態)のみ
- <context_hint> の他者発話・参考情報は **絶対に fact 化しない**(speaker=='other' になる内容は不要)
- 雑談・短い相槌・LLM自己言及・一般論は出力しない
- importance < 3 は出力しない
- 1 fact は日本語 120字以内、canonical キーワード優先 (toeic-prep / ashitabo / kanae-bot / clannad-routes 等)
- category は personal/project/tech/science/hobby/invest/ops/meta のいずれか

以下の JSON のみ返してください(説明・前置き・コードフェンス不要):
{{"facts":[{{"speaker":"self","text":"<日本語fact>","importance":<3-5>,"category":"<one of 8>","subcategory":"<kebab-case>","tags":["<tag>"]}}]}}

該当事実が無ければ facts は空配列で構いません。"""


async def extract_facts_for_self(
    user_text: str, assistant_text: str, context_hint: str = "",
) -> list[dict]:
    """Use Haiku 4.5 to extract senpai-only facts from one conversational turn.

    Returns a list of {speaker, text, importance, category, subcategory, tags}.
    Empty list on any failure (caller should fall back to Mem0 standard path).
    """
    try:
        from services.mem0.client import get_anthropic_client
        client = await get_anthropic_client()
        if client is None:
            return []
        from config import MEM0_GATE_MODEL  # local import to allow .env reload
        resp = await client.messages.create(
            model=MEM0_GATE_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": _EXTRACT_PROMPT.format(
                context_hint=_strip_extract_breakout_tags(context_hint or "(none)")[:1000],
                user_text=_strip_extract_breakout_tags(user_text or "")[:2000],
                assistant_text=_strip_extract_breakout_tags(assistant_text or "")[:2000],
            )}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        import json as _json
        data = _json.loads(text)
        facts = data.get("facts", [])
        # Double-defense: drop anything not marked self regardless of LLM claim
        return [f for f in facts if isinstance(f, dict) and f.get("speaker") == "self" and f.get("text")]
    except Exception as exc:
        log.warning("extract_facts_for_self failed: %s", exc)
        return []
