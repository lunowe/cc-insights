#!/bin/bash
# Install (or remove) the CC-Insights background ingest job.
#
#   ./scripts/install-launchd.sh            install and start
#   ./scripts/install-launchd.sh --uninstall stop and remove
#
# The job runs `cci ingest && cci derive` every 15 minutes. That is the whole
# point of the project: agent log directories are pruned on a rolling basis, so
# history that is not captured is lost permanently.
#
# It touches nothing outside ~/Library/LaunchAgents and your CC-Insights config
# directory, and it reads your agent logs read-only.

set -euo pipefail

LABEL="com.cc-insights"
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/${LABEL}.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
CONFIG_DIR="${CC_INSIGHTS_HOME:-${HOME}/.config/cc-insights}"
LOG_DIR="${CONFIG_DIR}/logs"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "removed ${LABEL}"
    exit 0
fi

CCI="$(command -v cci || true)"
if [[ -z "$CCI" ]]; then
    VENV_CCI="$(cd "$(dirname "$0")/.." && pwd)/.venv/bin/cci"
    [[ -x "$VENV_CCI" ]] && CCI="$VENV_CCI"
fi
if [[ -z "$CCI" ]]; then
    echo "error: cannot find the 'cci' executable." >&2
    echo "       install the package first:  pip install -e ." >&2
    exit 1
fi

"$CCI" init >/dev/null
mkdir -p "$LOG_DIR" "$(dirname "$PLIST_DST")"

sed -e "s|__CCI__|${CCI}|g" -e "s|__LOGDIR__|${LOG_DIR}|g" "$PLIST_SRC" > "$PLIST_DST"
plutil -lint "$PLIST_DST" >/dev/null

launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo "installed ${LABEL}"
echo "  runs    : ${CCI} ingest && ${CCI} derive"
echo "  every   : 15 minutes (and once now)"
echo "  logs    : ${LOG_DIR}/ingest.log"
echo "  remove  : $0 --uninstall"
