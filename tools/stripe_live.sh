#!/bin/bash
# Stripe を本番のキーに切り替える（11/5 11:00 予定）。
# 本番のシークレットキー（sk_live_…）を1回貼り付けると、登録してデプロイし直す。
# 起動すると、サンドボックスで作った契約の記録を1回だけ消し、本番の価格と通知先（Webhook）を自動で作る。
set -uo pipefail
cd "$(dirname "$0")/.."
if gcloud secrets describe STRIPE_WEBHOOK_SECRET >/dev/null 2>&1; then
  echo "注意：STRIPE_WEBHOOK_SECRET が登録されています。サンドボックスの値のままだと本番の通知を受け取れません。Claude に伝えてください。"
  exit 1
fi
read -r -s -p "【本番のシークレットキー】sk_live_ で始まるキーを1回貼り付けて Enter（画面には表示されません）: " v
echo
v=$(printf %s "$v" | tr -d '[:space:]')
case "$v" in
  sk_live_*|rk_live_*) ;;
  *) echo "sk_live_ で始まるキーではありません。やり直してください。"; exit 1 ;;
esac
printf %s "$v" | gcloud secrets versions add STRIPE_SECRET_KEY --data-file=- >/dev/null && echo "→ 本番キーを登録しました"
bash deploy.sh 2>&1 | tail -4
echo "→ 切り替え完了。1分ほどで本番の価格と通知先ができます。"
