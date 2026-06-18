# VSCode Server メモリリーク調査と対策

## 観測された問題

再起動前（25日連続稼働時点）:
- Extension Host 単一プロセスで **RSS 2.3GB**（システム全体の60%）
- 利用可能メモリ **105MiB**（ほぼ詰み状態）
- ディスク使用率 **87%**（残り6.4GB）

## 原因分析

### 1. メモリリークの主犯（推定）

Extension Host に常駐する **AI系エクステンション3つ** が並行動作している:

| エクステンション | 再起動直後のRSS | 性質 |
|---|---|---|
| `anthropic.claude-code` (2プロセス) | **468MB** | セッション履歴を `--resume` で展開 |
| `openai.chatgpt` (codex) | 65MB | LSP風常駐サーバー |
| `github.copilot-chat` | (起動時のみ) | チャット履歴常駐 |

claude-code が2プロセス並行で動いてるのは、`--resume <session-id>` で過去セッションを復元してるため。**この履歴が肥大化するとExtension Hostのメモリに直接乗る**。

実測データ:
```
~/.claude/projects/ 配下のセッションJSONL最大サイズ
  12,182,358 bytes (12MB) -- 単一ファイル
セッションファイル総数: 2,368個
~/.claude/projects 合計: 340MB
```

12MB の JSONL を `--resume` でメモリ上の構造体に展開すると、JSON parse後は **30〜50MBに膨張**するのが普通。これが何度も発生し、GCで回収しきれずに累積するとリーク様の挙動になる。

### 2. ディスク逼迫の真因

VSCode Serverの**過去バージョンが累積**している:

```
~/.vscode-server/cli/servers/
  Stable-560a9dba... : 380MB  ← 旧バージョン
  Stable-8b640eef...  : 375MB  ← 旧バージョン
  Stable-0958016b... : 376MB  ← 現行バージョン
合計: 1.13GB
```

加えて `~/.vscode-server/code-<hash>/` も同様に5バージョン残存（合計150MB）。

ログも累積:
```
~/.vscode-server/data/logs/20260512T205854/  79MB
  └ exthost1〜exthost12 (Extension Hostインスタンス12個分のログ)
```

5日前のセッション1回分のログだけで79MB。原因は**長時間稼働でExtension Hostが何度も再起動し、その都度新規ログディレクトリが切られている**こと。

### 3. なぜ25日で2.3GBまで膨らんだか

- ベース消費（claude-code 2プロセス + codex + その他）: 約500MB
- セッション履歴の累積展開（Node.jsのGC回収漏れ）: 数十MB/日のペースで蓄積
- 25日 × 数十MB ≒ **1.5GB+ の蓄積**
- 結果として 500MB ベース + 1.5-1.8GB の蓄積 = **2.3GB**

これはClaude Code Extensionに固有のリークというより、**長期間resume履歴を抱え続けたNode.jsプロセスの一般的な挙動**。

## 対策

### 即時対応（やった/やる）

| 対策 | 状態 | 効果 |
|---|---|---|
| `pkill -f vscode-server` で再起動 | **完了** | RSS 2.3GB → 637MB |
| `~/.vscode-server/cli/servers/Stable-560a*` 削除 | 未 | +380MB空き |
| `~/.vscode-server/cli/servers/Stable-8b64*` 削除 | 未 | +375MB空き |
| `~/.vscode-server/code-560a*` `code-8b64*` 等の旧版削除 | 未 | +120MB空き |
| `~/.vscode-server/data/logs/20260512T205854/` 削除 | 未 | +79MB空き |
| **合計回収見込み** | | **約950MB** |

### 中期対応（リーク再発防止）

**A. Extension Host の定期再起動**

cron で週次再起動を仕込む:
```bash
# 毎週日曜 04:00 に再起動
0 4 * * 0 pkill -f "type=extensionHost" 2>/dev/null
```

extensionHost のみキルすれば、VSCode本体への影響なしでメモリリセット可能。再接続時に自動復旧する。

**B. Claude Code セッション履歴のローテーション**

`~/.claude/projects/` 配下を1ヶ月超過分は削除:
```bash
find ~/.claude/projects -name "*.jsonl" -mtime +30 -delete
```

