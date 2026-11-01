"""国税庁「財産評価基準書（路線価図）」の町丁名索引から、町丁目 → 路線価図の図番 の表を作る（年1回）。

    python build_rosenka_index.py --year r08 --prefs tokyo/tokyo tokyo/kanagawa kanto/saitama tokyo/chiba

出力 rosenka_index.json
  {"year": "r08", "made": "2026-..", "source": "...",
   "cities": {"練馬区": {"pref": "tokyo/tokyo", "page": "d25401fr.htm",
                        "towns": {"貫井|2": ["48063", "48073"], "赤井|": [...]}}}}
  政令市の区は「横浜市緑区」「相模原市緑区」のように市の名前を付けて持つ。

使うのは索引ページの文字（町丁名と図番）だけで、地図（背景地図はゼンリンの地図が元）には触れない。
索引に載っていない町・大字は路線価地域ではない（倍率地域）ので、表にも入らない。
出典表示：「出典：国税庁ホームページ 財産評価基準書（https://www.rosenka.nta.go.jp/）を加工して作成」
（公共データ利用規約 第1.0版。国が作ったように見せない）

このクラウド環境からは rosenka.nta.go.jp に繋がらないため、GitHub Actions かノートPCで動かす。
アクセスは1ページごとに間を空ける（相手のサーバーに負担をかけない）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.request
from datetime import date
from html.parser import HTMLParser

BASE = "https://www.rosenka.nta.go.jp"
UA = "Mozilla/5.0 (compatible; rosenka-index-builder; once a year)"
SOURCE = "出典：国税庁ホームページ 財産評価基準書（https://www.rosenka.nta.go.jp/）を加工して作成"


def fetch(url: str, wait: float) -> str:
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            raw = urllib.request.urlopen(req, timeout=30).read()
            time.sleep(wait)
            for enc in ("shift_jis", "cp932", "utf-8"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("cp932", errors="replace")
        except Exception as e:  # noqa
            print("retry", url, e, file=sys.stderr)
            time.sleep(2 ** (i + 1))
    raise RuntimeError(f"取得できません: {url}")


class Rows(HTMLParser):
    """<tr> ごとに [(セルの文字, [href, ...]), ...] を集める。"""

    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.links = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell, self.links = [], []
        elif tag == "a" and self.cell is not None:
            href = dict(attrs).get("href") or ""
            self.links.append(href.split("?")[0])

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(("".join(self.cell).strip(), self.links))
            self.cell, self.links = None, None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def rows_of(html: str):
    p = Rows()
    p.feed(html)
    return p.rows


def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").replace(" ", "").replace("　", "")


def parse_city_list(html: str) -> list[tuple[str, str]]:
    """市区町村一覧 → [(名前, ページ)]。政令市の区は直前の「◯◯市」の見出しを付ける。"""
    out, parent = [], None
    for row in rows_of(html):
        cells = [(norm(t), l) for t, l in row]
        head = cells[0][0] if cells and not cells[0][1] else ""
        linked = [(t, l[0]) for t, l in cells if l and re.match(r"^[a-z]\d{5}fr\.htm$", l[0])]
        if not linked:
            only = [t for t, _ in cells if t]
            if len(only) == 1 and only[0].endswith("市"):
                parent = only[0]
            continue
        if head:            # 五十音の見出し（あ・い…）が出たら政令市の区の並びは終わり
            parent = None
        for name, page in linked:
            out.append(((parent + name) if (parent and name.endswith("区")) else name, page))
    return out


def parse_town_index(html: str) -> dict[str, list[str]]:
    """町丁名索引 → {"貫井|2": [図番...], "赤井|": [...]}。"""
    towns: dict[str, list[str]] = {}
    last = None
    for row in rows_of(html):
        nums, name = [], ""
        for t, links in row:
            got = [m.group(1) for h in links for m in [re.search(r"html/(\d+)f\.htm$", h)] if m]
            if got:
                nums += got
            elif norm(t) and not nums:
                name = norm(t)      # 1列目は五十音の見出し、2列目が町丁名（後の方を使う）
        if not nums:
            continue
        if not name:        # 図番が多くて次の行に続いている
            if last:
                towns[last] += [n for n in nums if n not in towns[last]]
            continue
        name = re.sub(r"^(大字|字)", "", name)
        m = re.match(r"^(.+?)(\d+)(丁目)?$", name)
        key = f"{m.group(1)}|{int(m.group(2))}" if m else f"{name}|"
        towns.setdefault(key, [])
        towns[key] += [n for n in nums if n not in towns[key]]
        last = key
    return towns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", default="r08")
    ap.add_argument("--prefs", nargs="+", default=["tokyo/tokyo", "tokyo/kanagawa", "kanto/saitama", "tokyo/chiba"])
    ap.add_argument("--out", default="rosenka_index.json")
    ap.add_argument("--wait", type=float, default=1.0)
    a = ap.parse_args()
    cities = {}
    for pref in a.prefs:
        root = f"{BASE}/main_{a.year}/{pref}/prices/"
        lst = parse_city_list(fetch(root + "city_frm.htm", a.wait))
        print(pref, len(lst), "市区町村", file=sys.stderr)
        for name, page in lst:
            towns = parse_town_index(fetch(root + page, a.wait))
            key = name if name not in cities else f"{name}@{pref}"
            cities[key] = {"pref": pref, "page": page, "towns": towns}
            print(" ", name, len(towns), file=sys.stderr)
    out = {"year": a.year, "made": date.today().isoformat(), "source": SOURCE, "cities": cities}
    json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    n = sum(len(c["towns"]) for c in cities.values())
    print("done", len(cities), "市区町村", n, "町丁", file=sys.stderr)
    ner = cities.get("練馬区", {}).get("towns", {})
    print("check 練馬区 貫井2 =", ner.get("貫井|2"), file=sys.stderr)


if __name__ == "__main__":
    main()
