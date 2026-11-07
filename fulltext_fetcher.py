"""
PMCオープンアクセス全文取得モジュール

PubMedレコードにPMCIDがある論文について、NCBI E-utilities（efetch db=pmc）で
JATS XMLを取得し、本文（序論〜考察）をテキストにする。
出版社の許諾がない論文は本文（<body>）が返らないため、その場合は抄録のみで要約する。
"""

import logging
import time
import xml.etree.ElementTree as ET
from typing import Optional

from Bio import Entrez

from pubmed_searcher import Paper

logger = logging.getLogger(__name__)

# 本文に含めないセクション（タイトルの小文字部分一致）
SKIP_SECTION_WORDS = [
    "reference", "acknowledg", "funding", "disclosure", "conflict",
    "author contribution", "data availability", "supplementa",
]


def _text(elem) -> str:
    return " ".join("".join(elem.itertext()).split())


def _section_lines(sec, depth: int = 0) -> list[str]:
    lines = []
    title_el = sec.find("title")
    title = _text(title_el) if title_el is not None else ""
    if title and any(w in title.lower() for w in SKIP_SECTION_WORDS):
        return []
    if title:
        lines.append(("#" * min(depth + 2, 4)) + " " + title)
    for child in sec:
        if child.tag == "p":
            t = _text(child)
            if t:
                lines.append(t)
        elif child.tag == "sec":
            lines.extend(_section_lines(child, depth + 1))
        elif child.tag in ("table-wrap", "fig"):
            # 表・図はキャプションだけ残す（数値照合の材料にもなる）
            cap = child.find("caption")
            label = child.find("label")
            if cap is not None:
                prefix = _text(label) + ": " if label is not None else ""
                lines.append("[" + prefix + _text(cap) + "]")
    return lines


def parse_jats_body(xml_text: str) -> str:
    """JATS XMLから本文テキストを抜き出す（本文がなければ空文字）"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning(f"PMC XMLのパースに失敗: {e}")
        return ""
    body = root.find(".//article/body")
    if body is None:
        body = root.find(".//body")
    if body is None:
        return ""

    lines = []
    for child in body:
        if child.tag == "sec":
            lines.extend(_section_lines(child))
        elif child.tag == "p":
            t = _text(child)
            if t:
                lines.append(t)
    return "\n".join(lines).strip()


class FullTextFetcher:
    """PMC全文を取得してPaper.full_textに格納するクラス"""

    def __init__(self, config: dict, rate_limit: float = 0.34):
        self.config = config.get("fulltext", {})
        self.rate_limit = rate_limit

    @property
    def enabled(self) -> bool:
        return self.config.get("enabled", True)

    def fetch_one(self, pmcid: str) -> Optional[str]:
        try:
            time.sleep(self.rate_limit)
            handle = Entrez.efetch(db="pmc", id=pmcid.replace("PMC", ""), retmode="xml")
            raw = handle.read()
            handle.close()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            return parse_jats_body(raw) or None
        except Exception as e:
            logger.warning(f"PMC全文の取得に失敗（{pmcid}）: {e}")
            return None

    def fetch(self, papers: list[Paper]) -> int:
        """全文を取得できた件数を返す"""
        min_chars = self.config.get("min_chars", 2000)
        got = 0
        for p in papers:
            if not p.pmcid:
                continue
            text = self.fetch_one(p.pmcid)
            if text and len(text) >= min_chars:
                p.full_text = text
                got += 1
                logger.info(f"全文取得: {p.pmcid}（{len(text)}文字） {p.title[:40]}...")
            else:
                logger.info(f"全文なし（抄録のみ）: {p.pmcid} {p.title[:40]}...")
        logger.info(f"PMC全文: {got}/{len(papers)}件で取得")
        return got
