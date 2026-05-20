import pathlib
from typing import Any

from umbrella.utils.result_envelope import ResultEnvelope, ErrorCode


def run_verify_loop(
    *,
    run_id: str,
    workspace_id: str,
    drive_root: pathlib.Path,
    max_attempts: int = 3,
    launcher: Any = None,
) -> ResultEnvelope:
    for attempt in range(max_attempts):
        result = _run_single_verify(
            run_id=run_id,
            workspace_id=workspace_id,
            drive_root=drive_root,
            launcher=launcher,
            attempt=attempt,
        )
        if result.ok:
            return result
        if attempt < max_attempts - 1:
            continue
    return ResultEnvelope.failure(
        ErrorCode.VERIFY_FAILED,
        f"Verification failed after {max_attempts} attempts",
        retryable=False,
        run_id=run_id,
    )


def _run_single_verify(
    *,
    run_id: str,
    workspace_id: str,
    drive_root: pathlib.Path,
    launcher: Any,
    attempt: int,
) -> ResultEnvelope:
    verify_script = drive_root.parent / "verify.sh"
    if not verify_script.exists():
        return ResultEnvelope.success(
            data={"skipped": True, "reason": "no verify.sh found"},
            run_id=run_id,
        )
    import subprocess
    try:
        proc = subprocess.run(
            ["sh", str(verify_script)],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode == 0:
            return ResultEnvelope.success(
                data={"passed": True, "attempt": attempt},
                run_id=run_id,
            )
        return ResultEnvelope.failure(
            ErrorCode.VERIFY_FAILED,
            f"verify.sh exit {proc.returncode}: {proc.stderr[:500]}",
            retryable=True,
            run_id=run_id,
        )
    except Exception as exc:
        return ResultEnvelope.failure(ErrorCode.VERIFY_FAILED, str(exc), run_id=run_id)
