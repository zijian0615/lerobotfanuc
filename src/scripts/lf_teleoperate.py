from __future__ import annotations

"""
High-level teleoperation entry script for lerobotfanuc.

This module provides a clean, user-facing API:
- TeleoperateConfig: high-level configuration for teleoperation
- teleop_loop:       runs a single teleoperation session
- teleoperate:       convenience wrapper around teleop_loop
- main:              CLI-style entry point

Robot-specific details (FANUC controller, UDP receiver, etc.) live
under `lerobotfanuc.src.robot.*`.
"""

from dataclasses import dataclass
from typing import Optional

from ..robot.fanuc.fanuc_config import (
    FanucRobotConfig,
    UDPReceiverConfig,
    PerformanceConfig,
    TeleopConfig as FanucTeleopConfig,
)
from ..robot.fanuc.robot_teleoperate import FanucTeleopControllerSlidingWindow


@dataclass
class TeleoperateConfig:
    """
    High-level teleoperation configuration.

    This is a thin wrapper around the FANUC-specific TeleopConfig so that
    callers only depend on `lerobotfanuc.src.scripts` rather than the
    underlying robot implementation modules.
    """

    robot: Optional[FanucRobotConfig] = None
    udp: Optional[UDPReceiverConfig] = None
    performance: Optional[PerformanceConfig] = None

    def to_fanuc_config(self) -> FanucTeleopConfig:
        """
        Convert this high-level config into the FANUC-specific TeleopConfig
        used by the low-level controller.
        """
        return FanucTeleopConfig(
            robot=self.robot,
            udp=self.udp,
            performance=self.performance,
        )


def teleop_loop(cfg: TeleoperateConfig) -> None:
    """
    Core teleoperation loop using the provided configuration.
    """
    fanuc_cfg = cfg.to_fanuc_config()
    controller = FanucTeleopControllerSlidingWindow(fanuc_cfg)
    controller.run()


def teleoperate(cfg: Optional[TeleoperateConfig] = None) -> None:
    """
    High-level API for starting a teleoperation session.

    Example:
        from lerobotfanuc.src.scripts.lf_teleoperate import (
            TeleoperateConfig,
            teleoperate,
        )

        cfg = TeleoperateConfig()
        teleoperate(cfg)
    """
    if cfg is None:
        cfg = TeleoperateConfig()
    teleop_loop(cfg)


def main() -> None:
    """
    CLI-style entry point.

    This is intentionally minimal: for advanced usage, construct a
    TeleoperateConfig in Python and call `teleoperate` directly.
    """
    teleoperate()


if __name__ == "__main__":
    main()

