"""売買版：本物のAIの読み取り結果（measure_sale.py の kekka.json）を正解表で採点し、Markdownの表を出す。
使い方：python tests/score_sale.py kekka.json seikai.json > saiten.md
正解表は社内限り（実物の図面の内容）なので、リポジトリには入れない。"""
import json
import sys
import unicodedata


def norm(x) -> str:
    if isinstance(x, list):
        x = " ".join(map(str, x))
    t = unicodedata.normalize("NFKC", str(x if x is not None else ""))
    return t.replace(" ", "").replace(",", "").replace("第一種", "第1種").replace("一種", "1種")


def get(d, path):
    for k in path.split("."):
        d = (d or {}).get(k) if isinstance(d, dict) else None
    return d


def ok(want, got) -> bool:
    if want is None or isinstance(want, bool) or isinstance(want, int):
        return got == want
    if isinstance(want, list):
        return all(norm(w) in norm(got) for w in want)
    return norm(want) in norm(got)


def main(kekka_path, seikai_path):
    rows = json.load(open(kekka_path, encoding="utf-8"))
    seikai = json.load(open(seikai_path, encoding="utf-8"))
    print("# 読み取りの採点\n")
    print("| 図面 | 思考 | 項目の正解 | 判定 | 注意 | 消してはいけない | 秒（全体） | 原価（円） |")
    print("|---|---|---|---|---|---|---|---|")
    detail = []
    for r in rows:
        key = r["図面"][:2]
        s = seikai.get(key)
        if not s:
            continue
        if "エラー" in r:
            print(f"| {key} | {r['思考']} | エラー：{r['エラー']} | | | | {r['秒_全体']} | |")
            continue
        f = r["result"]["事実"]
        miss = [k for k, w in s["必須"].items() if not ok(w, get(f, k))]
        notes = norm(r["result"].get("スタッフへの注意"))
        note_miss = [w for w in s["注意に出てほしい"] if norm(w) not in notes]
        hidden = norm(r["log"].get("白で消した文字"))
        bad_hide = [w for w in s["消してはいけない"] if norm(w) in hidden]
        n = len(s["必須"])
        lg = r["log"]
        print(f"| {key} | {r['思考']} | {n - len(miss)}/{n} | {'○' if r['判定'] == s['判定'] else '×' + r['判定']} | "
              f"{'○' if not note_miss else '×'} | {'○' if not bad_hide else '×'} | {r['秒_全体']} | {lg.get('原価_円', '')} |")
        for k in miss:
            detail.append(f"- {key}（{r['思考']}）{k}：正解「{s['必須'][k]}」／AI「{get(f, k)}」")
        for w in note_miss:
            detail.append(f"- {key}（{r['思考']}）スタッフへの注意に「{w}」が出ていない")
        for w in bad_hide:
            detail.append(f"- {key}（{r['思考']}）消してはいけない「{w}」を白で消した")
    print("\n## 違っていた項目\n")
    print("\n".join(detail) if detail else "なし")


if __name__ == "__main__":
    main(*sys.argv[1:3])
