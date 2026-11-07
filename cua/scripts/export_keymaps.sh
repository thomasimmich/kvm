#!/usr/bin/env bash
# Regenerates src/kvm_cua/data/keymaps.json from the web UI's keyboard layouts,
# so the CUA types text exactly like the browser's paste feature does.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
ui_src="$here/../../ui/src"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
npx -y esbuild@0.25.10 "$here/export_keymaps.ts" --bundle --platform=node \
  --alias:@="$ui_src" --log-level=warning --outfile="$tmp/export.js"
node "$tmp/export.js" > "$here/../src/kvm_cua/data/keymaps.json"
echo "wrote src/kvm_cua/data/keymaps.json"
