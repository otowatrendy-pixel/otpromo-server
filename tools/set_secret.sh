#!/bin/bash
# 秘密の値を Secret Manager に登録する。画面には表示されない。
# 使い方: bash tools/set_secret.sh LINE_CHANNEL_SECRET
set -euo pipefail
name=$1
read -r -s -p "${name} の値を貼り付けて Enter を押してください（画面には表示されません）: " v
echo
v=$(printf %s "$v" | tr -d '[:space:]')
if [ -z "$v" ]; then echo "空のため登録しませんでした。"; exit 1; fi
printf %s "$v" | gcloud secrets versions add "$name" --data-file=- >/dev/null
echo "${name} を登録しました（${#v}文字）。"
