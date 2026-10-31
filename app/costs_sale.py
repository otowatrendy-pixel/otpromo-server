"""売買版 1分資料：購入諸費用の概算（costs_sale）

賃貸版 costs.py とは別物。募集図面から読み取った事実（SaleFacts）と
会社ごとの設定（CompanySaleSettings）を受け取り、概算書に載せる行を返す。

方針
- 税率は「適用期間つきの税率表」（RATES）で持ち、日付で自動的に切り替える。
  印紙税は契約予定日、登録免許税・不動産取得税は引渡予定日で判定する。
  期限後になる場合は本則で計算し、注記を出す（延長が決まったら表を直すだけ）。
- 税金・ローンは断言しない。行ごとに kind を持たせ、表示側で「目安」「要確認」を出す。
    exact    : 図面の数字だけで決まる（仲介手数料・売買契約書の印紙税など）
    estimate : 仮定や推計を含む目安（登記費用・清算金・ローン関連など）
    check    : 情報が足りず金額を出さない（要確認）
- 推計の金額は 1,000円単位で切り上げる（少なく見せない）。
- 司法書士の協力会社名は出さない。報酬は会社設定の「目安」として扱う。

調べた日：2026年10月31日（出典は shohiyou_memo.md）
"""
from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal, Optional

import evaluation as ev

# ---------------------------------------------------------------------------
# 税率表（適用期間つき）。None = 期限なし（本則）
# 延長が決まったら end を書き換えるだけで済むようにする。
# ---------------------------------------------------------------------------
D = date

RATES = {
    # 登録免許税（国税庁パンフレット 令和8年4月、タックスアンサー No.7191）
    "reg_land_transfer": [  # 土地の売買による所有権移転
        {"start": D(2013, 4, 1), "end": D(2029, 3, 31), "rate": 0.015, "label": "軽減1.5%（2029年3月31日まで）"},
        {"start": None, "end": None, "rate": 0.02, "label": "本則2%"},
    ],
    "reg_house_transfer": [  # 住宅用家屋の所有権移転（売買）
        {"start": None, "end": D(2027, 3, 31), "rate": 0.003, "label": "住宅用家屋の軽減0.3%（2027年3月31日まで）"},
        {"start": None, "end": None, "rate": 0.02, "label": "本則2%"},
    ],
    "reg_house_preserve": [  # 住宅用家屋の所有権保存（新築）
        {"start": None, "end": D(2027, 3, 31), "rate": 0.0015, "label": "住宅用家屋の軽減0.15%（2027年3月31日まで）"},
        {"start": None, "end": None, "rate": 0.004, "label": "本則0.4%"},
    ],
    "reg_mortgage": [  # 住宅取得資金の抵当権設定
        {"start": None, "end": D(2027, 3, 31), "rate": 0.001, "label": "住宅用の軽減0.1%（2027年3月31日まで）"},
        {"start": None, "end": None, "rate": 0.004, "label": "本則0.4%"},
    ],
    # 認定住宅等（いずれも2027年3月31日まで）。使うのは図面に明記がある場合だけ
    "reg_choki_preserve": [{"start": None, "end": D(2027, 3, 31), "rate": 0.001, "label": "認定長期優良住宅0.1%"}],
    "reg_choki_transfer_mansion": [{"start": None, "end": D(2027, 3, 31), "rate": 0.001, "label": "認定長期優良住宅0.1%"}],
    "reg_choki_transfer_house": [{"start": None, "end": D(2027, 3, 31), "rate": 0.002, "label": "認定長期優良住宅（戸建）0.2%"}],
    "reg_teitanso": [{"start": None, "end": D(2027, 3, 31), "rate": 0.001, "label": "認定低炭素住宅0.1%"}],
    "reg_kaitori_saihan": [{"start": None, "end": D(2027, 3, 31), "rate": 0.001, "label": "買取再販住宅0.1%"}],
    # 不動産取得税（神奈川県・東京都・総務省の案内）
    "acq_rate_land_house": [
        {"start": None, "end": D(2027, 3, 31), "rate": 0.03, "label": "土地・住宅3%（2027年3月31日まで）"},
        {"start": None, "end": None, "rate": 0.04, "label": "本則4%"},
    ],
    "acq_rate_other_bldg": [{"start": None, "end": None, "rate": 0.04, "label": "住宅以外の家屋4%"}],
    "acq_takuchi_half": [  # 宅地評価土地の課税標準1/2
        {"start": None, "end": D(2027, 3, 31), "rate": 0.5, "label": "宅地の課税標準1/2（2027年3月31日まで）"},
        {"start": None, "end": None, "rate": 1.0, "label": "特例なし"},
    ],
    # 床面積の下限（不動産取得税の住宅軽減）。2026/4/1以後の取得は40㎡
    "acq_min_floor": [
        {"start": None, "end": D(2026, 3, 31), "rate": 50.0, "label": "50㎡以上"},
        {"start": D(2026, 4, 1), "end": None, "rate": 40.0, "label": "40㎡以上（2026年4月1日以後の取得）"},
    ],
    # 免税点（課税標準額がこれ未満なら非課税）。2026/4/1以後の取得
    "acq_exempt_land": [
        {"start": None, "end": D(2026, 3, 31), "rate": 100_000, "label": "10万円"},
        {"start": D(2026, 4, 1), "end": None, "rate": 160_000, "label": "16万円"},
    ],
    "acq_exempt_bldg_new": [
        {"start": None, "end": D(2026, 3, 31), "rate": 230_000, "label": "23万円"},
        {"start": D(2026, 4, 1), "end": None, "rate": 660_000, "label": "66万円"},
    ],
    "acq_exempt_bldg_other": [
        {"start": None, "end": D(2026, 3, 31), "rate": 120_000, "label": "12万円"},
        {"start": D(2026, 4, 1), "end": None, "rate": 340_000, "label": "34万円"},
    ],
    # 印紙税（売買契約書）の軽減の期限。契約日で判定
    "stamp_sale_reduced_until": [{"start": None, "end": D(2027, 3, 31), "rate": 1, "label": "印紙税の軽減（2027年3月31日まで）"}],
}


