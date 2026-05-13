"""Tests for ``_try_normalize_command`` (P2-3 command-arg validation)."""

from ouroboros.tools.umbrella_tools import _try_normalize_command


def test_list_passthrough() -> None:
    cmd, err = _try_normalize_command(["uv", "run", "python", "-m", "pytest"])
    assert err == ""
    assert cmd == ["uv", "run", "python", "-m", "pytest"]


def test_string_split_simple() -> None:
    cmd, err = _try_normalize_command("python -m pytest -q test_smoke.py")
    assert err == ""
    assert cmd is not None and cmd[:3] == ["python", "-m", "pytest"]


def test_json_array_string() -> None:
    cmd, err = _try_normalize_command('["uv","run","pytest","-q"]')
    assert err == ""
    assert cmd == ["uv", "run", "pytest", "-q"]


def test_multiline_string_rejected() -> None:
    cmd, err = _try_normalize_command("python -m pytest\nrm -rf /")
    assert cmd is None
    assert "newline" in err.lower()


def test_argv_with_embedded_newline_rejected() -> None:
    cmd, err = _try_normalize_command(
        ["python", "-c", "print(1)\nimport os; os.system('x')"]
    )
    assert cmd is None
    assert "newline" in err.lower()


def test_empty_string_rejected() -> None:
    cmd, err = _try_normalize_command("")
    assert cmd is None
    assert err  # any non-empty error message is fine


def test_empty_list_rejected() -> None:
    cmd, err = _try_normalize_command([])
    assert cmd is None
    assert "empty" in err.lower()
