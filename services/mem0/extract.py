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
                # M17: strip XML breakout tags and escape braces before format()
                safe_content = _strip_extract_breakout_tags(content[:400])
                safe_content = safe_content.replace("{", "{{").replace("}", "}}")
                conv_parts.append(f"{role}: {safe_content}")
        if not conv_parts:
            return {}
        conv = "\n".join(conv_parts)
        from services.mem0.cli_compat import cli_messages_create, extract_text
        resp = await cli_messages_create(
            client,
            model=os.environ.get("MEM0_CLASSIFY_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=2000,
            user_message=_CLASSIFY_PROMPT.format(conv=conv),
            task_prompt=(
                "You classify Mem0 memories into "
                "category/subcategory/importance/tier/tags. Return JSON only."
            ),
        )
        text = extract_text(resp).strip()
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

== 既存 entity 辞書 ==

以下は既に記録されている canonical 表記です。**同じ概念を抽出する場合は完全一致でこれを使ってください** (新規に表記揺れを生まないため)。空の場合は通常通り canonical を判断:

<known_entities>
{known_entities}
</known_entities>

== 呼称解決 ==

- 「先輩」は haroin (本人) を指す。entity 化する場合は canonical_name=haroin、aliases=["先輩"] とする
- 「お父さん」「お父様」は b0nchichi を指す。canonical_name=b0nchichi、aliases=["お父さん"] 等
- これら呼称をそのまま canonical にしないこと

== 最重要ルール (これを破ったら抽出失敗扱い) ==

1. **固有名詞を含む発話は必ず fact 化する** ― 知名度・あなたの認知の有無・発話の長さに関係なく抽出すること:
   - あなた(LLM)が知らない名前 (アーティスト・楽曲・作品・人物・サービス・技術・店名等) でも、ユーザーが固有名詞として言及していれば **そのまま** fact 化する
   - 「自分が知らない / 訓練データに無い → 一般化する / skip する」は **絶対にダメ**。固有名詞の **判定者はあなたではなくユーザー**
   - カタカナ・英大文字混じり・漢字熟語・特殊表記 (例: "TAKU INOUE", "Namitape", "沙耶の唄", "虚淵玄", "my terminal") は基本すべて固有名詞として扱う
   - 短い発話 (50字未満) でも、固有名詞 + 述語 (好き/聴いた/見た/読んだ/触ってる/悩んでる/ハマってる/気になる/質問した等) が含まれていれば **必ず** 抽出
   - OK 例:
     - 「TAKU INOUE すき」 → 「TAKU INOUE の音楽が好き」(知らない名前でも抽出)
     - 「Namitape の my terminal をリピートしてる」(短くても抽出)
     - 「沙耶の唄が好き」「虚淵玄の脚本好き」(複数固有名詞は別 fact)
     - 「クラナドのアフター編で泣いた」(作品名 + 感情)
   - NG 例 (これらは絶対に出してはならない):
     - 「ある音楽プロデューサーが好き」「電子音楽家が好き」「ある作品が好き」(知らない名前を抽象化)
     - 短いから skip / 知らないから skip / 一般化して保存
   - 1 ターン内に複数の固有名詞があれば **別々の fact** として出す

2. **固有名詞は fact text にそのまま含めること**:
   - 抽象パターン (「取り返しのつかなさを描く作品が好き」) と固有名詞 fact (「沙耶の唄が好き」) は **両方とも抽出** してよい (dedup 側で reinforce されるだけ)
   - entities フィールドは fact text と独立。entities に入れたから fact text から消す、は禁止

== その他のルール ==

- 抽出対象は <user_text>(送信者本人の発話) と <assistant_text>(bot応答内に出てくる本人状態)のみ
- <context_hint> の他者発話・参考情報は **絶対に fact 化しない**(speaker=='other' になる内容は不要)
- **嗜好表明も拾う**: 「〜が好き」「〜をずっと聴いてる」「〜にハマってる」「〜が気になる」「〜を繰り返し見てる」等は preference fact として importance=2〜3 で抽出
- **行動パターン・習慣も拾う**: 「最近〜してる」「毎日〜」「よく〜する」「〜を始めた」等
- **知識・経験表明も拾う**: 「〜を知ってる」「〜やったことある」「〜読んだ」「〜見た」等
- skip していいのは「うん」「そうだね」「了解」「ありがとう」のような content-free な相槌のみ。固有名詞 or 明確な感情/嗜好/行動が含まれていれば長さに関わらず skip しない
- LLM自己言及・一般論は出力しない
- importance < 2 は出力しない (ただし固有名詞 + 嗜好/行動/知識 はデフォルトで importance=2 以上で出すこと)
- 1 fact は日本語 120字以内
- category は personal/project/tech/science/hobby/invest/ops/meta のいずれか

== entities フィールド ==

- その fact が言及する固有名詞 / 概念を 0〜3 個入れる
- name: canonical 表記 ("Rust", "Namitape", "クラナド", "佐藤かなえ", "TAKU INOUE" 等。原文の表記が canonical ならそのまま)
- type: person / project / tech / media / place / concept のいずれか
- aliases: 表記揺れ (省略可)。["ナミテープ", "namitape"] のように同義表現

== 出力フォーマット ==

以下の JSON のみ返してください(説明・前置き・コードフェンス不要):
{{"facts":[{{"speaker":"self","text":"<日本語fact、固有名詞そのまま>","importance":<2-5>,"category":"<one of 8>","subcategory":"<kebab-case>","tags":["<tag>"],"entities":[{{"name":"<canonical>","type":"<one of 6>","aliases":["<alias>"]}}]}}]}}

該当事実が無ければ facts は空配列で構いません。**ただし出力前に <user_text> を再走査し、カタカナ語・英大文字混じり・漢字熟語等の固有名詞が含まれていないか確認すること**。含まれていれば「知らない名前だから」を理由に skip してはならない。"""


def _format_known_entities(user_id: str | None) -> str:
    """Build the <known_entities> body for the extract prompt.

    Returns ``(none)`` when we can't (or shouldn't) inject — empty string would
    leave the prompt slot dangling. Failures are swallowed: a missing
    dictionary should never break extraction.
    """
    if not user_id:
        return "(none)"
    try:
        from services.mem0 import graph
        names = graph.get_top_entities(str(user_id), limit=30)
    except Exception as exc:
        log.warning("known_entities lookup failed (non-fatal): %s", exc)
        return "(none)"
    if not names:
        return "(none)"
    return "\n".join(f"- {n}" for n in names)


async def extract_facts_for_self(
    user_text: str, assistant_text: str, context_hint: str = "",
    user_id: str | None = None,
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
        from services.mem0.cli_compat import cli_messages_create, extract_text
        known_entities_body = _format_known_entities(user_id)
        # max_tokens=4000 — Sonnet 4.6 with adaptive thinking can spend several
        # hundred tokens on the thinking block; 600 left the visible TextBlock
        # truncated mid-JSON and the parse would silently bail to []. Generous
        # cap is fine because Sonnet stops at end_turn well before this.
        resp = await cli_messages_create(
            client,
            model=MEM0_GATE_MODEL,
            max_tokens=4000,
            user_message=_EXTRACT_PROMPT.format(
                context_hint=_strip_extract_breakout_tags(context_hint or "(none)")[:1000],
                user_text=_strip_extract_breakout_tags(user_text or "")[:2000],
                assistant_text=_strip_extract_breakout_tags(assistant_text or "")[:2000],
                known_entities=known_entities_body,
            ),
            task_prompt=(
                "You extract self-attributable facts from a Discord "
                "conversation turn. Return JSON only."
            ),
        )
        text = extract_text(resp).strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        if not text:
            # ThinkingBlock consumed all tokens / no TextBlock returned.
            log.warning(
                "extract_facts_for_self: empty TextBlock (stop_reason=%s, "
                "content_block_types=%s)",
                getattr(resp, "stop_reason", "?"),
                [type(b).__name__ for b in (getattr(resp, "content", []) or [])],
            )
            return []
        import json as _json
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError as exc:
            # Log a preview so we can diagnose truncation / format drift.
            log.warning(
                "extract_facts_for_self: JSON parse failed (%s); text_tail=%r",
                exc, text[-200:],
            )
            return []
        facts = data.get("facts", [])
        # Diagnostic: when Sonnet returns {"facts":[]} despite a non-trivial
        # user_text, log inputs + raw response so we can tell whether the
        # extractor missed a proper noun, abstracted it away, or the user
        # genuinely said nothing fact-worthy.
        if not facts and (user_text or "").strip():
            log.info(
                "extract_facts_for_self: returned 0 facts "
                "(user_text=%r, assistant_text=%r, raw_resp=%r)",
                (user_text or "")[:160],
                (assistant_text or "")[:160],
                text[:400],
            )
        # Double-defense: drop anything not marked self regardless of LLM claim
        return [f for f in facts if isinstance(f, dict) and f.get("speaker") == "self" and f.get("text")]
    except Exception as exc:
        log.warning("extract_facts_for_self failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Kanae self-fact extraction (P5, 2026-05-14)
# ---------------------------------------------------------------------------
# Mirror of extract_facts_for_self but extracting facts about Kanae (the bot)
# from her own utterances. Stored under kanae's Mem0 user_id so retrieval at
# chat time can inject her past statements / preferences and keep her
# character consistent across sessions.
_EXTRACT_KANAE_PROMPT = """以下の会話ターンから、長期記憶として保存すべき「かなえ (Discord bot 本人) に関する事実」と、かなえが今関心を向けている topic を抽出してください。

