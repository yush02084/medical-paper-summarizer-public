"""
論文フィルタリング・優先度付けモジュール

論文タイプ、ジャーナルランク、専門領域マッチングに基づき
論文に優先度スコアを付与し、上位N件を選出する。
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from pubmed_searcher import Paper
from timeutil import now_jst

logger = logging.getLogger(__name__)


class PaperFilter:
    """論文フィルタリング・優先度付けクラス"""

    def __init__(self, config: dict, history_file: Optional[str] = None):
        """
        初期化

        Args:
            config: config.yamlから読み込んだ設定辞書
            history_file: 履歴ファイルのパス
        """
        self.config = config
        self.history_file = history_file or config.get("history", {}).get(
            "file", "history.json"
        )
        self.history = self._load_history()

    def _load_history(self) -> dict:
        """履歴ファイルを読み込む"""
        path = Path(self.history_file)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # reported_pmids キーがない場合（空の{}など）は補完する
                if "reported_pmids" not in data:
                    data["reported_pmids"] = {}
                return data
            except Exception as e:
                logger.warning(f"履歴ファイルの読み込みに失敗: {e}")
        return {"reported_pmids": {}}

    def save_history(self, papers: list[Paper]):
        """
        報告済みの論文PMIDを履歴に保存する

        Args:
            papers: 報告対象の論文リスト
        """
        now = now_jst().isoformat()
        for paper in papers:
            self.history["reported_pmids"][paper.pmid] = {
                "title": paper.title,
                "reported_at": now
            }

        # 古い履歴を削除
        retention_days = self.config.get("history", {}).get(
            "retention_days", 180
        )
        cutoff = now_jst() - timedelta(days=retention_days)

        cleaned = {}
        for pmid, info in self.history["reported_pmids"].items():
            try:
                reported_at = datetime.fromisoformat(info["reported_at"])
                if reported_at > cutoff:
                    cleaned[pmid] = info
            except (ValueError, KeyError):
                cleaned[pmid] = info

        self.history["reported_pmids"] = cleaned

        # 保存
        path = Path(self.history_file)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.history, f, ensure_ascii=False, indent=2)

        logger.info(f"履歴を保存しました（{len(cleaned)}件）")

    def filter_and_rank(self, papers: list[Paper], triage=None) -> list[Paper]:
        """
        論文をフィルタリングし、優先度順にランク付けする

        Args:
            papers: PubMed検索結果の論文リスト
            triage: PaperTriage（Noneまたは無効ならルール点数のみ）

        Returns:
            優先度順にソートされた上位N件の論文リスト
        """
        top_n = self.config.get("search", {}).get("top_n", 10)
        self.triage_ok = None  # None=未実施, True=成功, False=失敗

        # 1. 重複排除（過去に報告済みの論文を除外）
        new_papers = self._remove_duplicates(papers)
        logger.info(f"重複排除: {len(papers)}件 → {len(new_papers)}件")

        # 2. 除外対象の論文タイプを除外
        filtered = self._exclude_types(new_papers)
        logger.info(f"タイプ除外: {len(new_papers)}件 → {len(filtered)}件")

        # 3. アブストラクトなしの論文を除外
        filtered = [p for p in filtered if p.abstract.strip()]
        logger.info(f"アブストラクト有り: {len(filtered)}件")

        # 3.5. 基礎研究を除外（キーワードによる一次除外）
        filtered = self._exclude_basic_science(filtered)
        logger.info(f"基礎研究除外後: {len(filtered)}件")

        # 4. ルール点数で一次ランク付け
        for paper in filtered:
            paper.priority_score = self._calculate_score(paper)
        filtered.sort(key=lambda p: p.priority_score, reverse=True)

        # 5. 上位候補をAIで一括判定 → 基礎研究の除外とデザイン点の補正
        if triage is not None and triage.enabled and filtered:
            pool_size = self.config.get("triage", {}).get("pool_size", 40)
            pool = filtered[:pool_size]
            self.triage_ok = triage.triage(pool)

            exclude_cats = set(
                self.config.get("triage", {}).get("exclude_categories", ["basic", "genetic", "other"])
            )
            before = len(pool)
            pool = [p for p in pool if p.triage.get("category") not in exclude_cats]
            logger.info(
                f"AI判定で除外（{', '.join(sorted(exclude_cats))}）: {before}件 → {len(pool)}件"
            )

            for paper in pool:
                paper.priority_score = self._calculate_score(paper)
            pool.sort(key=lambda p: p.priority_score, reverse=True)
            filtered = pool

        # 6. 上位N件を選出
        top_papers = filtered[:top_n]
        for rank, paper in enumerate(top_papers, 1):
            paper.priority_rank = rank

        logger.info(f"上位 {len(top_papers)} 件を選出しました")
        return top_papers

    def _remove_duplicates(self, papers: list[Paper]) -> list[Paper]:
        """過去に報告済みの論文を除外する"""
        reported = set(self.history.get("reported_pmids", {}).keys())
        return [p for p in papers if p.pmid not in reported]

    def _exclude_types(self, papers: list[Paper]) -> list[Paper]:
        """除外対象の論文タイプを除外する"""
        exclude = set(self.config.get("exclude_types", []))
        result = []
        for paper in papers:
            # 論文タイプが全て除外対象の場合のみ除外
            paper_types = set(paper.pub_types)
            if paper_types and paper_types.issubset(
                exclude | {"Journal Article"}
            ):
                # 「Journal Article」のみ + 除外タイプのみの場合
                non_journal = paper_types - {"Journal Article"}
                if non_journal and non_journal.issubset(exclude):
                    continue
            result.append(paper)
        return result

    def _calculate_score(self, paper: Paper) -> float:
        """
        論文の優先度スコアを計算する

        スコア構成:
        - 論文タイプスコア: 0-10
        - ジャーナルスコア: 0-10
        - 専門領域マッチスコア: 0-15
        - 臨床関連性スコア: 0-10
        - 最新性ボーナス: 0-3

        Args:
            paper: 評価対象の論文

        Returns:
            優先度スコア（0-48）
        """
        score = 0.0

        # 論文タイプスコア
        score += self._score_study_type(paper)

        # ジャーナルスコア
        score += self._score_journal(paper)

        # 専門領域マッチスコア
        score += self._score_specialty_match(paper)

        # 臨床関連性スコア
        score += self._score_clinical_relevance(paper)

        # ボーナス：最新性
        score += self._score_recency(paper)

        return score

    def _score_study_type(self, paper: Paper) -> float:
        """
        論文タイプに基づくスコア

        PubMedのタグとAI判定のデザインの高い方を採用する
        （新着論文はPubMedのタグが未付与で「Journal Article」だけのことが多いため）
        """
        type_scores = self.config.get("study_type_scores", {})
        max_score = 0.0
        for pt in paper.pub_types:
            s = type_scores.get(pt, 3)  # デフォルト3点
            max_score = max(max_score, s)

        design = (paper.triage or {}).get("design")
        if design:
            design_scores = self.config.get("triage", {}).get("design_scores", {})
            max_score = max(max_score, design_scores.get(design, 3))
        return max_score

    def _score_journal(self, paper: Paper) -> float:
        """ジャーナルランクに基づくスコア"""
        journals = self.config.get("journals", {})

        # Tier 1: 10点
        if paper.journal in journals.get("tier1", []):
            return 10.0
        # Tier 2: 8点
        if paper.journal in journals.get("tier2", []):
            return 8.0
        # Tier 3: 6点
        if paper.journal in journals.get("tier3", []):
            return 6.0
        # それ以外: 3点
        return 3.0

    def _score_specialty_match(self, paper: Paper) -> float:
        """専門領域とのマッチ度スコア"""
        specialties = self.config.get("specialties", {})
        primary = [s.lower() for s in specialties.get("primary", [])]
        secondary = [s.lower() for s in specialties.get("secondary", [])]

        # タイトル、アブストラクト、MeSH、キーワードを統合して検索対象にする
        text = " ".join([
            paper.title.lower(),
            paper.abstract.lower(),
            " ".join([m.lower() for m in paper.mesh_terms]),
            " ".join([k.lower() for k in paper.keywords])
        ])

        score = 0.0

        # 最優先領域マッチ（各5点、最大15点）
        primary_matches = sum(1 for term in primary if term in text)
        score += min(primary_matches * 5.0, 15.0)

        # 次点領域マッチ（各2点、最大6点）
        secondary_matches = sum(1 for term in secondary if term in text)
        score += min(secondary_matches * 2.0, 6.0)

        # 最大15点に制限
        return min(score, 15.0)

    def _exclude_basic_science(self, papers: list[Paper]) -> list[Paper]:
        """基礎研究（動物・細胞実験のみ）を除外する"""
        exclude_phrases = self.config.get("basic_science_exclude", [])
        clinical_words = ["patient", "clinical", "human", "trial", "cohort", "registry"]
        result = []
        for paper in papers:
            text = (paper.title + " " + paper.abstract).lower()
            has_basic = any(ph in text for ph in exclude_phrases)
            has_clinical = any(cw in text for cw in clinical_words)
            if has_basic and not has_clinical:
                logger.debug(f"基礎研究除外: {paper.title[:60]}")
                continue
            result.append(paper)
        return result

    def _score_clinical_relevance(self, paper: Paper) -> float:
        """臨床関連性スコア（最大10点）"""
        cr = self.config.get("clinical_relevance", {})
        text = (paper.title + " " + paper.abstract).lower()

        score = 0.0
        if any(kw.lower() in text for kw in cr.get("high_value", [])):
            score += 5.0
        if any(kw.lower() in text for kw in cr.get("practical", [])):
            score += 3.0
        japan_kw = any(kw.lower() in text for kw in cr.get("japan_relevant", []))
        if japan_kw or (paper.triage or {}).get("japan_asian"):
            score += 2.0

        return min(score, 10.0)

    def _score_recency(self, paper: Paper) -> float:
        """最新性に基づくボーナススコア"""
        if not paper.pub_date:
            return 0.0

        try:
            pub = datetime.strptime(paper.pub_date, "%Y/%m/%d")
            days_ago = (now_jst() - pub).days

            if days_ago <= 3:
                return 3.0
            elif days_ago <= 7:
                return 2.0
            elif days_ago <= 14:
                return 1.0
        except ValueError:
            pass

        return 0.0
