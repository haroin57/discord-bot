#!/bin/bash
# TOEIC勉強リマインダー — 毎日9:03/17:03 JST に先輩にDMで通知
# 期限: 2026-05-16 以降は自動停止 & crontab自動削除

set -euo pipefail

BOT_TOKEN="MTQ4OTE3ODY4NDM5MTgxNzMzNg.G3mT8F.uvstX6OApUmjZdraxK08q5i6_Mdarve-OwOYy4"
USER_ID="573489206240870410"  # haroin（先輩）
LOG="/tmp/toeic-reminder.log"

# --- 期限チェック: 5/16を過ぎたら自動削除 ---
TODAY=$(TZ=Asia/Tokyo date +%Y-%m-%d)
EXPIRE="2026-05-16"
if [[ "$TODAY" > "$EXPIRE" ]]; then
    echo "$(date): Expired ($TODAY > $EXPIRE). Removing crontab entries." >> "$LOG"
    crontab -l 2>/dev/null | grep -v "toeic-reminder" | crontab -
    exit 0
fi

# --- 日付からフェーズとタスクを決定 ---
HOUR=$(TZ=Asia/Tokyo date +%H)
DOW=$(TZ=Asia/Tokyo date +%u)  # 1=Mon ... 7=Sun

get_task_message() {
    local today="$1"
    local hour="$2"
    local greeting=""
    local tasks=""

    if [[ "10#$hour" -lt 12 ]]; then
        greeting="おはようございます先輩。朝の勉強リマインドです。"
    else
        greeting="先輩、夕方のリマインドです。今日の分ちゃんとやりました？"
    fi

    # Phase判定（5/5リスケ版：詰め込みスケジュール 8h/日）
    if [[ "$today" < "2026-04-29" ]]; then
        # Phase 1: 基礎固め (4/11-4/28)
        phase="Phase 1（基礎固め）"
        tasks="📋 **今日のノルマ**
- **金フレ**: 100語周回（25分）
- **金の文法**: 25問（30分）
- **超集中リスニング**: 4日分 スクリプト読み→聴く→シャドーイング（35分）
- **読解特急**: 4題 時間無制限で丁寧に（35分）"

    elif [[ "$today" < "2026-05-08" ]]; then
        # Phase A: 1周目完了追い込み (5/5-5/7, 3日間)
        phase="Phase A（1周目完了追い込み）"
        tasks="📋 **今日のノルマ（缶詰モード 約8時間）**
- ⏰ 9:00-10:30 **金フレ**: 残り範囲一気読み＋間違いマーク（90分）
- ⏰ 10:30-12:00 **金の文法**: 残り問題を全部解く（90分）
- ⏰ 13:00-14:30 **超集中リスニング**: 残りユニット消化（90分）
- ⏰ 14:30-15:00 🏋️ジム準備・移動
- ⏰ 15:00-16:00 🏋️ジム
- ⏰ 16:30-18:00 **読解特急**: 残り問題を全部解く（90分）
- ⏰ 18:30-19:30 **金フレ**: 間違いマーク語を復習（60分）
🎯 5/7までに全教材1周目を完了させる"

    elif [[ "$today" < "2026-05-11" ]]; then
        # Phase B: テキスト2-3周目 + 公式1冊目 (5/8-5/10, 3日間)
        phase="Phase B（高速周回＋公式1冊目）"
        tasks="📋 **今日のノルマ（詰め込み 約8時間）**
- ⏰ 9:00-9:40 **金フレ**: 間違い語だけ高速周回（40分）
- ⏰ 9:40-10:20 **金の文法**: 間違い問題だけ解き直し（40分）
- ⏰ 10:30-11:15 **公式問題集1 Listening通し**（45分）
- ⏰ 11:15-12:00 **L復習**: スクリプト確認＋シャドーイング（45分）
- ⏰ 13:00-14:15 **公式問題集1 Reading通し**（75分）
- ⏰ 14:15-15:00 **R復習**: 間違い精読＋根拠確認（45分）
- ⏰ 15:00-16:00 🏋️ジム
- ⏰ 16:30-17:15 **リスニング2周目**: 間違いユニットのみ（45分）
- ⏰ 17:15-18:00 **読解特急2周目**: 間違い問題のみ（45分）
🎯 3日で公式1冊目を1周＋テキスト2-3周目を回す"

    elif [[ "$today" < "2026-05-14" ]]; then
        # Phase C: テキスト3-4周目 + 公式2冊目 (5/11-5/13, 3日間)
        phase="Phase C（テキスト仕上げ＋公式2冊目）"
        tasks="📋 **今日のノルマ（詰め込み 約8時間）**
- ⏰ 9:00-9:30 **金フレ**: 苦手語だけ超高速チェック（30分）
- ⏰ 9:30-10:00 **金の文法**: 間違い問題のみ（30分）
- ⏰ 10:00-10:45 **公式問題集2 Listening通し**（45分）
- ⏰ 10:45-11:30 **L復習**: スクリプト確認＋シャドーイング（45分）
- ⏰ 11:30-12:00 **リスニング3-4周目**: 苦手ユニット（30分）
- ⏰ 13:00-14:15 **公式問題集2 Reading通し**（75分）
- ⏰ 14:15-15:00 **R復習**: 間違い精読（45分）
- ⏰ 15:00-16:00 🏋️ジム
- ⏰ 16:30-17:15 **読解特急3-4周目**: 苦手問題のみ（45分）
- ⏰ 17:15-18:00 **公式1復習**: 間違い問題解き直し（45分）
🎯 3日で公式2冊目1周＋テキスト全教材4周完了"

    elif [[ "$today" < "2026-05-16" ]]; then
        # Phase D: 公式周回 (5/14-5/15, 2日間)
        phase="Phase D（公式問題集周回）"
        tasks="📋 **今日のノルマ（詰め込み 約8時間）**
- ⏰ 9:00-9:30 **金フレ**: 苦手語 最終チェック（30分）
- ⏰ 9:30-10:15 **公式1 間違いL**: 再挑戦＋シャドーイング（45分）
- ⏰ 10:15-11:30 **公式1 間違いR**: 再挑戦＋精読（75分）
- ⏰ 11:30-12:00 **金の文法**: 全問高速通し（30分）
- ⏰ 13:00-13:45 **公式2 間違いL**: 再挑戦＋シャドーイング（45分）
- ⏰ 13:45-15:00 **公式2 間違いR**: 再挑戦＋精読（75分）
- ⏰ 15:00-16:00 🏋️ジム
- ⏰ 16:30-17:30 **公式 2-3周目**: 間違い問題のみ高速回し（60分）
- ⏰ 17:30-18:00 **弱点ノート総復習**（30分）
🎯 2日で公式2冊とも3周完了"

    else
        # Phase E: 直前調整 (5/16のみ)
        phase="Phase E（直前調整）"
        if [[ "$today" == "2026-05-16" ]]; then
            tasks="📋 **明日本番です**
- 午前中に苦手問題だけ軽く確認して終了
- 午後は完全休養。持ち物確認、会場確認
- 22時には寝てください先輩
- 📦 持ち物: 受験票・写真付き身分証・筆記用具・腕時計・飲み物"
        else
            tasks="📋 **直前調整（新しい問題はやらない）**
- ⏰ 9:00-10:00 **公式**: 間違えた問題だけ最終チェック（60分）
- ⏰ 10:00-10:30 **金フレ**: 苦手語だけ最終チェック（30分）
- ⏰ 10:30-11:00 **金の文法**: 間違えた問題のみ（30分）
- ⏰ 11:00-12:00 **リスニング**: シャドーイングで耳慣らし（60分）
- 午後はリラックス。**詰め込みは今日で終わり**
- 🏋️ジムは軽めに"
        fi
    fi

    # 試験までの残り日数
    local exam_date="2026-05-17"
    local today_epoch=$(TZ=Asia/Tokyo date -d "$today" +%s)
    local exam_epoch=$(TZ=Asia/Tokyo date -d "$exam_date" +%s)
    local days_left=$(( (exam_epoch - today_epoch) / 86400 ))

    echo "${greeting}

🗓️ **${today}** ｜ ${phase} ｜ 本番まで**あと${days_left}日**

${tasks}

1セッション25分のポモドーロで。スマホは別の部屋に置いてからやってくださいね。"
}

MESSAGE=$(get_task_message "$TODAY" "$HOUR")

# --- DMチャンネル取得 ---
DM_CHANNEL=$(curl -s \
    -X POST "https://discord.com/api/v10/users/@me/channels" \
    -H "Authorization: Bot ${BOT_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"recipient_id\": \"${USER_ID}\"}" | jq -r '.id')

if [[ -z "$DM_CHANNEL" || "$DM_CHANNEL" == "null" ]]; then
    echo "$(date): Failed to open DM channel" >> "$LOG"
    exit 1
fi

# --- メッセージ送信 ---
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "https://discord.com/api/v10/channels/${DM_CHANNEL}/messages" \
    -H "Authorization: Bot ${BOT_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg content "$MESSAGE" '{content: $content}')")

echo "$(date): Sent TOEIC reminder (HTTP $HTTP_CODE, phase=$TODAY, hour=$HOUR)" >> "$LOG"