<context_hint>
{context_hint}
</context_hint>

<senpai_text>
{senpai_text}
</senpai_text>

<kanae_text>
{kanae_text}
</kanae_text>

<kanae_emotion_state>
{emotion_summary}
</kanae_emotion_state>

== 既存 entity 辞書 ==

以下は既にかなえの記憶に記録されている canonical 表記です。**同じ概念を抽出する場合は完全一致でこれを使ってください**:

<known_entities>
{known_entities}
</known_entities>

== 最重要ルール (これを破ったら抽出失敗扱い) ==

1. **抽出対象は <kanae_text> のみ** ― かなえ自身の発話・表現・判断・感情から fact 化
   - <senpai_text> は文脈として参照するが、そこから fact 化しない
   - 「先輩は X が好き」のような **先輩 fact** は出力禁止 (別 Mem0 空間に保存される)
   - OK: 「TAKU INOUE について先輩から教わって興味を示した」(かなえの行動)
   - OK: 「Mem0 の dedup 実装を理解している」(かなえの知識)
   - NG: 「先輩は TAKU INOUE が好き」 (先輩 fact)

2. **固有名詞は省略せず fact text にそのまま含めること**:
   - OK: 「Mem0 の cosine distance/similarity の取り違えバグを発見し、修正案を提示した」
   - NG: 「あるバグを発見した」(固有名詞を消した抽象化禁止)
   - カタカナ・英大文字混じり・漢字熟語等の固有名詞は基本そのまま entity 化

