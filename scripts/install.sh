#!/usr/bin/env bash
#
# CC-Insights, from a machine with nothing on it to a running install:
#
#   curl -fsSL https://raw.githubusercontent.com/lunowe/cc-insights/main/scripts/install.sh | bash
#
#   ... | bash -s -- --watch       follow the logs instead of every 15 minutes
#   ... | bash -s -- --uninstall   remove the job and the managed install
#
# Run from inside a checkout it installs that checkout rather than PyPI. That
# is not a convenience: until the first release is cut there is nothing on
# PyPI to install, and a bootstrap script that only works after the thing it
# bootstraps has shipped is useless on the day you need it.
#
# What it touches: a pipx install, or failing that a venv under
# ~/.local/share/cc-insights and one symlink in ~/.local/bin; then whatever
# `cci install` does -- config, database, first ingest, background job. It
# does not edit your shell rc. When PATH is wrong it prints the line for you
# to paste, because a script that silently appends to a file it did not write
# is a script you cannot undo.
#
# It never reads stdin, and cannot: piped from curl, stdin *is* this script,
# so a single `read -r` would swallow the rest of the file and run half an
# installer. Every choice here is therefore made from flags, the environment,
# or a default -- never from a prompt.

# Before `set -o pipefail`, which is not POSIX: under dash or a stock `sh`
# this file dies on line one with "Illegal option -o pipefail", which tells
# the user nothing about the actual mistake (`| sh` instead of `| bash`).
if [ -z "${BASH_VERSION:-}" ]; then
    echo "cc-insights: this installer needs bash." >&2
    echo "  curl -fsSL <url> | bash        # not | sh" >&2
    exit 1
fi

set -euo pipefail

readonly PACKAGE="cc-insights"
readonly MIN_PYTHON="3.11"

# Overridable so the test suite and anyone with a different layout can point
# this somewhere harmless. Defaults follow the XDG spec on Linux and are
# close enough on macOS, where pipx and pip's --user both already use them.
INSTALL_DIR="${CC_INSIGHTS_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/cc-insights}"
BIN_DIR="${CC_INSIGHTS_BIN_DIR:-$HOME/.local/bin}"
VENV_DIR="$INSTALL_DIR/venv"

if [ -t 1 ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'
    YELLOW=$'\033[33m'; GREEN=$'\033[32m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; YELLOW=""; GREEN=""; RESET=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
note() { printf '    %s%s%s\n' "$DIM" "$*" "$RESET"; }
warn() { printf '%swarning:%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
cc-insights installer

usage: install.sh [--watch] [--uninstall]

  --watch      install the live-follow background job instead of the
               15-minute interval one (passed to `cci install --watch`)
  --uninstall  remove the background job and this installer's managed
               install. Your config and database are kept.
  -h, --help   this

environment:
  CC_INSIGHTS_INSTALL_DIR   managed venv lives here   (~/.local/share/cc-insights)
  CC_INSIGHTS_BIN_DIR       where `cci` is linked     (~/.local/bin)
  CC_INSIGHTS_HOME          config + database         (~/.config/cc-insights)
EOF
}

# ---------------------------------------------------------------- options --

WATCH=0
UNINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --watch)     WATCH=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help)   usage; exit 0 ;;
        *)           usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

if [ "$WATCH" = 1 ] && [ "$UNINSTALL" = 1 ]; then
    die "--watch and --uninstall do opposite things; pick one"
fi

# ----------------------------------------------------------------- python --

python_is_new_enough() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null
}

# The loop is what matters here, not the order: on macOS `python3` is the
# 3.9 Apple ships and the usable one only answers to `python3.12`, so a
# search that took the first `python3` it found and gave up would tell most
# Mac users to install a Python they already have. Newest name first is only
# a preference on top of that.
find_python() {
    local candidate resolved
    for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
        resolved="$(command -v "$candidate" 2>/dev/null)" || continue
        if python_is_new_enough "$resolved"; then
            printf '%s\n' "$resolved"
            return 0
        fi
    done
    return 1
}

linux_distro() {
    [ -r /etc/os-release ] || return 0
    # shellcheck disable=SC1091  # not present at lint time, only at run time
    . /etc/os-release
    printf '%s %s\n' "${ID:-}" "${ID_LIKE:-}"
}

