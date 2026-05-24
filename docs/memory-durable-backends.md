# Durable memory backends (environment)

MemPalace via the canonical kernel writer (`umbrella/memory/kernel/writer.py`) is the **source of truth** for durable memory. [Hindsight](https://github.com/hindsight-dev/hindsight) is an optional secondary mirror and reflection backend — never the default product path without explicit opt-in.

Factory entrypoint: `umbrella/memory/backends/factory.py` → `create_durable_backend()`.

## Recommended: canonical only (default)

No extra variables required. This is the product default.

```dotenv
UMBRELLA_MEMORY_DURABLE_BACKEND=canonical
```

## Recommended: dual (canonical + Hindsight mirror)

Canonical writes go to MemPalace first. Hindsight receives best-effort mirrors for verified lessons/events when enabled.

### Required

```dotenv
UMBRELLA_MEMORY_DURABLE_BACKEND=dual
UMBRELLA_HINDSIGHT_ENABLED=1
```

`hindsight_mirror_enabled()` requires **both** `UMBRELLA_MEMORY_DURABLE_BACKEND` in `{dual, hindsight}` and `UMBRELLA_HINDSIGHT_ENABLED=1`. Without `ENABLED=1`, `retain_hindsight_*_best_effort` returns `skipped: disabled`.

### Server connection (when not embedded)

```dotenv
UMBRELLA_HINDSIGHT_BASE_URL=http://localhost:8888
UMBRELLA_HINDSIGHT_API_KEY=
UMBRELLA_HINDSIGHT_TIMEOUT_SECONDS=30
UMBRELLA_HINDSIGHT_EMBEDDED=0
UMBRELLA_HINDSIGHT_PROFILE=umbrella-dev
```

Use `UMBRELLA_HINDSIGHT_EMBEDDED=1` with the `hindsight-local` optional dependency for an embedded stack.

### Secondary write behavior

```dotenv
UMBRELLA_HINDSIGHT_RETAIN_ASYNC=1
UMBRELLA_HINDSIGHT_FAIL_CLOSED=0
```

When `UMBRELLA_HINDSIGHT_FAIL_CLOSED=1`, a failed Hindsight mirror can abort the caller even though canonical MemPalace already committed.

### Optional: reflexion and candidate queue

```dotenv
UMBRELLA_HINDSIGHT_REFLECT_ENABLED=1
UMBRELLA_HINDSIGHT_MAX_CANDIDATES=3
```

Reflection produces BKB **candidates** in the drive proposal queue — not direct BKB writes. Do not enable in production without review:

```dotenv
# UMBRELLA_HINDSIGHT_AUTO_ACCEPT_CANDIDATES=1
```

### Reflect LLM (when used by the Hindsight stack)

```dotenv
UMBRELLA_HINDSIGHT_LLM_PROVIDER=openai
UMBRELLA_HINDSIGHT_LLM_MODEL=gpt-4o-mini
```

## Do not use in product

| Variable | Why |
|----------|-----|
| `UMBRELLA_MEMORY_DURABLE_BACKEND=hindsight` without `UMBRELLA_ALLOW_HINDSIGHT_ONLY=1` | Factory falls back to canonical; hindsight-only bypasses MemPalace as source of truth |
| `UMBRELLA_ALLOW_HINDSIGHT_ONLY=1` | Experimental export/dev only |
| `UMBRELLA_HINDSIGHT_AUTO_ACCEPT_CANDIDATES=1` | Auto-accepts BKB candidates without gate review |

## Experimental: hindsight-only

Bypasses MemPalace as durable source of truth. Requires explicit opt-in:

```dotenv
UMBRELLA_MEMORY_DURABLE_BACKEND=hindsight
UMBRELLA_ALLOW_HINDSIGHT_ONLY=1
UMBRELLA_HINDSIGHT_ENABLED=1
```

## Live / release smoke tests (not default CI)

```dotenv
UMBRELLA_HINDSIGHT_REAL_TESTS=1
```

```bash
UMBRELLA_HINDSIGHT_REAL_TESTS=1 \
UMBRELLA_HINDSIGHT_ENABLED=1 \
UMBRELLA_HINDSIGHT_REFLECT_ENABLED=1 \
UMBRELLA_MEMORY_DURABLE_BACKEND=dual \
pytest -m hindsight umbrella/tests/test_hindsight_real_optional.py -v
```

## Related configuration

See also [Configuration — Memory](technical-report/11-configuration.md#memory) for general memory-related environment variables (TTL, reflexion gates, etc.).

## See also

- [Umbrella layer — MemPalace](umbrella-layer.md)
- [Architecture](architecture.md)
