"""初期費用の概算（賃貸専門）。

金額はマイソクの記載と、法令で決まっている上限・税額だけで計算する。
それ以外（保証会社、火災保険など）は「要確認」として金額を入れない。
"""
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


def rental(f: Facts, company: dict) -> List[Line]:
    rent = f.賃料.金額円
    admin = f.管理費_共益費.金額円 or 0
    lines = []
    if rent:
        lines.append(Line("翌月分賃料", rent, "前払い　※入居月の日割り分は別途"))
        lines.append(Line("翌月分管理費・共益費", admin, "前払い" if admin else "記載なし"))
    else:
        lines.append(Line("翌月分賃料", None, "賃料が読み取れませんでした"))
    lines.append(Line("敷金", _months_or_yen(f.敷金, rent), f.敷金.記載 or "記載なし"))
    lines.append(Line("礼金", _months_or_yen(f.礼金, rent), f.礼金.記載 or "記載なし"))
    months = company.get("賃貸_仲介手数料_ヶ月", 1.0)
    if rent:
        fee = int(rent * months * (1 + TAX))  # 端数切り捨て
        lines.append(Line("仲介手数料", fee, f"賃料 × {months:g}ヶ月 ＋ 消費税（弊社）"))
    else:
        lines.append(Line("仲介手数料", None, "要確認"))
    lines.append(Line("保証会社 初回保証料", None, f.保証会社 or "記載なし"))
    lines.append(Line("火災保険料（概算）", None, f.火災保険 or "記載なし"))
    for c in f.その他の費用:
        lines.append(Line(c.項目, c.金額円, c.記載))
    return lines


def estimate(f: Facts, company: dict):
    lines = rental(f, company)
    known = sum(l.金額円 for l in lines if l.金額円 is not None)
    unknown = [l.項目 for l in lines if l.金額円 is None]
    return lines, known, unknown
