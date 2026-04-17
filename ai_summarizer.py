"""
AI要約モジュール

Google Gemini APIを使用して、各論文の批判的要約を
日本語で生成する。モデルフォールバックチェーン対応。
"""

import time
import logging
from typing import Optional

import google.generativeai as genai

from pubmed_searcher import Paper

logger = logging.getLogger(__name__)


class AISummarizer:
    """AI論文要約クラス"""

    def __init__(self, config: dict, api_key: str):
        """
        初期化

        Args:
            config: config.yamlから読み込んだ設定辞書
            api_key: Gemini APIキー
        """
        self.config = config
        self.ai_config = config.get("ai", {})
        self.model_chain = self.ai_config.get("model_chain", [
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite-preview-09-2025",
            "gemini-2.0-flash"
        ])
        self.max_retries = self.ai_config.get("max_retries", 3)
        self.retry_delay = self.ai_config.get("retry_delay", 5)
        self.timeout = self.ai_config.get("timeout", 120)

        genai.configure(api_key=api_key)

    def _create_model(self, model_name: str):
        """指定モデルのインスタンスを作成する"""
        return genai.GenerativeModel(
            model_name=model_name,
            generation_config=genai.GenerationConfig(
                temperature=0.3,  # 正確性重視で低めの温度
                max_output_tokens=4096,
            )
        )

    def _call_with_fallback(self, prompt: str) -> Optional[str]:
        """
        フォールバックチェーンでAPIを呼び出す

        上位モデルからエラー/レート制限時に下位モデルへ順にフォールバック

        Args:
            prompt: 送信するプロンプト

        Returns:
            生成されたテキスト（全モデル失敗時はNone）
        """
        for model_name in self.model_chain:
            for attempt in range(self.max_retries):
                try:
                    logger.info(
                        f"モデル {model_name} を使用中"
                        f"（試行 {attempt + 1}/{self.max_retries}）"
                    )
                    model = self._create_model(model_name)
                    # タイムアウトを設定して呼び出し
                    response = model.generate_content(
                        prompt, 
                        request_options={"timeout": self.timeout}
                    )

                    if response.text:
                        logger.info(f"[OK] {model_name} で生成成功")
                        return response.text

                except Exception as e:
                    error_msg = str(e).lower()
                    logger.warning(
                        f"モデル {model_name} でエラー "
                        f"（試行 {attempt + 1}）: {e}"
                    )

                    # レート制限やモデル未対応の場合は次のモデルへ
                    if any(kw in error_msg for kw in [
                        "rate limit", "quota", "429",
                        "not found", "404", "not supported",
                        "504", "deadline", "timeout"
                    ]):
                        logger.info(f"→ 次のモデルに早めにフォールバックします")
                        break

                    # その他のエラーはリトライ
                    if attempt < self.max_retries - 1:
                        wait = self.retry_delay * (attempt + 1)
                        logger.info(f"  {wait}秒後にリトライ...")
                        time.sleep(wait)

        logger.error("全モデルで生成に失敗しました")
        return None

    def summarize_papers(
        self, papers: list[Paper], detailed_top_n: int = 10
    ) -> list[Paper]:
        """
        論文リストの要約を生成する

        Args:
            papers: 優先度順にソートされた論文リスト
            detailed_top_n: 詳細要約を行う上位論文数

        Returns:
            要約が付与された論文リスト
        """
        if detailed_top_n is None:
            detailed_top_n = self.config.get("search", {}).get(
                "detailed_top_n", 3
            )

        total = len(papers)
        logger.info(
            f"{total}件の論文を要約します"
            f"（詳細: {min(detailed_top_n, total)}件）"
        )

        for i, paper in enumerate(papers):
            is_detailed = (i < detailed_top_n)
            mode_str = "詳細" if is_detailed else "簡潔"

            logger.info(
                f"[{i+1}/{total}] {mode_str}要約中: "
                f"{paper.title[:50]}..."
            )

            prompt = self._build_prompt(paper, is_detailed)
            result = self._call_with_fallback(prompt)

            if result:
                paper.summary = {
                    "mode": "detailed" if is_detailed else "brief",
                    "content": result
                }
            else:
                paper.summary = {
                    "mode": "detailed" if is_detailed else "brief",
                    "content": "⚠ 要約の生成に失敗しました。"
                }

            # API呼び出し間隔を空ける
            if i < total - 1:
                time.sleep(2)

        return papers

    def _build_paper_info(self, paper: Paper) -> str:
        """論文基本情報ブロックを構築する"""
        if len(paper.authors) > 5:
            author_str = ", ".join(paper.authors[:5]) + " et al."
        else:
            author_str = ", ".join(paper.authors)
        pub_type_str = ", ".join(paper.pub_types) if paper.pub_types else "不明"
        return f"""【論文情報】
タイトル: {paper.title}
著者: {author_str}
ジャーナル: {paper.journal}
出版日: {paper.pub_date}
論文タイプ: {pub_type_str}
DOI: {paper.doi if paper.doi else "N/A"}
MeSH用語: {", ".join(paper.mesh_terms[:10]) if paper.mesh_terms else "N/A"}

【アブストラクト】
{paper.abstract}""".strip()

    def _detect_paper_type(self, paper: Paper) -> str:
        """
        論文タイプを判定する

        Returns:
            "guideline" / "synthesis" / "review" / "research"
        """
        types = set(paper.pub_types)
        if types & {"Practice Guideline", "Guideline"}:
            return "guideline"
        if types & {"Systematic Review", "Meta-Analysis"}:
            return "synthesis"
        if types & {"Review"}:
            return "review"
        # pub_typesが空またはキーワードでフォールバック
        text = (paper.title + " " + paper.abstract).lower()
        if "guideline" in text or "recommendation" in text:
            return "guideline"
        if "systematic review" in text or "meta-analysis" in text:
            return "synthesis"
        if "review" in text and "randomized" not in text:
            return "review"
        return "research"

    def _build_prompt(self, paper: Paper, detailed: bool) -> str:
        """論文タイプに応じてプロンプトを振り分ける"""
        paper_type = self._detect_paper_type(paper)
        logger.info(f"論文タイプ判定: {paper_type} （{paper.title[:40]}...）")
        if paper_type == "guideline":
            return self._build_guideline_prompt(paper)
        elif paper_type == "synthesis":
            return self._build_synthesis_prompt(paper)
        elif paper_type == "review":
            return self._build_review_prompt(paper)
        else:
            paper_info = self._build_paper_info(paper)
            if detailed:
                return self._build_detailed_prompt(paper_info)
            else:
                return self._build_brief_prompt(paper_info)

    def _build_detailed_prompt(self, paper_info: str) -> str:
        """詳細要約プロンプト"""
        return f"""あなたは循環器内科の専門医であり、優秀な医師アシスタントです。
以下の論文について、忙しい循環器内科医が短時間で本質をつかめる形式で、日本語で詳細な批判的要約を作成してください。

{paper_info}

以下の形式で出力してください。各セクションは明確に分けてください。
※「承知いたしました」「要約します」等といった前置きや挨拶は一切含めず、いきなり「## サマリーインデックス情報」から出力してください。

## サマリーインデックス情報
冒頭にインデックスを作成するため、以下の3点を極めて簡潔に出力してください。
- **重要度**: [重要度の基準]に従い、★を5つ並べて表記（例：★★★★★）
- **結論**: [40文字以内]で、この論文が何を示したか
- **実用**: [50文字以内]で、明日の臨床にどう活きるか。疾患名、薬剤名、具体的な数値などの重要キーワードは必ず**太字**にすること

## まず一言で
この論文が何を示したのかを1〜2文で日本語要約してください。

## 研究の概要
- **研究背景**: なぜこの研究が行われたか
- **研究デザイン**: どのような研究手法か（RCT、コホート等）
- **対象患者**: どのような患者が対象か（人数・特徴）
- **介入/比較**: 何を比較したか
- **主要評価項目**: 何を評価したか
- **主な結果**: 主要な数値結果（ハザード比、95%CI等を含む）

## 臨床的に重要なポイント
- 専門医の視点で何が重要か
- どの患者で役立つか
- 実臨床を変える可能性があるか
- 現場でどう使うか

## 限界
- バイアスの可能性
- 一般化可能性の限界
- サンプルサイズの問題
- 観察研究であることの限界（該当する場合）
- 対象患者の偏り
- 実装上の課題

## 日本の臨床への実践メモ
- 明日からの診療で意識すべきこと
- カンファレンスで紹介するなら何を強調するか
- 患者説明やチーム共有にどう活かせるか
- 日本の医療環境での適用可能性

重要度の基準：
★★★★★：明日の診療方針に直結する、必ず読むべきパラダイムシフト
★★★★☆：実用性が高く、知っておくべき重要な知見
★★★☆☆：特定の条件下で役立つ、または興味深い知見
★★☆☆☆：参考程度
★☆☆☆☆：現在の業務への直接的な影響は少ない

重要な注意事項:
- 不確かなことは断定しないでください
- 抄録の内容をなぞるだけでなく、批判的吟味を加えてください
- 根拠が弱い場合は弱いと明確に述べてください
- 誇張表現は避けてください
- 統計の細かい説明よりも臨床的解釈を優先してください
- ただし結果の信頼性に関わる統計上の注意点は簡潔に述べてください
"""

    def _build_brief_prompt(self, paper_info: str) -> str:
        """簡潔要約プロンプト"""
        return f"""あなたは循環器内科の専門医であり、優秀な医師アシスタントです。
以下の論文について、忙しい循環器内科医が短時間で把握できるよう、日本語で簡潔な批判的要約を作成してください。

{paper_info}

以下の形式で出力してください。
※「承知いたしました」「要約します」等といった前置きや挨拶は一切含めず、いきなり「## サマリーインデックス情報」から出力してください。

## サマリーインデックス情報
冒頭にインデックスを作成するため、以下の3点を極めて簡潔に出力してください。
- **重要度**: [重要度の基準]に従い、★を5つ並べて表記（例：★★★★★）
- **結論**: [40文字以内]で、この論文が何を示したか
- **実用**: [50文字以内]で、明日の臨床にどう活きるか。疾患名、薬剤名、具体的な数値などの重要キーワードは必ず**太字**にすること

## まず一言で
この論文が何を示したのかを1〜2文で日本語要約。

## 要点
- 研究デザインと対象（1-2行）
- 主な結果（数値を含む、2-3行）
- 臨床的意義（1-2行）
- 主な限界（1-2行）
- 明日からの診療への示唆（1-2行）

重要度の基準：
★★★★★：明日の診療方針に直結する、必ず読むべきパラダイムシフト
★★★★☆：実用性が高く、知っておくべき重要な知見
★★★☆☆：特定の条件下で役立つ、または興味深い知見
★★☆☆☆：参考程度
★☆☆☆☆：現在の業務への直接的な影響は少ない

重要な注意事項:
- 不確かなことは断定しない
- 根拠が弱い場合は弱いと明記
- 誇張表現は避ける
"""


    def _build_synthesis_prompt(self, paper: Paper) -> str:
        """システマティックレビュー・メタアナリシス向けプロンプト"""
        paper_info = self._build_paper_info(paper)
        return f"""あなたは循環器内科の専門医であり、優秀な医師アシスタントです。
以下のシステマティックレビュー/メタアナリシスについて、忙しい循環器内科医が短時間でエビデンスの質と臨床的意義を把握できるよう、日本語で詳細な批判的要約を作成してください。

{paper_info}

以下の形式で出力してください。
※「承知いたしました」等の前置きは一切含めず、いきなり「## サマリーインデックス情報」から出力してください。

## サマリーインデックス情報
- **重要度**: ★5段階で表記（例：★★★★☆）
- **結論**: [40文字以内]で、このレビューが示したこと
- **実用**: [50文字以内]で、明日の臨床にどう活きるか。重要キーワードは**太字**にすること

## まず一言で
このレビュー/メタアナリシスが示したことを1〜2文で要約してください。

## レビューの概要
- **リサーチクエスチョン**: 何を明らかにしようとしたか（PICO形式が望ましい）
- **採用文献**: 何本の研究を統合したか（対象期間・研究デザイン）
- **採用基準**: どのような研究が含まれたか

## 主な結果
- プールされた統計（ハザード比・オッズ比・RR・95%CI・NNT/NNHなど数値を明記）
- サブグループ解析で重要な結果があれば記載

## エビデンスの質
- **GRADE評価**: あれば記載（なければ"記載なし"）
- **異質性**: I²値・τ²など。臨床的に許容範囲か
- **出版バイアス**: ファネルプロット等の評価があれば

## 限界
- 含まれる研究自体の質の問題
- 異質性・一般化可能性の限界
- 日本人データの有無

## 日本の臨床への実践メモ
- 明日からの診療で意識すべきこと
- 現行ガイドラインとの整合性
- 日本の医療環境での適用可能性

重要度の基準：
★★★★★：明日の診療方針に直結する、必ず読むべきパラダイムシフト
★★★★☆：実用性が高く、知っておくべき重要な知見
★★★☆☆：特定の条件下で役立つ、または興味深い知見
★★☆☆☆：参考程度
★☆☆☆☆：現在の業務への直接的な影響は少ない

重要な注意事項:
- 統計値は正確に記載し、信頼区間を省略しないでください
- 異質性が高い場合は必ず指摘してください
- 根拠が弱い場合は弱いと明確に述べてください
"""

    def _build_review_prompt(self, paper: Paper) -> str:
        """ナラティブレビュー向けプロンプト"""
        paper_info = self._build_paper_info(paper)
        return f"""あなたは循環器内科の専門医であり、優秀な医師アシスタントです。
以下のレビュー論文について、忙しい循環器内科医が短時間で全体像を把握できるよう、日本語で要約を作成してください。

{paper_info}

以下の形式で出力してください。
※「承知いたしました」等の前置きは一切含めず、いきなり「## サマリーインデックス情報」から出力してください。

## サマリーインデックス情報
- **重要度**: ★5段階で表記（例：★★★★☆）
- **結論**: [40文字以内]で、このレビューが示したこと
- **実用**: [50文字以内]で、明日の臨床にどう活きるか。重要キーワードは**太字**にすること

## まず一言で
このレビューが何を扱い、何を伝えようとしているかを1〜2文で要約してください。

## レビューの概要
- **対象テーマ・範囲**: 何について・どこまでカバーしているか
- **執筆の目的**: なぜこのレビューが書かれたか

## 主なエビデンスのまとめ
（3〜5点の箇条書きで、臨床的に重要な知見を記載）

## 現時点での知見のギャップ・今後の課題
- まだ明らかになっていないこと
- 今後必要な研究

## 日本の臨床への実践メモ
- 現場で使える示唆
- 日本の医療環境での適用可能性

重要度の基準：
★★★★★：明日の診療方針に直結する、必ず読むべきパラダイムシフト
★★★★☆：実用性が高く、知っておくべき重要な知見
★★★☆☆：特定の条件下で役立つ、または興味深い知見
★★☆☆☆：参考程度
★☆☆☆☆：現在の業務への直接的な影響は少ない

重要な注意事項:
- ナラティブレビューは著者の選択バイアスが入りやすいことを念頭に置いてください
- 誇張表現は避け、エビデンスの強さに応じた表現を使ってください
"""

    def _build_guideline_prompt(self, paper: Paper) -> str:
        """ガイドライン向けプロンプト"""
        paper_info = self._build_paper_info(paper)
        return f"""あなたは循環器内科の専門医であり、優秀な医師アシスタントです。
以下のガイドラインについて、忙しい循環器内科医が短時間で要点を把握できるよう、日本語で要約を作成してください。

{paper_info}

以下の形式で出力してください。
※「承知いたしました」等の前置きは一切含めず、いきなり「## サマリーインデックス情報」から出力してください。

## サマリーインデックス情報
- **重要度**: ★5段階で表記（例：★★★★☆）
- **結論**: [40文字以内]で、このガイドラインの最重要メッセージ
- **実用**: [50文字以内]で、明日の臨床にどう活きるか。重要キーワードは**太字**にすること

## まず一言で
このガイドラインの対象と最重要メッセージを1〜2文で要約してください。

## ガイドラインの概要
- **対象疾患・領域**: 何の疾患・状況を対象としているか
- **発行機関**: 誰が発行したか（ESC/AHA/ACC/JCS等）
- **対象読者**: 誰に向けたガイドラインか

## 主要推奨事項（3〜5点）
（各推奨事項について、推奨クラスと根拠レベルを必ず記載）
- 例: 【Class I / Level A】〇〇患者には△△を推奨する

## 前回版からの主な変更点
（前回版との比較が明記されていれば記載。なければ「本文中に明記なし」）

## 日本の実臨床での注意点
- 国内ガイドライン（JCS等）との差異があれば記載
- 日本未承認薬・保険適用外の推奨があれば明記
- 日本の医療環境での実装上の課題

重要度の基準：
★★★★★：明日の診療方針に直結する、必ず読むべきパラダイムシフト
★★★★☆：実用性が高く、知っておくべき重要な知見
★★★☆☆：特定の条件下で役立つ、または興味深い知見
★★☆☆☆：参考程度
★☆☆☆☆：現在の業務への直接的な影響は少ない

重要な注意事項:
- アブストラクトに全推奨事項が含まれないことが多いため、記載のない推奨は「アブストラクトに記載なし」と明示してください
- 推奨クラスと根拠レベルは正確に転記してください
"""

    def generate_selection_reason(self, paper: Paper) -> str:
        """
        論文選出理由を簡潔に生成する

        Args:
            paper: 対象論文

        Returns:
            選出理由テキスト
        """
        reasons = []

        # ジャーナルランク
        journals = self.config.get("journals", {})
        if paper.journal in journals.get("tier1", []):
            reasons.append(f"トップジャーナル（{paper.journal}）掲載")
        elif paper.journal in journals.get("tier2", []):
            reasons.append(f"主要循環器専門誌（{paper.journal}）掲載")

        # 論文タイプ
        high_types = [
            "Randomized Controlled Trial", "Meta-Analysis",
            "Systematic Review", "Practice Guideline"
        ]
        matched_types = [t for t in paper.pub_types if t in high_types]
        if matched_types:
            reasons.append(f"研究デザインが強固（{', '.join(matched_types)}）")

        # 専門領域マッチ
        primary = self.config.get("specialties", {}).get("primary", [])
        title_lower = paper.title.lower()
        matched_areas = [
            s for s in primary if s.lower() in title_lower
        ]
        if matched_areas:
            reasons.append(
                f"最優先領域に関連（{', '.join(matched_areas)}）"
            )

        if reasons:
            return "選出理由: " + "; ".join(reasons)
        return "選出理由: 臨床的重要性が高いと判断"
