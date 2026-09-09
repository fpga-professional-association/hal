#!/usr/bin/env bash
#
# Install HAL's build dependencies.
#
# This script fails fast. Every package manager invocation is checked, and a failure names the
# package that could not be installed instead of letting the run continue: a missing dependency
# that is only noticed by cmake ("Could NOT find RapidJSON") or by the compiler is far harder to
# diagnose than an apt error at the moment it happens.
#
# Environment:
#   HAL_DOCKER=1                  assume a root shell in an Ubuntu container (no sudo, no
#                                 lsb_release); this is what the top-level Dockerfile sets
#   HAL_DEPENDENCIES_DRY_RUN=1    resolve and check the package list, then stop before installing
#                                 anything; use it to verify the list on a distribution
#   HAL_OS_RELEASE=<file>         read the distribution id from <file> instead of /etc/os-release
#   additional_deps="a b c"       extra apt packages to install alongside HAL's own
#
# -E so the ERR trap below also fires inside functions; -u so a typo in a variable name is an
# error, not an empty package list.
set -Eeuo pipefail

SCRIPT_NAME="$(basename "$0")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------

note() {
    printf '%s: %s\n' "$SCRIPT_NAME" "$1"
}

die() {
    printf '%s: error: %s\n' "$SCRIPT_NAME" "$1" >&2
    exit 1
}

# Anything that fails without an explicit check still stops the script (set -e) and still says
# where it happened, so no failure can be mistaken for a successful install.
on_error() {
    local status=$1 line=$2 command=$3
    printf '%s: error: command failed with exit code %s at line %s: %s\n' \
        "$SCRIPT_NAME" "$status" "$line" "$command" >&2
    printf '%s: dependencies are NOT fully installed; fix the error above and re-run.\n' \
        "$SCRIPT_NAME" >&2
}
trap 'on_error "$?" "$LINENO" "$BASH_COMMAND"' ERR

dry_run() {
    [[ "${HAL_DEPENDENCIES_DRY_RUN:-0}" == "1" ]]
}

# ---------------------------------------------------------------------------
# package lists
#
# One list per distribution family. RapidJSON is a hard cmake requirement
# (cmake/detect_dependencies.cmake: find_package(RapidJSON REQUIRED)), as are z3, a Python 3
# development install and pybind11, so every list below must carry them -- tools/
# test_install_dependencies.py asserts exactly that, which is how a list stays complete.
# ---------------------------------------------------------------------------

# Debian/Ubuntu/Mint. The same list is used for the docker path: the official Dockerfile builds an
# Ubuntu image, and two lists that are supposed to be equal are two lists that drift apart.
APT_PACKAGES=(
    # toolchain and build system
    build-essential
    git
    cmake
    ninja-build
    pkgconf
    ccache
    autoconf
    autotools-dev
    lsb-release
    # HAL libraries
    libboost-all-dev
    libsodium-dev
    rapidjson-dev
    libspdlog-dev
    libz3-dev
    z3
    libreadline-dev
    libomp-dev
    libsuitesparse-dev
    libgraphviz-dev
    graphviz
    # Python bindings
    libpython3-dev
    python3-pip
    pybind11-dev
    python3-pybind11
    python3-dateutil
    # simulation, coverage, documentation
    verilator
    lcov
    gcovr
    doxygen
    python3-sphinx
    python3-sphinx-rtd-theme
)
# Not listed on purpose: 'apport' (Ubuntu's crash reporter). It arrived upstream with the GUI
# extension plugin, nothing in this fork references it, and it does not exist on Debian -- where
# it made this very script abort with "Package 'apport' has no installation candidate". This list
# is used for every Debian-like distribution, so it may only contain packages all of them have.

ARCH_PACKAGES=(
    base-devel
    git
    cmake
    ninja
    pkgconf
    ccache
    autoconf
    lsb-release
    boost
    boost-libs
    libsodium
    rapidjson
    spdlog
    z3
    readline
    suitesparse
    graphviz
    python
    python-pip
    pybind11
    python-dateutil
    verilator
    lcov
    gcovr
    doxygen
    python-sphinx
    python-sphinx_rtd_theme
)

