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


MONEY_KEYS = ("賃料", "管理費_共益費", "敷金", "礼金", "保証金")


def apply_choice(f: Facts, i: int) -> Facts:
    """利用者が選んだ条件（区画・使い方）の金額を、事実の欄に入れた写しを返す。"""
    c = f.条件が複数[i]
    g = f.model_copy(deep=True)
    for k in MONEY_KEYS:
        m = getattr(c, k)
        if m.記載 or m.金額円 is not None or m.ヶ月 is not None:
            setattr(g, k, m)
        else:
            setattr(g, k, Money())
    g.選んだ条件 = i
    return g


def _differing(f: Facts) -> dict:
    """条件が2通り以上あってまだ選ばれていないとき、条件によって違う項目 → 「店舗①：…／店舗②：…」。"""
    if len(f.条件が複数) < 2 or f.選んだ条件 is not None:
        return {}
    out = {}
    for k in MONEY_KEYS:
        vals = [(c.名前, getattr(c, k)) for c in f.条件が複数]
        keyset = {(m.記載, m.金額円, m.ヶ月) for _, m in vals}
        if len(keyset) > 1:
            out[k] = "／".join(f"{n}：{m.記載 or 'なし'}" for n, m in vals)
    return out


# 「読み取りに自信がない項目」の言葉 → 金額の項目
_SHAKY_WORDS = {"賃料": "賃料", "家賃": "賃料", "管理費": "管理費_共益費", "共益費": "管理費_共益費",
                "敷金": "敷金", "礼金": "礼金", "保証金": "保証金", "火災保険": "火災保険", "保証会社": "保証会社"}


def _shaky_keys(f: Facts) -> set:
    keys = set()
    for item in f.読み取りに自信がない項目:
        for word, key in _SHAKY_WORDS.items():
            if word in item:
                keys.add(key)
    return keys


# 概算書の行が、どの読み取り項目にもとづくか
def _depends(line: "Line", f: Facts) -> set:
    name = line.項目
    by_month = lambda m: m.金額円 is None and m.ヶ月 is not None
    if name == "翌月分賃料" or name == "仲介手数料":
        return {"賃料"}
    if name == "翌月分管理費・共益費":
        return {"管理費_共益費"}
    if name.startswith("賃料・共益費の消費税"):
        return {"賃料", "管理費_共益費"}
    for key in ("保証金", "敷金", "礼金"):
        if name == key:
            return {key} | ({"賃料"} if by_month(getattr(f, key)) else set())
    if name.startswith("火災保険"):
        return {"火災保険"}
    if name.startswith("保証会社"):
        return {"保証会社"}
    return set()


def estimate(f: Facts, company: dict):
    """初期費用の明細・合計・要確認の項目と、合計に入れない入居後の費用（参考）を返す。
    条件が2通り以上あってまだ選ばれていない項目と、読み取りに自信がない金額の項目は、金額を入れず「要確認」にする。"""
    lines, later = rental(f, company)
    diff, shaky = _differing(f), _shaky_keys(f)
    for l in lines:
        dep = _depends(l, f)
        hit = [k for k in dep if k in diff]
        if hit:
            l.金額円 = None
            l.メモ = "条件により異なります（" + "、".join(diff[k] for k in hit) + "）"
        elif dep & shaky and l.金額円 is not None:
            l.金額円 = None
            l.メモ = f"読み取りに自信がないため要確認（図面の記載：{l.メモ}）" if l.メモ else "読み取りに自信がないため要確認"
    known = sum(l.金額円 for l in lines if l.金額円 is not None)
    unknown = [l.項目 for l in lines if l.金額円 is None]
    return lines, known, unknown, later
