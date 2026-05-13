"""
Umbrella integration layer.

This package provides the integration layer between ouroboros, gmas, and workspaces.
"""

__version__ = "0.1.0"

# Expose Umbrella API for Ouroboros integration
from umbrella.umbrella_api import UmbrellaAPI, get_umbrella_api

__all__ = ["UmbrellaAPI", "get_umbrella_api"]