# "Python 3.11+ is required" is true and useless. What unblocks someone is the
# command for the machine they are actually sitting at, so this names one.
python_advice() {
    case "$(uname -s)" in
        Darwin)
            if command -v brew >/dev/null 2>&1; then
                say "  brew install python@3.12"
            else
                say "  install Homebrew from https://brew.sh, then:  brew install python@3.12"
                say "  or take the macOS installer from https://www.python.org/downloads/"
            fi
            ;;
        Linux)
            case " $(linux_distro) " in
                *debian*|*ubuntu*)
                    say "  sudo apt update && sudo apt install -y python3 python3-venv" ;;
                *fedora*|*rhel*|*centos*) say "  sudo dnf install -y python3" ;;
                *arch*)   say "  sudo pacman -S --needed python" ;;
                *suse*)   say "  sudo zypper install -y python311" ;;
                *alpine*) say "  sudo apk add python3" ;;
                *)
                    say "  install python3.11 or newer with your package manager" ;;
            esac
            say "  or, without root:  curl https://pyenv.run | bash && pyenv install 3.12"
            ;;
        MINGW*|MSYS*|CYGWIN*)
            say "  winget install Python.Python.3.12"
            say "  (and prefer a native PowerShell install over this shell)"
            ;;
        *)
            say "  https://www.python.org/downloads/"
            ;;
    esac
}

require_python() {
    local py
    if ! py="$(find_python)"; then
        printf '%serror:%s no Python %s+ found. Install one:\n' \
            "$RED" "$RESET" "$MIN_PYTHON" >&2
        python_advice >&2
        exit 1
    fi
    # Debian ships `venv` as a separate package, and without it `python3 -m
    # venv` fails several steps later with a traceback about ensurepip that
    # reads like a bug in this script. Catch it while we can still name the
    # package to install.
    if ! "$py" -c 'import ensurepip, venv' >/dev/null 2>&1; then
        printf '%serror:%s %s cannot create virtualenvs (no ensurepip).\n' \
            "$RED" "$RESET" "$py" >&2
        case " $(linux_distro) " in
            *debian*|*ubuntu*) say "  sudo apt install -y python3-venv" >&2 ;;
            *) say "  install your distribution's python3-venv / python3-virtualenv package" >&2 ;;
        esac
        exit 1
    fi
    printf '%s\n' "$py"
}

# ---------------------------------------------------------------- the repo --

# `${BASH_SOURCE[0]}` is this file when run as a file and something unusable
# ("bash", "main") when piped from curl, hence the -f test rather than trust.
script_directory() {
    local src="${BASH_SOURCE[0]:-}"
    [ -n "$src" ] && [ -f "$src" ] || return 0
    (cd "$(dirname "$src")" && pwd)
}

# A checkout is where pyproject.toml declares *this* package -- not merely any
# pyproject.toml, or running the installer from inside some unrelated Python
# project one directory below a clone would install that project instead.
find_checkout() {
    local start dir
    for start in "$(script_directory)" "$PWD"; do
        [ -n "$start" ] || continue
        dir="$start"
        while [ "$dir" != "/" ] && [ -n "$dir" ]; do
            if [ -f "$dir/pyproject.toml" ] &&
               grep -q '^name = "cc-insights"' "$dir/pyproject.toml" 2>/dev/null &&
               [ -d "$dir/src/cc_insights" ]; then
                printf '%s\n' "$dir"
                return 0
            fi
            dir="$(dirname "$dir")"
        done
    done
    return 1
}

