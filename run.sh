#!/bin/bash
# Development runner: creates a venv, installs the package, runs with ./config.yaml
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f config.yaml ]; then
    echo "No config.yaml found: cp config.example.yaml config.yaml and edit it" >&2
    exit 1
fi

[ -d venv ] || python3 -m venv venv
venv/bin/pip install --quiet -e '.[dev]'

# Load .env so ${VAR} references in config.yaml resolve
if [ -f .env ]; then
    set -a; source .env; set +a
fi

exec venv/bin/python -m printer_notifier config.yaml
