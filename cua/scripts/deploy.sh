#!/usr/bin/env bash
# Deploy this repo's firmware to the KVM from an Apple Silicon Mac.
# Starts the x86 build VM (colima profile "x86build", Rosetta) if needed, then
# runs dev_deploy.sh. The debug firmware runs only while this script runs.
# Usage: npm run deploy [-- extra dev_deploy.sh options, e.g. -i to install]
set -euo pipefail
cd "$(dirname "$0")/../.."

# The KVM address: KVM_IP if set, else the one saved in the dashboard (cua/.kvm-device.json),
# else KVM_URL from cua/.env.
if [ -z "${KVM_IP:-}" ] && [ -f cua/.kvm-device.json ]; then
  KVM_IP=$(sed -nE 's#.*"url": *"https?://([^:/"]*).*#\1#p' cua/.kvm-device.json)
fi
if [ -z "${KVM_IP:-}" ] && [ -f cua/.env ]; then
  KVM_IP=$(sed -nE 's#^KVM_URL=https?://([^:/]*).*#\1#p' cua/.env)
fi
: "${KVM_IP:?enter the JetKVM in the dashboard, set KVM_URL in cua/.env or KVM_IP in the environment}"

if ! colima status x86build >/dev/null 2>&1; then
  echo "▶ Starting the x86 build VM (colima x86build) ..."
  if colima list 2>/dev/null | grep -q '^x86build'; then
    colima start x86build
  else
    colima start x86build --arch x86_64 --vm-type vz --vz-rosetta --cpu 6 --memory 8
  fi
fi
export DOCKER_CONTEXT=colima-x86build

exec ./dev_deploy.sh -r "$KVM_IP" "$@"
