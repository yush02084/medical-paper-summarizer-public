"""
専門領域カスタム設定ジェネレーター

自分の専門領域を入力するだけで config.yaml を自動生成します。
GitHub Actions の「Setup」ワークフローから実行してください。
"""

import os
import sys
import json
import yaml
import re
import google.generativeai as genai


PROMPT_TEMPLATE = """
あなたは医学論文収集システムの設定エキスパートです。
以下の専門領域に特化した config.yaml の設定値を生成してください。

専門領域: {specialty}

以下のJSON形式で出力してください。前置きや説明は不要です。JSONのみ出力してください。

{{
  "specialties": {{
    "primary": ["PubMedで使う主要キーワード（英語）を5〜7個"],
    "secondary": ["関連キーワード（英語）を8〜12個"]
  }},
  "journals": {{
    "tier1": ["最高権威ジャーナル名（PubMed表記）を3〜5個"],
    "tier2": ["専門領域の主要ジャーナル名を3〜5個"],
    "tier3": ["その他の重要ジャーナル名を5〜8個"]
  }},
  "daily_themes": {{
    "Monday": {{
      "specialties": ["月曜のサブテーマキーワード（英語）"],
      "journals": ["関連専門誌名"]
    }},
    "Tuesday": {{
      "specialties": ["火曜のサブテーマキーワード（英語）"],
      "journals": ["関連専門誌名"]
    }},
    "Wednesday": {{
      "specialties": ["水曜のサブテーマキーワード（英語）"],
      "journals": ["関連専門誌名"]
    }},
    "Thursday": {{
      "specialties": ["木曜のサブテーマキーワード（英語）"],
      "journals": ["関連専門誌名"]
    }},
    "Friday": {{
      "specialties": ["金曜のサブテーマキーワード（英語）"],
      "journals": ["関連専門誌名"]
    }},
    "Saturday": {{
      "specialties": ["土曜のサブテーマキーワード（英語）"],
      "journals": ["関連専門誌名"]
    }},
    "Sunday": {{
      "specialties": ["日曜のサブテーマキーワード（英語）"],
      "journals": ["関連専門誌名"]
    }}
  }}
}}

重要なルール:
- ジャーナル名は必ずPubMedに登録されている正式略称で記載すること
- キーワードはPubMedのMeSH用語や検索で実際に使われる英語表現にすること
- 曜日ごとに専門領域のサブテーマを分散させること（例: 心不全→不整脈→虚血性心疾患...）
- JSONのみ出力し、前置き・説明・マークダウンコードブロックは不要
"""

BASE_CONFIG = {
    "search": {
        "days_back": 7,
        "max_results": 200,
        "top_n": 10,
        "detailed_top_n": 10
    },
    "study_type_scores": {
        "Randomized Controlled Trial": 10,
        "Meta-Analysis": 9,
        "Systematic Review": 9,
        "Clinical Trial": 8,
        "Multicenter Study": 7,
        "Observational Study": 6,
        "Cohort Study": 6,
        "Practice Guideline": 10,
        "Guideline": 10,
        "Review": 4,
        "Case Reports": 1,
        "Editorial": 2,
        "Comment": 1,
        "Letter": 1
    },
    "exclude_types": [
        "Case Reports",
        "Editorial",
        "Comment",
        "Letter",
        "Published Erratum"
    ],
    "clinical_relevance": {
        "high_value": [
            "randomized controlled trial",
            "clinical practice guideline",
            "treatment outcome",
            "all-cause mortality",
            "cardiovascular outcome",
            "major adverse cardiovascular events",
            "primary endpoint"
        ],
        "practical": [
            "real-world",
            "routine clinical",
            "pragmatic",
            "clinical decision",
            "patient management",
            "standard of care",
            "clinical outcome"
        ],
        "japan_relevant": [
            "japanese",
            "asian",
            "japan",
            "east asian"
        ]
    },
    "basic_science_exclude": [
        "in vitro",
        "mouse model",
        "rat model",
        "cell line",
        "ex vivo",
        "murine",
        "knockout mice",
        "animal model",
        "zebrafish"
    ],
    "ai": {
        "model_chain": [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite-preview-09-2025",
            "gemini-2.0-flash"
        ],
        "timeout": 120,
        "max_retries": 3,
        "retry_delay": 5
    },
    "output": {
        "directory": "output",
        "filename_format": "医学論文レビュー_{date}.docx"
    },
    "history": {
        "file": "history.json",
        "retention_days": 180
    }
}


def generate_specialty_config(specialty: str, api_key: str) -> dict:
    """Gemini APIで専門領域設定を生成する"""
    print(f"専門領域「{specialty}」の設定を生成中...")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        "gemini-2.5-flash",
        generation_config=genai.GenerationConfig(temperature=0.3)
    )

    prompt = PROMPT_TEMPLATE.format(specialty=specialty)
    response = model.generate_content(prompt)
    text = response.text.strip()

    # コードブロックを除去
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)

    generated = json.loads(text)
    return generated


def build_config(specialty: str, generated: dict) -> dict:
    """ベース設定と生成設定をマージしてconfig全体を組み立てる"""
    config = BASE_CONFIG.copy()
    config["specialties"] = generated.get("specialties", {})
    config["journals"] = generated.get("journals", {})
    config["daily_themes"] = generated.get("daily_themes", {})
    return config


def main():
    specialty = os.environ.get("SPECIALTY", "").strip()
    if not specialty:
        print("エラー: 専門領域が指定されていません。")
        print("使い方: SPECIALTY='消化器内科' python setup.py")
        sys.exit(1)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("エラー: GEMINI_API_KEY が設定されていません。")
        sys.exit(1)

    try:
        generated = generate_specialty_config(specialty, api_key)
        config = build_config(specialty, generated)

        with open("config.yaml", "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)

        print("=" * 50)
        print(f"config.yaml を生成しました（専門領域: {specialty}）")
        print("=" * 50)
        print(f"主要キーワード: {', '.join(config['specialties'].get('primary', []))}")
        print(f"Tier1ジャーナル: {', '.join(config['journals'].get('tier1', []))}")
        print("=" * 50)
        print("次のステップ: GitHub Actionsの「Daily Paper Summary」が")
        print("毎朝自動で論文を収集・要約してメールに届けます。")

    except json.JSONDecodeError as e:
        print(f"エラー: AIの出力をパースできませんでした: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"エラー: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