# Installing a checkout means building a wheel from it, and pyproject.toml
# force-includes `frontend/dist` into the package. That include is not
# optional: hatchling aborts with "Forced include not found", which looks
# like a packaging bug and is really "you have not run the frontend build".
# The same missing directory is why a published wheel must be built after the
# frontend and never before -- see the Releasing section of the README.
ensure_dashboard_is_built() {
    local repo="$1"
    [ -f "$repo/frontend/dist/index.html" ] && return 0

    step "Building the dashboard (it is not committed, and the wheel needs it)"
    if command -v pnpm >/dev/null 2>&1; then
        (cd "$repo/frontend" && pnpm install --frozen-lockfile && pnpm build)
    elif command -v npm >/dev/null 2>&1; then
        (cd "$repo/frontend" && npm install && npm run build)
    else
        die "$repo/frontend/dist is missing and there is no pnpm or npm to build it.
       Install pnpm (https://pnpm.io/installation), or install from PyPI
       instead by running this script from outside the checkout."
    fi
    [ -f "$repo/frontend/dist/index.html" ] ||
        die "the frontend build finished but produced no $repo/frontend/dist/index.html"
}

# ------------------------------------------------------------- installing --

pipx_bin_dir() {
    pipx environment --value PIPX_BIN_DIR 2>/dev/null ||
        printf '%s\n' "${PIPX_BIN_DIR:-$HOME/.local/bin}"
}

# `pipx install` refuses an already-installed package, so a second run of this
# script would fail on the machine most likely to run it twice. --force is the
# idempotent spelling: it reinstalls, which is also the upgrade path.
install_with_pipx() {
    local python="$1" spec="$2"
    pipx install --force --python "$python" "$spec" >&2
    printf '%s/cci\n' "$(pipx_bin_dir)"
}

install_with_venv() {
    local python="$1" spec="$2"

    # A venv built by a Python that has since been upgraded out from under it
    # (Homebrew does this on every minor bump) has a dangling interpreter, and
    # every command in it fails with "bad interpreter". Rebuilding is the only
    # repair, and it costs nothing -- the venv holds no state.
    if [ -e "$VENV_DIR" ] && ! python_is_new_enough "$VENV_DIR/bin/python"; then
        warn "the existing venv at $VENV_DIR is broken or too old; rebuilding it"
        rm -rf "$VENV_DIR"
    fi

    if [ ! -x "$VENV_DIR/bin/python" ]; then
        mkdir -p "$INSTALL_DIR"
        "$python" -m venv "$VENV_DIR" >&2 ||
            die "could not create a virtualenv at $VENV_DIR"
    fi

    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip >&2
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade "$spec" >&2
    printf '%s\n' "$VENV_DIR/bin/cci"
}

# The console script carries the venv's interpreter in its shebang, so a plain
# symlink is enough -- no wrapper that would have to be regenerated whenever
# the venv moves.
link_into_bin_dir() {
    local target="$1" link="$BIN_DIR/cci"

    mkdir -p "$BIN_DIR"
    if [ -e "$link" ] || [ -L "$link" ]; then
        if [ "$(readlink "$link" 2>/dev/null || true)" != "$target" ]; then
            warn "replacing $link, which pointed somewhere else"
        fi
        rm -f "$link"
    fi
    ln -s "$target" "$link"
    printf '%s\n' "$link"
}

# ------------------------------------------------------------------- PATH --

rc_file_for_shell() {
    case "$(basename "${SHELL:-sh}")" in
        zsh)  printf '%s\n' "$HOME/.zshrc" ;;
        fish) printf '%s\n' "$HOME/.config/fish/config.fish" ;;
        ksh)  printf '%s\n' "$HOME/.kshrc" ;;
        bash)
            # macOS Terminal starts login shells, which read .bash_profile and
            # never .bashrc. Naming the wrong one produces a line the user
            # adds, a terminal they reopen, and a PATH that still has no cci.
            if [ "$(uname -s)" = "Darwin" ]; then
                printf '%s\n' "$HOME/.bash_profile"
            else
                printf '%s\n' "$HOME/.bashrc"
            fi
            ;;
        *) printf '%s\n' "$HOME/.profile" ;;
    esac
}

path_line_for_shell() {
    local dir="$1"
    if [ "$(basename "${SHELL:-sh}")" = "fish" ]; then
        printf 'fish_add_path %s\n' "$dir"
    else
        # shellcheck disable=SC2016  # $PATH must reach the rc file unexpanded
        printf 'export PATH="%s:$PATH"\n' "$dir"
    fi
}

report_on_path() {
    local installed="$1" dir found
    dir="$(dirname "$installed")"
    found="$(command -v cci 2>/dev/null || true)"

    if [ "$found" = "$installed" ]; then
        return 0
    fi

    if [ -n "$found" ]; then
        # Two cci on PATH is worse than none: `cci doctor` would report on
        # this install while the shell keeps running the other one.
        warn "your shell finds a different cci first:
           $found   (found on PATH)
           $installed   (just installed)
         Remove the other one, or put $dir earlier in PATH."
        return 0
    fi

    warn "$dir is not on your PATH, so \`cci\` will not be found in a new shell."
    say ""
    say "  Add this to $(rc_file_for_shell) and open a new terminal:"
    say ""
    say "      $(path_line_for_shell "$dir")"
    say ""
}

