"""
Processor module for robot actions and observations.

This module defines the fundamental types and utilities for processing
robot actions and observations in the LeRobot FANUC system.
"""

from typing import Any

# Type alias for robot actions (dict with arbitrary keys and values)
RobotAction = dict[str, Any]

# Type alias for robot observations (dict with arbitrary keys and values)
RobotObservation = dict[str, Any]

__all__ = [
    "RobotAction",
    "RobotObservation",
]
