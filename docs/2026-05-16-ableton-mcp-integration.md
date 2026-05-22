# Ableton MCP 統合設計

## Status: draft (未実装)
## Date: 2026-05-16

## 目的

Discord bot（かなえ）からリモートで先輩のAbleton Liveを操作できるようにする。
自然言語でトラック操作、MIDIクリップ作成、コード進行生成、デバイス操作等を行う。

---

## 候補ツール比較

| | **AbletonMCP** (ahujasid) | **Producer Pal** (MrBenJ) | **Ableton MCP Extended** (uisato) |
|---|---|---|---|
| 通信方式 | MIDI Remote Script → Socket | Max for Live → Node for Max | UDP Hybrid Server |
| セットアップ | Remote Script + pip install | .amxd ドラッグ&ドロップ | Remote Script + pip install |
| ツール数 | ~15 | ~25 (MIDI DSL付き) | ~35 |
| 特徴 | シンプル、安定 | 独自MIDI DSL、M4L内で完結、SSEリモート対応 | XYマウスコントローラ、実験的ツール多数 |
| Ableton要件 | Live 11/12 | Live 11/12 + Max for Live | Live 11/12 |
| GitHub | github.com/ahujasid/ableton-mcp | github.com/MrBenJ/producer-pal | github.com/uisato/ableton-mcp |

### 推奨

- **Phase 1**: AbletonMCP (ahujasid) — シンプルで安定。まず動かす
- **Phase 2**: Producer Pal — MIDI DSLとリヴォイシングが強力。SSEリモート対応あり
- **Phase 3**: 必要に応じて Extended を検討

---

## アーキテクチャ

```
[先輩のWindows PC]                    [Ubuntu サーバー]
Ableton Live 12                       Discord Bot (かなえ)
  ↕ Remote Script (Python)              ↕ Claude Agent SDK
Ableton MCP Server (Python)             ↕
  ↕ localhost → 0.0.0.0:xxxx         MCP Client
  ←───── Tailscale Tunnel ─────→        ↑ ここから叩く
```

### ネットワーク

- **Tailscale** で NAT 越え。VPN設定不要
- Windows PC 側の MCP サーバーポートのみ ACL で開放
- Ubuntu → Windows の片方向のみ許可

### 制約

- Ableton Live は **Windows / macOS 専用**（Linux不可）
- 先輩のローカル PC で Ableton が起動していないと動作しない
- Remote Script は Ableton の起動時に読み込まれる（ホットリロード不可）

---

## 実装手順

### Phase 1: ローカル動作確認（先輩のWindows PC単体）

1. AbletonMCP リポジトリを clone
2. Remote Script を配置
   ```
   %APPDATA%\Ableton\Live 12\Preferences\User Remote Scripts\AbletonMCP\
   ```
3. `pip install -r requirements.txt`（または `uv`）
4. Ableton Live を起動し、Preferences > Link, Tempo & MIDI > Control Surface で AbletonMCP を選択
5. MCP Server を起動して Claude Code ローカルから動作確認

### Phase 2: Tailscale トンネル構築

1. Windows PC に Tailscale をインストール（未導入の場合）
2. Ubuntu サーバーの Tailscale は導入済みか確認
3. MCP Server の listen アドレスを `0.0.0.0` に変更
4. Tailscale ACL 設定:
   ```json
   {
     "acls": [
       {
         "action": "accept",
         "src": ["ubuntu-server"],
         "dst": ["windows-pc:<mcp-port>"]
       }
     ]
   }
   ```
5. Ubuntu 側から `ping <windows-tailscale-ip>` で疎通確認
6. Ubuntu 側から MCP サーバーへの接続テスト

### Phase 3: Discord Bot 統合

1. Discord bot の MCP 設定に Tailscale IP + ポートを追加
2. Ableton 操作用のコマンドまたは自然言語ハンドラを実装
3. 「Ableton が起動していない場合」のエラーハンドリング
4. かなえが自発的に「今 Ableton 開いてますか？」と確認するフローの設計

---

## セキュリティ考慮

- Tailscale ACL で Ubuntu → Windows の MCP ポートのみに制限
- MCP Server に認証トークンを追加することを検討（AbletonMCP デフォルトは認証なし）
- Ableton 操作は admin ユーザーのみ（Discord bot の権限モデルに準拠）
- `0.0.0.0` listen 時は Tailscale インターフェースのみにバインドする設定を推奨

---

## 未解決事項

- [ ] 先輩の Windows PC に Tailscale が導入済みか
- [ ] Ableton のバージョン（Live 11 or 12）
- [ ] Max for Live ライセンスの有無（Producer Pal に必要）
- [ ] MCP Server のポート番号の決定
- [ ] 認証トークンの実装要否
- [ ] Ableton 未起動時のフォールバック動作の定義

---

## 参考

- AbletonMCP: https://github.com/ahujasid/ableton-mcp
- Producer Pal: https://github.com/MrBenJ/producer-pal
- Ableton MCP Extended: https://github.com/uisato/ableton-mcp
