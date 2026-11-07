"""
ネットワークなしで動く回帰テスト

実行: python -m pytest tests/ -q   （またはリポジトリ直下で python tests/test_offline.py）
"""

import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from fulltext_fetcher import parse_jats_body  # noqa: E402
from llm_client import GeminiClient, parse_json_loose  # noqa: E402
from number_checker import check_numbers, extract_numbers  # noqa: E402
from paper_filter import PaperFilter  # noqa: E402
from paper_triage import PaperTriage  # noqa: E402
from pubmed_searcher import Paper  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config():
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["history"]["file"] = os.path.join(tempfile.mkdtemp(), "history.json")
    return cfg


# ---------------------------------------------------------------- 数値照合
def test_number_check_flags_missing_numbers():
    source = (
        "Dapagliflozin reduced the primary outcome (HR 0.74; 95% CI, 0.65-0.85; "
        "P<0.001) among 4,744 patients. Event rate 16.3% vs 21.2%."
    )
    summary = (
        "## サマリーインデックス情報\n- **重要度**: ★★★★★\n"
        "主要評価項目はHR 0.74（95%CI 0.65–0.85）、対象は4744例。"
        "発生率16.3%対21.2%。NNTは21（計算値）。死亡はHR 0.83。2019年の試験。"
    )
    result = check_numbers(summary, source)
    assert result["unverified"] == ["0.83"], result
    # 星の行・年号・（計算値）・小さい整数は対象外
    nums = extract_numbers(summary)
    assert "2019" not in nums and "21" not in nums


def test_number_check_thousands_next_to_kanji():
    # 実運用で誤検知した例: 「成人3,042名」→「042」、欧州式の「19 499」
    summary = "成人3,042名（追跡は2,169名、1,988名を解析）。一般住民19,499名。2002-2022年の研究"
    source = "3042 adults; 2169 followed; 1988 analysed. A total of 19 499 participants (2002-2022)."
    assert check_numbers(summary, source)["unverified"] == []
    assert "1988" in extract_numbers(summary)
    assert "2002" not in extract_numbers(summary)


def test_number_check_ignores_ci_term_but_flags_derived_value():
    # 実運用の例: 「95%信頼区間は記載なし」は用語なので対象外。
    # 抄録の「4.0% women」から計算した「96%が男性」は（計算値）表記がないので要確認
    source = "average age, 44 years; 4.0% women. 312 of 794 (39.3%) were SCD."
    summary = "（※ハザード比・95%CI・P値は原文に記載なし）95%信頼区間も不明。96%が男性。39.3%がSCD。"
    assert check_numbers(summary, source)["unverified"] == ["96"]


def test_number_check_japanese_units():
    # 実運用の例: 原文「5.5 million (95% CI: 5.1-5.8 million)」を「550万人（510万〜580万人）」と訳した
    source = "An estimated 5.5 million (95% CI: 5.1-5.8 million) adults; 498,000 fewer cases"
    summary = "推定550万人（95% CI: 510万〜580万人）。498,000例が回避。600万人という数値は誤り"
    assert check_numbers(summary, source)["unverified"] == ["600万"]


def test_number_check_trailing_zero_and_fullwidth():
    source = "HR 0.8 (95% CI 0.70-0.91)"
    summary = "ＨＲ ０.８０（95%CI 0.70〜0.91）"
    assert check_numbers(summary, source)["unverified"] == []


# ---------------------------------------------------------------- PMC全文
def test_parse_jats_body_skips_references():
    xml = """<pmc-articleset><article><front><article-meta/></front><body>
      <sec><title>Methods</title><p>We enrolled 1,200 patients.</p>
        <table-wrap><label>Table 1</label><caption><p>Baseline HR 0.82</p></caption></table-wrap>
      </sec>
      <sec><title>Results</title><p>The HR was 0.82.</p></sec>
      <sec><title>Funding</title><p>Sponsor X.</p></sec>
    </body><back><ref-list><ref>Ref 1</ref></ref-list></back></article></pmc-articleset>"""
    text = parse_jats_body(xml)
    assert "We enrolled 1,200 patients." in text
    assert "Table 1: Baseline HR 0.82" in text
    assert "Sponsor X" not in text and "Ref 1" not in text


def test_parse_jats_without_body_returns_empty():
    xml = "<pmc-articleset><article><front/></article></pmc-articleset>"
    assert parse_jats_body(xml) == ""


