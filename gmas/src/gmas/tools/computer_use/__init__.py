"""Computer-use tool package."""

from .client import (
    ComputerUseClient,
    artifact_to_base64_url,
    build_computer_use_full_schema,
    build_computer_use_tool_schema,
    observation_to_openai_content,
)
from .controller import ComputerUseController
from .framework import ComputerUseTool
from .mock import MockComputerRuntime

try:
    from .linux import LinuxComputerRuntime
except ImportError:
    LinuxComputerRuntime = None  # ty: ignore[invalid-assignment]

try:
    from .macos import MacOSComputerRuntime
except ImportError:
    MacOSComputerRuntime = None  # ty: ignore[invalid-assignment]
from .models import (
    ComputerAction,
    ComputerActionResult,
    ComputerActionType,
    ComputerArtifact,
    ComputerBounds,
    ComputerCoordinate,
    ComputerObservation,
    ComputerRuntimeCapabilities,
    ComputerSession,
    ComputerSessionConfig,
    ComputerUseCommand,
    ComputerUseOperation,
    ComputerUseResponse,
    ComputerViewport,
    MouseButton,
    ObservationMode,
    ObservationRequest,
    SafetyMode,
    UIElementRef,
    WindowInfo,
)
from .runtime import ComputerRuntime

try:
    from .windows import WindowsComputerRuntime
except ImportError:
    WindowsComputerRuntime = None  # ty: ignore[invalid-assignment]

__all__ = [
    # Models
    "ComputerAction",
    "ComputerActionResult",
    "ComputerActionType",
    "ComputerArtifact",
    "ComputerBounds",
    "ComputerCoordinate",
    "ComputerObservation",
    "ComputerRuntime",
    "ComputerRuntimeCapabilities",
    "ComputerSession",
    "ComputerSessionConfig",
    # Tool adapters
    "ComputerUseClient",
    "ComputerUseCommand",
    # Controller & runtimes
    "ComputerUseController",
    "ComputerUseOperation",
    "ComputerUseResponse",
    "ComputerUseTool",
    "ComputerViewport",
    "MockComputerRuntime",
    "MouseButton",
    "ObservationMode",
    "ObservationRequest",
    "SafetyMode",
    "UIElementRef",
    "WindowInfo",
    # Multimodal / base64 helpers
    "artifact_to_base64_url",
    "build_computer_use_full_schema",
    # Schema builders
    "build_computer_use_tool_schema",
    "observation_to_openai_content",
]

if WindowsComputerRuntime is not None:
    __all__ += ["WindowsComputerRuntime"]
if LinuxComputerRuntime is not None:
    __all__ += ["LinuxComputerRuntime"]
if MacOSComputerRuntime is not None:
    __all__ += ["MacOSComputerRuntime"]
