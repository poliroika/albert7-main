"""
Computer-use runtime abstraction.

Defines the abstract base class that all computer-use backends must implement.
Each backend (Windows native, mock, Selenium, …) subclasses ComputerRuntime
and plugs into the controller without any changes to the upper layers.
"""

from abc import ABC, abstractmethod

from .models import (
    ComputerAction,
    ComputerActionResult,
    ComputerObservation,
    ComputerRuntimeCapabilities,
    ComputerSession,
    ComputerSessionConfig,
    ObservationRequest,
)


class ComputerRuntime(ABC):
    """Abstract runtime for browser, desktop, or remote execution."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable runtime name."""

    @abstractmethod
    def capabilities(self) -> ComputerRuntimeCapabilities:
        """Return runtime capabilities."""

    @abstractmethod
    def start_session(self, config: ComputerSessionConfig) -> ComputerSession:
        """Start a new session."""

    @abstractmethod
    def get_observation(
        self,
        session: ComputerSession,
        request: ObservationRequest | None = None,
    ) -> ComputerObservation:
        """Read the latest observation for a session."""

    @abstractmethod
    def execute(self, session: ComputerSession, action: ComputerAction) -> ComputerActionResult:
        """Execute one action and return its result."""

    @abstractmethod
    def close_session(self, session: ComputerSession) -> ComputerSession:
        """Close a session and return the final snapshot."""
