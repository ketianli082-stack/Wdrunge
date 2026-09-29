# Wdrunge frontend

React + Vite frontend, also packaged with Tauri. Run `npm install` and `npm run dev` for the browser, or `npm run tauri:dev` for the desktop window. Start the Python API separately as described in the root README.

The frontend calls `http://127.0.0.1:8000` by default. Set `VITE_API_BASE_URL` to use another backend address.

API types and the ordinary HTTP SDK in `src/generated/` come from FastAPI's OpenAPI schema. After changing backend models or endpoints, run:

```sh
cd ../backend && uv run python -m scripts.export_openapi
cd ../frontend && npm run api:generate
```

Use the generated types for requests and responses. The chat endpoint uses a small custom `fetch` wrapper in `src/api.ts` to read POST-based SSE frames incrementally.
