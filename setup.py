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
from llm_client import GeminiClient


PROMPT_TEMPLATE = """
あなたは医学論文収集システムの設定エキスパートです。
以下の専門領域に特化した config.yaml の設定値を生成してください。

専門領域: {specialty}
曜日テーマの希望: {daily_theme_preferences}

★★★ 最重要ルール: キーワードの形式について ★★★
このシステムでは、キーワードを PubMed の「[Title/Abstract]」フィールドで完全一致検索します。
そのため、キーワードは「実際に論文のタイトルやアブストラクトに出現するフレーズ」でなければなりません。

【禁止】MeSH見出し語やカテゴリ名をそのままキーワードにすること
  NG例: "Cardiovascular diseases", "Arrhythmias", "Neoplasms", "Digestive System Diseases"
  → これらはMeSHの階層見出し語であり、論文のTitle/Abstractにはほぼ出現しません

【必須】論文中に実際に登場する具体的な疾患名・病態・治療法を使うこと
  OK例: "heart failure", "atrial fibrillation", "myocardial infarction", "coronary artery disease"
  OK例: "gastric cancer", "inflammatory bowel disease", "hepatocellular carcinoma"
  → すべて小文字で記述すること（PubMedのTitle/Abstract検索は大文字小文字を区別しないが、統一のため）

以下のJSON形式で出力してください。前置きや説明は不要です。JSONのみ出力してください。

{{
  "specialties": {{
    "primary": ["論文のTitle/Abstractに頻出する主要キーワード（英語・小文字）を5〜7個"],
    "secondary": ["同様に論文中に出現する関連キーワード（英語・小文字）を8〜12個"]
  }},
  "journals": {{
    "tier1": ["最高権威ジャーナル名（PubMed ISOAbbreviation表記）を3〜5個"],
    "tier2": ["専門領域の主要ジャーナル名を3〜5個"],
    "tier3": ["その他の重要ジャーナル名を5〜8個"]
  }},
  "daily_themes": {{
    "Monday": {{
      "specialties": ["月曜のサブテーマ（論文Title/Abstractに出現する具体的フレーズ、小文字）を3〜5個。同義語・表記揺れも含めて網羅的に"],
      "journals": ["関連専門誌名を2〜3個"]
    }},
    "Tuesday": {{
      "specialties": ["火曜のサブテーマ（同上）を3〜5個"],
      "journals": ["関連専門誌名を2〜3個"]
    }},
    "Wednesday": {{
      "specialties": ["水曜のサブテーマ（同上）を3〜5個"],
      "journals": ["関連専門誌名を2〜3個"]
    }},
    "Thursday": {{
      "specialties": ["木曜のサブテーマ（同上）を3〜5個"],
      "journals": ["関連専門誌名を2〜3個"]
    }},
    "Friday": {{
      "specialties": ["金曜のサブテーマ（同上）を3〜5個"],
      "journals": ["関連専門誌名を2〜3個"]
    }},
    "Saturday": {{
      "specialties": ["土曜のサブテーマ（同上）を3〜5個"],
      "journals": ["関連専門誌名を2〜3個"]
    }},
    "Sunday": {{
      "specialties": ["日曜のサブテーマ（同上）を3〜5個"],
      "journals": ["関連専門誌名を2〜3個"]
    }}
  }},
  "clinical_relevance": {{
    "high_value": [
      "この専門領域で特に重要な臨床アウトカムキーワード（英語・小文字）を8〜10個",
      "例: 循環器なら cardiovascular death / myocardial infarction、消化器なら gastrointestinal bleeding / hepatic decompensation など"
    ],
    "practical": [
      "実臨床への応用性を示す専門領域特有のキーワード（英語・小文字）を5〜8個",
      "例: standard of care / treatment algorithm / clinical decision-making など"
    ]
  }}
}}

重要なルール:
- ジャーナル名は必ずPubMedに登録されているISOAbbreviation（正式略称）で記載すること
- キーワードは全て小文字の英語で、論文のタイトル・アブストラクトに実際に頻出するフレーズにすること
- MeSHの見出し語形式（大文字始まりのカテゴリ名）は絶対に使わないこと
- 複数形カテゴリ名（"diseases", "disorders"）ではなく、具体的な疾患名を使うこと
- 曜日ごとに専門領域のサブテーマを分散させること（例: heart failure→atrial fibrillation→coronary artery disease...）
- 曜日テーマのspecialtiesは3〜5個指定すること。specialties同士はOR検索なので、同義語・表記揺れ・関連語を多く含めるほどヒット数が増える
  例: 土曜日「予防」なら→ "cardiovascular prevention", "primary prevention", "secondary prevention", "cardiovascular risk", "lifestyle intervention" のように5個
  NG例: "lipid management" のような論文に出にくい抽象的フレーズは使わないこと
- 「曜日テーマの希望」が入力されている場合は、その希望を優先して曜日テーマを設定すること。希望が空の場合はAIが自動で決める
- clinical_relevance は「例:」部分を含めず、実際のキーワードのみリストに入れること
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
        # 起動時にAPIのモデル一覧と照合し、廃止済みモデルは自動で除外される
        "model_chain": [
            "gemini-3.8-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite"
        ],
        "timeout": 180,
        "max_retries": 3,
        "retry_delay": 5
    },
    # AI一括判定（臨床研究以外を除外し、研究デザインを判定する）
    "triage": {
        "enabled": True,
        "pool_size": 40,
        # basic=基礎研究 / genetic=GWAS等の遺伝疫学 / other=計量書誌学・方法論・医療政策など
        "exclude_categories": ["basic", "genetic", "other"],
        "design_scores": {
            "rct": 10, "guideline": 10, "meta_analysis": 9, "prospective_cohort": 7,
            "registry": 6, "diagnostic": 6, "retrospective": 5, "cross_sectional": 4,
            "review": 4, "other": 3
        }
    },
    # PMCオープンアクセス全文を要約に使う
    "fulltext": {"enabled": True, "max_chars": 60000, "min_chars": 2000},
    # 要約失敗の割合がこれ以上ならメール件名に⚠を付ける
    "alert": {"summary_failure_ratio": 0.5},
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

    # 本体と同じフォールバックチェーンを使う（廃止モデルは自動除外）
    client = GeminiClient({"ai": BASE_CONFIG["ai"]}, api_key)

    daily_theme_preferences = os.environ.get("DAILY_THEME_PREFERENCES", "").strip()
    pref_text = daily_theme_preferences if daily_theme_preferences else "（希望なし。AIが自動で決める）"
    prompt = PROMPT_TEMPLATE.format(specialty=specialty, daily_theme_preferences=pref_text)
    text = client.generate(prompt, temperature=0.3, max_output_tokens=8192)
    if not text:
        raise ValueError("全モデルで生成に失敗しました。時間をおいて再実行してください。")
    text = text.strip()

    # JSONオブジェクトを抽出（前置き文やコードブロックを無視）
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        raise ValueError(f"AIの出力にJSONオブジェクトが見つかりませんでした:\n{text[:500]}")
    generated = json.loads(match.group())
    return generated


def validate_generated(generated: dict, specialty: str):
    """生成された設定の必須フィールドを検証する"""
    required_keys = ["specialties", "journals", "daily_themes"]
    missing = [k for k in required_keys if k not in generated]
    if missing:
        raise ValueError(f"AIの出力に必須フィールドが不足しています: {missing}")
    primary = generated.get("specialties", {}).get("primary", [])
    if not primary:
        raise ValueError("specialties.primary が空です。AIの出力を確認してください。")
    tier1 = generated.get("journals", {}).get("tier1", [])
    if not tier1:
        raise ValueError("journals.tier1 が空です。AIの出力を確認してください。")
    print(f"バリデーション OK — primary: {len(primary)}語、tier1: {len(tier1)}誌")


def build_config(specialty: str, generated: dict, include_basic_science: bool) -> dict:
    """ベース設定と生成設定をマージしてconfig全体を組み立てる"""
    import copy
    config = copy.deepcopy(BASE_CONFIG)
    config["specialty_name"] = specialty
    config["specialties"] = generated.get("specialties", {})
    config["journals"] = generated.get("journals", {})
    config["daily_themes"] = generated.get("daily_themes", {})

    # 臨床関連性キーワードを専門領域に合わせて上書き
    gen_cr = generated.get("clinical_relevance", {})
    if gen_cr.get("high_value"):
        config["clinical_relevance"]["high_value"] = gen_cr["high_value"]
    if gen_cr.get("practical"):
        config["clinical_relevance"]["practical"] = gen_cr["practical"]
    # japan_relevantは汎用なのでそのまま維持

    # 基礎研究を含める場合はキーワード除外を空にし、AI判定でも基礎研究・遺伝疫学を残す
    if include_basic_science:
        config["basic_science_exclude"] = []
        config["triage"]["exclude_categories"] = ["other"]

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

    include_basic_science = os.environ.get("INCLUDE_BASIC_SCIENCE", "").startswith("はい")

    try:
        generated = generate_specialty_config(specialty, api_key)
        validate_generated(generated, specialty)
        config = build_config(specialty, generated, include_basic_science)

        # 既存 config.yaml をバックアップ
        import shutil
        from pathlib import Path
        if Path("config.yaml").exists():
            shutil.copy("config.yaml", "config.yaml.bak")
            print("既存の config.yaml を config.yaml.bak にバックアップしました")

        with open("config.yaml", "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)

        print("=" * 50)
        print(f"config.yaml を生成しました（専門領域: {specialty}）")
        print("=" * 50)
        print(f"主要キーワード: {', '.join(config['specialties'].get('primary', []))}")
        print(f"Tier1ジャーナル: {', '.join(config['journals'].get('tier1', []))}")
        print(f"基礎研究を含める: {'はい' if include_basic_science else 'いいえ'}")
        print("=" * 50)
        print("次のステップ: GitHub Actionsの「Daily Paper Summary」が")
        print("毎朝自動で論文を収集・要約してメールに届けます。")

    except (json.JSONDecodeError, ValueError) as e:
        print(f"エラー: AIの出力を処理できませんでした: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"エラー: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