def pick(key: str, on: date) -> Optional[dict]:
    """その日に有効な税率を返す（上から順に最初に当てはまるもの）。"""
    for r in RATES[key]:
        if (r["start"] is None or r["start"] <= on) and (r["end"] is None or on <= r["end"]):
            return r
    return None


def is_reduced(key: str, on: date) -> bool:
    r = pick(key, on)
    return r is not None and r["end"] is not None


# 印紙税（国税庁 No.7140 本則、No.7108 軽減）。(上限金額, 本則, 軽減後)
STAMP_TABLE = [
    (9_999, 0, 0),
    (100_000, 200, 200),
    (500_000, 400, 200),
    (1_000_000, 1_000, 500),
    (5_000_000, 2_000, 1_000),
    (10_000_000, 10_000, 5_000),
    (50_000_000, 20_000, 10_000),
    (100_000_000, 60_000, 30_000),
    (500_000_000, 100_000, 60_000),
    (1_000_000_000, 200_000, 160_000),
    (5_000_000_000, 400_000, 320_000),
    (math.inf, 600_000, 480_000),
]


def stamp_tax(amount: int, reduced: bool) -> int:
    for upper, base, red in STAMP_TABLE:
        if amount <= upper:
            return red if reduced else base
    raise ValueError


# 中古住宅の不動産取得税の控除額（新築された時期で決まる）。(新築日の始まり, 控除額)
# 神奈川県・埼玉県・千葉県の案内と同じ表（東京都の一次資料は要確認）
USED_HOUSE_DEDUCTION = [
    (D(1997, 4, 1), 12_000_000),
    (D(1989, 4, 1), 10_000_000),
    (D(1985, 7, 1), 4_500_000),
    (D(1981, 7, 1), 4_200_000),   # 1981/7/1〜12/31 は新耐震の証明が必要
    (D(1976, 1, 1), 3_500_000),
    (D(1973, 1, 1), 2_300_000),
    (D(1964, 1, 1), 1_500_000),
    (D(1954, 7, 1), 1_000_000),
]

SHINTAISHIN = D(1982, 1, 1)  # この日以後の新築は新耐震とみなす

# ---------------------------------------------------------------------------
# 推計に使う仮定（評価額が分からないとき）。会社設定で使わないこともできる
# ---------------------------------------------------------------------------
ASSUMPTIONS = {
    "land_eval_ratio": 0.65,         # 土地の評価額 ≒ 土地相当の価格 × 0.65（公示価格の7割程度という考え方）
    "bldg_unit": {"wood": 100_000, "steel": 110_000, "rc": 120_000},  # 建物の評価額（新築時）1㎡あたりの仮の単価
    "bldg_decay": {"wood": 0.04, "steel": 0.027, "rc": 0.016},        # 1年あたりの減り方（下限20%）
    "bldg_floor_ratio": 0.2,
    "bldg_market_mult": 1.4,         # 建物の評価額 → 建物の時価（評価額は時価の7割程度）
    "mansion_eval_total_ratio": 0.35,  # マンションの評価額の合計 ≒ 価格 × 0.35（都内の中古マンションは評価額が時価よりかなり低い）
    "mansion_land_min_ratio": 0.05,    # マンションの土地の評価額は少なくとも価格の5%
    "kotei_rate": 0.014,             # 固定資産税
    "toshi_rate": 0.003,             # 都市計画税（23区）
}


