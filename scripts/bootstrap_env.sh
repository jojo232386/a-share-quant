#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"

if [ -e .venv ] && [ ! -L .venv ]; then
    echo "Refusing to replace the existing .venv directory; move it aside first." >&2
    exit 2
fi

UV_PROJECT_ENVIRONMENT=venv uv sync --locked

if [ -L .venv ]; then
    if [ "$(readlink .venv)" != "venv" ]; then
        echo "Refusing to replace .venv because it points somewhere other than venv." >&2
        exit 2
    fi
else
    ln -s venv .venv
fi

UV_PROJECT_ENVIRONMENT=venv uv run python -c \
    'import aquant, akshare, backtrader; print(akshare.__version__, backtrader.__version__)'
