#!/bin/bash
# 売買版のサーバー（APP_MODE=sale）を Cloud Run（東京）に載せる。Cloud Shell で実行。
#   本番：bash deploy_sale.sh            （サービス otpromo-api-sale・Firestore sale）
#   試験：bash deploy_sale.sh stg        （サービス otpromo-api-sale-stg・Firestore sale-stg・Stripeはテストモード・FAXは自社だけ）
#   初回の準備：bash deploy_sale.sh setup / bash deploy_sale.sh stg setup
# 賃貸版（deploy.sh・otpromo-api）には一切さわらない。秘密の値は名前の頭で分ける（本番 SALE_ / 試験 STG_SALE_）。
set -euo pipefail
PROJECT=otpromo-maisoku
REGION=asia-northeast1
SA=otpromo-api@${PROJECT}.iam.gserviceaccount.com
if [ "${1:-}" = "stg" ]; then
  STAGE=stg; shift
  SERVICE=otpromo-api-sale-stg; DB=sale-stg; BUCKET=otpromo-maisoku-sale-stg-files; P=STG_SALE_
  MAXI=1
else
  STAGE=""; SERVICE=otpromo-api-sale; DB=sale; BUCKET=otpromo-maisoku-sale-files; P=SALE_
  MAXI=3
fi
gcloud config set project $PROJECT >/dev/null
# 売買版で別にする秘密（LINEは売買版専用の公式アカウント、Stripeは試験だけテストモードのキー）
OWN="LINE_CHANNEL_SECRET LINE_CHANNEL_ACCESS_TOKEN STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET"
# 賃貸版と同じものを使う秘密（AI・メール・FAX・合言葉）
SHARED="MAISOKU_API_KEY APP_SECRET RESEND_API_KEY FAXIMO_ID FAXIMO_PASSWORD ADMIN_LINE_TOKEN ADMIN_LINE_SECRET ADMIN_USAGE_KEY ADMIN_OPS_KEY"

if [ "${1:-}" = "setup" ]; then
  gcloud firestore databases create --database=$DB --location=$REGION --type=firestore-native 2>/dev/null || true
  gcloud storage buckets create gs://$BUCKET --location=$REGION --uniform-bucket-level-access 2>/dev/null || true
  echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}' > /tmp/lc.json
  gcloud storage buckets update gs://$BUCKET --lifecycle-file=/tmp/lc.json
  gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member=serviceAccount:$SA --role=roles/storage.objectAdmin >/dev/null
  for s in $OWN; do
    gcloud secrets create ${P}$s --replication-policy=automatic 2>/dev/null || true
    gcloud secrets add-iam-policy-binding ${P}$s --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor >/dev/null
  done
  echo "準備ができました。次に秘密の値を登録してください："
  echo "  bash tools/set_secret.sh $(for s in $OWN; do printf '%s ' ${P}$s; done)"
  exit 0
fi

SECRETS=""
for s in $OWN; do
  if gcloud secrets versions list ${P}$s --limit=1 --format='value(name)' 2>/dev/null | grep -q .; then
    SECRETS="${SECRETS}${SECRETS:+,}$s=${P}$s:latest"
  fi
done
for s in $SHARED; do
  if gcloud secrets versions list $s --limit=1 --format='value(name)' 2>/dev/null | grep -q .; then
    SECRETS="${SECRETS}${SECRETS:+,}$s=$s:latest"
  fi
done
URL=$(gcloud run services describe $SERVICE --region=$REGION --format='value(status.url)' 2>/dev/null || true)
ENVS="APP_MODE=sale,FIRESTORE_DB=$DB,RENT_FIRESTORE_DB=(default),BASE_URL=${BASE_URL:-${URL:-https://example.invalid}},GCP_PROJECT=$PROJECT,BUCKET=$BUCKET,MODEL=claude-sonnet-5-5,EFFORT=low"
if [ -n "$STAGE" ]; then
  # 試験：FAXは自社の番号だけ（FAX_ALLOW に入れた番号以外は送らずに「届いた」扱い。空ならどこにも送らない）。
  #   例：FAX_ALLOW="03-0000-0000;03-1111-1111" bash deploy_sale.sh stg　（区切りは ; ）。運営への通知には【試験】が付く
  ENVS="$ENVS,STAGE=stg,FAX_ALLOW=${FAX_ALLOW:-}"
fi
gcloud run deploy $SERVICE --source=. --region=$REGION --service-account=$SA \
  --allow-unauthenticated --no-cpu-throttling --cpu=2 --memory=2Gi --concurrency=8 --timeout=300 \
  --min-instances=0 --max-instances=$MAXI \
  --set-env-vars="^@^${ENVS//,/@}" \
  ${SECRETS:+--set-secrets=$SECRETS}
URL=$(gcloud run services describe $SERVICE --region=$REGION --format='value(status.url)')
if [ -z "${BASE_URL:-}" ]; then
  gcloud run services update $SERVICE --region=$REGION --update-env-vars=BASE_URL=$URL >/dev/null
fi
echo "公開URL: $URL"
echo "LINE Webhook（売買版${STAGE:+の試験用}LINEに設定）: $URL/line/webhook"
echo "Stripe Webhook（${STAGE:+テストモードの}Stripeに設定）: $URL/stripe/webhook"
