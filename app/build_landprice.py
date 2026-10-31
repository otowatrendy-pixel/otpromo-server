"""evaluation.py が使うデータを作る（国土数値情報からダウンロードして小さなJSONにする）。

使い方（ネットにつながる場所で。Cloud Shell など）:
    python3 build_landprice.py --years 2023 2026 --prefs 13 14 11 12 --isj-version 19.0
出力: landprice_points.json, chome_points.json（このファイルと同じフォルダ）

- 地価公示 L01 / 都道府県地価調査 L02（2019年以降は CC BY 4.0）
- 位置参照情報（大字・町丁目レベル）… 町丁目の代表点（緯度経度）
- 年1回（地価公示は3月、地価調査は9月に公開）作り直す。評価替えの基準年（2023年、次は2026年）の分が要る。
- 2026-10-31時点、このクラウド作業環境からは nlftp.mlit.go.jp に直接つながらない（ノートPCのブラウザからは取得できることを確認済み）。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from evaluation import split_address, normalize  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PREF_NAME = {"11": "埼玉県", "12": "千葉県", "13": "東京都", "14": "神奈川県"}


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def geojson_from_zip(b: bytes) -> dict:
    z = zipfile.ZipFile(io.BytesIO(b))
    name = next(n for n in z.namelist() if n.endswith(".geojson"))
    return json.loads(z.read(name).decode("utf-8"))


def points(kind: str, year: int, pref: str) -> list[dict]:
    yy = str(year)[2:]
    url = f"https://nlftp.mlit.go.jp/ksj/gml/data/{kind}/{kind}-{yy}/{kind}-{yy}_{pref}_GML.zip"
    g = geojson_from_zip(fetch(url))
    out = []
    for f in g["features"]:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"][:2]
        # 項目番号（2023年版で確認）：L01 は _001用途・_006価格・_024所在・_057容積率、
        #                           L02 は _001用途・_006価格・_022所在・_051容積率
        f_addr, f_youseki = ("024", "057") if kind == "L01" else ("022", "051")
        f_eki, f_youto = ("049", "050") if kind == "L01" else ("045", "046")   # 駅距離(m)・用途地域
        use = p.get(f"{kind}_001")
        price = p.get(f"{kind}_006")
        addr = (p.get(f"{kind}_{f_addr}") or "").replace(PREF_NAME[pref], "").replace("　", "")
        youseki = p.get(f"{kind}_{f_youseki}")
        if not price or not addr:
            continue
        city, town, chome = split_address(addr)
        out.append({"lon": round(lon, 5), "lat": round(lat, 5), "price": int(price), "use": str(use),
                    "city": city, "town": town, "chome": chome,
                    "youseki": int(youseki) if isinstance(youseki, (int, float)) and youseki > 0 else None,
                    "year": year,
                    "eki_m": int(p[f"{kind}_{f_eki}"]) if isinstance(p.get(f"{kind}_{f_eki}"), (int, float)) else None,
                    "youto": (p.get(f"{kind}_{f_youto}") or None) if p.get(f"{kind}_{f_youto}") not in ("_", "") else None})
    return out


def chome_points(pref: str, ver: str) -> dict:
    url = f"https://nlftp.mlit.go.jp/isj/dls/data/{ver}b/{pref}000-{ver}b.zip"
    z = zipfile.ZipFile(io.BytesIO(fetch(url)))
    name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
    text = z.read(name).decode("cp932")
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        city = normalize(row["市区町村名"])
        city_, town, chome = split_address(PREF_NAME[pref] + city + normalize(row["大字町丁目名"]))
        out[f"{city_}|{town}|{chome or ''}"] = [round(float(row["経度"]), 6), round(float(row["緯度"]), 6)]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2023, 2026])
    ap.add_argument("--prefs", nargs="+", default=["13", "14", "11", "12"])
    ap.add_argument("--isj-version", default="19.0")
    a = ap.parse_args()
    pts = []
    for y in a.years:
        for pref in a.prefs:
            for kind in ("L01", "L02"):
                try:
                    got = points(kind, y, pref)
                    pts += got
                    print(kind, y, pref, len(got))
                except Exception as e:  # まだ公開されていない年など
                    print("skip", kind, y, pref, e)
    ch = {}
    for pref in a.prefs:
        try:
            ch.update(chome_points(pref, a.isj_version))
        except Exception as e:
            print("skip isj", pref, e)
    credit = "国土数値情報（地価公示データ・都道府県地価調査データ・位置参照情報）（国土交通省）を加工して作成"
    json.dump({"credit": credit, "points": pts}, open(os.path.join(HERE, "landprice_points.json"), "w", encoding="utf-8"), ensure_ascii=False)
    json.dump({"credit": credit, "chome": ch}, open(os.path.join(HERE, "chome_points.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print("points", len(pts), "chome", len(ch))


if __name__ == "__main__":
    main()