# -------------------------------------------------------------- uninstall --

# Whatever the user has, in the order we would have created it. The managed
# copies come first: an uninstall must remove what this script installed, not
# whichever cci happens to be first on PATH.
existing_cci() {
    local candidate
    for candidate in "$VENV_DIR/bin/cci" "$BIN_DIR/cci" "$(command -v cci 2>/dev/null || true)"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

do_uninstall() {
    local cci link

    # The background job goes first, while a working `cci` still exists to
    # remove it. Deleting the venv first leaves a launchd plist pointing at a
    # binary that is gone, which fails every 15 minutes into a log nobody
    # reads and survives every reboot.
    step "Removing the background job"
    if cci="$(existing_cci)"; then
        "$cci" install --uninstall || warn "\`$cci install --uninstall\` failed; carrying on"
    else
        note "no cci found — nothing to unload"
    fi

    step "Removing the install"
    if command -v pipx >/dev/null 2>&1 && pipx list --short 2>/dev/null | grep -q "^$PACKAGE "; then
        pipx uninstall "$PACKAGE"
    fi

    link="$BIN_DIR/cci"
    if [ -L "$link" ] &&
       case "$(readlink "$link")" in "$INSTALL_DIR"/*) true ;; *) false ;; esac; then
        rm -f "$link"
        note "removed $link"
    elif [ -e "$link" ]; then
        # Somebody else's cci. Deleting a file we did not create is how an
        # uninstaller breaks an unrelated install.
        warn "left $link alone — it is not ours"
    fi

    if [ -d "$INSTALL_DIR" ]; then
        rm -rf "$INSTALL_DIR"
        note "removed $INSTALL_DIR"
    fi

    say ""
    say "${GREEN}Done.${RESET} Your config and database are untouched at"
    say "  ${CC_INSIGHTS_HOME:-$HOME/.config/cc-insights}"
    say "Delete that directory too if you want the history gone."
}

# ------------------------------------------------------------------- main --

main() {
    if [ "$UNINSTALL" = 1 ]; then
        do_uninstall
        return 0
    fi

    local python spec checkout installed cci_path rc

    step "Looking for Python $MIN_PYTHON+"
    python="$(require_python)"
    note "$python ($("$python" -c 'import platform; print(platform.python_version())'))"

    if checkout="$(find_checkout)"; then
        step "Installing from this checkout"
        note "$checkout"
        ensure_dashboard_is_built "$checkout"
        spec="$checkout"
    else
        step "Installing $PACKAGE from PyPI"
        spec="$PACKAGE"
    fi

    if command -v pipx >/dev/null 2>&1; then
        note "using pipx"
        installed="$(install_with_pipx "$python" "$spec")"
        cci_path="$installed"
    else
        note "no pipx; using a managed venv at $VENV_DIR"
        installed="$(install_with_venv "$python" "$spec")"
        cci_path="$(link_into_bin_dir "$installed")"
    fi

    [ -x "$cci_path" ] || die "installed, but no executable at $cci_path"
    note "$cci_path"

    report_on_path "$cci_path"

    # Absolute path from here on. PATH may well be wrong -- we just said so --
    # and the whole point of this script is that the setup completes anyway.
    step "Setting up (config, first ingest, background job)"
    if [ "$WATCH" = 1 ]; then
        "$cci_path" install --watch
    else
        "$cci_path" install
    fi

    step "Checking it"
    rc=0
    "$cci_path" doctor || rc=$?
    if [ "$rc" -ne 0 ]; then
        say ""
        warn "the checks above did not all pass. Each line names the command to run."
        return "$rc"
    fi

    say ""
    say "${GREEN}cc-insights is installed and capturing.${RESET}  \`cci serve\` to look at it."
    return 0
}

# The test suite sets this and sources the file, so the Python detection, the
# checkout search and the shell-rc advice can be run and checked rather than
# grepped for. Grep would pass on a version of this script that finds the
# wrong Python or names the wrong rc file. Every other caller runs main.
if [ "${CC_INSIGHTS_INSTALL_SH_LIB:-}" != "1" ]; then
    main
fi
