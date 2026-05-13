# Часть 10. Web bridge: HTTP-сервер, API, фронтенд

[← Оглавление](README.md) · [← Часть 9](09-verification.md) · [Далее: конфигурация →](11-configuration.md)

---

## 10.1 Почему один процесс

`umbrella/web_bridge/server.py` поднимает `ThreadingHTTPServer` с handler-классом из `handler.py`. В одном процессе совмещены:

- раздача **статики** SPA (каталог `web/build`, запасной `web/dist`);
- обработка **JSON API** под префиксом `/api`.

Так устраняется класс проблем CORS и «фронт на одном порту, API на другом» для production-сценария оператора. Для разработки UI отдельно включён **прокси** dev-сервера на bridge.

## 10.2 Запуск и аргументы

Команда из корня: `uv run bridge` (эквивалент `uv run python -m umbrella.web_bridge`).

| Аргумент | Значение по умолчанию | Смысл |
|----------|----------------------|--------|
| `--host` | `127.0.0.1` | Интерфейс прослушивания |
| `--port` | `8765` | Порт (при занятости сервер подскажет `--port 8766`) |
| `--repo-root` | текущий cwd | Корень checkout для резолва путей |
| `--log-level` | `INFO` | Уровень логирования |

## 10.3 Карта GET-маршрутов API

Реализация в `umbrella/web_bridge/handler.py` (`_dispatch_get`). Ниже перечень по состоянию разработки; при добавлении эндпоинтов документ обновляют вместе с PR.

| Путь | Назначение |
|------|------------|
| `/api/health` | Проверка живости сервиса |
| `/api/workspaces` | Список workspace |
| `/api/workspaces/<id>` | Детали workspace |
| `/api/threads` | Потоки чата (фильтр `workspace_id`) |
| `/api/threads/<id>` | Поток |
| `/api/threads/<id>/messages` | Сообщения |
| `/api/runs` | Список прогонов (`workspace_id`, пагинация) |
| `/api/runs/<id>` | Прогон |
| `/api/runs/<id>/steps` | Шаги таймлайна |
| `/api/logs` | Логи с фильтрами |
| `/api/memory` | Узлы памяти |
| `/api/memory/<id>` | Узел |
| `/api/settings` | Настройки (`workspace_id` обязателен) |
| `/api/dashboard/stats` | Статистика дашборда |
| `/api/models` | Каталог моделей для UI |
| `/api/tools` | Каталог инструментов (справочно) |
| `/api/user-input` | Запросы ввода от агента |
| `/api/permission-request` | Запросы разрешений |
| `/api/mcp/servers` | MCP серверы |

## 10.4 POST/PATCH/DELETE (операции)

Основные мутации:

- `POST /api/workspaces` — создание workspace;
- `PATCH /api/workspaces/<id>` — обновление;
- `DELETE /api/workspaces/<id>` — удаление (с guard’ами);
- `POST /api/threads`, `POST .../messages` — чат;
- `POST /api/runs` — старт прогона harness/Ouroboros;
- `POST /api/runs/<id>/cancel` — отмена;
- `PATCH /api/settings` — persisted настройки;
- CRUD для MCP серверов;
- ответы на user-input и permission-request по под-путям.

Ошибки отмены или активного состояния часто мапятся в **409 Conflict** с телом `reason`, чтобы фронт показал осмысленное сообщение.

## 10.5 Статика и SPA-fallback

Для путей без совпадения с API handler ищет файл под `WEB_BUILD_DIR` / `WEB_DIST_DIR`. Если запрошен «маршрут приложения» без расширения, для не-asset запросов отдаётся `index.html` — стандартное поведение SPA.

## 10.6 Фронтенд `web/`

- Сборка: `yarn build` → `web/build`.
- Роутинг React Router: `/`, `/chat`, `/workspaces`, `/runs`, `/memory`, `/logs`, `/dashboard`, `/mcp`, `/settings` (`web/src/App.js`).
- Axios базируется на `REACT_APP_BACKEND_URL + '/api'` (`web/src/lib/api.js`).

## 10.7 Режим разработки

1. Bridge на `:8765`.
2. `yarn start` в `web/` — dev-сервер (часто `:3000`), прокси `/api` → `REACT_APP_DEV_API_PROXY` или `http://127.0.0.1:8765` (`web/craco.config.js`).

Если открыть только CRA без bridge, в консоли появятся предупреждения про HTML вместо JSON — это ожидаемая диагностика.

---

Далее сводка конфигурации: [11-configuration.md](11-configuration.md).
