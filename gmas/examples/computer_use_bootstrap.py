"""Bootstrap example for the standalone computer-use package."""

from gmas.tools.computer_use import (
    ComputerAction,
    ComputerActionType,
    ComputerSessionConfig,
    ComputerUseClient,
    ComputerUseController,
    ComputerUseOperation,
    ObservationMode,
    ObservationRequest,
    WindowsComputerRuntime,
)
from gmas.utils import configure_console


def main() -> None:
    configure_console()

    if WindowsComputerRuntime is None:
        message = "WindowsComputerRuntime is unavailable in this environment"
        raise RuntimeError(message)

    tool = ComputerUseClient(ComputerUseController(WindowsComputerRuntime()))

    start = tool.execute(
        operation=ComputerUseOperation.START,
        config=ComputerSessionConfig(
            observation=ObservationRequest(
                mode=ObservationMode.DETAILED,
                include_clipboard=True,
                active_window_only=True,
            ),
        ),
    )
    session = start.session
    if session is None:
        message = "computer_use example did not create a session"
        raise RuntimeError(message)

    act = tool.execute(
        operation=ComputerUseOperation.ACT,
        session_id=session.session_id,
        action=ComputerAction(
            action_type=ComputerActionType.EXTRACT_TEXT,
            metadata={"strategy": "window_title"},
        ),
    )

    close = tool.execute(
        operation=ComputerUseOperation.CLOSE,
        session_id=session.session_id,
    )

    screenshot_path = start.observation.screenshot.path if start.observation and start.observation.screenshot else "n/a"
    print("Tool:", tool.name)
    print("Capabilities:", start.capabilities.model_dump() if start.capabilities else {})
    print("Artifact root:", str(start.session.artifact_root) if start.session else "n/a")
    print("Title:", act.observation.title if act.observation else "n/a")
    print("Screenshot:", screenshot_path)
    print("Summary:", act.action_result.summary if act.action_result else "n/a")
    print("Closed:", close.session.status if close.session else "n/a")


if __name__ == "__main__":
    main()
