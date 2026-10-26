#!/bin/bash
# Cloud Run（東京）に載せる。Cloud Shell で実行: bash deploy.sh
set -euo pipefail
PROJECT=otpromo-maisoku
REGION=asia-northeast1
SERVICE=otpromo-api
BUCKET=otpromo-maisoku-files
SA=otpromo-api@${PROJECT}.iam.gserviceaccount.com
gcloud config set project $PROJECT

# 初回だけ：API・Firestore・保存場所・サービスアカウント
if [ "${1:-}" = "setup" ]; then
  gcloud services enable run.googleapis.com firestore.googleapis.com storage.googleapis.com \
    secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
  gcloud firestore databases create --location=$REGION --type=firestore-native 2>/dev/null || true
  gcloud storage buckets create gs://$BUCKET --location=$REGION --uniform-bucket-level-access 2>/dev/null || true
  # 資料のファイルは30日で自動で消す
  echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}' > /tmp/lc.json
  gcloud storage buckets update gs://$BUCKET --lifecycle-file=/tmp/lc.json
  gcloud iam service-accounts create otpromo-api --display-name="1分資料 API" 2>/dev/null || true
  gcloud projects add-iam-policy-binding $PROJECT --member=serviceAccount:$SA --role=roles/datastore.user --condition=None >/dev/null
  gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member=serviceAccount:$SA --role=roles/storage.objectAdmin >/dev/null
  for s in MAISOKU_API_KEY LINE_CHANNEL_SECRET LINE_CHANNEL_ACCESS_TOKEN STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET APP_SECRET; do
    gcloud secrets create $s --replication-policy=automatic 2>/dev/null || true
    gcloud secrets add-iam-policy-binding $s --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor >/dev/null
  done
  echo "初回の準備が終わりました。秘密の値を登録してから bash deploy.sh を実行してください。"
  exit 0
fi

URL=$(gcloud run services describe $SERVICE --region=$REGION --format='value(status.url)' 2>/dev/null || true)
SECRETS=""
for s in MAISOKU_API_KEY LINE_CHANNEL_SECRET LINE_CHANNEL_ACCESS_TOKEN STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET APP_SECRET; do
  if gcloud secrets versions list $s --limit=1 --format='value(name)' 2>/dev/null | grep -q .; then
    SECRETS="${SECRETS}${SECRETS:+,}$s=$s:latest"
  fi
done
gcloud run deploy $SERVICE --source=. --region=$REGION --service-account=$SA \
  --allow-unauthenticated --no-cpu-throttling --cpu=2 --memory=2Gi --concurrency=8 --timeout=300 \
  --min-instances=0 --max-instances=3 \
  --set-env-vars="BASE_URL=${BASE_URL:-${URL:-https://example.invalid}},GCP_PROJECT=$PROJECT,BUCKET=$BUCKET,MODEL=claude-sonnet-5-5,EFFORT=low" \
  ${SECRETS:+--set-secrets=$SECRETS}
URL=$(gcloud run services describe $SERVICE --region=$REGION --format='value(status.url)')
# 初回は公開URLが決まってから BASE_URL を入れ直す
if [ -z "${BASE_URL:-}" ]; then
  gcloud run services update $SERVICE --region=$REGION --update-env-vars=BASE_URL=$URL >/dev/null
fi
echo "公開URL: $URL"
echo "LINE Webhook: $URL/line/webhook"
echo "Stripe Webhook: $URL/stripe/webhook"
