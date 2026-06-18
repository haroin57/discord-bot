# ローカル Claude Code に投げるプロンプト案

以下を **そのまま** ローカル Claude Code セッションに貼り付けて実行してもらえる粒度で書いてあります。Windows / Mac / Linux のどこから実行しても動く想定。

---

## プロンプト本文 (これをコピーしてローカル Claude に渡す)

```
discord-bot リポジトリ (ローカル clone 済み) の feature/dynam-ueda-parser ブランチに、
ダイナム長野上田店のスロット稼働データを min-repo.com からスクレイピングするスクリプトがあります。

このスクリプトを実行して、出力 4 ファイル (events.csv, by_machine.csv, variety_units.csv,
timeseries.csv) を Discord に貼れる状態にしてください。

【手順】

1. リポジトリのルートに移動して、ブランチを切り替える:
       cd /path/to/discord-bot
       git fetch origin
       git checkout feature/dynam-ueda-parser
       git pull origin feature/dynam-ueda-parser

2. 依存を入れる (まだなら):
       pip install requests

3. スクリプトを実行する (直近 2 ヶ月分):
       python scripts/dynam_ueda_parser.py \
           --start 2026-04-18 \
           --end 2026-06-17 \
           --out ./dynam_ueda_output

   注意:
   - rate limit を 3 秒に取ってあるので、完了まで 3〜10 分くらいかかります
   - 標準エラー出力に [INFO] / [OK] / [WARN] が流れます、放置で OK
   - 途中で [WARN] article XXX fetch failed が数件出るのは正常 (rate limit 周りの一時失敗)、
     5 割以上が WARN になったら止めて私に報告

4. 完了したら出力ファイルの行数を確認:
       wc -l dynam_ueda_output/*.csv

5. 4 ファイルを zip にまとめる:
       cd dynam_ueda_output
       zip dynam_ueda_$(date +%Y%m%d).zip events.csv by_machine.csv variety_units.csv timeseries.csv

6. zip ファイルを Discord の bot チャンネルに添付して送ってください
   (haroin57 がやります、ローカル Claude は zip を吐くところまで)

【期待する出力サイズ (サニティチェック)】

| ファイル          | 期待行数         | 用途                       |
|------------------|------------------|---------------------------|
| events.csv       | 50〜60 行 + ヘッダ | 各日のサマリ              |
| by_machine.csv   | 2,500〜3,500 行   | 機種別平均 (約 50 機種×60 日) |
| variety_units.csv| 1,200〜1,800 行   | バラエティ個別 (約 25 台×60 日) |
| timeseries.csv   | by_machine と同水準 | グラフ用時系列            |

行数が大幅にずれてる場合:
- 「上田店」ヒット数が 30 未満 → --tag-pages を 10 に増やして再実行
- 全体的に空 → ネットワーク or rate limit 問題、私に状況報告
- WARN が多発 → min-repo の HTML 構造変化、私 (クラウド側のかなえ) に報告

【完了後のフロー】

zip を bot チャンネルに添付してもらえれば、クラウド側のかなえ (このリポジトリで動いてる
bot) が受け取って分析・狙い台レコメンド・グラフ可視化を出します。
```

---

## メモ (haroin57 用、ローカル Claude には渡さなくて OK)

- スクリプトの設計検証は 2026-06-18 セッション (6/17 1日分で動作確認済み)
- ローカル実行を推奨する理由: daidata.goraggio.com の CloudFront WAF が datacenter ASN を遮断 → bot 側 EC2 から取れない (Issue #22)。min-repo は residential アクセスなら問題ないが、bot 側 EC2 でも取れる (今のところ)。ただし長期的にはローカルでの実行が rate limit / マナー観点で筋良い
- スロセレ取材日 (`SLOSELE_DAYS` 定数) は判明分のみ列挙、新たな取材日が判明したらコード内定数に追記して PR ください
- 完成 CSV をかなえに渡したあと、分析の最終出力は: 機種別の特定日プラス回数ランキング、出率分布、7/7 (月曜・7のつく日) の狙い候補レコメンド
