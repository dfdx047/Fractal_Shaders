#!/bin/bash
# Publishes the upload Worker to the Cloudflare account wrangler is logged in to.
# 1st time: creates the KV namespace and asks for the GitHub token (paste it; it is stored as a Worker secret).
set -e
cd "$(dirname "$0")"
export TMPDIR="${TMPDIR:-$HOME/AndroidStudioProjects/Fractal-trabalho/tmp}"
W="npx --yes wrangler@3"
if grep -q KV_NAMESPACE_ID wrangler.toml; then
  id=$($W kv namespace create fractal-shaders-kv 2>&1 | tee /dev/stderr | grep -o 'id = "[0-9a-f]*"' | grep -o '[0-9a-f]\{32\}')
  [ -n "$id" ] || { echo "could not create the KV namespace"; exit 1; }
  sed -i "s/KV_NAMESPACE_ID/$id/" wrangler.toml
fi
$W deploy
if [ "${1:-}" = "--token" ] || ! $W secret list 2>/dev/null | grep -q GITHUB_TOKEN; then
  echo; echo "Paste the GitHub token for dfdx047/Fractal_Shaders (input hidden), then Enter:"
  $W secret put GITHUB_TOKEN
fi
echo; echo "Done: https://fractal-shaders.$( $W whoami 2>/dev/null | grep -o '[a-z0-9-]*\.workers\.dev' | head -1 || echo '<subdomain>.workers.dev')"
