# Часть 11. Конфигурация: окружение, CLI, Web

[← Оглавление](README.md) · [← Часть 10](10-web-bridge.md) · [Далее: Meta-Harness →](12-meta-harness.md)

---

## 11.1 Файл `.env`

Загрузка через `umbrella.env.load_env`: перебираются кандидаты в корне репозитория и cwd. При наличии `python-dotenv` используется он; иначе простой парсер строк `KEY=value`.

### Минимум для live LLM

| Переменная | Назначение |
|------------|------------|
| `LLM_API_KEY` или `OPENAI_API_KEY` | Ключ API |
| `LLM_MODEL` | Модель по умолчанию для части путей |
| `LLM_BASE_URL` | Необязательно; совместимые прокси |

### Модели Ouroboros / Web

`OUROBOROS_MODEL` и/или `LLM_MODEL` участвуют в выборе дефолта для чата (`resolve_default_ouroboros_model` в `umbrella/web_bridge/util.py`). Для анализа кода отдельно читается `UMBRELLA_CODE_ANALYZER_MODEL`.

### Лимиты цикла

| Переменная | Смысл |
|------------|--------|
| `OUROBOROS_MAX_ROUNDS` | Потолок раундов LLM в цикле менеджера; согласуется с `--max-rounds` в `app_ouroboros` |
| `OUROBOROS_WEB_MAX_VERIFY_RETRIES` | Дефолт retries verification при старте рана из Web bridge |

Другие отладочные ключи логируются на уровне DEBUG в `load_env` (например `OUROBOROS_LLM_BASE_URL`).

## 11.2 CLI `umbrella/app_ouroboros.py`

Ключевые аргументы (см. `_parse_args` в исходнике):

| Флаг | Комментарий |
|------|----------------|
| `workspace` | Путь к workspace; по умолчанию dashboard workspace id из конфига |
| `--task`, `--task-file` | Явная задача вместо `TASK_MAIN.md` |
| `--repo-root` | Корень checkout |
| `--live`, `--mock` | Режим LLM |
| `--timeout-hours`, `--max-budget` | Ограничения времени/денег |
| `--max-rounds` | Проброс в `OUROBOROS_MAX_ROUNDS` |
| `--max-verify-retries`, `--no-verify`, `--verification-timeout-seconds` | Verification |
| `--require-instance` / `--allow-seed-writes` | Политика записи |
| `--verbose` | DEBUG логирование |

Устаревшие флаги dashboard игнорируются с предупреждением.

## 11.3 `run_ouroboros_self_improve.py`

Требует ключ в `.env` на старте. Имеет собственный набор аргументов (`--help`) для непрерывного режима и связки с Meta-Harness.

## 11.4 Фронтенд env

| Переменная | Где |
|------------|-----|
| `REACT_APP_BACKEND_URL` | Префикс перед `/api` в production-like сборках |
| `REACT_APP_DEV_API_PROXY` | Цель прокси для `/api` в dev (`craco.config.js`) |

---

Далее внешний контур экспериментов: [12-meta-harness.md](12-meta-harness.md).
