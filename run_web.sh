#!/usr/bin/env bash
set -e
export PDF_EDITOR_HOST="${PDF_EDITOR_HOST:-0.0.0.0}"
export PDF_EDITOR_PORT="${PDF_EDITOR_PORT:-8000}"
exec python main.py
