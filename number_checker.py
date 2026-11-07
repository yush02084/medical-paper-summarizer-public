"""
数値照合モジュール

AI要約に出てくる数値（小数・パーセント・3桁以上の整数）を抜き出し、
要約の元にした原文（タイトル・抄録・PMC全文）に同じ数値があるかを確認する。
原文に見つからない数値は「要確認」としてWordに表示する。

- 「（計算値）」と明記された直前の数値は照合対象から外す
- 星（★）、見出しの文字数指定、年号（19xx/20xx）は対象外
"""

import re
import unicodedata

# 小数（0.82, 1.5）/ パーセント（23%）/ 3桁以上の整数（1,234 / 4744）
# 前後判定はASCII英数字のみで行う（\wは漢字にもマッチし「成人3,042名」の3が拾えなくなるため）
_NUM_RE = re.compile(r"(?<![0-9A-Za-z.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(\s*%)?")
_CALC_RE = re.compile(r"[（(]\s*計算値\s*[）)]")
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
# 「95%CI」「95%信頼区間」は数値ではなく用語なので照合しない
_CI_TERM_RE = re.compile(r"^\s*(CI|ＣＩ|信頼区間|confidence)", re.IGNORECASE)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    # 3桁区切りのカンマ・空白を除去（1,234 / 19 499 → 1234 / 19499）
    # \bは漢字の前で効かないため、後ろに数字が続かないことで判定する
    text = re.sub(r"(?<=\d)[, \u2009\u202f](?=\d{3}(?!\d))", "", text)
    # 中黒の小数点（0·82）をピリオドに
    text = text.replace("·", ".")
    return text


def _strip_index_noise(summary: str) -> str:
    """照合対象外の部分（重要度の星の行など）を除く"""
    lines = []
    for line in summary.splitlines():
        if "重要度" in line or "★" in line:
            continue
        lines.append(line)
    return "\n".join(lines)


_JA_UNITS = {"万": 1e4, "億": 1e8}
_EN_UNIT_RE = re.compile(
    r"(?<![0-9A-Za-z.])(\d+(?:\.\d+)?)\s*(thousand|million|billion)\b", re.IGNORECASE
)
# 「4.7-5.4 million」「4.7 to 5.4 million」の下限側にも単位を付ける
_EN_RANGE_RE = re.compile(
    r"(?<![0-9A-Za-z.])(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*\d+(?:\.\d+)?\s*(thousand|million|billion)\b",
    re.IGNORECASE,
)
_EN_UNITS = {"thousand": 1e3, "million": 1e6, "billion": 1e9}


def _source_values(source: str) -> set:
    """原文中の「5.5 million」などを実数に直した集合（単位換算の照合用）"""
    vals = set()
    for regex in (_EN_UNIT_RE, _EN_RANGE_RE):
        for m in regex.finditer(source):
            vals.add(round(float(m.group(1)) * _EN_UNITS[m.group(2).lower()]))
    return vals


def extract_numbers(summary: str) -> list[str]:
    return [n for n, _ in extract_numbers_with_unit(summary)]


def extract_numbers_with_unit(summary: str) -> list[tuple]:
    """(数値文字列, 日本語の単位倍率 or None) のリスト"""
    text = _normalize(_strip_index_noise(summary))
    found = []
    seen = set()
    for m in _NUM_RE.finditer(text):
        raw, pct = m.group(1), m.group(2)
        num = raw.replace(",", "")
        # 直後に（計算値）があれば照合しない
        tail = text[m.end(): m.end() + 12]
        if _CALC_RE.search(tail):
            continue
        if pct and num in ("90", "95", "99") and _CI_TERM_RE.match(text[m.end(): m.end() + 12]):
            continue
        is_decimal = "." in num
        is_percent = bool(pct)
        is_large_int = num.isdigit() and len(num) >= 3
        if not (is_decimal or is_percent or is_large_int):
            continue
        if num.isdigit() and _YEAR_RE.match(num):
            # 年号らしいもの（2019年、2002-2022、(2024)）だけ除外し、1,988名のような症例数は照合する
            before = text[max(0, m.start() - 1): m.start()]
            after = text[m.end(): m.end() + 1]
            if after in ("年", "-", "–", "〜", "~", ")", "）") or before in ("-", "–", "〜", "~", "(", "（"):
                continue
        unit = _JA_UNITS.get(text[m.end(): m.end() + 1]) if not pct else None
        key = (num, unit)
        if key not in seen:
            seen.add(key)
            found.append(key)
    return found


def _present(num: str, source: str) -> bool:
    pattern = r"(?<![\d.])" + re.escape(num) + r"(?![\d])"
    if re.search(pattern, source):
        return True
    # 0.80 と 0.8 のような末尾ゼロの違いを許容
    if "." in num:
        stripped = num.rstrip("0").rstrip(".")
        if stripped and stripped != num:
            return bool(re.search(r"(?<![\d.])" + re.escape(stripped) + r"(?![\d])", source))
    return False


def check_numbers(summary: str, source_text: str) -> dict:
    """
    Returns:
        {"total": 照合した数値の数, "unverified": 原文に見つからなかった数値のリスト}
    """
    source = _normalize(source_text)
    source_vals = _source_values(source)
    numbers = extract_numbers_with_unit(summary)
    unverified = []
    for num, unit in numbers:
        if _present(num, source):
            continue
        # 「550万人」と原文の「5.5 million」のような単位換算を許容
        if unit and round(float(num) * unit) in source_vals:
            continue
        label = num + ("万" if unit == 1e4 else "億" if unit == 1e8 else "")
        if label not in unverified:
            unverified.append(label)
    return {"total": len(numbers), "unverified": unverified}
