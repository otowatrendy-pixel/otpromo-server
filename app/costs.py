"""初期費用の概算（賃貸専門）。

金額は募集図面の記載と、法令で決まっている上限・税額だけで計算する。
それ以外（保証会社、火災保険など）は「要確認」として金額を入れない。
"""
import re
from dataclasses import dataclass
from typing import List, Optional

from schema import Facts, Money

TAX = 0.10


@dataclass
class Line:
    項目: str
    金額円: Optional[int]
    メモ: str = ""


def _months_or_yen(m: Money, base: Optional[int]) -> Optional[int]:
    if m.金額円 is not None:
        return m.金額円
    if m.ヶ月 is not None and base:
        return int(m.ヶ月 * base)
    if m.記載 and m.記載.strip() in ("なし", "無", "0", "0円", "-", "－"):
        return 0
    return None


def _yen_in(text: Optional[str]) -> Optional[int]:
    """「要 2年20,000円」のような記載から円の金額を1つだけ取り出す。2つ以上あれば決めずに None。"""
    if not text:
        return None
    found = re.findall(r"([0-9][0-9,]*)\s*円", text.replace("￥", "").replace("¥", ""))
    return int(found[0].replace(",", "")) if len(found) == 1 else None


# AIが時期を付け忘れても、項目名から入居時以外の費用を見分ける
_LATER = (("退去", "退去時"), ("解約", "退去時"), ("更新", "更新時"), ("月額", "月額"), ("毎月", "月額"), ("/月", "月額"))


def _when(c) -> str:
    if c.時期 not in ("入居時", "不明"):
        return c.時期
    text = f"{c.項目} {c.記載}"
    for word, when in _LATER:
        if word in text:
            return when
    return c.時期


def rental(f: Facts, company: dict) -> List[Line]:
    rent = f.賃料.金額円
    admin = f.管理費_共益費.金額円 or 0
    lines = []
    if rent:
        lines.append(Line("翌月分賃料", rent, "前払い　※入居月の日割り分は別途"))
        lines.append(Line("翌月分管理費・共益費", admin, "前払い" if admin else "記載なし"))
    else:
        lines.append(Line("翌月分賃料", None, "賃料が読み取れませんでした"))
    biz = f.用途 == "事業用"
    if rent and f.賃料_消費税 == "税別":
        lines.append(Line("賃料・共益費の消費税（翌月分）", int((rent + admin) * TAX), "図面に税別と記載"))
    elif rent and biz and f.賃料_消費税 == "記載なし":
        lines.append(Line("賃料・共益費の消費税（翌月分）", None, "税込・税別の記載なし（事業用は課税されることが多い）"))
    if f.保証金.記載 or f.保証金.金額円 is not None or f.保証金.ヶ月 is not None:
        lines.append(Line("保証金", _months_or_yen(f.保証金, rent), f.保証金.記載 or ""))
        if f.敷金.記載 or f.敷金.金額円 is not None or f.敷金.ヶ月 is not None:
            lines.append(Line("敷金", _months_or_yen(f.敷金, rent), f.敷金.記載))
    else:
        lines.append(Line("敷金", _months_or_yen(f.敷金, rent), f.敷金.記載 or "記載なし"))
    lines.append(Line("礼金", _months_or_yen(f.礼金, rent), f.礼金.記載 or "記載なし"))
    months = company.get("事業用_仲介手数料_ヶ月" if biz else "賃貸_仲介手数料_ヶ月",
                         company.get("賃貸_仲介手数料_ヶ月", 1.0))
    if rent:
        fee = int(rent * months * (1 + TAX))  # 端数切り捨て
        lines.append(Line("仲介手数料", fee, f"賃料 × {months:g}ヶ月 ＋ 消費税（弊社）"))
    else:
        lines.append(Line("仲介手数料", None, "要確認"))
    guarantor = Line("保証会社 初回保証料", None, f.保証会社 or "記載なし")
    insurance = Line("火災保険料（店舗総合保険など）" if biz else "火災保険料", _yen_in(f.火災保険), f.火災保険 or "記載なし")
    lines += [guarantor, insurance]
    later = []
    if f.償却:
        later.append(Line("保証金・敷金の償却", None, f"退去時　{f.償却}（返還額から差し引かれます）"))
    for c in f.その他の費用:
        # 保険・保証料が「その他」にも書かれていたら、上の行にまとめて二重に載せない
        if c.時期 in ("入居時", "不明") and "保険" in c.項目 and "保証" not in c.項目:
            if insurance.金額円 is None:
                insurance.金額円, insurance.メモ = c.金額円, c.記載
            continue
        if c.時期 in ("入居時", "不明") and "保証" in c.項目 and "更新" not in c.項目:
            if guarantor.金額円 is None:
                guarantor.金額円, guarantor.メモ = c.金額円, c.記載
            continue
        when = _when(c)
        if when in ("月額", "更新時", "退去時"):
            later.append(Line(c.項目, c.金額円, f"{when}　{c.記載}"))
            continue
        memo = c.記載 if when == "入居時" else f"{c.記載}（支払時期は要確認）"
        lines.append(Line(c.項目, c.金額円, memo))
    return lines, later


def estimate(f: Facts, company: dict):
    """初期費用の明細・合計・要確認の項目と、合計に入れない入居後の費用（参考）を返す。"""
    lines, later = rental(f, company)
    known = sum(l.金額円 for l in lines if l.金額円 is not None)
    unknown = [l.項目 for l in lines if l.金額円 is None]
    return lines, known, unknown, later
