import json

from ouroboros.loop import (
    _append_round_io,
    _round_full_input_snapshot,
    _round_input_snapshot,
)


def test_append_round_io_writes_full_phase_input_sidecar(tmp_path):
    logs = tmp_path / "logs"
    full_tail = "x" * 3000
    messages = [
        {"role": "system", "content": "phase system"},
        {
            "role": "user",
            "content": f"LLM_API_KEY=secret-value keep this full tail {full_tail}",
        },
    ]

    _append_round_io(
        logs,
        task_id="run-1:execute",
        round_idx=2,
        round_event={
            "ts": "2026-05-27T00:00:00Z",
            "phase": "execute",
            "model": "test-model",
        },
        input_snapshot=_round_input_snapshot(messages),
        full_input_snapshot=_round_full_input_snapshot(messages),
        msg={"role": "assistant", "content": "ok"},
    )

    rows = [
        json.loads(line)
        for line in (logs / "round_io.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["full_input_path"]
    sidecar = tmp_path / "logs" / "round_io_full"
    full_files = list(sidecar.glob("*.json"))
    assert len(full_files) == 1
    payload = json.loads(full_files[0].read_text(encoding="utf-8"))
    content = payload["input"]["messages"][1]["content"]
    assert full_tail in content
    assert "secret-value" not in content
    assert "[redacted]" in content
