#!/bin/zsh
# Start backend (:8000) and frontend (:5173) dev servers together.
set -e
cd "$(dirname "$0")"

export PATH="$HOME/.local/node/bin:$PATH"

backend/.venv/bin/uvicorn app.main:app --app-dir backend --port 8000 --reload &
BACKEND_PID=$!
trap "kill $BACKEND_PID" EXIT

cd frontend && npm run dev