cron化推奨:
```bash
# 毎日深夜2時に古いセッション削除
0 2 * * * find ~/.claude/projects -name "*.jsonl" -mtime +30 -delete
```

これで `.claude/projects` のサイズが安定する。`--resume` の対象も最近30日に限定されるので、Extension Hostのメモリ圧迫も軽減。

**C. AI extension の絞り込み**

現在 **3つのAI extension** が並行常駐している（claude-code / chatgpt / copilot-chat）。**1つで十分なはず**。使ってない2つはアンインストール推奨。

優先度: claude-code を残し、chatgpt と copilot-chat を外す（先輩の使用パターン的に）。

**D. メモリ監視のアラート化**

シンプルなcron監視:
```bash
# 毎時、Extension HostのRSSが1.5GBを超えたら通知
0 * * * * /home/ubuntu/discord-bot/scripts/check_exthost_memory.sh
```

スクリプト雛形:
```bash
#!/bin/bash
RSS_KB=$(ps -o rss= -p $(pgrep -f "type=extensionHost" | head -1) 2>/dev/null)
if [ -n "$RSS_KB" ] && [ "$RSS_KB" -gt 1500000 ]; then
    # Discordに通知 or ログ
    echo "$(date): Extension Host RSS = ${RSS_KB}KB" >> /tmp/exthost_alert.log
fi
```

### 長期対応（構造改善）

**E. swap活用の検証**

現在 swap 8GB のうち 1.4GB 使用中。これは正常な動作だが、Extension Host のリーク分が swap に逃げてる可能性もある。`swappiness` を下げる:

```bash
# 現在値確認
cat /proc/sys/vm/swappiness
# 推奨: 10 (デフォルトは60、サーバー用途では低めが望ましい)
sudo sysctl vm.swappiness=10
echo "vm.swappiness=10" | sudo tee -a /etc/sysctl.conf
```

**F. 開発ワークフローの見直し**

VSCode Serverを「常時起動」する必要があるか？
- 使ってない時間帯（深夜・外出時）はsystemdで止める運用も可
- ただしClaude Code経由のbot運用がVSCode依存なら現状維持

## 推奨される実行順序

```bash
# 1. ディスク回収 (即時、950MB空き)
rm -rf ~/.vscode-server/cli/servers/Stable-560a9dba96f961efea7b1612916f89e5d5d4d679
rm -rf ~/.vscode-server/cli/servers/Stable-8b640eef5a6c6089c029249d48efa5c99adf7d51
rm -rf ~/.vscode-server/code-560a9dba96f961efea7b1612916f89e5d5d4d679
rm -rf ~/.vscode-server/code-8b640eef5a6c6089c029249d48efa5c99adf7d51
rm -rf ~/.vscode-server/code-41dd792b5e652393e7787322889ed5fdc58bd75b
rm -rf ~/.vscode-server/code-0870c2a0c7c0564e7631bfed2675573a94ba4455
rm -rf ~/.vscode-server/data/logs/20260512T205854

# 2. Claude Codeセッション履歴の30日超過分削除
find ~/.claude/projects -name "*.jsonl" -mtime +30 -delete

# 3. cron登録（週次extensionHost再起動 + 日次セッションクリーンアップ）
(crontab -l 2>/dev/null; echo "0 4 * * 0 pkill -f 'type=extensionHost' 2>/dev/null") | crontab -
(crontab -l 2>/dev/null; echo "0 2 * * * find ~/.claude/projects -name '*.jsonl' -mtime +30 -delete") | crontab -

# 4. swap調整
sudo sysctl vm.swappiness=10
echo "vm.swappiness=10" | sudo tee -a /etc/sysctl.conf

# 5. 不要extension削除（optional）
# ~/.vscode-server/extensions/openai.chatgpt-26.513.21555-linux-x64 削除
# ~/.vscode-server/extensions/github.copilot-chat-0.48.1 削除
```

## 注意

削除操作はディスクから不可逆。実行前に重要なものが含まれてないか確認すること。
特に `~/.claude/projects/` の30日超過削除は、**過去のClaude Codeセッション履歴を失う**。先輩がレビュー目的で過去履歴を参照する習慣があるなら、削除前にバックアップを取ること:

```bash
tar czf ~/claude_projects_backup_$(date +%Y%m%d).tar.gz ~/.claude/projects
```
