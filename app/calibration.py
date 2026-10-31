"""実際の評価額と推計を比べて、地域ごとのずれを補正する（使うほど精度が上がる仕組み）。

保存先はサーバー側（Firestore など）。ここでは保存の形と計算だけを持つ。
  記録の形：{"city": "練馬区", "town": "貫井", "est_unit": 推計の1㎡単価, "actual_unit": 実際の1㎡単価, "at": "2027-01-15"}
  実際の単価＝スタッフが送った土地の評価額 ÷ 土地面積（マンションは敷地持分が分かるときだけ記録する）
"""
from __future__ import annotations

import statistics
from typing import Iterable, Optional

MIN_TOWN, MIN_CITY = 3, 5
CLAMP = (0.6, 1.6)


def make_record(city: str, town: str, est_unit: int, actual_land_eval: int, land_area: Optional[float], at: str) -> Optional[dict]:
    if not land_area or land_area <= 0 or not est_unit:
        return None
    return {"city": city, "town": town, "est_unit": int(est_unit), "actual_unit": int(actual_land_eval / land_area), "at": at}


def factor(records: Iterable[dict], city: str, town: str) -> tuple[float, str]:
    """(補正係数, 説明)。同じ町に3件以上 → 町の中央値、同じ市区町村に5件以上 → 市区町村の中央値、なければ1.0。"""
    recs = [r for r in records if r.get("est_unit") and r.get("actual_unit")]
    t = [r["actual_unit"] / r["est_unit"] for r in recs if r["city"] == city and r["town"] == town]
    if len(t) >= MIN_TOWN:
        return _clamp(statistics.median(t)), f"{town}の実績{len(t)}件"
    c = [r["actual_unit"] / r["est_unit"] for r in recs if r["city"] == city]
    if len(c) >= MIN_CITY:
        return _clamp(statistics.median(c)), f"{city}の実績{len(c)}件"
    return 1.0, ""


def _clamp(x: float) -> float:
    return min(CLAMP[1], max(CLAMP[0], x))
