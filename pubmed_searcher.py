"""
PubMed検索モジュール

PubMed E-utilities APIを使用して、指定された条件に基づき
医学論文を検索・取得する。
"""

import time
import logging
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field

from Bio import Entrez

from timeutil import now_jst

logger = logging.getLogger(__name__)


@dataclass
class Paper:
    """論文データクラス"""
    pmid: str = ""
    title: str = ""
    authors: list = field(default_factory=list)
    journal: str = ""
    pub_date: str = ""
    abstract: str = ""
    pub_types: list = field(default_factory=list)
    doi: str = ""
    mesh_terms: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    # フィルタリング後に付与
    priority_score: float = 0.0
    priority_rank: int = 0
    # PMC（オープンアクセス全文）
    pmcid: str = ""
    full_text: str = ""
    # AI一括判定の結果（design / category / japan_asian / reason など）
    triage: dict = field(default_factory=dict)
    # AI要約結果
    summary: dict = field(default_factory=dict)


class PubMedSearcher:
    """PubMed検索クラス"""

    def __init__(self, config: dict, email: str, api_key: Optional[str] = None):
        """
        初期化

        Args:
            config: config.yamlから読み込んだ設定辞書
            email: NCBI E-utilities用メールアドレス
            api_key: NCBI APIキー（任意）
        """
        self.config = config
        Entrez.email = email
        # NCBI APIキーのバリデーション（無効なキーはセットしない）
        self._has_api_key = False
        if api_key and len(api_key) > 10 and api_key != "none":
            Entrez.api_key = api_key
            self._has_api_key = True
            self.rate_limit = 0.1  # 10リクエスト/秒
            logger.info("NCBI APIキーを設定しました")
        else:
            self.rate_limit = 0.34  # 3リクエスト/秒
            logger.info("NCBI APIキーなしで動作します（レート制限: 3リクエスト/秒）")

    def _build_query(self, days_back: int) -> str:
        """
        PubMed検索クエリを構築する

        ジャーナルフィルタ + 専門領域キーワードを組み合わせる
        ※日付フィルタはesearchのmindate/maxdateパラメータで指定するため、クエリには含めない

        Returns:
            PubMed検索クエリ文字列
        """
        # ジャーナルフィルタ（tier1のみでクエリを短く保つ）
        tier1_journals = self.config.get("journals", {}).get("tier1", [])
        tier2_journals = self.config.get("journals", {}).get("tier2", [])
        tier3_journals = self.config.get("journals", {}).get("tier3", [])
        # 重複を除去しつつ順序を保持（tier1優先）
        all_journals = list(dict.fromkeys(tier1_journals + tier2_journals + tier3_journals))

        if all_journals:
            journal_terms = " OR ".join(
                [f'"{j}"[Journal]' for j in all_journals]
            )
            journal_filter = f"({journal_terms})"
        else:
            journal_filter = ""

        # 専門領域キーワード（primaryのみ）
        primary_specialties = self.config.get("specialties", {}).get("primary", [])

        if primary_specialties:
            specialty_terms = " OR ".join(
                [f'"{s}"[Title/Abstract]' for s in primary_specialties]
            )
            specialty_filter = f"({specialty_terms})"
        else:
            specialty_filter = ""

        # クエリ組み立て（日付は含めない）
        parts = []
        if journal_filter:
            parts.append(journal_filter)
        if specialty_filter:
            parts.append(specialty_filter)

        query = " AND ".join(parts) if parts else "cardiology"

        logger.info(f"検索クエリ: {query[:200]}...")
        return query

    def _execute_esearch(
        self, query: str, max_results: int,
        min_date: str, max_date: str
    ) -> Optional[dict]:
        """
        PubMed ESearchを実行する（フォールバック付き）

        HTTP 400等のエラー発生時に以下の順でリトライする:
        1. APIキーを除外して再試行
        2. クエリをジャーナルフィルタのみに簡略化して再試行

        Returns:
            検索結果dict（全て失敗時はNone）
        """
        import urllib.error

        # 試行1: 通常の検索
        result = self._try_esearch(query, max_results, min_date, max_date)
        if result is not None:
            return result

        # 試行2: APIキーが原因の可能性 → APIキーを一時的に無効化してリトライ
        if self._has_api_key:
            logger.warning("APIキーを無効化してリトライします...")
            saved_key = Entrez.api_key
            Entrez.api_key = None
            self.rate_limit = 0.34

            result = self._try_esearch(query, max_results, min_date, max_date)

            # APIキーを復元
            Entrez.api_key = saved_key
            self.rate_limit = 0.1

            if result is not None:
                logger.warning(
                    "APIキーなしで検索成功しました。"
                    "NCBI_API_KEYの値が不正な可能性があります。"
                    "Settings → Secrets で確認してください。"
                )
                return result

        # 試行3: クエリが原因の可能性 → ジャーナルフィルタのみに簡略化
        logger.warning("クエリを簡略化してリトライします...")
        tier1_journals = self.config.get("journals", {}).get("tier1", [])
        if tier1_journals:
            simple_terms = " OR ".join(
                [f'"{j}"[Journal]' for j in tier1_journals[:5]]
            )
            simple_query = f"({simple_terms})"
            logger.info(f"簡略化クエリ: {simple_query[:200]}")

            if self._has_api_key:
                saved_key = Entrez.api_key
                Entrez.api_key = None
            result = self._try_esearch(
                simple_query, max_results, min_date, max_date
            )
            if self._has_api_key:
                Entrez.api_key = saved_key

            if result is not None:
                logger.warning("簡略化クエリで検索成功しました")
                return result

        logger.error("全ての検索試行が失敗しました")
        return None

    def _try_esearch(
        self, query: str, max_results: int,
        min_date: str, max_date: str
    ) -> Optional[dict]:
        """esearchを1回試行する"""
        try:
            time.sleep(self.rate_limit)
            handle = Entrez.esearch(
                db="pubmed",
                term=query,
                retmax=max_results,
                sort="relevance",
                usehistory="y",
                datetype="pdat",
                mindate=min_date,
                maxdate=max_date
            )
            search_results = Entrez.read(handle, validate=False)
            handle.close()
            return search_results
        except Exception as e:
            logger.error(f"PubMed検索中にエラー: {e}")
            logger.error(f"送信したクエリ: {query}")
            if self._has_api_key:
                logger.error(
                    f"APIキー設定: あり "
                    f"(末尾: ...{str(Entrez.api_key)[-4:] if Entrez.api_key else 'None'})"
                )
            return None

    def search(self, days_back: Optional[int] = None) -> list[Paper]:
        """
        PubMedを検索し、論文リストを返す

        Args:
            days_back: 過去何日分を検索するか（Noneの場合config値を使用）

        Returns:
            Paper オブジェクトのリスト
        """
        if days_back is None:
            days_back = self.config.get("search", {}).get("days_back", 7)

        max_results = self.config.get("search", {}).get("max_results", 200)
        query = self._build_query(days_back)

        # ESearch: PMIDリスト取得　※日付はmindate/maxdateパラメータで指定
        end_date = now_jst()
        start_date = end_date - timedelta(days=days_back)
        min_date_str = start_date.strftime("%Y/%m/%d")
        max_date_str = end_date.strftime("%Y/%m/%d")

        logger.info(f"PubMed検索を実行中（過去{days_back}日間）...")
        logger.info(f"日付範囲: {min_date_str} - {max_date_str}")
        logger.info(f"検索クエリ全文: {query}")

        search_results = self._execute_esearch(
            query, max_results, min_date_str, max_date_str
        )
        if search_results is None:
            return []

        id_list = search_results.get("IdList", [])
        total_count = int(search_results.get("Count", 0))
        logger.info(f"検索結果: {total_count}件（取得: {len(id_list)}件）")

        if not id_list:
            logger.warning("該当する論文が見つかりませんでした")
            return []

        # EFetch: 論文詳細取得（バッチ処理）
        papers = []
        batch_size = 50

        for start in range(0, len(id_list), batch_size):
            batch_ids = id_list[start:start + batch_size]
            logger.info(
                f"論文詳細を取得中... ({start + 1}-{start + len(batch_ids)}"
                f"/{len(id_list)})"
            )

            try:
                time.sleep(self.rate_limit)
                handle = Entrez.efetch(
                    db="pubmed",
                    id=",".join(batch_ids),
                    rettype="xml",
                    retmode="xml"
                )
                records = Entrez.read(handle, validate=False)
                handle.close()
            except Exception as e:
                logger.error(f"論文詳細取得中にエラー: {e}")
                continue

            # XMLパース
            for article in records.get("PubmedArticle", []):
                paper = self._parse_article(article)
                if paper:
                    papers.append(paper)

        logger.info(f"合計 {len(papers)} 件の論文を取得しました")
        return papers

    def _parse_article(self, article: dict) -> Optional[Paper]:
        """
        PubMed XMLレコードからPaperオブジェクトを構築する

        Args:
            article: Entrez.readで取得した1論文のdict

        Returns:
            Paperオブジェクト（パース失敗時はNone）
        """
        try:
            medline = article.get("MedlineCitation", {})
            article_data = medline.get("Article", {})
            pmid = str(medline.get("PMID", ""))

            # タイトル
            title = str(article_data.get("ArticleTitle", ""))

            # 著者
            authors = []
            author_list = article_data.get("AuthorList", [])
            for author in author_list:
                last = author.get("LastName", "")
                fore = author.get("ForeName", "")
                if last:
                    authors.append(f"{last} {fore}".strip())

            # ジャーナル
            journal_info = article_data.get("Journal", {})
            journal = str(journal_info.get("ISOAbbreviation", ""))
            if not journal:
                journal = str(journal_info.get("Title", ""))

            # 出版日
            pub_date = self._extract_pub_date(article_data, journal_info)

            # アブストラクト
            abstract = self._extract_abstract(article_data)

            # 論文タイプ
            pub_types = []
            pub_type_list = article_data.get("PublicationTypeList", [])
            for pt in pub_type_list:
                pub_types.append(str(pt))

            # DOI
            doi = ""
            article_ids = article_data.get("ELocationID", [])
            for aid in article_ids:
                if aid.attributes.get("EIdType", "") == "doi":
                    doi = str(aid)
                    break

            # PubmedDataからDOI（未取得時）とPMCIDを取得する
            pmcid = ""
            pubmed_data = article.get("PubmedData", {})
            for aid in pubmed_data.get("ArticleIdList", []):
                id_type = aid.attributes.get("IdType", "")
                if id_type == "doi" and not doi:
                    doi = str(aid)
                elif id_type == "pmc":
                    pmcid = str(aid)

            # MeSH用語
            mesh_terms = []
            mesh_list = medline.get("MeshHeadingList", [])
            for mesh in mesh_list:
                descriptor = mesh.get("DescriptorName", "")
                if descriptor:
                    mesh_terms.append(str(descriptor))

            # キーワード
            keywords = []
            keyword_list = medline.get("KeywordList", [])
            for kw_group in keyword_list:
                for kw in kw_group:
                    keywords.append(str(kw))

            return Paper(
                pmid=pmid,
                title=title,
                authors=authors,
                journal=journal,
                pub_date=pub_date,
                abstract=abstract,
                pub_types=pub_types,
                doi=doi,
                mesh_terms=mesh_terms,
                keywords=keywords,
                pmcid=pmcid,
            )

        except Exception as e:
            logger.warning(f"論文パース中にエラー: {e}")
            return None

    def _extract_pub_date(self, article_data: dict, journal_info: dict) -> str:
        """出版日を抽出する"""
        # ArticleDateを試す
        article_dates = article_data.get("ArticleDate", [])
        if article_dates:
            date = article_dates[0]
            year = date.get("Year", "")
            month = date.get("Month", "01")
            day = date.get("Day", "01")
            return f"{year}/{month}/{day}"

        # JournalIssueのPubDateを試す
        journal_issue = journal_info.get("JournalIssue", {})
        pub_date = journal_issue.get("PubDate", {})
        year = pub_date.get("Year", "")
        month = pub_date.get("Month", "")
        day = pub_date.get("Day", "")

        if year:
            # 月名をゼロ詰め数値に変換
            month_map = {
                "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
                "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
                "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"
            }
            month = month_map.get(month, month if month else "01")
            day = day if day else "01"
            return f"{year}/{month}/{day}"

        return ""

    def _extract_abstract(self, article_data: dict) -> str:
        """アブストラクトを抽出する"""
        abstract_data = article_data.get("Abstract", {})
        abstract_texts = abstract_data.get("AbstractText", [])

        if not abstract_texts:
            return ""

        parts = []
        for text in abstract_texts:
            label = text.attributes.get("Label", "") if hasattr(text, "attributes") else ""
            content = str(text)
            if label:
                parts.append(f"【{label}】{content}")
            else:
                parts.append(content)

        return "\n".join(parts)