# Experimental, and unverified by CI -- see the RHEL branch below.
RHEL_BASE_PACKAGES=(
    pkgconfig
    git
    llvm
    cmake
    flex
    bison
    python3
    graphviz
    graphviz-devel
    boost
    readline
    readline-devel
    gcc-c++
    make
)

RHEL_EPEL_PACKAGES=(
    boost-devel
    rapidjson-devel
    spdlog-devel
    z3
    z3-devel
    python3-devel
    pybind11-devel
    verilator
)

# ---------------------------------------------------------------------------
# platform detection
# ---------------------------------------------------------------------------

os_release_id() {
    local file="${HAL_OS_RELEASE:-/etc/os-release}"
    [[ -r "$file" ]] || return 1
    # Read in a subshell: /etc/os-release sets ID/VERSION_ID/ID_LIKE and we do not want those
    # leaking into the rest of the script.
    (
        # shellcheck disable=SC1090
        . "$file"
        printf '%s %s %s\n' "${ID:-unknown}" "${VERSION_ID:-unknown}" "${ID_LIKE:-}"
    )
}

detect_distribution() {
    local info
    if info="$(os_release_id)"; then
        distribution="$(printf '%s' "$info" | cut -d' ' -f1 | tr '[:upper:]' '[:lower:]')"
        release="$(printf '%s' "$info" | cut -d' ' -f2)"
        distribution_like="$(printf '%s' "$info" | cut -d' ' -f3- | tr '[:upper:]' '[:lower:]')"
        return 0
    fi
    # lsb_release is the fallback, not a second source of truth. It is also allowed to fail (it is
    # a Python script on Debian and breaks with a broken Python install): a failure here must end
    # in the explanation below, not in an unrelated ERR-trap message about 'lsb_release -is'.
    local lsb_id lsb_version
    if command -v lsb_release >/dev/null 2>&1 \
        && lsb_id="$(lsb_release -is 2>/dev/null)" \
        && lsb_version="$(lsb_release -rs 2>/dev/null)" \
        && [[ -n "$lsb_id" ]]; then
        distribution="$(printf '%s' "$lsb_id" | tr '[:upper:]' '[:lower:]')"
        release="$lsb_version"
        distribution_like=""
        return 0
    fi
    die "cannot determine the Linux distribution: neither ${HAL_OS_RELEASE:-/etc/os-release} nor
lsb_release is available. Install lsb-release (or run this on a distribution that ships
/etc/os-release) and try again."
}

is_debian_like() {
    case "$distribution" in
        ubuntu | linuxmint | debian | pop | elementary | zorin | neon | raspbian) return 0 ;;
    esac
    case " $distribution_like " in
        *" debian "* | *" ubuntu "*) return 0 ;;
    esac
    return 1
}

# ---------------------------------------------------------------------------
# apt
# ---------------------------------------------------------------------------

