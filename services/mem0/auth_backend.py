"""BotOAuthBackend — llm_mem0 (Mem0α) への bot 固有 auth 実装の注入。

llm_mem0 は auth を pluggable にしている(llm_mem0.auth.AuthBackend)。この
backend は bot が既に持つ認証資産を**複製せずに呼ぶだけ**で、3つの bot 固有
要件を α 経路に持ち込む:

1. **oauth_manager を単一真実源に**: トークンの取得・更新は
   services.oauth_manager の専権のまま(α 側の自己 refresh は
   LLM_MEM0_ALLOW_TOKEN_REFRESH=0 で封じる — _config_bridge 参照)。
2. **CLI 指紋課金ルーティング**: complete() は既存の
   client.get_anthropic_client() + cli_compat.cli_messages_create() を経由
   するので、サブスク超過分の Extra Usage ルーティングと betas/thinking の
   ワークアラウンド(cli_compat.py の長大コメント参照)がそのまま効く。
   遅延 import なので、テストの patch 面
   (services.mem0.client.get_anthropic_client /
   services.mem0.cli_compat.cli_messages_create) も無修正で生きる。
3. **DeepSeek フォールバックチェーン**: OAuth 不可時は OpenRouter →
   OpenCode Zen (client._desired_llm_provider)。provider_id() がこれを
   返すため、α の client は provider 切替を検知して Memory インスタンスを
   再構築する(旧 _mem0_provider drift 再構築と等価)。

注: 旧実装では OpenAI 互換クライアントに対しても cli_messages_create
(client.messages.create 前提) を呼んで AttributeError → fail-open "" に
なっていた。ここでは chat.completions に正しく分岐する(意図的な修正。
パリティ検証の除外項目として文書化済み)。
"""

from __future__ import annotations

import logging
import os

from llm_mem0.auth.base import AuthBackend

log = logging.getLogger(__name__)

# client.py の _DEEPSEEK_V4_FLASH と同一値。circular import を避けるため
# 定数はここに重複させず、フォールバック呼び出し時に client から取る。


class BotOAuthBackend(AuthBackend):
    """oauth_manager + cli_compat + フォールバックチェーンの注入 backend。"""

    async def complete(self, *, system: str, user_message: str,
                       model: str, max_tokens: int) -> str:
        # 遅延 import: sys.modules["services.mem0.client"] は flag=1 では
        # client_alpha ファサード。patch("services.mem0.client.
        # get_anthropic_client") がここに効く。
        from services.mem0 import client as client_mod
        llm_client = await client_mod.get_anthropic_client()
        if llm_client is None:
            return ""
        try:
            if hasattr(llm_client, "messages"):
                # Anthropic 経路 — CLI 指紋 + Extra Usage 課金ルーティング。
                # system 引数は cli_compat の task_prompt スロット(system[3])
                # に入り、CLI 身元の system[0..2] は cli_compat が透過付与。
                from services.mem0.cli_compat import cli_messages_create, extract_text
                resp = await cli_messages_create(
                    llm_client, model=model, max_tokens=max_tokens,
                    user_message=user_message, task_prompt=system,
                )
                return extract_text(resp)
            # OpenAI 互換フォールバック (OpenRouter / OpenCode の DeepSeek)。
            resp = await llm_client.chat.completions.create(
                model=client_mod._DEEPSEEK_V4_FLASH,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
            )
            if getattr(resp, "choices", None):
                return resp.choices[0].message.content or ""
            return ""
        except Exception as exc:
            log.warning("BotOAuthBackend.complete failed (fail-open): %s", exc)
            return ""

    def mem0_llm_config(self, *, model: str) -> dict:
        """mem0 内部 LLM の config ブロック。bot の _build_config が組む
        llm ブロック(anthropic / openrouter / opencode の3分岐)をそのまま
        使う — フォールバック判断を二重実装しない。``model`` は同じ env
        (MEM0_LLM_MODEL) から _build_config 側でも解決されるため引数側の
        値は情報として一致する。"""
        from services.mem0 import client as client_mod
        return client_mod._build_config()["llm"]

    def default_model(self) -> str:
        from services.codex_app_server import auxiliary
        if auxiliary.enabled():
            from config import CODEX_APP_SERVER_MODEL
            return CODEX_APP_SERVER_MODEL
        return os.environ.get("MEM0_LLM_MODEL", "claude-haiku-4-5-20251001")

    def provider_id(self) -> str:
        """実効 provider (anthropic/openrouter/opencode/none)。値が変わると
        α の client が Memory インスタンスを再構築する(drift 追従)。"""
        from services.mem0 import client as client_mod
        try:
            return client_mod._desired_llm_provider() or "none"
        except Exception:
            return "unknown"

    def refresh_memory_llm(self, memory) -> None:
        """ローテーションした OAuth トークンを mem0 内部の Anthropic
        クライアントへ反映(旧 _refresh_mem0_client_token 相当)。同期
        フックなので oauth_manager のキャッシュ済みトークンを読むだけ —
        更新自体は bot 常駐の oauth_manager バックグラウンド更新に任せる。"""
        try:
            from services.oauth_manager import get_access_token
            token = get_access_token()
            if not token:
                return
            llm = getattr(memory, "llm", None)
            llm_client = getattr(llm, "client", None) if llm else None
            if llm_client is not None and hasattr(llm_client, "auth_token"):
                if getattr(llm_client, "auth_token", None) != token:
                    llm_client.auth_token = token
                    log.debug("BotOAuthBackend: refreshed mem0 internal client token")
        except Exception as exc:
            log.debug("BotOAuthBackend.refresh_memory_llm failed (non-fatal): %s", exc)
