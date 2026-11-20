#!/usr/bin/env bash
# Deploy this repo's firmware to the KVM from an Apple Silicon Mac.
# Starts the x86 build VM (colima profile "x86build", Rosetta) if needed, then
# runs dev_deploy.sh.
#
# By default the firmware is installed permanently (it survives reboots) and the
# device's auto-update is turned off, so no upstream release replaces it.
# With --temp, a debug build runs only while this script runs; the installed
# firmware is untouched and comes back when the script ends or the device reboots.
#
# Usage: npm run deploy:fix | npm run deploy:temp  [-- extra dev_deploy.sh options]
set -euo pipefail
cd "$(dirname "$0")/../.."

TEMP=false
ARGS=()
for a in "$@"; do
  case "$a" in
    --temp) TEMP=true ;;
    -i|--install) ;; # installing is the default
    *) ARGS+=("$a") ;;
  esac
done
if [ "$TEMP" = false ]; then
  ARGS+=(-i)
fi

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

if [ "$TEMP" = false ]; then
  echo "▶ Installing the firmware permanently; the device reboots when done (use --temp for a test run)"
fi
exec ./dev_deploy.sh -r "$KVM_IP" ${ARGS[@]+"${ARGS[@]}"}
