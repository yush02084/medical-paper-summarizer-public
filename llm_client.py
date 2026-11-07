"""
Gemini APIクライアント（google-genai SDK版）

- 旧SDK google-generativeai はサポート終了のため google-genai を使用
- 起動時にAPIから利用可能なモデル一覧を取得し、廃止済みモデルを自動で除外
- 設定のモデルが全滅していた場合は、利用可能なFlash系モデルから自動で補う
"""

import json
import logging
import re
import time
from typing import Optional

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# 一時的なエラー（同じモデルで待って再試行する価値がある）
_TRANSIENT_KEYWORDS = ["500", "internal"]
# 次のモデルへすぐ切り替えるべきエラー
# 503（high demand）は数十秒待っても解消しないことが実運用で確認されたため即切り替える
_SWITCH_KEYWORDS = [
    "503", "unavailable", "overloaded", "high demand",
    "rate limit", "quota", "429", "resource_exhausted",
    "not found", "404", "not supported", "deprecated",
    "504", "deadline", "timeout", "permission", "403",
]


class GeminiClient:
    """フォールバックチェーン付きのGemini呼び出しクラス"""

    def __init__(self, config: dict, api_key: str):
        ai_config = config.get("ai", {})
        self.configured_chain = ai_config.get("model_chain", [])
        self.max_retries = ai_config.get("max_retries", 3)
        self.retry_delay = ai_config.get("retry_delay", 5)
        self.timeout = ai_config.get("timeout", 120)
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=self.timeout * 1000),
        )
        self.model_chain = self._resolve_chain()
        # 実行状況の記録（メール件名の警告判定に使う）
        self.models_used: dict[str, int] = {}
        self.failures = 0

    # ------------------------------------------------------------
    # モデル一覧の解決
    # ------------------------------------------------------------
    def _list_available_models(self) -> Optional[set]:
        """generateContentに対応したモデルIDの集合を返す（取得失敗時はNone）"""
        try:
            names = set()
            for m in self.client.models.list():
                actions = getattr(m, "supported_actions", None) or []
                if actions and "generateContent" not in actions:
                    continue
                name = (m.name or "").replace("models/", "")
                if name:
                    names.add(name)
            return names
        except Exception as e:
            logger.warning(f"モデル一覧の取得に失敗（設定のチェーンをそのまま使用）: {e}")
            return None

    @staticmethod
    def _flash_sort_key(name: str):
        """Flash系モデルを新しい順に並べるためのキー"""
        nums = re.findall(r"\d+(?:\.\d+)?", name)
        version = float(nums[0]) if nums else 0.0
        is_lite = "lite" in name
        return (-version, is_lite)

    def _resolve_chain(self) -> list[str]:
        available = self._list_available_models()
        if available is None:
            return list(self.configured_chain)

        chain = [m for m in self.configured_chain if m in available]
        dropped = [m for m in self.configured_chain if m not in available]
        if dropped:
            logger.warning(f"利用できないモデルを除外しました: {dropped}")

        if not chain:
            # 設定のモデルが全滅 → 利用可能な安定版Flash系から自動選択
            candidates = [
                n for n in available
                if n.startswith("gemini-") and "flash" in n
                and not any(x in n for x in [
                    "preview", "exp", "image", "tts", "live", "audio", "embedding"
                ])
            ]
            chain = sorted(candidates, key=self._flash_sort_key)[:3]
            logger.warning(
                f"設定のモデルが全て利用不可のため自動選択しました: {chain}"
                " → config.yaml の model_chain を更新してください"
            )

        logger.info(f"使用するモデルチェーン: {chain}")
        return chain

    # ------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------
    def generate(
        self,
        prompt: str,
        temperature: float = 0.3,
        max_output_tokens: int = 4096,
        json_mode: bool = False,
    ) -> Optional[str]:
        """フォールバックチェーンでテキストを生成する（全失敗時はNone）"""
        gen_config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json" if json_mode else None,
        )

        for model_name in self.model_chain:
            for attempt in range(self.max_retries):
                try:
                    logger.info(
                        f"モデル {model_name} を使用中（試行 {attempt + 1}/{self.max_retries}）"
                    )
                    response = self.client.models.generate_content(
                        model=model_name, contents=prompt, config=gen_config
                    )
                    text = response.text
                    if text:
                        logger.info(f"[OK] {model_name} で生成成功")
                        self.models_used[model_name] = self.models_used.get(model_name, 0) + 1
                        return text
                    logger.warning(f"{model_name} の応答が空でした")
                except Exception as e:
                    msg = str(e).lower()
                    logger.warning(f"モデル {model_name} でエラー（試行 {attempt + 1}）: {e}")
                    if any(kw in msg for kw in _SWITCH_KEYWORDS):
                        logger.info("→ 次のモデルにフォールバックします")
                        break
                    if not any(kw in msg for kw in _TRANSIENT_KEYWORDS) and attempt >= 1:
                        break
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay * (attempt + 1))

        logger.error("全モデルで生成に失敗しました")
        self.failures += 1
        return None

    def generate_json(self, prompt: str, **kwargs):
        """JSONを返させてパースする（失敗時はNone）"""
        text = self.generate(prompt, json_mode=True, **kwargs)
        if not text:
            return None
        return parse_json_loose(text)


def parse_json_loose(text: str):
    """コードフェンス等が混ざったJSON文字列を寛容にパースする"""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # 最初の [ または { から最後の ] または } までを抜き出して再挑戦
        m = re.search(r"(\[.*\]|\{.*\})", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    logger.warning("JSONのパースに失敗しました")
    return None
