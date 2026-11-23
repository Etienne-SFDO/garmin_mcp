#!/usr/bin/env bash
# Build + deploy garmin-mcp to Cloud Run (read-only tools + create-only workout tools).
#
#   deploy/deploy.sh build    # Cloud Build -> Artifact Registry (tag = git short SHA)
#   deploy/deploy.sh tokens   # upload ~/.garminconnect/garmin_tokens.json to the bucket
#   deploy/deploy.sh deploy   # deploy/update the Cloud Run service
#   deploy/deploy.sh all      # build + deploy
#
# Prereqs (already created once): Artifact Registry repo, private token bucket,
# runtime SA with objectUser on the bucket + accessor on the bearer secret.
set -euo pipefail

PROJECT=garmin-connect-515416
REGION=europe-west2
SERVICE=garmin-mcp
REPO=garmin-mcp
BUCKET=${PROJECT}-tokens
SA=garmin-mcp-run@${PROJECT}.iam.gserviceaccount.com
SECRET=garmin-mcp-http-token
TAG=$(git rev-parse --short HEAD)
# Write tools enabled on top of the read-only set. Create-only workout tools:
# Claude can design, schedule and push workouts but cannot delete, unschedule
# or modify anything existing. Set READ_ONLY=1 to deploy read-only.
WRITE_TOOLS="create_run_workout,create_run_interval_workout,create_strength_workout,\
create_walk_run_workout,create_z2_walk_workout,upload_workout,upload_workouts,\
schedule_workout,schedule_workouts,schedule_week,push_workout_to_device"
[[ "${READ_ONLY:-0}" == 1 ]] && WRITE_TOOLS=""
IMAGE=${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}
cd "$(dirname "$0")/.."

build() {
  if [[ -n "$(git status --porcelain -- src pyproject.toml uv.lock Dockerfile)" ]]; then
    echo "Uncommitted changes in src/deps - commit first so the tag matches." >&2; exit 1
  fi
  gcloud builds submit --tag "$IMAGE" --region "$REGION" --project "$PROJECT" .
}

tokens() {
  local f=${GARMINTOKENS:-$HOME/.garminconnect}/garmin_tokens.json
  [[ -f $f ]] || { echo "No token file at $f - run deploy/one_shot_login.py first." >&2; exit 1; }
  gcloud storage cp "$f" "gs://${BUCKET}/garmin_tokens.json" --project "$PROJECT"
}

deploy() {
  # Allowlist = every tool annotated READ_ONLY + $WRITE_TOOLS, which must all be
  # ADDITIVE (create-only) tools - a typo or a destructive tool aborts the deploy.
  local tools
  tools=$(WRITE_TOOLS="$WRITE_TOOLS" uv run python -I -c "
import os, sys; sys.path.insert(0, 'src')
from garmin_mcp.tool_annotations import READ_ONLY_TOOLS as R, ADDITIVE_TOOLS as A
w = {t for t in os.environ['WRITE_TOOLS'].split(',') if t}
bad = w - A
if bad: sys.exit(f'not create-only tools: {sorted(bad)}')
print(','.join(sorted(R | w)))" 2>/dev/null) || { echo "Bad WRITE_TOOLS list" >&2; exit 1; }
  echo "Enabling $(tr ',' '\n' <<<"$tools" | wc -l | tr -d ' ') tools (write: ${WRITE_TOOLS:-none})"
  local envfile; envfile=$(mktemp)
  cat > "$envfile" <<EOF
GARMIN_MCP_TRANSPORT: streamable-http
GARMIN_MCP_HOST: 0.0.0.0
GARMIN_MCP_PORT: "8080"
GARMIN_MCP_STATELESS_HTTP: "true"
GARMINTOKENS: /tokens
GARMIN_ENABLED_TOOLS: "${tools}"
EOF
  gcloud run deploy "$SERVICE" \
    --image "$IMAGE" --region "$REGION" --project "$PROJECT" \
    --service-account "$SA" \
    --execution-environment gen2 \
    --allow-unauthenticated \
    --port 8080 --min-instances 0 --max-instances 1 \
    --cpu 1 --memory 512Mi --timeout 300 --concurrency 20 \
    --env-vars-file "$envfile" \
    --set-secrets "GARMIN_MCP_HTTP_TOKEN=${SECRET}:latest" \
    --add-volume "name=tokens,type=cloud-storage,bucket=${BUCKET}" \
    --add-volume-mount "volume=tokens,mount-path=/tokens" \
    --startup-probe "httpGet.path=/healthz,httpGet.port=8080,initialDelaySeconds=0,periodSeconds=3,failureThreshold=20"
  rm -f "$envfile"
  gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format='value(status.url)'
}

case "${1:-}" in
  build) build ;;
  tokens) tokens ;;
  deploy) deploy ;;
  all) build; deploy ;;
  *) sed -n 2,8p "$0"; exit 1 ;;
esac
