# Wdrunge
A quite powerful web develop agent team.

The integrated version combines the `agent-enhanced` backend (`a02d55d`) with the English frontend (`9d99fbc`), website selection and batched annotations. The backend is preserved without changes; frontend execution labels use its existing APIs. See [integration notes](docs/final-integration.md) and [visual feedback](docs/visual-feedback.md) for scope and verification details.

## Development

Start the Python API (see [backend/README.md](backend/README.md) for setup):

```sh
cd backend
cp .env.example .env
# Add your DASHSCOPE_API_KEY to .env
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 17420
```

In a second terminal, start the React frontend:

```sh
cd frontend
npm install
npm run dev
```

Open `http://localhost:1420`, or run `npm run tauri:dev` for the desktop app. The frontend connects to `http://127.0.0.1:17420` by default; set `VITE_API_BASE_URL` when the API is elsewhere.

FastAPI validates JSON using Pydantic models and serves its generated API reference at `http://127.0.0.1:17420/docs`. The repository's `docs/` directory is for project design documents. See [frontend/README.md](frontend/README.md) for the frontend type and SDK generation workflow.

The backend starts a Leader plus product, backend, frontend, designer, and testing agents in the `idle` state. `GET /api/team` shows their current status and token usage. The Leader can answer directly, call `delegate_task` for one specialist, or call `delegate_tasks` to run independent assignments in parallel.
