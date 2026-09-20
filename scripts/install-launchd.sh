#!/bin/bash
# Install (or remove) the CC-Insights background job.
#
#   ./scripts/install-launchd.sh             every 15 minutes (the default)
#   ./scripts/install-launchd.sh --watch     a process that follows the logs
#   ./scripts/install-launchd.sh --uninstall stop and remove both
#
# Either way the point is the same: agent log directories are pruned on a
# rolling basis, so history that is not captured is lost permanently. The
# interval job runs `cci ingest && cci derive` every 15 minutes and exits;
# --watch leaves one `cci watch` running, which keeps the database seconds
# behind the agents instead of minutes.
#
# INSTALL ONE OR THE OTHER. Both at once means two writers on one database;
# --uninstall removes whichever is loaded, and installing either removes the
# other first.
#
# It touches nothing outside ~/Library/LaunchAgents and your CC-Insights config
# directory, and it reads your agent logs read-only.

set -euo pipefail

MODE="interval"
case "${1:-}" in
    --watch) MODE="watch" ;;
esac

LABEL="com.cc-insights"
WATCH_LABEL="com.cc-insights.watch"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="${CC_INSIGHTS_HOME:-${HOME}/.config/cc-insights}"
LOG_DIR="${CONFIG_DIR}/logs"
AGENTS="${HOME}/Library/LaunchAgents"

if [[ "$MODE" == "watch" ]]; then
    THIS_LABEL="$WATCH_LABEL"; OTHER_LABEL="$LABEL"
else
    THIS_LABEL="$LABEL"; OTHER_LABEL="$WATCH_LABEL"
fi
PLIST_SRC="${SCRIPT_DIR}/${THIS_LABEL}.plist"
PLIST_DST="${AGENTS}/${THIS_LABEL}.plist"

unload() {
    launchctl unload "${AGENTS}/${1}.plist" 2>/dev/null || true
    rm -f "${AGENTS}/${1}.plist"
}

if [[ "${1:-}" == "--uninstall" ]]; then
    unload "$LABEL"
    unload "$WATCH_LABEL"
    echo "removed ${LABEL} and ${WATCH_LABEL}"
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

# Two writers on one database is the failure this prevents.
unload "$OTHER_LABEL"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo "installed ${THIS_LABEL}"
if [[ "$MODE" == "watch" ]]; then
    echo "  runs    : ${CCI} watch --quiet, restarted if it exits"
    echo "  cadence : follows the logs, ~2s behind"
    echo "  logs    : ${LOG_DIR}/watch.log"
else
    echo "  runs    : ${CCI} ingest && ${CCI} derive"
    echo "  every   : 15 minutes (and once now)"
    echo "  logs    : ${LOG_DIR}/ingest.log"
fi
echo "  remove  : $0 --uninstall"
