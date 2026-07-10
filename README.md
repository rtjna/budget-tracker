# Budget Tracker

A personal, self-hosted spending tracker for UK bank accounts, fed by CSV
imports (and optionally the Monzo API), with a categorization engine that
learns from corrections.

Single-user, non-commercial. See [ROADMAP.md](ROADMAP.md) for the plan,
[PRIVACY.md](PRIVACY.md) for the privacy notice, and [TERMS.md](TERMS.md)
for terms of use.

## Running (development)

```sh
./dev.sh
```

Starts the FastAPI backend on http://localhost:8000 and the Vite frontend on
http://localhost:5173 (open this one). Requires `backend/.venv` (create with
`python3 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements.txt`)
and Node (expected at `~/.local/node`, or on PATH).

## Running (Docker)

```sh
docker compose up --build
```

Serves the whole app on http://localhost:8000. The SQLite database lives in
`./data/` on the host in both modes; bank CSV exports and the database are
gitignored and must never be committed.
