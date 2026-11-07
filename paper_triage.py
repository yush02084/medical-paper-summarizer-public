"""
AI一括判定モジュール

ルール点数で絞った候補（既定40本）のタイトル・抄録を1回のAI呼び出しで
まとめて判定し、次の3点をJSONで返させる。

- category   : clinical（ヒト対象の臨床研究） / basic（動物・細胞・分子機序が主） /
               genetic（GWAS・TWAS・オミクス・メンデルランダム化などの遺伝疫学） / other
- design     : rct / meta_analysis / guideline / prospective_cohort / retrospective /
               registry / cross_sectional / diagnostic / review / other
- japan_asian: 日本人・アジア人のデータを含むか

PubMedの論文タイプのタグは掲載直後には付かないことが多いため、
新着RCTを正しく加点する目的もある。
"""

import json
import logging

from llm_client import GeminiClient
from pubmed_searcher import Paper

logger = logging.getLogger(__name__)

DESIGN_LABELS = {
    "rct": "RCT",
    "meta_analysis": "メタ解析/SR",
    "guideline": "ガイドライン",
    "prospective_cohort": "前向きコホート",
    "retrospective": "後ろ向き研究",
    "registry": "レジストリ",
    "cross_sectional": "横断研究",
    "diagnostic": "診断精度研究",
    "review": "ナラティブレビュー",
    "other": "その他",
}

# 選出理由で「強固」と表示するデザイン
STRONG_DESIGNS = {"rct", "meta_analysis", "guideline"}

VALID_DESIGNS = set(DESIGN_LABELS.keys())
VALID_CATEGORIES = {"clinical", "basic", "genetic", "other"}

ABSTRACT_CHARS = 1500  # 1本あたり抄録の文字数上限（トークン節約）


def _build_prompt(papers: list[Paper], specialty_name: str = "臨床") -> str:
    items = []
    for p in papers:
        items.append({
            "pmid": p.pmid,
            "title": p.title,
            "journal": p.journal,
            "pubmed_types": [t for t in p.pub_types if t != "Journal Article"],
            "abstract": p.abstract[:ABSTRACT_CHARS],
        })
    papers_json = json.dumps(items, ensure_ascii=False)

    return f"""あなたは{specialty_name}の専門医で、臨床疫学に精通しています。
以下の論文候補それぞれについて、タイトルと抄録だけを根拠に判定し、JSON配列のみを出力してください。

判定項目:
- "pmid": 入力のpmidをそのまま
- "category": 次のいずれか
  - "clinical": ヒトの患者・住民を対象にし、診断・治療・予後・リスク評価についての結論を出す研究（臨床試験、観察研究、メタ解析、ガイドライン、臨床レビュー）
  - "basic": 動物・細胞・組織・分子機序の実験が主の研究（ヒト検体を一部使っていても、主眼が機序解明なら basic）
  - "genetic": 遺伝子座・遺伝子発現・オミクスの同定が主眼の研究（GWAS、TWAS、eQTL、ポリジェニックスコア、メンデルランダム化など）。
    ヒトの大規模集団を使っていても、主な結論が「遺伝子・経路の同定」なら genetic
  - "other": 上記以外（計量書誌学・文献動向分析、方法論、医療政策、医学教育、症例報告の集積など）
- "design": 次のいずれか
  "rct", "meta_analysis", "guideline", "prospective_cohort", "retrospective", "registry",
  "cross_sectional", "diagnostic", "review", "other"
  （RCTの二次解析・サブ解析は "rct"。システマティックレビューは "meta_analysis"）
- "japan_asian": 日本人またはアジア人の対象者データを含むことが抄録から読み取れれば true、それ以外は false
- "reason": 臨床医が読む価値を20字以内の日本語で（例: "初めて死亡を減らしたRCT"）

注意:
- 抄録に書かれていないことは推測しないでください
- 全候補について1件ずつ出力し、配列の長さは入力と同じにしてください

候補:
{papers_json}
"""


class PaperTriage:
    """候補論文をAIでまとめて判定するクラス"""

    def __init__(self, config: dict, client: GeminiClient):
        self.client = client
        self.config = config.get("triage", {})
        self.specialty_name = config.get("specialty_name", "臨床")

    @property
    def enabled(self) -> bool:
        return self.config.get("enabled", True)

    def triage(self, papers: list[Paper]) -> bool:
        """
        各Paperの .triage に判定結果を書き込む

        Returns:
            判定に成功したか（失敗時はルール点数だけで続行する）
        """
        if not papers:
            return True

        result = self.client.generate_json(
            _build_prompt(papers, self.specialty_name), temperature=0.0, max_output_tokens=16384
        )
        if not isinstance(result, list):
            logger.warning("AI一括判定に失敗しました。ルール点数のみで選定します")
            return False

        by_pmid = {}
        for item in result:
            if not isinstance(item, dict):
                continue
            pmid = str(item.get("pmid", "")).strip()
            design = str(item.get("design", "")).strip().lower()
            category = str(item.get("category", "")).strip().lower()
            by_pmid[pmid] = {
                "design": design if design in VALID_DESIGNS else "other",
                "category": category if category in VALID_CATEGORIES else "other",
                "japan_asian": bool(item.get("japan_asian", False)),
                "reason": str(item.get("reason", ""))[:40],
            }

        matched = 0
        for p in papers:
            if p.pmid in by_pmid:
                p.triage = by_pmid[p.pmid]
                matched += 1

        logger.info(f"AI一括判定: {matched}/{len(papers)}件の判定を取得")
        # 半分以上取れていなければ失敗扱い（部分的な結果は使う）
        return matched >= len(papers) / 2
