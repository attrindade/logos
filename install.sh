#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

echo "[Logos] Setting up environment..."
if [ ! -d ".venv" ]; then
    echo "[Logos] Creating Python virtual environment..."
    python3 -m venv .venv
fi

echo "[Logos] Installing dependencies..."
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

echo "[Logos] Launching interactive TUI installer..."
.venv/bin/python setup.py
