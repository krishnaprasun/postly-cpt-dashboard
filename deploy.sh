#!/bin/bash
# Trigger a Render deploy of the current main.
#
# This service was created through the Render REST API rather than Render's GitHub App,
# so GitHub has no webhook pointing at it and `git push` on its own may not deploy.
# Push first, then run this. Reads the key from $RENDER_API_KEY or ~/.anthropic/render_key;
# the key is never stored in this repo.
set -euo pipefail
SRV="${RENDER_SERVICE_ID:-srv-da3cttibkg8c738a4nvg}"
KEY="${RENDER_API_KEY:-$(cat "${RENDER_KEY_FILE:-$HOME/.anthropic/render_key}")}"
curl -fsS -X POST "https://api.render.com/v1/services/$SRV/deploys" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"clearCache":"do_not_clear"}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('deploy', d.get('id'), '->', d.get('status'))"
echo "watch: https://dashboard.render.com/web/$SRV"
