"""Very simple end-to-end smoke check for computer_use + LLM summary."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gmas.tools import ComputerUseTool, create_openai_caller
from gmas.tools.computer_use import observation_to_openai_content
from gmas.tools.computer_use.models import ComputerObservation
from gmas.utils import configure_console, load_dotenv_file


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    msg = f"Missing required environment variable: {name}"
    raise RuntimeError(msg)


def main() -> None:
    configure_console()
    load_dotenv_file(Path(__file__).resolve().parents[1] / ".env")

    llm = create_openai_caller(
        api_key=_require_env("LLM_API_KEY"),
        base_url=_require_env("LLM_BASE_URL"),
        model=_require_env("LLM_MODEL"),
        temperature=0.0,
        max_tokens=220,
        http_proxy=os.environ.get("LLM_HTTP_PROXY"),
    )

    runtime_name = os.getenv("GMAS_COMPUTER_USE_RUNTIME", "auto")
    report_root = Path.cwd() / ".gmas" / "artifacts" / "computer_use_smoke"
    report_root.mkdir(parents=True, exist_ok=True)

    with ComputerUseTool(runtime_name=runtime_name) as computer_use:
        start_result = computer_use.execute(
            operation="start",
            config={
                "runtime_name": runtime_name,
                "observation": {
                    "mode": "standard",
                    "include_screenshot": True,
                    "include_text": True,
                    "include_windows": True,
                },
            },
        )
        if not start_result.success:
            raise RuntimeError(start_result.error or "computer_use start failed")

        payload = json.loads(start_result.output)
        session = payload.get("session") or {}
        observation = payload.get("observation") or {}
        screenshot = observation.get("screenshot") or {}
        screenshot_path = screenshot.get("path")
        runtime_used = session.get("runtime_name", runtime_name)
        session_id = session.get("session_id")

        obs_model = ComputerObservation.model_validate(observation)
        content = observation_to_openai_content(obs_model)
        content.append(
            {
                "type": "text",
                "text": (
                    "Кратко на русском (1-2 предложения) опиши, что видно на скриншоте/экране. "
                    "Если неуверен, так и напиши."
                ),
            }
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "Ты кратко и фактически описываешь экран пользователя."},
            {"role": "user", "content": content},
        ]

        try:
            answer = llm(messages)
        except Exception:
            text_only = [block for block in content if block.get("type") == "text"]
            messages[1]["content"] = text_only
            answer = llm(messages)

        if not isinstance(answer, str):
            answer = str(answer)

        computer_use.execute(operation="close", session_id=session_id)

    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "runtime": runtime_used,
        "session_id": session_id,
        "screenshot_path": screenshot_path,
        "answer": answer.strip(),
    }
    report_path = report_root / "last_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("Runtime:", runtime_used)
    print("Answer:", report["answer"])
    print("Screenshot:", screenshot_path or "n/a")
    print("Report:", str(report_path))


if __name__ == "__main__":
    main()
