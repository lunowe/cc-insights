#!/bin/bash
# Install (or remove) the CC-Insights background job, from a source checkout.
#
#   ./scripts/install-launchd.sh             every 15 minutes (the default)
#   ./scripts/install-launchd.sh --watch     a process that follows the logs
#   ./scripts/install-launchd.sh --uninstall stop and remove both
#
# This is a wrapper. The install logic lives in `cci install`, because it has
# to work for someone who ran `pipx install cc-insights` and has no checkout
# to run a script from -- and two implementations of "which job is loaded"
# would eventually disagree about the one thing that must not be wrong.
#
# What the wrapper adds is finding `cci` when it is not on PATH, which in a
# checkout it usually is not: `pip install -e .` puts it in .venv/bin and
# nothing has activated that venv.
#
# Either way the point is the same: agent log directories are pruned on a
# rolling basis, so history that is not captured is lost permanently.
#
# It touches nothing outside ~/Library/LaunchAgents and your CC-Insights
# config directory, and it reads your agent logs read-only.

set -euo pipefail

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

case "${1:-}" in
    --uninstall) exec "$CCI" install --uninstall ;;
    --watch)     exec "$CCI" install --watch ;;
    "")          exec "$CCI" install ;;
    *)
        echo "usage: $0 [--watch | --uninstall]" >&2
        exit 2
        ;;
esac
