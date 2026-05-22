# UK学生ビザ下のフリーランス収入と仮想通貨マネロンの検出構造

調査日: 2026-05-17
範囲: 実態調査 (やってる人の存在 / 検出メカニズム / 規制動向)。具体的な回避手法の組み立ては範囲外。

---

## 結論

**やってる人は存在する。ただし2026年1月のCARF発効で構造が大きく変わった。**

2025年までは「やり方次第で何とか」、2026年以降はチェーン分析 × CARF × lifestyle audit の三段網になっており、完全匿名チェーンを通しても、UKで生活する以上どこかで現実世界と接続する点が必ず生まれて、そこで照合される。

**技術的匿名性と経済的匿名性は別問題。後者を切れないと前者の意味がなくなる。**

---

## 1. やってる層の典型

- 母国に既存クライアントがいる IT / デザイナー / ライター / チューター
- 受取経路: 仮想通貨で受け取り → 非KYC交換所 / P2P で分割 off-ramp
- r/ukvisa / Quora / JustAnswer に定期的に相談・自白・摘発体験談が上がる

---

## 2. 2026年1月の構造変化: CARF発効

**CARF (Crypto-Asset Reporting Framework)**

- 2026-01-01 発効
- UK内の crypto-asset service provider (取引所・カストディアン・ウォレットprovider) が**ユーザー本人情報＋取引記録をHMRCに年次報告する義務**
- 52ヶ国同時実装、初回データ交換は2027年5月
- HMRCはCARFで2026-2030に £350M の追徴予算を計上
- Binance UK / Kraken UK / Coinbase UK 等、口座作った時点でHMRCに筒抜け

これ以前から、HMRC ↔ Home Office ↔ DWP の3者間 cross-check は稼働済み (FOI開示で2015-2020の5年間に463,000人分の納税データがHMRCからHome Officeへ共有された実績あり)。CARFはここに**仮想通貨レイヤーを追加**した。

---

## 3. 非KYC回避の限界

**非KYC P2Pは技術的には存在する**
- Bisq / Haveno / ChangeNOW 等

**ただし出口 (off-ramp) で詰む**
- 最終的にGBPで家賃・食費を払う必要がある以上、どこかで銀行と接続 → AML check + SAR (Suspicious Activity Report)
- Crypto Visa Card (Crypto.com, Wirex) も FCA 規制下 → CARF対象
- Monero は中央集権取引所からほぼ delist 済み、出口経路はさらに細い

---

## 4. 実際の摘発事例

| 案件 | 時期 | 概要 |
|---|---|---|
| Samourai Wallet (mixer) | 2025-11 | 共同創業者が5年・4年の実刑。$237M処理。DOJはnon-custodial mixerもunlicensed money transmitterとして扱う立場を確立 |
| Miami case | 2025-09 | ビザoverstayの19歳カナダ人＋共犯者が暗号通貨詐欺$13M。マネロン共謀、最大20年 |
| Gugnin case | - | 暗号通貨決済会社経由で制裁回避＋マネロン。BSA violation で起訴 |

---

## 5. 既存の検出経路 (CARF以前から稼働)

1. HMRC ↔ Home Office ↔ DWP の3者間 cross-check (DLA Piper 2025が公式言及)
2. 大学 (sponsor) の通報義務
   - Coventry大学が学費支払い1日遅れただけで即UKVI通報→curtailmentした実例 (Guardian 2026-04)
3. 銀行 AML / SAR 報告 — 不審入金で flag → HMRC 照会
4. 雇用主の digital right-to-work check 義務化 (2025)
5. visa extension / Graduate visa 申請時の past activity 質問
6. 公的タレコミ＋過去コンプライアンス訪問のフォローアップ

---

## 6. 構造的詰みポイント: lifestyle audit

仮にチェーン上の追跡を完璧に切っても、**生活実態と申告所得の乖離**が残る。

- UK滞在は学費＋生活費で年£20-30k必要
- 申告資金源 (家族送金・貯金) と支出のギャップでHMRCが lifestyle audit を発動できる
- 大学スポンサーも financial soundness を見る (Coventry即通報事例の系統)
- ビザ更新 / Graduate visa 申請時に過去活動を照会される

---

## 参考リンク

- [JustAnswer: ナイジェリア人留学生フリーランス事例](https://www.justanswer.co.uk/immigration-law/s71vy-friend-student-visa-involved-freelance.html)
- [Rapid Formations: 国際学生の起業に関する解説](https://www.rapidformations.co.uk/blog/can-an-international-student-start-a-business-in-the-uk/)
- [EIN: HMRC/Home Office間データ共有 (FOI開示)](https://www.ein.org.uk/news/hm-revenue-and-customs-shared-tax-data-over-460000-migrants-home-office-between-2015-and-2020)
- [Guardian 2026-04: Coventry大学事例](https://www.theguardian.com/education/2026/apr/15/sri-lankan-student-could-be-deported-from-uk-student-fee-delay-coventry-university)