# Root in a container has no sudo; a desktop user needs it. Deciding here (instead of hard-coding
# `sudo`) is what lets the ubuntu path run unchanged inside a fresh ubuntu:24.04 container.
privileged() {
    if [[ "$(id -u)" == "0" ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        die "this script needs root to install packages, but it is not running as root and sudo is
not installed. Re-run it as root, or install sudo."
    fi
}

# Extract the package names apt complained about. apt-get reports unknown or uninstallable
# packages as 'E: Unable to locate package foo' / "E: Package 'foo' has no installation candidate",
# which is exactly the information the caller has to be told.
apt_unavailable_packages() {
    local output="$1"
    printf '%s\n' "$output" \
        | sed -n -e "s/^E: Unable to locate package \(.*\)$/\1/p" \
                 -e "s/^E: Package '\([^']*\)' has no installation candidate.*$/\1/p" \
                 -e "s/^E: Couldn't find any package by regex '\([^']*\)'.*$/\1/p" \
        | sort -u \
        | tr '\n' ' ' \
        | sed -e 's/ $//'
}

apt_install() {
    local packages=("$@")

    note "updating the apt package index"
    if ! privileged apt-get update; then
        die "'apt-get update' failed. Without a usable package index no dependency can be
installed; check the network connection and /etc/apt/sources.list, then re-run."
    fi

    # Resolve the whole list first. This is what turns 'a package name is wrong' from a cmake
    # error 20 minutes later into an error right here, naming the package.
    note "checking that all ${#packages[@]} packages exist on ${distribution} ${release}"
    local resolve_output resolve_status=0
    # Same command as the real install below, minus the installing: resolving a *different* set
    # than the one that gets installed would defeat the point of checking first.
    resolve_output="$(privileged apt-get install -y --dry-run -- "${packages[@]}" 2>&1)" \
        || resolve_status=$?
    if [[ $resolve_status -ne 0 ]]; then
        local unavailable
        unavailable="$(apt_unavailable_packages "$resolve_output")"
        printf '%s\n' "$resolve_output" >&2
        if [[ -n "$unavailable" ]]; then
            die "the following package(s) are not available on ${distribution} ${release}:
    ${unavailable}
HAL cannot be built without them. Check the package names for this distribution (or add a
distribution-specific block to ${SCRIPT_NAME}) and re-run; nothing was installed."
        fi
        die "apt could not resolve HAL's dependencies on ${distribution} ${release} (exit code
${resolve_status}); the apt output above says why. Nothing was installed."
    fi

    if dry_run; then
        note "HAL_DEPENDENCIES_DRY_RUN=1: package list verified, stopping before installing"
        return 0
    fi

    note "installing ${#packages[@]} packages"
    if privileged apt-get install -y -- "${packages[@]}"; then
        return 0
    fi

    # The bulk install failed even though every name resolved (a broken package, a failing
    # post-install script, a full disk, ...). Find out which package it was rather than making the
    # user bisect the list by hand.
    note "the bulk install failed; retrying package by package to identify the culprit"
    local failed=()
    local package
    for package in "${packages[@]}"; do
        if ! privileged apt-get install -y -- "$package" >/dev/null 2>&1; then
            failed+=("$package")
        fi
    done
    if [[ ${#failed[@]} -gt 0 ]]; then
        die "failed to install the following package(s): ${failed[*]}
Re-run 'apt-get install ${failed[*]}' to see the full error. HAL will not build without them."
    fi
    die "'apt-get install' failed but every package installed individually; re-run this script.
If it keeps failing, run the apt-get command by hand to see the full error."
}

# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

ensure_line_in_file() {
    local line="$1" file="$2"
    touch "$file"
    if ! grep -Fxq "$line" "$file"; then
        printf '%s\n' "$line" >> "$file"
    fi
}

install_macos_dependencies() {
    command -v brew >/dev/null 2>&1 \
        || die "Homebrew is required on macOS but 'brew' is not on PATH; install it from
https://brew.sh and re-run."

    note "executing brew bundle"
    if ! brew bundle --file="${REPO_ROOT}/Brewfile"; then
        die "'brew bundle' failed; the Brewfile line it stopped on names the formula that could
not be installed. Fix it and re-run -- HAL will not configure with a partial dependency set."
    fi

    if dry_run; then
        note "HAL_DEPENDENCIES_DRY_RUN=1: stopping before installing Python requirements"
        return 0
    fi

    if ! pip3 install -r "${REPO_ROOT}/requirements.txt"; then
        die "'pip3 install -r requirements.txt' failed; HAL's documentation build needs those
packages. Fix the error above (a virtualenv or --break-system-packages is often what is missing)
and re-run."
    fi

    local brew_prefix
    brew_prefix="$(brew --prefix)"
    local -a path_lines=(
        "export PATH=\"${brew_prefix}/opt/llvm@14/bin:\$PATH\""
        "export PATH=\"${brew_prefix}/opt/flex/bin:\$PATH\""
        "export PATH=\"${brew_prefix}/opt/bison/bin:\$PATH\""
    )

    local profile=""
    if [ -n "$("${SHELL}" -c 'echo ${ZSH_VERSION:-}')" ]; then
        profile="${HOME}/.zshrc"
    elif [ -n "$("${SHELL}" -c 'echo ${BASH_VERSION:-}')" ]; then
        profile="${HOME}/.bash_profile"
    else
        die "unknown user shell '${SHELL}': cannot add Homebrew's llvm@14, flex and bison to your
PATH. Add these lines to your shell profile by hand and re-run the build:
    ${path_lines[0]}
    ${path_lines[1]}
    ${path_lines[2]}"
    fi

    local line
    for line in "${path_lines[@]}"; do
        ensure_line_in_file "$line" "$profile"
    done
    note "PATH entries for llvm@14, flex and bison are in ${profile}; open a new shell (or run
'source ${profile}') before configuring HAL"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

platform='unknown'
distribution='unknown'
release='unknown'
distribution_like=''

unamestr="$(uname)"
if [[ "${HAL_DOCKER:-0}" == "1" ]]; then
    # The official Dockerfile builds an Ubuntu image; detection still runs so that a wrong base
    # image is reported here rather than by apt.
    platform='docker'
    detect_distribution
elif [[ "$unamestr" == 'Linux' ]]; then
    platform='linux'
    detect_distribution
elif [[ "$unamestr" == 'Darwin' ]]; then
    platform='macOS'
fi

case "$platform" in
    macOS)
        install_macos_dependencies
        ;;
    linux | docker)
        # shellcheck disable=SC2206  # word splitting is the documented interface of $additional_deps
        extra_packages=(${additional_deps:-})
        if is_debian_like; then
            apt_install "${APT_PACKAGES[@]}" ${extra_packages[@]+"${extra_packages[@]}"}
        elif [[ "$distribution" == "arch" || "$distribution_like" == *"arch"* ]]; then
            command -v yay >/dev/null 2>&1 \
                || die "the Arch path uses 'yay' to install $(printf '%s ' "${ARCH_PACKAGES[@]}")
but yay is not installed; install yay (or install those packages with pacman) and re-run."
            if dry_run; then
                note "HAL_DEPENDENCIES_DRY_RUN=1: stopping before installing ${#ARCH_PACKAGES[@]} packages"
                exit 0
            fi
            if ! yay -S --needed --noconfirm "${ARCH_PACKAGES[@]}" \
                ${extra_packages[@]+"${extra_packages[@]}"}; then
                die "'yay -S' failed; the output above names the package that could not be
installed. HAL will not build without the full list, so fix it and re-run."
            fi
        elif [[ "$distribution" == "rhel" ]]; then
            rhel_version="$(printf '%s' "$release" | cut -d. -f1)"
            note "running the EXPERIMENTAL setup for RedHat Enterprise Linux ${rhel_version} <${release}>."
            note "it installs some development packages from Fedora Rawhide and is not covered by CI."
            yn=''
            read -r -p "Is that OK? [yN] " yn \
                || die "no answer on stdin; re-run this script from an interactive terminal."
            if [ "$yn" != 'y' ] && [ "$yn" != 'Y' ]; then
                die "aborted on user request; no package was installed."
            fi
            if dry_run; then
                note "HAL_DEPENDENCIES_DRY_RUN=1: stopping before installing packages"
                exit 0
            fi
            for pkg in "${RHEL_BASE_PACKAGES[@]}"; do
                privileged yum install -y "$pkg" \
                    || die "'yum install ${pkg}' failed; HAL cannot be built without it."
            done
            privileged yum install -y \
                "https://dl.fedoraproject.org/pub/epel/epel-release-latest-${rhel_version}.noarch.rpm" \
                || die "could not install the EPEL release package for RHEL ${rhel_version};
the remaining dependencies (${RHEL_EPEL_PACKAGES[*]}) come from it."
            privileged yum update -y || die "'yum update' failed after enabling EPEL."
            for pkg in "${RHEL_EPEL_PACKAGES[@]}"; do
                privileged yum install -y "$pkg" \
                    || die "'yum install ${pkg}' failed; HAL cannot be built without it."
            done
            die "the RHEL path is experimental: the packages above are installed, but this
distribution is not covered by CI and the build is not known to work. Continue at your own risk."
        else
            die "unsupported Linux distribution '${distribution}' (${release}). Supported:
Ubuntu/Linux Mint/Debian (apt), Arch (yay) and -- experimentally -- RHEL (yum). HAL needs at
least: ${APT_PACKAGES[*]}"
        fi
        ;;
    *)
        die "unsupported platform '${unamestr}'. HAL builds on Linux and macOS; see the Build
Instructions in README.md."
        ;;
esac

note "dependency installation finished successfully"
