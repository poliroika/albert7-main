# Docker Deployment

The Docker image serves the React UI and the Python web bridge from one process.
The app listens on `0.0.0.0:${PORT:-8765}` and exposes `/api/health`.

## Local Build

```powershell
docker build -t umbrella:latest .
docker run --rm --env-file .env -p 8765:8765 umbrella:latest
```

Open `http://127.0.0.1:8765`.

## Docker Compose

```powershell
docker compose up --build
```

Compose keeps runtime state in named volumes:

- `/app/.umbrella`
- `/app/outputs`
- `/app/workspaces`

## Deploy As A Link

Use any Docker-capable host such as Render, Railway, Fly.io, Coolify, or a VPS.

1. Connect this repository and select Dockerfile-based deployment.
2. Set `PORT` to the public HTTP port expected by the platform if it is not supplied automatically.
3. Add secrets as environment variables, especially `LLM_API_KEY` or `OPENAI_API_KEY`.
4. Add persistent storage for `/app/.umbrella`, `/app/outputs`, and `/app/workspaces` if runs and generated workspaces must survive redeploys.
5. Point the platform health check to `/api/health`.

Do not bake `.env` into the image. `.dockerignore` excludes local env files so secrets are passed only at runtime.
