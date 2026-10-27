#!/bin/bash
# 秘密の値を Secret Manager に登録する。画面には表示されない。
# 使い方: bash tools/set_secret.sh LINE_CHANNEL_SECRET [ほかの名前 ...]
#         名前を省くと MAISOKU_API_KEY・LINE の2つ・STRIPE_SECRET_KEY を順に聞く
set -uo pipefail
names=("$@")
[ ${#names[@]} -eq 0 ] && names=(MAISOKU_API_KEY LINE_CHANNEL_SECRET LINE_CHANNEL_ACCESS_TOKEN STRIPE_SECRET_KEY)
for name in "${names[@]}"; do
  while :; do
    read -r -s -p "【${name}】1回だけ貼り付けて Enter（画面には表示されません）: " v
    echo
    v=$(printf %s "$v" | tr -d '[:space:]')
    [ -z "$v" ] && continue
    # 何回か貼ってしまったときに気づけるよう、先頭と文字数だけ見せて確かめる
    read -r -p "  先頭「${v:0:7}…」全${#v}文字。これで登録しますか？（y=はい / n=やり直す）: " ok
    [ "$ok" = y ] && break
  done
  if printf %s "$v" | gcloud secrets versions add "$name" --data-file=- >/dev/null; then
    echo "  → ${name} を登録しました"
  else
    echo "  → ${name} の登録に失敗しました"
  fi
done
unset v
echo "おわりました"
