#!/bin/bash
# 売買版の読み取り精度・原価・時間の測定（Cloud Shell で実行）。
# 事前に ~/sokutei/ に試験用の販売図面（PDFか写真）を入れておく。賃貸版の作業フォルダ（~/otpromo-server）は変更しない。
set -euo pipefail
DATA=~/sokutei
SRC=~/sokutei-src
ls "$DATA"/*.* >/dev/null 2>&1 || { echo "~/sokutei/ に販売図面を入れてから、もう一度実行してください"; exit 1; }
echo "1/4 売買版のプログラムを用意しています（賃貸版のフォルダはそのまま）"
git -C ~/otpromo-server fetch -q origin claude/baibai-ai
rm -rf "$SRC"; git -C ~/otpromo-server worktree prune
git -C ~/otpromo-server worktree add -q --detach "$SRC" FETCH_HEAD
echo "2/4 動かす環境を作っています（初回は5分ほど）"
docker build -q -t sale-sokutei "$SRC" >/dev/null
echo "3/4 AIで読み取っています（図面1件につき2回）"
KEY=$(gcloud secrets versions access latest --secret=MAISOKU_API_KEY --project=otpromo-maisoku)
docker run --rm -e MAISOKU_API_KEY="$KEY" -e STORE=local -v "$DATA":/data -v "$SRC/tests":/tests sale-sokutei \
  python /tests/measure_sale.py /data
unset KEY
echo "4/4 結果をまとめています"
cd "$DATA" && rm -f kekka.zip && zip -qr kekka.zip kekka
git -C ~/otpromo-server worktree remove --force "$SRC" || true
cloudshell download "$DATA/kekka.zip" 2>/dev/null || echo "右上の︙→ダウンロード で ~/sokutei/kekka.zip を保存してください"
echo "完了しました。ダウンロードされた kekka.zip を、Claudeのスレッドに送ってください。"
