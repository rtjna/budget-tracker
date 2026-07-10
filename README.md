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

Serves the whole app on http://localhost:8000.

## Where the data lives

All financial data stays outside the repository, in `~/FinanceData/`
(override with the `DATA_DIR` env var): the SQLite database at its root and
bank CSV exports in `bank-exports/`. Import CSVs through the app's drop-zone
directly from wherever they were downloaded — they never need to enter the
project folder. Nothing under the project directory holds financial data,
and export formats are gitignored as a second line of defence.
