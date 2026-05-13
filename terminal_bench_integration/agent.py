"""Terminal-Bench installed-agent adapter for the umbrella / ouroboros stack."""



import logging
import os
import shlex
import tempfile
from pathlib import Path

from terminal_bench.agents.base_agent import AgentResult
from terminal_bench.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from terminal_bench.terminal.models import TerminalCommand
from terminal_bench.terminal.tmux_session import TmuxSession

from terminal_bench_integration._repo_packer import build_repo_tarball

log = logging.getLogger(__name__)

REPO_ROOT_IN_CONTAINER = "/opt/umbrella"
WORKSPACE_ID = "terminal_bench"

# How long a single Ouroboros attempt may run, in hours. The Terminal-Bench
# harness has its own per-task wall-clock cap (`--agent-timeout-sec`); this
# value should be a hair shorter so that we exit cleanly rather than getting
# SIGKILL'd by the harness.
DEFAULT_TIMEOUT_HOURS = 0.45  # ≈ 27 minutes
DEFAULT_MAX_ROUNDS = 200


class UmbrellaAgent(AbstractInstalledAgent):
    """Ship the entire umbrella repo into the task container, run app_ouroboros.

    Wiring:

    1. ``perform_task`` (overridden) packs the host repo into a tarball and
       copies it into ``/installed-agent/umbrella.tar.gz`` *before* invoking
       the abstract base class.
    2. ``_install_agent_script_path`` returns ``setup.sh``, which the
       abstract base sources inside the tmux session. That script extracts
       the tarball to ``/opt/umbrella``, builds a venv and installs the
       runtime deps.
    3. ``_run_agent_commands`` issues a single shell command:
       ``python -m terminal_bench_integration.run_inside --instruction-file ...``
       which writes the per-task instruction into the seed workspace's
       ``TASK_MAIN.md`` and then runs ``umbrella.app_ouroboros`` against it.
    """

    @staticmethod
    def name() -> str:
        # Custom agents identified by import path do not need to live in the
        # AgentName enum; this string is used purely for log/result labels.
        return "umbrella"

    def __init__(self, *args, **kwargs) -> None:
        # Pop the kwargs we own so they don't leak into BaseAgent.__init__.
        self._timeout_hours = float(
            kwargs.pop("timeout_hours", os.environ.get("UMBRELLA_TB_TIMEOUT_HOURS", DEFAULT_TIMEOUT_HOURS))
        )
        self._max_rounds = int(
            kwargs.pop("max_rounds", os.environ.get("UMBRELLA_TB_MAX_ROUNDS", DEFAULT_MAX_ROUNDS))
        )
        host_repo = kwargs.pop("repo_root", None) or os.environ.get("UMBRELLA_REPO_ROOT")
        if host_repo:
            self._host_repo_root = Path(host_repo).resolve()
        else:
            self._host_repo_root = Path(__file__).resolve().parents[1]

        super().__init__(*args, **kwargs)

        if not (self._host_repo_root / "umbrella" / "app_ouroboros.py").is_file():
            raise RuntimeError(
                f"Could not locate umbrella/app_ouroboros.py under {self._host_repo_root}. "
                "Set UMBRELLA_REPO_ROOT to the repo root or pass repo_root=<path>."
            )

        self._tarball_path: Path | None = None

    # ------------------------------------------------------------------ env

    @property
    def _env(self) -> dict[str, str]:
        """Environment variables exported inside the container before install."""
        # Required: an LLM endpoint reachable from the container.
        # The garfield3 endpoint used during local development is on a
        # corporate network and may or may not be reachable from inside
        # Docker; if it isn't, this will manifest as an OpenAI client error
        # in the agent log -- which is the honest failure mode and not
        # something we should paper over.
        llm_key = os.environ.get("LLM_API_KEY")
        if not llm_key:
            raise RuntimeError(
                "LLM_API_KEY is not set on the host. Set it before running tb. "
                "(See .env in the repo root for the GLM-4.7 / garfield3 values.)"
            )
        llm_base = os.environ.get("LLM_BASE_URL", "")
        llm_model = os.environ.get("LLM_MODEL", "GLM-4.7")

        env = {
            "LLM_API_KEY": llm_key,
            "LLM_BASE_URL": llm_base,
            "LLM_MODEL": llm_model,
            # Ouroboros uses its own env-var names but defaults to LLM_* if
            # OUROBOROS_* are unset; setting them explicitly removes
            # ambiguity.
            "OUROBOROS_LLM_API_KEY": os.environ.get("OUROBOROS_LLM_API_KEY", llm_key),
            "OUROBOROS_LLM_BASE_URL": os.environ.get("OUROBOROS_LLM_BASE_URL", llm_base),
            "OUROBOROS_MODEL": os.environ.get("OUROBOROS_MODEL", llm_model),
            "OUROBOROS_MODEL_FALLBACK_LIST": os.environ.get(
                "OUROBOROS_MODEL_FALLBACK_LIST", llm_model
            ),
            "PYTHONUNBUFFERED": "1",
            # Suppress python "found .pyc" noise inside the container.
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        # Don't leak empty strings.
        return {k: v for k, v in env.items() if v != ""}

    # ---------------------------------------------------------------- install

    @property
    def _install_agent_script_path(self) -> Path:
        return Path(__file__).resolve().parent / "setup.sh"

    # ------------------------------------------------------------------- run

    def _run_agent_commands(self, instruction: str) -> list[TerminalCommand]:
        # The abstract base appends `; tmux wait -S done` to every blocking
        # command. That breaks bash heredocs (the wait line gets folded
        # into the heredoc body), so we ship the instruction as a single
        # base64 payload that `base64 -d` decodes back into the file.
        # base64 is a single-token argument, contains no shell-special
        # characters, and survives any heredoc / tmux shenanigans.
        import base64
        encoded = base64.b64encode(instruction.encode("utf-8")).decode("ascii")
        write_instruction_cmd = (
            "mkdir -p /tmp/tb && "
            f"echo {encoded} | base64 -d > /tmp/tb/instruction.txt && "
            "echo '[umbrella] instruction written ('$(wc -c < /tmp/tb/instruction.txt)' bytes)'"
        )

        run_cmd = " ".join(
            [
                f"{REPO_ROOT_IN_CONTAINER}/.venv/bin/python",
                "-m",
                "terminal_bench_integration.run_inside",
                f"--repo-root={REPO_ROOT_IN_CONTAINER}",
                f"--workspace-id={WORKSPACE_ID}",
                "--instruction-file=/tmp/tb/instruction.txt",
                f"--timeout-hours={self._timeout_hours}",
                f"--max-rounds={self._max_rounds}",
            ]
        )

        return [
            TerminalCommand(
                command=write_instruction_cmd,
                min_timeout_sec=1.0,
                max_timeout_sec=30.0,
                block=True,
                append_enter=True,
            ),
            TerminalCommand(
                command=run_cmd,
                min_timeout_sec=0.0,
                max_timeout_sec=float("inf"),
                block=True,
                append_enter=True,
            ),
        ]

    # ------------------------------------------------------- perform_task

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
    ) -> AgentResult:
        """Pack the host repo, ship it into the container, then delegate."""
        tmpdir = Path(tempfile.mkdtemp(prefix="umbrella-tb-"))
        tarball = tmpdir / "umbrella.tar.gz"
        try:
            log.info("Building umbrella tarball from %s", self._host_repo_root)
            build_repo_tarball(self._host_repo_root, tarball)

            log.info("Copying tarball into container at /installed-agent/")
            session.copy_to_container(
                tarball,
                container_dir="/installed-agent",
                container_filename="umbrella.tar.gz",
            )

            self._tarball_path = tarball
            return super().perform_task(instruction, session, logging_dir)
        finally:
            try:
                if tarball.exists():
                    tarball.unlink()
                tmpdir.rmdir()
            except OSError:
                log.debug("Tempdir cleanup failed (non-fatal)", exc_info=True)
            self._tarball_path = None
