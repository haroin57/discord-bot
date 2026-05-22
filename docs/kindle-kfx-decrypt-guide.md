# Kindle KFX DRM解除 & PDF変換ガイド

> 2026年5月時点の手順。Kindle for PC 2.9.1 + Satsuoni fork DeDRM v10.0.20 で動作確認済み。

## 背景

- 2025年4月以降、AmazonはKindle DRMの暗号化を大幅に強化した
- 旧来の「Kindle for PC 1.17 + AZW3フォールバック + DeDRM 10.0.3」ルートは**完全に時代遅れ**
- 現行のKindle for PCはKFX形式がデフォルト配信。AZW3にフォールバックさせる方が困難
- noDRM/DeDRM_tools (公式) は v10.0.9 が最新だが、Kindle PC 2.9.x には未対応
- **Satsuoni fork の DeDRM v10.0.20** が 2.9.1 に対応する `KFXArchiver291.exe` を同梱している

## バージョン互換マトリクス

| DeDRM | 対応 Kindle for PC | 備考 |
|---|---|---|
| noDRM v10.0.9 | 2.4.0以下 | 2025年4月以前購入の本のみ |
| Satsuoni v10.0.18 | 2.8.0〜2.8.3 | KFXKeyExtractor同梱 |
| **Satsuoni v10.0.20** | **2.9.0〜2.9.1** | **KFXArchiver291.exe同梱（推奨）** |

## 必要ツール

| ツール | バージョン | 入手先 |
|---|---|---|
| Kindle for PC | 2.9.1.71006（最新版） | Amazon公式 |
| Calibre | 9.8.0+ | https://calibre-ebook.com |
| DeDRM plugin | v10.0.20 (Satsuoni fork) | https://github.com/Satsuoni/DeDRM_tools/releases/tag/v10.0.20 |
| KFX Input plugin | v2.25.0 (jhowell) | https://github.com/jhowell/calibre-kfx-input |
| KFXArchiver291.exe | DeDRM v10.0.20に同梱 | 上記リリースのZIP内 |

## KFXファイルの構造

KFXは単一ファイルではなく、Amazon独自のIonバイナリが複数ファイルに分かれて格納される：

```
B0836KPMHM_EBOK/
├── B0836KPMHM_EBOK.azw          (42KB)  ← マニフェスト/ヘッダ
├── CR!0MFFT03Q590M59AN45SZR41J1N5N.azw.res  (3MB)   ← コンテンツ chunk
├── CR!2VX2M4NC0N74N0SD2BRJ4TGPFDDF.azw.res  (1.8MB) ← コンテンツ chunk
├── ... (他にも複数の .azw.res)
└── amzn1.drm-voucher.v1.*.voucher            ← DRM鍵
```

## 手順

### 1. Calibreにプラグインをインストール

```bash
# DeDRM v10.0.20 (Satsuoni fork)
calibre-customize -a DeDRM_plugin.zip

# KFX Input (jhowell)
calibre-customize -a KFX_Input.zip

# 確認
calibre-customize -l | grep -E "DeDRM|KFX"
```

### 2. Kindle for PCで本をダウンロード

- Kindle for PC 2.9.1 を起動し、amazon.co.jp アカウントでサインイン
- ライブラリから対象の本をダウンロード
- ダウンロード先: `%USERPROFILE%\OneDrive\ドキュメント\My Kindle Content\<ASIN>_EBOK\`

### 3. KFXArchiver291.exeでDRM解除

**重要: Kindle for PCが起動中の状態で実行する**（プロセスメモリから鍵を抽出するため）

```bash
# 日本語パスを含むとエラーになるため、ASCII-onlyのパスにコピー
mkdir -p C:\kindle_temp\MyKindleContent
xcopy "%USERPROFILE%\OneDrive\ドキュメント\My Kindle Content\*" C:\kindle_temp\MyKindleContent\ /E /Y

# KFXArchiver291.exe を実行
cd "%LOCALAPPDATA%\Amazon\Kindle\application"
KFXArchiver291.exe "C:\kindle_temp\MyKindleContent" "output_folder" "output.k4i"
```

出力:
- `output_folder/<ASIN>_EBOK.kfx-zip` — DRM解除済みKFXアーカイブ
- `output.k4i` — 抽出された復号鍵（以後の復号に再利用可能）

### 4. PDF変換

```bash
ebook-convert "output_folder\<ASIN>_EBOK.kfx-zip" "output\<タイトル>.pdf"
```

漫画の場合、追加オプションは不要。Calibreが自動でページ構成を判定する。

## 既知の注意点

### 日本語パス問題
`KFXArchiver291.exe` は日本語を含むパス（例: `ドキュメント`）を処理できない。エラーコード `Could not open book directory : 3` が出る場合は、ASCII-onlyのパスにコピーしてから実行する。

### 鍵の取得元
- Kindle for PC 1.39以降: デバイスキーではなくアカウント認証ベース
- `.kinf2024`（新フォーマット）は DeDRM 10.0.9 以前の `kindlekey.py` では読めない
- `KFXArchiver291.exe` は Kindle プロセスのメモリから直接鍵を抽出するため、`.kinf` のバージョンに依存しない

### 失敗したアプローチ（参考）

| アプローチ | 結果 | 理由 |
|---|---|---|
| Kindle 1.17 + renderer-test.exe リネームで AZW3 強制 | 失敗 | 帯域制限で DL が途中停止。時代遅れ |
| mitmproxy でリクエスト書換 | 失敗 | TLS pinningは無いが、認証トークン(`X-ADP-Authentication-Token`)が暗号化されており書換不能 |
| Amazonアカウントの Country/Region 変更 | 効果なし | JP CDNサーバ自体が物理的にUS西海岸(Oregon)にある |
| noDRM DeDRM v10.0.9 + Kindle 2.9.x | 失敗 | `.kinf2024` 未対応 |

### _EBSP形式
旧KFX形式（`_EBSP`サフィックス）は本手順では復号できない。別ルートが必要。

## 一括処理スクリプト例

```bash
#!/bin/bash
# Usage: ./kfx_batch_decrypt.sh
set -e

SRC="C:\kindle_temp\MyKindleContent"
KINDLE_APP="%LOCALAPPDATA%\Amazon\Kindle\application"
OUT_KFX="$KINDLE_APP\output_folder"
OUT_PDF="C:\Users\%USERNAME%\Downloads\pdf_output"

mkdir -p "$OUT_PDF"

# 1. コピー (日本語パス回避)
xcopy "%USERPROFILE%\OneDrive\ドキュメント\My Kindle Content\*" "$SRC\" /E /Y

# 2. 復号 (Kindle起動中に実行)
cd "$KINDLE_APP"
KFXArchiver291.exe "$SRC" "output_folder" "output.k4i"

# 3. PDF変換
for kfx in "$OUT_KFX"/*.kfx-zip; do
  base=$(basename "$kfx" .kfx-zip)
  ebook-convert "$kfx" "$OUT_PDF/${base}.pdf"
done

echo "Done. PDFs in $OUT_PDF"
```