def ceil_k(x: float) -> int:
    """推計値は1,000円単位で切り上げ。"""
    return int(math.ceil(x / 1000.0) * 1000) if x > 0 else 0


def floor_unit(x: float, unit: int) -> int:
    return int(x // unit * unit)


def reg_tax(base: float, rate: float) -> int:
    """登録免許税：課税標準は1,000円未満切捨、税額は100円未満切捨、最低1,000円。"""
    b = floor_unit(base, 1000)
    t = floor_unit(b * rate, 100)
    return max(t, 1000) if b > 0 else 0


def acq_tax(base: float, rate: float) -> int:
    """不動産取得税：課税標準は1,000円未満切捨、税額は100円未満切捨。"""
    return floor_unit(floor_unit(base, 1000) * rate, 100)


# ---------------------------------------------------------------------------
# 入力
# ---------------------------------------------------------------------------
PropertyType = Literal["mansion", "house", "land"]
Structure = Literal["wood", "light_steel", "steel", "rc", "src"]


@dataclass
class SaleFacts:
    """募集図面から読み取った事実。書いていないものは None のまま。"""
    property_type: PropertyType
    price: int                                  # 価格（税込の表示額）
    price_tax: Optional[int] = None             # 「うち消費税」の額（書いてあれば）
    is_new: bool = False                        # 新築（未入居・完成1年未満）
    seller_is_business: Optional[bool] = None   # 売主が業者（取引態様「売主」など）。不明は None
    land_area: Optional[float] = None           # 土地面積（マンションは敷地持分。書いていないことが多い）
    floor_area: Optional[float] = None          # 建物面積・専有面積（図面の値）
    floor_area_is_wall_center: bool = True      # マンションの図面は壁芯面積が普通
    built: Optional[date] = None                # 築年月（新築年月）
    structure: Optional[Structure] = None
    kanri_hi: Optional[int] = None              # 管理費（月額）
    shuzen_hi: Optional[int] = None             # 修繕積立金（月額）
    other_monthly: Optional[int] = None         # その他の月額（管理準備金ではなく、駐輪場など毎月の費用は入れない）
    shuzen_kikin: Optional[int] = None          # 修繕積立基金（新築、一時金）
    kanri_junbikin: Optional[int] = None        # 管理準備金（一時金）
    land_eval: Optional[int] = None             # 固定資産税評価額（土地）。分かれば
    bldg_eval: Optional[int] = None             # 固定資産税評価額（建物）。分かれば
    annual_property_tax: Optional[int] = None   # 固定資産税・都市計画税の年額。分かれば
    choki_yuryo: bool = False                   # 認定長期優良住宅（図面に明記がある場合だけ True）
    teitanso: bool = False                      # 認定低炭素住宅
    kaitori_saihan: bool = False                # 業者による買取再販（一定の増改築あり）
    shakuchi: bool = False                      # 借地権（土地の所有権移転がない）
    address: Optional[str] = None               # 所在地（図面の表記。町丁目まで分かれば十分）
    youseki: Optional[int] = None               # 容積率（%）。マンションの敷地持分の仮置きに使う
    commercial_area: bool = False               # 用途地域が商業系（近隣商業・商業）
    rosenka_per_m2: Optional[int] = None        # 相続税路線価（円/㎡）。スタッフが送った場合
    kotei_rosenka_per_m2: Optional[int] = None  # 固定資産税路線価（円/㎡）。スタッフが送った場合


@dataclass
class LoanPlan:
    amount: int                                  # 借入額
    fee_type: Literal["teiritsu", "hoshoryo"] = "teiritsu"  # 定率型（事務手数料2.2%・保証料なし）／保証料型
    fee_rate: float = 0.022                      # 定率型の事務手数料（税込）
    fixed_fee: int = 33_000                      # 保証料型の事務手数料（税込）
    hoshoryo_per_10m: int = 206_000              # 保証料型：借入1,000万円あたりの保証料の目安（35年・一括前払い）
    electronic_contract: bool = False            # 電子契約なら印紙税なし


@dataclass
class CompanySaleSettings:
    fee_mode: Literal["max", "half", "free", "custom", "rate"] = "max"  # 仲介手数料：上限・半額・無料・任意額・割合
    fee_custom: Optional[int] = None             # 任意額（税込）
    fee_rate: Optional[float] = None             # 割合（税抜の物件価格に対する率。2%なら0.02）。消費税を足し、上限額を超えない
    teiren_tokurei: bool = False                 # 800万円以下の物件で低廉な空家等の特例（上限33万円）を使う
    estimate_eval: bool = True                   # 評価額が分からないとき、価格から推計する（False なら要確認）
    shihoshoshi_transfer: int = 66_000           # 司法書士報酬の目安（税込）：所有権移転
    shihoshoshi_preserve: int = 33_000           # 所有権保存（新築）
    shihoshoshi_mortgage: int = 44_000           # 抵当権設定
    touki_jippi: int = 8_000                     # 登記事項証明・郵送などの実費の目安
    fire_insurance: dict = field(default_factory=lambda: {"mansion": 60_000, "house": 200_000, "land": 0})  # 5年分の目安
    kisanbi_month: int = 1                       # 固定資産税の清算の起算日（関東は1月1日が多い）


@dataclass
class Line:
    group: Literal["must", "loan", "later"]
    name: str
    amount: Optional[int]
    kind: Literal["exact", "estimate", "check"]
    note: str = ""


@dataclass
class Result:
    lines: list[Line]
    notes: list[str]              # お客様向けの注記
    staff_notes: list[str]        # スタッフ向けの確認事項（お客様向けPDFには出さない）
    evals_used: dict
    total_must: int
    total_loan: int
    total_later_known: int


def with_evals(f: SaleFacts, land_eval: Optional[int] = None, bldg_eval: Optional[int] = None,
               annual_property_tax: Optional[int] = None) -> SaleFacts:
    """スタッフが送った評価額・年税額で作り直すときに使う。
    評価額は評価証明書・納税通知書の「価格」欄（課税標準額ではない）。マンションは敷地の持分で按分した額。"""
    from dataclasses import replace
    return replace(f,
                   land_eval=land_eval if land_eval is not None else f.land_eval,
                   bldg_eval=bldg_eval if bldg_eval is not None else f.bldg_eval,
                   annual_property_tax=annual_property_tax if annual_property_tax is not None else f.annual_property_tax)


# ---------------------------------------------------------------------------
# 計算本体
# ---------------------------------------------------------------------------

def brokerage_fee(price_ex_tax: int, s: CompanySaleSettings) -> tuple[Optional[int], str]:
    """仲介手数料（税込）。報酬告示：200万円以下5.5%、200〜400万円4.4%、400万円超3.3%。"""
    p = price_ex_tax
    mx = min(p, 2_000_000) * 0.055 + max(0, min(p, 4_000_000) - 2_000_000) * 0.044 + max(0, p - 4_000_000) * 0.033
    mx = int(mx)
    note = "上限額（物件価格×3%＋6万円＋消費税）" if p > 4_000_000 else "上限額"
    if s.teiren_tokurei and p <= 8_000_000:
        mx = max(mx, 330_000)
        note = "800万円以下の物件の特例による額（事前にご説明・ご同意のうえ）"
    if s.fee_mode == "max":
        return mx, note
    if s.fee_mode == "half":
        return mx // 2, "上限額の半額"
    if s.fee_mode == "free":
        return 0, "無料"
    if s.fee_mode == "rate":
        if not s.fee_rate:
            return None, "金額は担当者にご確認ください"
        fee = min(int(p * s.fee_rate * 1.1), mx)
        return fee, f"物件価格×{s.fee_rate * 100:g}%＋消費税"
    if s.fee_custom is None:
        return None, "金額は担当者にご確認ください"
    return min(s.fee_custom, mx), "当社規定の額"


def price_split(f: SaleFacts) -> tuple[int, int]:
    """(税抜の本体価格, 建物の消費税)。消費税は業者売主の建物だけ。"""
    tax = f.price_tax or 0
    return f.price - tax, tax


_LAND_DATA: Optional[ev.LandData] = None


def land_data() -> ev.LandData:
    global _LAND_DATA
    if _LAND_DATA is None:
        _LAND_DATA = ev.LandData.load()
    return _LAND_DATA


@dataclass
class EvalInfo:
    land: Optional[int]
    bldg: Optional[int]
    estimated: bool
    price_based: bool          # 物件価格からの粗い推計を使った（地価データがなかった）
    labels: list[str]          # スタッフ向けの説明


def estimate_evals(f: SaleFacts, on: date, data: Optional[ev.LandData] = None) -> EvalInfo:
    """固定資産税評価額（土地・建物）。分かっていればそれを使い、なければ推計する。"""
    A = ASSUMPTIONS
    data = data if data is not None else land_data()
    land, bldg = f.land_eval, f.bldg_eval
    est, price_based, labels = False, False, []
    ex, _ = price_split(f)

    if f.property_type != "land" and bldg is None:
        kind = "kyodo" if f.property_type == "mansion" else "kyotaku"
        bldg = ev.building_eval(f.structure, f.floor_area, f.built, on, kind, f.is_new)
        if bldg is not None:
            est = True
            labels.append(f"建物 約{bldg // 10000:,}万円（東京法務局の認定基準の単価×床面積×経年減価補正率）")

    if land is None and not f.shakuchi:
        le = ev.land_unit(f.address, on, data, commercial=f.commercial_area,
                          rosenka_per_m2=f.rosenka_per_m2, kotei_rosenka_per_m2=f.kotei_rosenka_per_m2)
        area, area_note = f.land_area, ""
        if f.property_type == "mansion" and le is not None and area is None:
            yo = f.youseki or le.youseki
            area = ev.mansion_land_share(f.floor_area, yo) if f.floor_area else None
            if area:
                area_note = f"、敷地持分は専有面積×1.15÷容積率{yo}%で約{area:.1f}㎡と仮置き"
        if le is not None and area:
            land = round(le.unit * area)
            cap = ex * (0.5 if f.property_type == "mansion" else 0.8)
            if land > cap:
                labels.append("地価公示からの推計が物件価格に比べて高すぎるため、上限をかけています。")
                land = int(cap)
            est = True
            labels.append(f"土地 約{land // 10000:,}万円（{le.label}、1㎡あたり約{le.unit:,}円{area_note}）")
        else:
            # 地価データがない・面積がない → 物件価格からの粗い推計
            if f.property_type == "land":
                land = int(ex * A["land_eval_ratio"])
            elif f.property_type == "mansion":
                land = int(max(ex * A["mansion_eval_total_ratio"] - (bldg or 0), ex * A["mansion_land_min_ratio"]))
            else:
                land = int(max(0, ex - (bldg or 0) * A["bldg_market_mult"]) * A["land_eval_ratio"])
            est, price_based = True, True
            labels.append(f"土地 約{land // 10000:,}万円（近くの地価データが使えないため、物件価格からの粗い推計）")
    return EvalInfo(land, bldg if f.property_type != "land" else None, est, price_based, labels)


def jutaku_reg_ok(f: SaleFacts, staff: list[str]) -> Optional[bool]:
    """登録免許税の住宅用家屋の軽減に当たりそうか。True/False/None(要確認)。
    要件：自己居住、登記簿の床面積50㎡以上、新耐震（1982年以後の新築か証明書）、取得後1年以内の登記。"""
    if f.floor_area is None:
        staff.append("床面積が図面にないため、登録免許税の住宅用家屋の軽減を判定できません。")
        return None
    area = f.floor_area
    if f.property_type == "mansion" and f.floor_area_is_wall_center:
        if area < 50:
            return False
        if area < 55:
            staff.append(f"専有面積{area:.2f}㎡は壁芯面積です。登記簿（内法）では50㎡を下回る場合があり、登録免許税の軽減が使えないことがあります。登記簿面積をご確認ください。")
            return None
    elif area < 50:
        return False
    if not f.is_new:
        if f.built is None:
            staff.append("築年月が図面にないため、新耐震（1982年以後の新築）かどうか判定できません。")
            return None
        if f.built < SHINTAISHIN:
            staff.append("1981年以前の新築のため、登録免許税・不動産取得税の住宅の軽減には耐震基準適合証明書などが必要です。")
            return None
    return True


def compute(f: SaleFacts, s: CompanySaleSettings, loan: Optional[LoanPlan] = None,
            contract_day: Optional[date] = None, handover_day: Optional[date] = None,
            today: Optional[date] = None, land_data_override: Optional[ev.LandData] = None) -> Result:
    today = today or date.today()
    contract_day = contract_day or today
    handover_day = handover_day or (today + timedelta(days=60))
    lines: list[Line] = []
    notes: list[str] = []
    staff: list[str] = []

    ex, ctax = price_split(f)
    if f.seller_is_business and f.property_type != "land" and f.price_tax is None:
        staff.append("売主が業者のようですが、価格の「うち消費税」が読み取れません。仲介手数料と印紙税は税込価格で計算しています（多めの目安）。")
    if f.seller_is_business is None:
        staff.append("売主が個人か業者か図面から読み取れません（建物の消費税・契約不適合責任に関係します）。")

    # 1. 仲介手数料
    fee, fee_note = brokerage_fee(ex, s)
    lines.append(Line("must", "仲介手数料", fee, "exact" if fee is not None else "check", fee_note))

    # 2. 印紙税（売買契約書）。消費税額が書いてあれば税抜の金額で判定
    stamp_reduced = is_reduced("stamp_sale_reduced_until", contract_day)
    st = stamp_tax(ex, stamp_reduced)
    lines.append(Line("must", "印紙税（売買契約書）", st, "exact",
                      "軽減措置後の額" if stamp_reduced else "本則の額（軽減措置の期限後）"))
    if not stamp_reduced:
        notes.append("契約予定日が印紙税の軽減措置の期限（2027年3月31日）より後のため、本則の税額で計算しています。")

    # 3. 登記費用（所有権）
    evi = estimate_evals(f, handover_day, land_data_override)
    land_eval, bldg_eval, estimated = evi.land, evi.bldg, evi.estimated
    if estimated and not s.estimate_eval:
        land_eval = f.land_eval
        bldg_eval = f.bldg_eval
        estimated = False
    jutaku = None if f.property_type == "land" else jutaku_reg_ok(f, staff)

    reg_total = 0
    reg_known = True
    reg_parts = []
    expired = []
    if not f.shakuchi:
        if land_eval is None:
            reg_known = False
        else:
            r = pick("reg_land_transfer", handover_day)
            reg_total += reg_tax(land_eval, r["rate"])
            reg_parts.append("土地" + r["label"].split("（")[0])
            if r["end"] is None:
                expired.append("土地の所有権移転")
    if f.property_type != "land":
        if bldg_eval is None:
            reg_known = False
        else:
            if f.is_new and f.seller_is_business:
                key = "reg_house_preserve"
                if f.choki_yuryo and pick("reg_choki_preserve", handover_day):
                    key = "reg_choki_preserve"
                elif f.teitanso and pick("reg_teitanso", handover_day):
                    key = "reg_teitanso"
            else:
                key = "reg_house_transfer"
                if f.choki_yuryo and pick("reg_choki_transfer_" + ("house" if f.property_type == "house" else "mansion"), handover_day):
                    key = "reg_choki_transfer_" + ("house" if f.property_type == "house" else "mansion")
                elif f.teitanso and pick("reg_teitanso", handover_day):
                    key = "reg_teitanso"
                elif f.kaitori_saihan and pick("reg_kaitori_saihan", handover_day):
                    key = "reg_kaitori_saihan"
            if jutaku is True:
                r = pick(key, handover_day)
                if r is None or r["end"] is None:
                    r = pick("reg_house_preserve" if key == "reg_house_preserve" else "reg_house_transfer", handover_day)
            else:
                # 軽減が使えない・判定できないときは本則で（多めの目安）
                r = {"rate": 0.004 if key in ("reg_house_preserve", "reg_choki_preserve") or (key == "reg_teitanso" and f.is_new) else 0.02,
                     "label": "本則", "end": None}
            reg_total += reg_tax(bldg_eval, r["rate"])
            reg_parts.append("建物" + r["label"].split("（")[0])
            if r["end"] is None and jutaku is True:
                expired.append("建物の登記")
    fee_shiho = s.shihoshoshi_transfer + s.touki_jippi + (s.shihoshoshi_preserve if (f.is_new and f.seller_is_business and f.property_type != "land") else 0)
    if reg_known:
        lines.append(Line("must", "登記費用（所有権）", ceil_k(reg_total + fee_shiho), "estimate",
                          "登録免許税＋司法書士報酬の目安" + ("（評価額は推計）" if estimated else "")))
    else:
        lines.append(Line("must", "登記費用（所有権）", None, "check", "固定資産税評価額が分かり次第お知らせします"))
        staff.append("固定資産税評価額が分からないため、登録免許税を計算していません（評価証明書・公課証明書で確認）。")
    if estimated:
        if evi.price_based:
            notes.append("固定資産税評価額が分からないため、登記費用・税金は物件価格からの推計です。実際の評価額で変わります。")
        else:
            notes.append("固定資産税評価額が分からないため、登記費用・税金は近くの地価公示や建物の築年数などからの推計です。実際の評価額で変わります。")
        staff.append("評価額は推計です。" + " ".join(evi.labels) +
                     " 評価証明書や納税通知書の「価格」欄の数字、または路線価をLINEで送っていただければ、その値で作り直します（件数に数えません）。")
    if jutaku is None and f.property_type != "land":
        notes.append("建物の登録免許税は、軽減措置を使わない場合の額で多めに見ています。")
    if expired:
        notes.append("引渡予定日が軽減措置の期限後のため、" + "・".join(expired) + "は本則の税率で計算しています。")

    # 4. 固定資産税・都市計画税の清算金（引渡日から年末まで）
    annual = f.annual_property_tax
    tax_est = False
    if annual is None and (land_eval is not None or bldg_eval is not None):
        A = ASSUMPTIONS
        lv = land_eval or 0
        bv = bldg_eval or 0
        if f.property_type == "land":
            land_part = lv * (A["kotei_rate"] + A["toshi_rate"]) * 0.7   # 更地は住宅用地の特例なし（負担調整は粗く7割）
        else:
            land_part = lv * (A["kotei_rate"] / 6 + A["toshi_rate"] / 3)  # 小規模住宅用地
        annual = land_part + bv * (A["kotei_rate"] + A["toshi_rate"])
        if f.is_new:
            annual = None  # 新築は年の途中で課税が始まるため清算しないことが多い
        tax_est = True
    if f.is_new and f.seller_is_business:
        lines.append(Line("must", "固定資産税等の清算金", None, "check", "新築は売主との取り決めによります"))
    elif annual is None:
        lines.append(Line("must", "固定資産税等の清算金", None, "check", "年税額が分かり次第お知らせします"))
    else:
        start = D(handover_day.year, s.kisanbi_month, 1)
        year_end = D(start.year + 1, start.month, 1) - timedelta(days=1)
        if handover_day < start:
            start = D(handover_day.year - 1, s.kisanbi_month, 1)
            year_end = D(handover_day.year, s.kisanbi_month, 1) - timedelta(days=1)
        days_year = (year_end - start).days + 1
        days_buyer = (year_end - handover_day).days + 1
        amt = annual * days_buyer / days_year
        lines.append(Line("must", "固定資産税等の清算金", ceil_k(amt) if tax_est else int(amt), "estimate",
                          f"引渡日から{year_end.month}月{year_end.day}日まで{days_buyer}日分" + ("（年税額は推計）" if tax_est else "")))

    # 5. 管理費・修繕積立金の清算（引渡月の日割）
    if f.property_type == "mansion":
        monthly = (f.kanri_hi or 0) + (f.shuzen_hi or 0) + (f.other_monthly or 0)
        if f.kanri_hi is None and f.shuzen_hi is None:
            lines.append(Line("must", "管理費等の清算金", None, "check", "管理費・修繕積立金の月額をご確認ください"))
        else:
            dim = calendar.monthrange(handover_day.year, handover_day.month)[1]
            days = dim - handover_day.day + 1
            lines.append(Line("must", "管理費等の清算金", ceil_k(monthly * days / dim), "estimate",
                              f"月額{monthly:,}円の引渡月{days}日分（翌月分の前払いが必要な場合があります）"))
        if f.shuzen_kikin:
            lines.append(Line("must", "修繕積立基金", f.shuzen_kikin, "exact", "図面記載の額"))
        if f.kanri_junbikin:
            lines.append(Line("must", "管理準備金", f.kanri_junbikin, "exact", "図面記載の額"))

    # 6. 住宅ローン関連
    if loan:
        lines.append(Line("loan", "印紙税（ローン契約書）",
                          0 if loan.electronic_contract else stamp_tax(loan.amount, False), "estimate",
                          "電子契約の場合はかかりません"))
        if loan.fee_type == "teiritsu":
            lines.append(Line("loan", "ローン事務手数料", int(loan.amount * loan.fee_rate), "estimate",
                              f"借入額の{loan.fee_rate*100:.1f}%（金融機関により異なります）"))
        else:
            lines.append(Line("loan", "ローン事務手数料", loan.fixed_fee, "estimate", "金融機関により異なります"))
            lines.append(Line("loan", "ローン保証料", ceil_k(loan.amount / 10_000_000 * loan.hoshoryo_per_10m), "estimate",
                              "35年・一括前払いの場合の目安"))
        if jutaku is True:
            r = pick("reg_mortgage", handover_day)
        else:
            r = {"rate": 0.004, "end": None}
        lines.append(Line("loan", "登記費用（抵当権設定）",
                          ceil_k(reg_tax(loan.amount, r["rate"]) + s.shihoshoshi_mortgage), "estimate",
                          "登録免許税＋司法書士報酬の目安"))
        fi = s.fire_insurance.get(f.property_type, 0)
        if fi:
            lines.append(Line("loan", "火災保険・地震保険", fi, "estimate", "5年分の目安（補償内容で変わります）"))
        notes.append(f"住宅ローンは借入額{loan.amount // 10000:,}万円とした場合の目安です。審査や条件は金融機関によって異なります。")

    # 7. 後日かかる税金（参考）：不動産取得税
    acq = acquisition_tax(f, land_eval, bldg_eval, handover_day, jutaku, staff)
    lines.append(Line("later", "不動産取得税", acq, "estimate" if acq is not None else "check",
                      ("要件を満たせば軽減措置でかからない見込みです（要件の確認が必要です）" if acq == 0
                       else ("住宅を新築すると軽減を受けられる場合があります（期限などの要件あり）" if f.property_type == "land"
                             else "引渡しの数か月後に都税事務所から納税通知書が届きます")) if acq is not None
                      else "軽減措置の要件をご確認のうえお知らせします"))

    total_must = sum(l.amount or 0 for l in lines if l.group == "must")
    total_loan = sum(l.amount or 0 for l in lines if l.group == "loan")
    total_later = sum(l.amount or 0 for l in lines if l.group == "later")
    return Result(lines, notes, staff, {"land": land_eval, "bldg": bldg_eval, "estimated": estimated},
                  total_must, total_loan, total_later)


def acquisition_tax(f: SaleFacts, land_eval: Optional[int], bldg_eval: Optional[int], on: date,
                    jutaku: Optional[bool], staff: list[str]) -> Optional[int]:
    """不動産取得税の目安。個人が自分で住むことが前提。要件が判定できないときは None。"""
    rate_lh = pick("acq_rate_land_house", on)["rate"]
    half = pick("acq_takuchi_half", on)["rate"]
    min_floor = pick("acq_min_floor", on)["rate"]
    total = 0

    # 建物
    bldg_ok = None
    if f.property_type != "land":
        if bldg_eval is None or f.floor_area is None:
            return None
        area = f.floor_area
        if f.property_type == "mansion" and f.floor_area_is_wall_center and area < min_floor + 5:
            staff.append(f"専有面積{area:.2f}㎡（壁芯）は、不動産取得税の軽減の床面積{min_floor:.0f}㎡を登記簿面積で下回るかもしれません。")
        if area < min_floor or area > 240:
            bldg_ok = False
        if f.is_new:
            deduction = 13_000_000 if f.choki_yuryo else 12_000_000
            exempt = pick("acq_exempt_bldg_new", on)["rate"]
        else:
            if f.built is None:
                return None
            deduction = 0
            for start, d in USED_HOUSE_DEDUCTION:
                if f.built >= start:
                    deduction = d
                    break
            if f.built < SHINTAISHIN:
                staff.append("1981年以前の新築のため、不動産取得税の軽減は耐震基準適合証明書などの有無で変わります（金額は出していません）。")
                return None
            exempt = pick("acq_exempt_bldg_other", on)["rate"]
        bldg_ok = True if bldg_ok is None else bldg_ok
        base = max(0, bldg_eval - (deduction if bldg_ok else 0))
        if floor_unit(base, 1000) >= exempt:
            total += acq_tax(base, rate_lh)

    # 土地
    if land_eval is not None and not f.shakuchi:
        base = land_eval * half
        tax = acq_tax(base, rate_lh)
        if f.property_type != "land" and bldg_ok:
            # 住宅用土地の減額：45,000円（税率3%のとき）と、1㎡あたり評価額×1/2×床面積の2倍（200㎡まで）×税率の大きい方
            gen = 1_500_000 * rate_lh
            if f.land_area:
                unit = land_eval / f.land_area * half
                gen = max(gen, unit * min(f.floor_area * 2, 200) * rate_lh)
            elif f.property_type == "mansion":
                gen = max(gen, tax)  # 敷地持分は床面積の2倍より小さいことが多い→減額で0になる見込み
                staff.append("敷地持分の面積が分からないため、土地の不動産取得税は減額で0円になる前提で見ています（要確認）。")
            tax = max(0, tax - int(gen))
        if floor_unit(base, 1000) >= pick("acq_exempt_land", on)["rate"]:
            total += floor_unit(tax, 100)
    elif land_eval is None and f.property_type != "mansion" and not f.shakuchi:
        return None

    return ceil_k(total) if total else 0