# ---------------------------------------------------------------- JSONパース
def test_parse_json_loose():
    assert parse_json_loose('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert parse_json_loose('結果です: [{"a": 2}] 以上') == [{"a": 2}]


# ---------------------------------------------------------------- モデル解決
def test_model_chain_drops_retired_models():
    client = GeminiClient.__new__(GeminiClient)
    client.configured_chain = ["gemini-2.0-flash", "gemini-3.6-flash"]
    client._list_available_models = lambda: {"gemini-3.6-flash", "gemini-3.5-flash-lite"}
    assert client._resolve_chain() == ["gemini-3.6-flash"]


def test_model_chain_auto_selects_when_all_retired():
    client = GeminiClient.__new__(GeminiClient)
    client.configured_chain = ["gemini-2.0-flash"]
    client._list_available_models = lambda: {
        "gemini-3.6-flash", "gemini-3.8-flash", "gemini-3.5-flash-lite",
        "gemini-3-flash-preview", "gemini-3.8-flash-tts", "gemini-4-argon",
    }
    assert client._resolve_chain() == [
        "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"
    ]


def test_503_switches_model_without_retry():
    client = GeminiClient.__new__(GeminiClient)
    client.model_chain = ["busy-model", "ok-model"]
    client.max_retries = 3
    client.retry_delay = 0
    client.models_used = {}
    client.failures = 0
    calls = []

    def fake_generate_content(model, contents, config):
        calls.append(model)
        if model == "busy-model":
            raise RuntimeError("503 UNAVAILABLE. This model is currently experiencing high demand.")
        return SimpleNamespace(text="ok")

    client.client = SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate_content))
    assert client.generate("p") == "ok"
    assert calls == ["busy-model", "ok-model"], calls


# ---------------------------------------------------------------- 選定
def make_paper(pmid, title, abstract, journal="Circulation", types=None):
    return Paper(
        pmid=pmid, title=title, abstract=abstract, journal=journal,
        pub_types=types or ["Journal Article"], pub_date="2026/11/05",
    )


class FakeClient:
    def __init__(self, response):
        self.response = response

    def generate_json(self, prompt, **kwargs):
        return self.response


def test_triage_excludes_basic_and_boosts_untagged_rct():
    cfg = load_config()
    cfg["search"]["top_n"] = 3
    papers = [
        # 基礎研究（"patient"を含むためキーワード除外をすり抜ける）
        make_paper("1", "PRMT3 stabilizes PCSK9 in aortic valve calcification",
                   "In human valve tissue from patients and mice, PRMT3 methylates PCSK9 in heart failure models.",
                   journal="Circulation"),
        # タグなしの新着RCT
        make_paper("2", "Drug X in heart failure",
                   "In this randomized trial of 3000 patients with heart failure, drug X reduced events.",
                   journal="JACC Heart Fail"),
        make_paper("3", "Registry of heart failure in Japan",
                   "A nationwide registry of heart failure patients.", journal="Heart"),
        make_paper("5", "GWAS of heart failure loci",
                   "Genome-wide association in 100000 heart failure patients identified loci.", journal="Circulation"),
        make_paper("6", "Global research trends in heart failure: a bibliometric analysis",
                   "We analyzed publications on heart failure.", journal="Heart Rhythm"),
        make_paper("4", "Narrative review of heart failure",
                   "We review heart failure therapies.", journal="Eur Heart J", types=["Journal Article", "Review"]),
    ]
    fake = FakeClient([
        {"pmid": "1", "category": "basic", "design": "other", "japan_asian": False, "reason": "機序研究"},
        {"pmid": "2", "category": "clinical", "design": "rct", "japan_asian": False, "reason": "新規RCT"},
        {"pmid": "3", "category": "clinical", "design": "registry", "japan_asian": True, "reason": "日本の登録研究"},
        {"pmid": "4", "category": "clinical", "design": "review", "japan_asian": False, "reason": "総説"},
        {"pmid": "5", "category": "genetic", "design": "other", "japan_asian": False, "reason": "GWAS"},
        {"pmid": "6", "category": "other", "design": "other", "japan_asian": False, "reason": "計量書誌学"},
    ])
    filt = PaperFilter(cfg)
    top = filt.filter_and_rank(papers, triage=PaperTriage(cfg, fake))
    pmids = [p.pmid for p in top]
    assert not {"1", "5", "6"} & set(pmids), pmids
    assert pmids[0] == "2", pmids
    assert filt.triage_ok is True


def test_triage_failure_falls_back_to_rules():
    cfg = load_config()
    papers = [make_paper("9", "Heart failure trial", "Patients with heart failure were randomized.")]
    filt = PaperFilter(cfg)
    top = filt.filter_and_rank(papers, triage=PaperTriage(cfg, FakeClient(None)))
    assert [p.pmid for p in top] == ["9"]
    assert filt.triage_ok is False


# ---------------------------------------------------------------- Word出力
def test_word_generation_with_verification_note():
    from docx import Document
    from word_generator import WordGenerator

    cfg = load_config()
    p = make_paper("2", "Drug X in heart failure", "abstract")
    p.priority_rank = 1
    p.summary = {
        "mode": "detailed",
        "content": "## サマリーインデックス情報\n- **タイトル和訳**: 心不全における薬剤X\n"
                   "- **重要度**: ★★★★☆\n- **結論**: 有効\n- **実用**: **HFrEF**で検討\n\n## まず一言で\nHR 0.83",
        "source": "抄録のみ",
        "number_check": {"total": 1, "unverified": ["0.83"]},
        "failed": False,
    }
    out = os.path.join(tempfile.mkdtemp(), "t.docx")
    WordGenerator(cfg).generate([p], out)
    text = "\n".join(par.text for par in Document(out).paragraphs)
    assert "⚠数値要確認1件" in text
    assert "要約の元: 抄録のみ" in text
    assert "0.83" in text


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if failures else 0)
