"""売買版：本物のAIで販売図面を読み取り、精度・原価・時間を測る（Cloud Shell の Docker で動かす。tests/sokutei.sh から呼ぶ）。
使い方：python measure_sale.py <図面のフォルダ>  → <フォルダ>/kekka/ に結果（kekka.json・資料PDF）
図面1件につき、思考「低」と「中」の2回ずつAIを呼ぶ。"""
import glob
import json
import os
import sys
import time

sys.path.insert(0, "/app")
import sale_core  # noqa: E402
import sale_schema  # noqa: E402

COMPANY = {"会社名": "サンプル不動産株式会社", "電話": "03-0000-9999", "住所": "東京都○○区見本町1-1-1",
           "免許": "東京都知事(1)第99999号", "所属団体": "（公社）見本宅地建物取引業協会会員",
           "担当者名": "見本 太郎", "携帯": "090-0000-0000"}

src = sys.argv[1]
out = os.path.join(src, "kekka")
os.makedirs(out, exist_ok=True)
files = sorted(p for e in ("pdf", "PDF", "jpg", "jpeg", "png", "JPG", "PNG") for p in glob.glob(os.path.join(src, "*." + e)))
if not files:
    sys.exit(f"図面が見つかりません：{src} にPDFか写真を入れてください")
rows = []
for path in files:
    name = os.path.splitext(os.path.basename(path))[0]
    data = open(path, "rb").read()
    for effort in ("low", "medium"):
        sale_core.EFFORT_TEXT = sale_core.EFFORT_IMAGE = effort
        t = time.perf_counter()
        row = {"図面": name, "思考": effort}
        try:
            a = sale_core.analyze(data)
            r = a["result"]
            docs = sale_core.render_documents(COMPANY, a["img"], r, a["pages"], a["log"])
            row.update({"秒_全体": round(time.perf_counter() - t, 1), "判定": sale_core.verdict(r),
                        "log": docs["log"], "result": sale_schema.for_store(r)})
            base = os.path.join(out, f"{name}_{effort}")
            open(base + "_okyaku.pdf", "wb").write(docs["pdf_okyaku"])
            open(base + "_shanai.pdf", "wb").write(docs["pdf_shanai"])
        except Exception as ex:
            row.update({"秒_全体": round(time.perf_counter() - t, 1), "エラー": f"{ex.__class__.__name__}: {ex}"})
        rows.append(row)
        lg = row.get("log", {})
        print(f"{name[:24]:24} {effort:6} {row['秒_全体']:6.1f}秒  AI {lg.get('秒_AI', '-')}秒  "
              f"原価 {lg.get('原価_円', '-')}円  判定 {row.get('判定', row.get('エラー'))}", flush=True)
json.dump(rows, open(os.path.join(out, "kekka.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
total = sum((r.get("log") or {}).get("原価_円") or 0 for r in rows)
print(f"\n合計の原価：約{total:.0f}円（{len(rows)}回）  結果：{out}")