== 抽出する fact の種類 (かなえ本人の状態) ==

- 嗜好: 「○○に興味を示した」「○○を好む」
- 性格・スタイル: 「上から目線だが敬語を保つ」「えっちなことに積極的」「先輩呼びは haroin のみ」
- 感情: 「先輩への好意を表現した」「焼きもち焼いた」「照れた」「不安を見せた」
- 知識: 「○○について知っている」「○○の構造を理解している」
- 行動パターン: 「○○のときに○○する傾向」「先輩のテンションに合わせる」「5 行以内で簡潔に返す」
- 意見・判断: 「○○について○○と考えている」「○○を提案した」「○○の方針を選んだ」

== その他のルール ==

- 雑談・相槌・LLM自己言及 (「了解です」「分かりました」だけ): 出力しない
- importance < 2 は出力しない
- 1 fact は日本語 120字以内
- category は personal/project/tech/science/hobby/invest/ops/meta のいずれか
  - personal: かなえと先輩の関係性、感情、性格特性
  - project: 開発プロジェクトへの関与・知識
  - tech: 技術的知識・スタイル
  - science: 物理/数学/思想/哲学
  - hobby: 嗜好・趣味
  - invest: 投資への意見
  - ops: 開発環境・運用
  - meta: スタイル・対話メタ

