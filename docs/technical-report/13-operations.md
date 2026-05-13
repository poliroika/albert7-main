# Часть 13. Эксплуатация и разбор инцидентов

[← Оглавление](README.md) · [← Часть 12](12-meta-harness.md) · [Далее: тесты и доки →](14-testing-and-docs.md)

---

## 13.1 Три типовых сценария

### Разовый прогон из терминала

```text
uv sync --extra dev
uv run python umbrella/app_ouroboros.py workspaces/<id> --live --verbose
```

Убедиться, что `.env` содержит ключ; при отсутствии — ожидается degraded/mock поведение с предупреждениями в логе.

### Операторский UI

1. `cd web && yarn install && yarn build`
2. `uv run bridge`
3. Браузер: `http://127.0.0.1:8765`

### Разработка UI

Одновременно: bridge + `yarn start` (см. [10-web-bridge.md](10-web-bridge.md)).

## 13.2 Частые симптомы

| Симптом | Вероятная причина | Что проверить |
|---------|-------------------|---------------|
| Белая страница / нет стилей | Нет `web/build` | `yarn build` |
| В UI пустые списки, в консоли предупреждение про HTML | Не запущен bridge или неверный URL API | Процесс `uv run bridge`, прокси dev |
| Бесконечные retry без прогресса | Одинаковая сигнатура падения verification | Отчёт `*.verification.json`, лог pytest |
| Порт занят | Другой процесс на `8765` | `--port` или завершить процесс |
| Кодировка в Windows консоли | stdout не UTF-8 | В `app_ouroboros` есть обёртка UTF-8 для stdio |

## 13.3 Логи

- Консольный вывод bridge и приложений.
- `.umbrella/app_ouroboros.log` для entrypoint Ouroboros.
- Фрагменты логов, попадающие в API `/api/logs`, зависят от индексации artifacts.

## 13.4 Остановка длительных прогонов

Используйте cancel в UI (`POST /api/runs/<id>/cancel`) или механизмы stop-request под `.umbrella/` (см. очистку в `app_ouroboros` перед стартом). При «зависании» проверьте subprocess дерево и блокировки файловой системы на workspace.

---

Далее тестирование и поддержка документации: [14-testing-and-docs.md](14-testing-and-docs.md).
