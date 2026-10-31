"""固定資産税評価額の推計（売買版 1分資料・購入諸費用の概算書用）

販売図面には固定資産税評価額がまず載っていないため、次の順で推計する。

土地（1㎡あたりの評価額）
  1. 固定資産税路線価が分かる（スタッフが入力）      → そのまま使う
  2. 相続税路線価が分かる（スタッフが入力）          → 路線価 ÷ 0.8 × 0.7
     （相続税路線価は地価公示の8割、固定資産税評価額は7割という考え方）
  3. 近くの地価公示・地価調査（評価替えの基準年の価格） → 価格 × 0.7
     - 町丁目の代表点から800m以内の同じ用途の地点を、近い順に3つまで距離で重み付け
     - 代表点が分からなければ、同じ町名の地点の中央値
     - それもなければ、同じ市区町村の地点の中央値（精度は低い）
  4. どれもなければ None（呼び出し側で価格からの推計に戻る）
  面積：土地・戸建は図面の土地面積。マンションは敷地持分が図面にないことが多いので
        専有面積 × 1.15（共用部分の分）÷ 容積率 で仮置きする（容積率は図面、なければ近くの地点の指定容積率）。

建物
  東京法務局「新築建物課税標準価格認定基準表」（基準年度 令和6年度）の1㎡単価 × 床面積
  × 「経年減価補正率表」（固定資産評価基準の経年減点補正率の平均値）
  ※評価額のない建物を登記するときに法務局が使う公式の算定方法。実際の評価額とは差がある。

データ
  landprice_points.json … build_landprice.py が国土数値情報（地価公示 L01・地価調査 L02、CC BY 4.0）から作る。
  chome_points.json     … 同じく国土数値情報「位置参照情報（大字・町丁目レベル）」から作る（町丁目の代表点）。
  出典表示：「国土数値情報（地価公示データ・都道府県地価調査データ・位置参照情報）（国土交通省）を加工して作成」
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))

# 評価替えの基準年度と、その評価の元になる地価公示の年（1月1日時点）
#   令和6基準年度（2024〜2026年度）← 2023年1月1日の地価公示
#   令和9基準年度（2027〜2029年度）← 2026年1月1日の地価公示
BASE_YEARS = [
    {"from": date(2024, 4, 1), "to": date(2027, 3, 31), "kouji_year": 2023},
    {"from": date(2027, 4, 1), "to": date(2030, 3, 31), "kouji_year": 2026},
]
KOTEI_RATIO = 0.7          # 固定資産税評価額 ≒ 地価公示 × 0.7
ROSENKA_TO_KOTEI = 0.7 / 0.8
NEAR_M = 800
MANSION_COMMON_FACTOR = 1.15

# 東京法務局 新築建物課税標準価格認定基準表（基準年度 令和6年度、1㎡あたり円）
# https://houmukyoku.moj.go.jp/tokyo/page000564.html （2024年4月1日更新）
BLDG_UNIT = {
    "kyotaku": {"wood": 107_000, "light_steel": 121_000, "steel": 130_000, "rc": 166_000},   # 居宅
    "kyodo":   {"wood": 116_000, "light_steel": 121_000, "steel": 130_000, "rc": 166_000},   # 共同住宅
}
# 経年減価補正率表（同ページ 別紙2）。木造は27年以上0.20、非木造は45年以上0.20
WOOD_DECAY = [None, .80, .75, .70, .67, .64, .62, .59, .56, .53, .50, .48, .45, .42, .39, .37, .34, .32,
              .30, .28, .25, .24, .24, .23, .22, .21, .21]
NONWOOD_DECAY = [None, .9579, .9309, .9038, .8803, .8569, .8335, .8100, .7866, .7632, .7397, .7163, .6929,
                 .6695, .6460, .6225, .5992, .5757, .5523, .5288, .5054, .4820, .4585, .4388, .4189, .3992,
                 .3794, .3596, .3398, .3228, .3059, .2916, .2774, .2631, .2488, .2345, .2294, .2243, .2191,
                 .2140, .2089, .2071, .2053, .2036, .2018]


def decay_rate(structure: str, years: int) -> float:
    if years <= 0:
        return 1.0
    if structure == "wood":
        return WOOD_DECAY[years] if years < len(WOOD_DECAY) else 0.20
    return NONWOOD_DECAY[years] if years < len(NONWOOD_DECAY) else 0.20


def building_eval(structure: Optional[str], floor_area: Optional[float], built: Optional[date],
                  on: date, kind: str = "kyotaku", is_new: bool = False) -> Optional[int]:
    if floor_area is None:
        return None
    st = structure or ("rc" if kind == "kyodo" else "wood")
    if st == "src":
        st = "rc"
    unit = BLDG_UNIT[kind][st]
    years = 0
    if not is_new and built is not None:
        years = on.year - built.year - ((on.month, on.day) < (built.month, built.day))
    return int(unit * floor_area * decay_rate(st, years))


# ---------------------------------------------------------------------------
# 土地
# ---------------------------------------------------------------------------

def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").replace(" ", "").replace("　", "")
    kan = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    s = re.sub(r"([一二三四五六七八九十]+)丁目", lambda m: f"{_kan2int(m.group(1), kan)}丁目", s)
    return s


def _kan2int(t: str, kan: dict) -> int:
    if t == "十":
        return 10
    if t.startswith("十"):
        return 10 + kan[t[1]]
    if t.endswith("十"):
        return kan[t[0]] * 10
    if "十" in t:
        a, b = t.split("十")
        return kan[a] * 10 + kan[b]
    return kan[t]


def split_address(addr: str) -> tuple[str, str, Optional[int]]:
    """'東京都練馬区貫井2丁目' → ('練馬区', '貫井', 2)。番地以下は無視。"""
    a = normalize(addr)
    a = re.sub(r"^(東京都|神奈川県|埼玉県|千葉県)", "", a)
    # 政令市の区 → 区 → 市 → 郡の町村 の順に試す（「武蔵村山市」を「武蔵村」と切らないため）
    m = None
    for pat in (r"(.+?市.+?区)(.+)", r"(.+?区)(.+)", r"(.+?市)(.+)", r"(.+?郡.+?[町村])(.+)", r"(.+?[町村])(.+)"):
        m = re.match(pat, a)
        if m:
            break
    if not m:
        return "", a, None
    city, rest = m.group(1), m.group(2)
    m3 = re.match(r"(.+?)(\d+)丁目", rest)
    if m3:
        return city, m3.group(1), int(m3.group(2))
    town = re.split(r"\d", rest)[0]
    return city, town, None


def haversine_m(lon1, lat1, lon2, lat2) -> float:
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


@dataclass
class LandPoint:
    lon: float
    lat: float
    price: int          # 円/㎡
    use: str            # "000"=住宅地、"005"=商業地 など（国土数値情報のコード）
    city: str           # '練馬区'
    town: str           # '貫井'
    chome: Optional[int]
    youseki: Optional[int]
    year: int
    eki_m: Optional[int] = None    # 最寄り駅までの距離（m）
    youto: Optional[str] = None    # 用途地域（"1低専" "1住居" "近商" "商業" など国土数値情報の略称）


class LandData:
    def __init__(self, points: list[LandPoint], chome: dict[str, tuple[float, float]]):
        self.points = points
        self.chome = chome   # '練馬区|貫井|2' → (lon, lat)

    @classmethod
    def load(cls, path_points: str = None, path_chome: str = None) -> "LandData":
        path_points = path_points or os.path.join(HERE, "landprice_points.json")
        path_chome = path_chome or os.path.join(HERE, "chome_points.json")
        pts, ch = [], {}
        if os.path.exists(path_points):
            for r in json.load(open(path_points, encoding="utf-8"))["points"]:
                pts.append(LandPoint(**r))
        if os.path.exists(path_chome):
            ch = {k: tuple(v) for k, v in json.load(open(path_chome, encoding="utf-8"))["chome"].items()}
        return cls(pts, ch)


@dataclass
class LandEstimate:
    unit: int               # 1㎡あたりの固定資産税評価額の推計
    method: str             # 'kotei_rosenka' / 'rosenka' / 'kouji_near' / 'kouji_town' / 'kouji_city'
    label: str              # スタッフ向けの説明
    youseki: Optional[int] = None
    points_used: int = 0


SEARCH_M = 1500
WALK_ADJ_PER_MIN = 0.015     # 駅徒歩1分の差で地価が約1.5%変わるとみなす（補正は±20%まで）


def _walk_adj(walk_prop: Optional[float], p: LandPoint) -> float:
    if walk_prop is None or not p.eki_m:
        return 1.0
    return min(1.2, max(0.8, 1 - WALK_ADJ_PER_MIN * (walk_prop - p.eki_m / 80)))


def _similarity_score(p: LandPoint, dist_m: Optional[float], walk: Optional[float], youto: Optional[str],
                      youseki: Optional[int]) -> float:
    sc = (dist_m / 500) if dist_m is not None else 0.0
    if walk is not None and p.eki_m:
        sc += abs(walk - p.eki_m / 80) / 5
    if youto and p.youto:
        sc += 0 if normalize(youto).startswith(p.youto[:2]) or p.youto in normalize(youto) else 0.5
    if youseki and p.youseki:
        sc += abs(math.log(youseki / p.youseki))
    return sc


def land_unit(address: Optional[str], on: date, data: Optional[LandData], *, commercial: bool = False,
              rosenka_per_m2: Optional[int] = None, kotei_rosenka_per_m2: Optional[int] = None,
              walk_min: Optional[float] = None, youto: Optional[str] = None, youseki: Optional[int] = None,
              calib: float = 1.0) -> Optional[LandEstimate]:
    """1㎡あたりの固定資産税評価額の推計。
    地価公示の地点は「近さ」だけでなく、駅徒歩・用途地域・容積率が似ている地点を優先し、
    駅徒歩の差で補正する。calib は実際の評価額との比較から出した地域ごとの補正（calibration.py）。"""
    if kotei_rosenka_per_m2:
        return LandEstimate(int(kotei_rosenka_per_m2), "kotei_rosenka", f"固定資産税路線価 {kotei_rosenka_per_m2:,}円/㎡")
    if rosenka_per_m2:
        u = round(rosenka_per_m2 * ROSENKA_TO_KOTEI)
        return LandEstimate(u, "rosenka", f"相続税路線価 {rosenka_per_m2:,}円/㎡ ÷0.8×0.7")
    if not address or data is None or not data.points:
        return None
    year = next((b["kouji_year"] for b in BASE_YEARS if b["from"] <= on <= b["to"]), BASE_YEARS[-1]["kouji_year"])
    city, town, chome = split_address(address)
    want = "005" if commercial else "000"
    cand = [p for p in data.points if p.year == year and p.use == want and p.city == city]
    if not cand and want == "005":
        cand = [p for p in data.points if p.year == year and p.use == "000" and p.city == city]
    if not cand:
        return None

    key = f"{city}|{town}|{chome}" if chome else f"{city}|{town}|"
    center = data.chome.get(key)
    method, where = "kouji_city", f"{city}全体"
    pool: list[tuple[Optional[float], LandPoint]] = []
    if center:
        pool = [(haversine_m(center[0], center[1], p.lon, p.lat), p) for p in cand]
        pool = [(d, p) for d, p in pool if d <= SEARCH_M]
        if pool:
            method, where = "kouji_near", "近く"
    if not pool:
        same_town = [p for p in cand if p.town == town]
        if same_town:
            pool, method, where = [(None, p) for p in same_town], "kouji_town", town
        else:
            pool = [(None, p) for p in cand]

    scored = sorted(((_similarity_score(p, d, walk_min, youto, youseki), d, p) for d, p in pool), key=lambda x: x[0])[:3]
    w = [1 / (sc + 0.2) for sc, _, _ in scored]
    price = sum(wi * p.price * _walk_adj(walk_min, p) for wi, (_, _, p) in zip(w, scored)) / sum(w)
    unit = int(price * KOTEI_RATIO * calib)
    ys = [p.youseki for _, _, p in scored if p.youseki]
    near_txt = f"（最寄り{int(scored[0][1])}m）" if scored[0][1] is not None else ""
    label = (f"{year}年地価公示 {where}の条件が似た{len(scored)}地点{near_txt}"
             + ("・駅徒歩で補正" if walk_min is not None else "") + "×0.7"
             + (f"×地域補正{calib:.2f}" if abs(calib - 1) > 0.005 else "")
             + ("（精度低い）" if method == "kouji_city" else ""))
    return LandEstimate(unit, method, label, ys[0] if ys else None, len(scored))


def mansion_land_share(floor_area: float, youseki_pct: Optional[int]) -> Optional[float]:
    """敷地持分の面積の仮置き：専有面積×1.15÷容積率。"""
    if not floor_area or not youseki_pct:
        return None
    return floor_area * MANSION_COMMON_FACTOR / (youseki_pct / 100)