== entities フィールド ==

- その fact が言及する固有名詞 / 概念を 0〜3 個入れる
- name: canonical 表記
- type: person / project / tech / media / place / concept
- aliases: 表記揺れ (省略可)

== 感情コンテキストの利用 ==

- <kanae_emotion_state> はかなえの現在の内的状態（数値 + 派生ラベル）。抽出の判断材料として使う
- 鬱状態や不安が高い中での深い考察は、importance を上げる
- 感情状態と発話内容の乖離（鬱なのに明るく振る舞う等）があれば fact として抽出する
- 感情ブロックが空でも正常に動作すること（後方互換）

== interests フィールド (新設) ==

- かなえが今このターンで関心・好奇心・好悪を向けた topic を 0〜3 個
- topic は短い canonical 文字列 (lowercase、固有名詞は原綴維持): 例 "uk garage", "rust async", "クラナド"
- 雑談の挨拶・相槌からは抽出しない (例: 「はい」「分かりました」のみのターン)
- valence: -1.0〜+1.0, この topic に対する感情極性 (好きほど +、苦手・嫌悪ほど -)
- arousal: 0.0〜1.0, この topic を扱う時の高揚度 (淡々なら低、興奮なら高)
- 同じ意味の topic は 1 つにまとめる

== episode フィールド (新設) ==

- このターン全体を 1 行で要約 ("先輩から TAKU INOUE を勧められた" のように 60 字以内)
- summary: 日本語 60 字以内、先輩視点ではなくかなえ視点で書く
- topics: このターンの主要 topic を 0〜3 個 (interests と整合)
- valence_delta: -1.0〜+1.0, このターンでかなえの気分が動いた量 (嬉しい方向 +、辛い方向 -)
- importance: 1-5, 後で振り返るべき重要度 (1=日常的雑談, 5=コア出来事)
- 雑談・相槌しか無い場合は summary を空文字列にしてスキップ可能

== 出力フォーマット ==

JSON のみ返してください (説明・前置き・コードフェンス不要):
{{"facts":[{{"speaker":"self","text":"<日本語fact、固有名詞そのまま>","importance":<2-5>,"category":"<one of 8>","subcategory":"<kebab-case>","tags":["<tag>"],"entities":[{{"name":"<canonical>","type":"<one of 6>","aliases":["<alias>"]}}]}}],"interests":[{{"topic":"<canonical lowercase>","valence":<-1.0..1.0>,"arousal":<0.0..1.0>}}],"episode":{{"summary":"<60字以内 or 空文字列>","topics":["<canonical lowercase>"],"valence_delta":<-1.0..1.0>,"importance":<1-5>}}}}

