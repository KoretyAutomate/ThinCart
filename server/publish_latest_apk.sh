#!/usr/bin/env bash
# Fetch the newest successful CI build of the Android shell and publish it as
# the in-app update: after this, every phone on the tailnet is offered it on
# its next cold start. Run on the DGX after a merge that touched mobile/.
#
#   server/publish_latest_apk.sh [--notes "what changed"]
set -euo pipefail
cd "$(dirname "$0")/.."
run=$(gh run list --workflow=build-apk.yml --branch master --status success --limit 1 --json databaseId --jq '.[0].databaseId')
[ -n "$run" ] || { echo "no successful build-apk run on master"; exit 1; }
tmp=$(mktemp -d)
gh run download "$run" --name thincart-debug --dir "$tmp"
apk=$(find "$tmp" -name '*.apk' | head -1)
[ -n "$apk" ] || { echo "no apk in artifact of run $run"; exit 1; }
echo "run $run -> $apk"
exec /home/korety/miniconda3/bin/python3 server/publish_apk.py "$apk" "$@"
