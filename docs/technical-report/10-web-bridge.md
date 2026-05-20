# Part 10: Web Bridge and UI

The web bridge is a single HTTP process that serves the React frontend and the JSON API on the same port.

## Server Architecture

```
Browser --HTTP--> web_bridge/server.py (uvicorn/Flask)
                     |
                     +--> Static files: web/build/ (React SPA)
                     |
                     +--> /api/* routes (JSON)
                            |
                            +--> handler.py (route registration)
                            +--> app.py (main application logic)
                            +--> api/report_api.py (report routes)
```

Start: `uv run bridge` (port 8765 by default).

## API Endpoints

### Core

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | Health check |
| GET | `/api/workspaces` | List workspaces |
| GET | `/api/runs` | List runs |
| GET | `/api/runs/<id>` | Run details |
| GET | `/api/runs/<id>/report` | FinalReport JSON |
| GET | `/api/runs/<id>/report.md` | FinalReport rendered markdown |
| GET | `/api/logs` | Log viewer |
| GET | `/api/memory` | Memory nodes |
| GET | `/api/dashboard/stats` | Dashboard statistics |

### Chat

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/threads` | List chat threads |
| POST | `/api/threads` | Create thread |
| DELETE | `/api/threads/<id>` | Delete thread |
| GET | `/api/threads/<id>/messages` | Get messages |
| POST | `/api/threads/<id>/messages` | Send message |

### Run Control

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/runs/start` | Start workspace run |
| POST | `/api/runs/<id>/cancel` | Cancel run |
| GET | `/api/runs/<id>/steps` | Run step timeline |
| GET | `/api/runs/<id>/phases` | Phase plan visualization |

### Agent Communication

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/agent/user-input-requests` | Poll for user input requests |
| POST | `/api/agent/user-input-requests/<id>/respond` | Respond to request |
| GET | `/api/agent/permission-requests` | Poll for permission escalation |
| POST | `/api/agent/permission-requests/<id>/respond` | Respond to permission |

### Settings

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/settings` | Get settings |
| PUT | `/api/settings` | Update settings |

### Discovery

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/models` | Discover available LLM models |
| GET | `/api/tools` | List available tools |
| POST | `/api/mcp/discover` | Discover MCP tools |

## React Frontend

### Pages

| Route | Component | Purpose |
|-------|-----------|---------|
| `/` | `Landing` | Standalone landing page |
| `/chat` | `Chat` | Main chat interface with thread management |
| `/runs` | `Runs` | Run history and details |
| `/logs` | `Logs` | Log viewer |
| `/dashboard` | `Dashboard` | Statistics dashboard |
| `/workspaces` | `Workspaces` | Workspace management |
| `/memory` | `MemoryGraph` | Memory node graph viewer |
| `/mcp` | `MCPRegistry` | MCP server registry |
| `/settings` | `Settings` | Settings editor |

### Chat Page Architecture

Three-panel layout:
- **Left**: `ThreadList` (resizable 180-420px)
- **Center**: Messages + `Composer` input
- **Right**: `TimelinePanel` (resizable 260-640px)

Features:
- Thread CRUD
- Real-time run polling (1500ms interval)
- PhaseRunner plan visualization (phase timeline)
- Agent request cards (`UserInputRequestCard`, `PermissionRequestCard`)
- TASK_MAIN.md button for workspace-level tasks
- Harness mode toggle with candidate visualization

### API Client (`web/src/lib/api.js`)

Axios-based client hitting `/api/*`. Covers workspace CRUD, thread/message management, run control, agent comms, settings, discovery. Includes defensive `asJsonArray()` helper for misconfigured static servers.

## Development Setup

### Production mode (single process)

```powershell
cd web && yarn install && yarn build && cd ..
uv run bridge
# Open http://127.0.0.1:8765
```

### Development mode (hot reload, two processes)

```powershell
# Terminal 1: API
uv run bridge

# Terminal 2: React dev server
cd web && yarn start
# Open http://localhost:3000
```

In dev mode, Craco proxies `/api` to `http://127.0.0.1:8765`. Override with `REACT_APP_DEV_API_PROXY`.

For production builds with a separate API, set `REACT_APP_BACKEND_URL`.

---

Next: [Part 11 — Configuration](11-configuration.md)