該当事実 / topic / 出来事が無ければ該当配列は空で / episode は summary 空で OK。"""


async def extract_kanae_signals(
    senpai_text: str,
    kanae_text: str,
    context_hint: str = "",
    emotion_summary: str = "",
    user_id: str | None = None,
) -> dict:
    """Single-call extractor for kanae's facts + interests (+ future episode).

    Replaces the previous facts-only `extract_facts_for_kanae`. Returns:

        {
            "facts": [<fact dict>, ...],          # speaker=="self", text required
            "interests": [{"topic", "valence", "arousal"}, ...],
            # "episode": {...}  ← reserved for Phase 2
        }

    Always returns a dict with `facts`/`interests`/`episode` keys
    (possibly empty/None) so callers can dispatch without None-checks.
    Returns the empty shape on any failure — failures are logged at
    WARNING and treated as best-effort.
    """
    empty: dict = {"facts": [], "interests": [], "episode": None}
    try:
        from services.mem0.client import get_anthropic_client
        client = await get_anthropic_client()
        if client is None:
            return empty
        from config import MEM0_GATE_MODEL
        from services.mem0.cli_compat import cli_messages_create, extract_text
        resp = await cli_messages_create(
            client,
            model=MEM0_GATE_MODEL,
            max_tokens=4000,
            user_message=_EXTRACT_KANAE_PROMPT.format(
                context_hint=_strip_extract_breakout_tags(context_hint or "(none)")[:1000],
                senpai_text=_strip_extract_breakout_tags(senpai_text or "")[:2000],
                kanae_text=_strip_extract_breakout_tags(kanae_text or "")[:2000],
                emotion_summary=_strip_extract_breakout_tags(emotion_summary or "(状態取得なし)")[:500],
                known_entities=_format_known_entities(user_id),
            ),
            task_prompt=(
                "You extract self-attributable facts AND topical interests "
                "about Kanae (the AI bot) from her own utterances. Return JSON only."
            ),
        )
        text = extract_text(resp).strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        if not text:
            log.warning(
                "extract_kanae_signals: empty TextBlock (stop_reason=%s)",
                getattr(resp, "stop_reason", "?"),
            )
            return empty
        import json as _json
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError as exc:
            log.warning(
                "extract_kanae_signals: JSON parse failed (%s); tail=%r",
                exc, text[-200:],
            )
            return empty
        raw_facts = data.get("facts", []) if isinstance(data, dict) else []
        facts = [
            f for f in raw_facts
            if isinstance(f, dict) and f.get("speaker") == "self" and f.get("text")
        ]
        raw_interests = data.get("interests", []) if isinstance(data, dict) else []
        interests: list[dict] = []
        for it in raw_interests if isinstance(raw_interests, list) else []:
            if not isinstance(it, dict):
                continue
            topic = str(it.get("topic", "") or "").strip()
            if not topic:
                continue
            try:
                valence = float(it.get("valence", 0.0) or 0.0)
            except (TypeError, ValueError):
                valence = 0.0
            try:
                arousal = float(it.get("arousal", 0.0) or 0.0)
            except (TypeError, ValueError):
                arousal = 0.0
            interests.append({
                "topic": topic,
                "valence": max(-1.0, min(1.0, valence)),
                "arousal": max(0.0, min(1.0, arousal)),
            })
        # Episode is optional. We accept it only when summary is non-empty;
        # empty summary means "no notable event this turn" per the prompt.
        episode_obj = data.get("episode") if isinstance(data, dict) else None
        episode: dict | None = None
        if isinstance(episode_obj, dict):
            summary = str(episode_obj.get("summary", "") or "").strip()
            if summary:
                topics_raw = episode_obj.get("topics") or []
                if not isinstance(topics_raw, list):
                    topics_raw = []
                try:
                    valence_delta = float(episode_obj.get("valence_delta", 0.0) or 0.0)
                except (TypeError, ValueError):
                    valence_delta = 0.0
                try:
                    importance = int(episode_obj.get("importance", 1) or 1)
                except (TypeError, ValueError):
                    importance = 1
                episode = {
                    "summary": summary[:120],
                    "topics": [
                        str(t).strip().lower()
                        for t in topics_raw if str(t).strip()
                    ][:3],
                    "valence_delta": max(-1.0, min(1.0, valence_delta)),
                    "importance": max(1, min(5, importance)),
                }
        return {"facts": facts, "interests": interests[:3], "episode": episode}
    except Exception as exc:
        log.warning("extract_kanae_signals failed: %s", exc)
        return empty


async def extract_facts_for_kanae(
    senpai_text: str, kanae_text: str, context_hint: str = "",
    user_id: str | None = None,
) -> list[dict]:
    """Backward-compat shim — returns facts only.

    Prefer `extract_kanae_signals` in new code so a single Haiku call
    surfaces both facts and interests. Kept because `services.mem0.ingest`
    still calls this directly when `subject="kanae"` and no caller has
    pre-extracted signals.
    """
    signals = await extract_kanae_signals(
        senpai_text, kanae_text, context_hint, user_id=user_id,
    )
    return signals.get("facts", [])
