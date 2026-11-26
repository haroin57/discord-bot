from __future__ import annotations

import json

import pytest

from services.response_preferences import (
    ResponsePreferences,
    build_prompt_guidance,
    infer_preferences,
    prepare_presentation,
)


@pytest.mark.parametrize("text,verbosity", [
    ("熟考して一言で", "brief"),
    ("熟考して詳しく", "detailed"),
    ("十分に調べたうえで結論だけ", "brief"),
    ("独立した資料で検証して、手短に答えて", "brief"),
])
def test_short_presentation_keeps_independent_verification(text, verbosity):
    result = infer_preferences(text)
    assert result.evidence == "independent"
    assert result.verbosity == verbosity


def test_amendment_and_restart_preserve_other_dimensions():
    initial = infer_preferences("最新の物件を十分に調べて比較して、時間をかけて詳しく")
    recovered = ResponsePreferences.from_dict(json.loads(json.dumps(initial.to_dict())))
    amended = infer_preferences("今は短く", current=recovered)
    assert amended.intent == "compare"
    assert amended.evidence == "independent"
    assert amended.wait == "patient"
    assert amended.verbosity == "brief"


@pytest.mark.parametrize("text,field,expected", [
    ("この2つを比較して", "intent", "compare"),
    ("順番に仕組みを理解したい", "intent", "learn"),
    ("新しい画面を実装して", "intent", "create"),
    ("この下書きを指定先に送信して", "intent", "effect"),
    ("結論を教えて", "intent", "conclusion"),
    ("最新の価格を確認して", "evidence", "current"),
    ("この文脈だけで答えて", "evidence", "contextual"),
    ("まず暫定結果を見せて", "wait", "partial"),
    ("急がないので時間をかけて", "wait", "patient"),
    ("すぐ答えて", "wait", "immediate"),
    ("説明量は標準で", "verbosity", "normal"),
    ("Verify carefully and answer in one sentence", "evidence", "independent"),
])
def test_dimensions_are_inferred_independently(text, field, expected):
    assert getattr(infer_preferences(text), field) == expected


def test_quick_first_result_does_not_skip_verification():
    result = infer_preferences("急いで、ただし十分に調べて結論だけ")
    assert result.wait == "immediate"
    assert result.evidence == "independent"
    assert result.verbosity == "brief"


def test_explicit_override_is_authoritative_and_validated():
    result = infer_preferences("熟考して詳しく", overrides={"verbosity": "brief"})
    assert result.evidence == "independent"
    assert result.verbosity == "brief"
    with pytest.raises(ValueError):
        infer_preferences("", overrides={"verbosity": "gigantic"})
    with pytest.raises(ValueError):
        infer_preferences("", overrides={"silent_typo": "brief"})
    with pytest.raises(ValueError):
        ResponsePreferences.from_dict({"evidence": None})


def test_storage_accepts_missing_fields_and_unrelated_metadata():
    result = ResponsePreferences.from_dict({"verbosity": "brief", "version": 2})
    assert result == ResponsePreferences(verbosity="brief")


def test_smalltalk_has_no_research_or_artifact_by_default():
    prefs = infer_preferences("おはよう")
    result = prepare_presentation("おはようございます。", prefs)
    assert prefs.evidence == "contextual"
    assert prefs.verbosity == "normal"
    assert result.display_text == "おはようございます。"
    assert not result.artifact_required


def test_summary_retains_full_report_and_material_unknowns():
    original = "結論はAですが、費用未確認です。\n\n" + ("比較資料の詳細です。\n" * 200)
    result = prepare_presentation(
        original, ResponsePreferences(verbosity="brief"),
        summary="費用条件を確認できる場合はAを採用します。",
        uncertainties=("管理費の金額が未確認です。",),
        next_action="管理費を確認します。", provisional=True, revision=2,
        previous_revision=1, change_summary="徒歩条件を変更しました。",
    )
    assert result.artifact_content == original
    assert result.full_text == original
    assert result.artifact_required
    assert "暫定結果です。" in result.display_text
    assert "管理費の金額が未確認です。" in result.display_text
    assert "管理費を確認します。" in result.display_text
    assert "前の版1を更新します。" in result.display_text
    assert "保存しました" not in result.display_text


def test_no_summary_means_labelled_complete_excerpt_not_silent_truncation():
    first = "Aを採用します。ただし審査が必要です。"
    original = first + "\n\n" + "後続の詳細です。" * 200
    result = prepare_presentation(original, {"verbosity": "brief"})
    assert result.display_text == "本文の冒頭抜粋です。\n" + first
    assert result.artifact_content == original


@pytest.mark.parametrize("opening", [
    "```python\ntext = '''\n\ncode resembling prose\n\n'''\n```",
    "| 項目 | 値 |\n|---|---|\n| A | B |",
    "途切れさせてはいけない文章" * 200,
])
def test_fallback_does_not_cut_code_tables_or_sentences(opening):
    original = opening + "\n\n" + "本文の残りです。" * 200
    result = prepare_presentation(original, {"verbosity": "brief"})
    assert "別の成果物" in result.display_text
    assert "code resembling prose" not in result.display_text
    assert result.artifact_content == original


def test_limits_never_drop_important_caveats():
    caveat = "重要な未確認条件です。" * 200
    result = prepare_presentation(
        "結論です。", ResponsePreferences(), uncertainties=(caveat,),
    )
    assert caveat in result.display_text
    with pytest.raises(ValueError):
        prepare_presentation("本文", ResponsePreferences(), display_limit=1)
    with pytest.raises(ValueError):
        prepare_presentation("本文", ResponsePreferences(), revision=2, previous_revision=2)


def test_prompt_distinguishes_evidence_latency_and_authorization():
    prompt = build_prompt_guidance(ResponsePreferences(
        intent="effect", evidence="independent", wait="immediate", verbosity="brief",
    ))
    assert "短い表示の指定でこの水準を下げません" in prompt
    assert "暫定と明示" in prompt
    assert "操作の許可にはなりません" in prompt
    assert "実際に成功した範囲" in prompt


@pytest.mark.parametrize("auto_enabled", (False, True))
def test_router_respects_verification_even_when_display_is_short(auto_enabled):
    from services.claude_api.router import route_request

    brief = route_request("熟考して一言で", auto_enabled=auto_enabled)
    detailed = route_request("熟考して詳しく", auto_enabled=auto_enabled)
    quick = route_request("さくっと十分に調べて一言で", auto_enabled=auto_enabled)
    assert brief.tier == detailed.tier == quick.tier == "deliberate"


def test_router_brief_alone_does_not_lower_automatic_task_tier():
    from services.claude_api.router import route_request

    for suffix in ("一言で", "手短に", "ざっくりでいい"):
        assert route_request("この二案を比較して、" + suffix).tier == "deliberate"
    assert route_request("さくっとこの二案を比較して").tier == "chat"


@pytest.mark.asyncio
async def test_router_uses_recovered_work_preferences():
    from services.claude_api.router import decide_tier

    persisted = infer_preferences("熟考して").to_dict()
    amended = infer_preferences("今は結論だけ", current=persisted)
    result = await decide_tier("今は結論だけ", auto_enabled=False, preferences=amended)
    assert result.tier == "deliberate"
