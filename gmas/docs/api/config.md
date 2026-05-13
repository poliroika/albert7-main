# Config API

## Configuration

```python
from gmas.config import FrameworkSettings

settings = FrameworkSettings()
```

## Environment Variables

```bash
# API Configuration
export GMAS_API_KEY="sk-..."
export GMAS_BASE_URL="https://api.provider.example"
export GMAS_API_KEY_FILE=/path/to/keyfile

# LLM Configuration
export GMAS_MODEL="gpt-4"
export GMAS_TEMPERATURE="0.7"
export GMAS_MAX_TOKENS="1000"

# Request Configuration
export GMAS_TIMEOUT="30"
export GMAS_MAX_RETRIES="3"
export GMAS_RETRY_DELAY="1"

# Logging
export GMAS_LOG_LEVEL="INFO"
```

## Settings Usage

```python
from gmas.config import FrameworkSettings

settings = FrameworkSettings()

# Access settings
api_key = settings.resolved_api_key
model = settings.model
temperature = settings.temperature

# Use with LLM
caller = create_openai_caller(
    api_key=settings.resolved_api_key,
    model=settings.model,
)
```

## Validation

Settings are validated on load. Invalid values raise errors:

```python
# Missing required key
# RuntimeError: GMAS_API_KEY is required

# Invalid value
# ValidationError: temperature must be between 0 and 2
```
