#!/bin/bash
# Install the umbrella stack inside a Terminal-Bench task container.
#
# This script is *sourced* by `AbstractInstalledAgent.perform_task` inside the
# tmux session. It must therefore not call `exit` on failure -- doing so would
# kill the tmux pane and the harness would not detect the install failure
# properly. Instead all real work is done in `_umbrella_install` and the script
# returns with a non-zero status; the abstract agent appends
# `|| echo 'INSTALL_FAIL_STATUS'` so the harness picks that up.

_umbrella_install() {
    set -e

    local TARBALL="/installed-agent/umbrella.tar.gz"
    local TARGET="/opt"
    local APP_DIR="/opt/umbrella"
    local PYBIN
    local PY_MM

    if [ ! -f "${TARBALL}" ]; then
        echo "[umbrella-setup] FATAL: tarball ${TARBALL} not found" >&2
        return 1
    fi

    echo "[umbrella-setup] Updating apt index"
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get update -y >/tmp/apt-update.log 2>&1 \
            || { echo "[umbrella-setup] apt-get update failed; see /tmp/apt-update.log" >&2; return 1; }

        echo "[umbrella-setup] Installing python3, pip, venv, curl, ca-certificates, git"
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            python3 python3-venv python3-pip curl ca-certificates git \
            >/tmp/apt-install.log 2>&1 \
            || { echo "[umbrella-setup] apt-get install failed; see /tmp/apt-install.log" >&2; return 1; }
    else
        echo "[umbrella-setup] No apt-get found; assuming python3 is already present" >&2
    fi

    PYBIN="$(command -v python3 || true)"
    if [ -z "${PYBIN}" ]; then
        echo "[umbrella-setup] FATAL: python3 is not available after install" >&2
        return 1
    fi
    echo "[umbrella-setup] Using python: ${PYBIN} ($("${PYBIN}" --version 2>&1))"
    PY_MM="$("${PYBIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    echo "[umbrella-setup] Python minor version: ${PY_MM}"

    echo "[umbrella-setup] Extracting umbrella tarball into ${TARGET}"
    mkdir -p "${TARGET}"
    tar -xzf "${TARBALL}" -C "${TARGET}" \
        || { echo "[umbrella-setup] tar extract failed" >&2; return 1; }

    if [ ! -d "${APP_DIR}" ]; then
        echo "[umbrella-setup] FATAL: ${APP_DIR} missing after extraction" >&2
        return 1
    fi

    echo "[umbrella-setup] Creating venv at ${APP_DIR}/.venv"
    "${PYBIN}" -m venv "${APP_DIR}/.venv" \
        || { echo "[umbrella-setup] venv creation failed" >&2; return 1; }

    # shellcheck disable=SC1091
    . "${APP_DIR}/.venv/bin/activate"

    echo "[umbrella-setup] Upgrading pip / wheel"
    pip install --quiet --upgrade pip wheel \
        || { echo "[umbrella-setup] pip upgrade failed" >&2; return 1; }

    echo "[umbrella-setup] Installing umbrella runtime deps"
    # umbrella + ouroboros runtime (kept in sync with the root pyproject.toml).
    # `mempalace` is the persistent memory store ouroboros uses across rounds;
    # without it `Palace.recent` / `Palace.search` raise "No module named
    # 'mempalace'" on every task and the agent has no scratchpad memory.
    #
    # Some Terminal-Bench containers still ship Python 3.9 / 3.10. We therefore
    # choose the newest dependency set that remains installable on the detected
    # interpreter instead of blindly using the host project's latest floor.
    if [ "${PY_MM}" = "3.9" ]; then
        echo "[umbrella-setup] Python 3.9 detected; using py39-compatible runtime pins"
        pip install --quiet \
            "flask>=3.1.3" \
            "flask-sse>=1.0.0" \
            "mempalace>=3.2.0" \
            "openai>=2.30.0" \
            "pydantic>=2.11,<3" \
            "pydantic-settings>=2.11,<2.12" \
            "pyyaml>=6.0" \
            "requests>=2.31" \
            || { echo "[umbrella-setup] runtime dep install failed" >&2; return 1; }
    else
        pip install --quiet \
            "flask>=3.1.3" \
            "flask-sse>=1.0.0" \
            "mempalace>=3.2.0" \
            "openai>=2.30.0" \
            "pydantic>=2.12.5" \
            "pydantic-settings>=2.13.1" \
            "pyyaml>=6.0" \
            "requests>=2.31" \
            || { echo "[umbrella-setup] runtime dep install failed" >&2; return 1; }
    fi

    # gmas (frontier-ai-gmas) runtime deps. We deliberately omit the
    # multi-hundred-MB `torch` wheel: the GNN / embedding paths inside
    # gmas that need torch are not exercised by `umbrella.app_ouroboros`
    # against the `terminal_bench` adapter workspace, and pulling in
    # torch on every TB task container would add ~5 minutes of install
    # latency per task. If a specific TB task ends up needing the
    # tensor stack, the agent can `pip install torch --index-url
    # https://download.pytorch.org/whl/cpu` on demand.
    echo "[umbrella-setup] Installing gmas runtime deps (torch-less)"
    pip install --quiet \
        "rustworkx>=0.17.1" \
        "loguru>=0.7.3" \
        "httpx>=0.28.1" \
        "semver>=3.0.4" \
        || { echo "[umbrella-setup] gmas dep install failed" >&2; return 1; }

    # Install umbrella itself in editable mode so that
    # `python -m umbrella.app_ouroboros` resolves to /opt/umbrella/umbrella.
    #
    # `--ignore-requires-python` is mandatory: a number of TB task containers
    # (~10/86 in the v0.1.x dataset) ship with python 3.10, while the root
    # `pyproject.toml` declares `requires-python = ">=3.11"`. The umbrella
    # source itself runs fine on 3.10, so the gate is too strict; without
    # this flag those containers fail with `INSTALL_FAIL_STATUS` before the
    # agent ever starts.
    pip install --quiet --no-deps --ignore-requires-python -e "${APP_DIR}" \
        || { echo "[umbrella-setup] editable install of umbrella failed" >&2; return 1; }

    # gmas: install editable with --no-deps so we don't drag in torch
    # but still get a real `gmas` distribution registered with pip.
    if [ -d "${APP_DIR}/gmas" ] && "${PYBIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
        echo "[umbrella-setup] Installing gmas (editable, --no-deps)"
        pip install --quiet --no-deps --ignore-requires-python -e "${APP_DIR}/gmas" \
            || { echo "[umbrella-setup] WARN: gmas editable install failed; falling back to PYTHONPATH" >&2; }
    elif [ -d "${APP_DIR}/gmas" ]; then
        echo "[umbrella-setup] WARN: skipping gmas editable install on Python ${PY_MM}; gmas officially requires >=3.12" >&2
    fi

    deactivate

    # Sanity checks: both entrypoints must import cleanly. gmas is
    # best-effort -- a missing gmas does not abort install (the
    # adapter workspace doesn't import it).
    "${APP_DIR}/.venv/bin/python" -c "import umbrella.app_ouroboros" \
        || { echo "[umbrella-setup] FATAL: cannot import umbrella.app_ouroboros" >&2; return 1; }
    if "${APP_DIR}/.venv/bin/python" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" \
        && "${APP_DIR}/.venv/bin/python" -c "import gmas" 2>/dev/null; then
        echo "[umbrella-setup] gmas import OK"
    else
        echo "[umbrella-setup] WARN: gmas not importable on this interpreter; runtime will continue without gmas package import" >&2
    fi

    echo "[umbrella-setup] OK -- umbrella installed at ${APP_DIR}"
    return 0
}

_umbrella_install
_UMBRELLA_INSTALL_RC=$?
unset -f _umbrella_install
return ${_UMBRELLA_INSTALL_RC} 2>/dev/null || ( exit ${_UMBRELLA_INSTALL_RC} )
