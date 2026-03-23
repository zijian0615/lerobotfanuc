"""
fanuc_replay.py
===============
从 HDF5 episode 文件回放已录制的轨迹。

回放语义：
    读取 /action/pose[t] 和 /action/gripper[t]，
    按录制时的相对时间间隔依次发送给机器人。

用法：
    # 直接运行
    python fanuc_replay.py --file episodes/episode_20240101_120000.h5

    # 调速（0.5x 慢放，2.0x 快放）
    python fanuc_replay.py --file episode.h5 --speed 0.5

    # 只回放前 100 步
    python fanuc_replay.py --file episode.h5 --steps 100

    # 循环回放 3 次
    python fanuc_replay.py --file episode.h5 --loop 3

    # 作为模块导入
    from fanuc_replay import FanucReplayController
    ctrl = FanucReplayController(config)
    ctrl.replay("episode.h5", speed_scale=1.0)

回放前安全确认：
    脚本启动后会打印第一帧目标位姿，并要求用户按回车确认，
    再倒计时 3 秒后开始运动。可用 --no-confirm 跳过（自动化场景）。
"""

import argparse
import logging
import time
import os
from typing import Optional, Tuple

import h5py
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

RO_PORT_OPEN          = 3
RO_PORT_CLOSE         = 4
VALVE_SWITCH_DELAY_MS = 0.080   # 秒


# ═══════════════════════════════════════════════════════════════════════════════
# Episode 读取
# ═══════════════════════════════════════════════════════════════════════════════
class EpisodeReader:
    """从 HDF5 文件读取 episode 数据。"""

    def __init__(self, path: str):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Episode file not found: {path}")
        self.path = path
        self._f: Optional[h5py.File] = None

        # 加载到内存，避免回放时 IO 延迟
        with h5py.File(path, "r") as f:
            self.n_steps:       int        = int(f.attrs.get("n_steps", 0))
            self.action_pose:   np.ndarray = f["action/pose"][:]       # [N, 6] float32
            self.action_gripper:np.ndarray = f["action/gripper"][:]    # [N]    bool
            self.action_t:      np.ndarray = f["action/t"][:]          # [N]    float64

        if self.n_steps == 0:
            raise ValueError(f"Episode is empty: {path}")

        logger.info(f"📂 Loaded {self.n_steps} steps from {path}")
        logger.info(f"   Duration: {self.action_t[-1] - self.action_t[0]:.2f}s")
        logger.info(f"   First pose: {self._fmt_pose(self.action_pose[0])}")
        logger.info(f"   Last  pose: {self._fmt_pose(self.action_pose[-1])}")

    @staticmethod
    def _fmt_pose(pose: np.ndarray) -> str:
        x, y, z, w, p, r = pose
        return f"X={x:.1f} Y={y:.1f} Z={z:.1f} W={w:.1f} P={p:.1f} R={r:.1f}"

    def intervals(self) -> np.ndarray:
        """
        返回相邻步之间的原始时间间隔（秒）。
        长度为 n_steps-1，第 0 步间隔定义为 0。
        """
        dt = np.diff(self.action_t)
        dt = np.clip(dt, 0.0, 0.5)   # 防止异常大间隔（录制暂停等）
        return np.concatenate([[0.0], dt])


# ═══════════════════════════════════════════════════════════════════════════════
# Replay 控制器
# ═══════════════════════════════════════════════════════════════════════════════
class FanucReplayController:
    """
    从 episode 文件向机器人回放轨迹。

    依赖 FRCUnifiedClient（ABC 三线程架构），
    可独立使用，也可从 fanuc_record 的配置初始化。
    """

    BUFFER_SIZE = 8   # 与 record 保持一致

    def __init__(self, host: str, port: int, group: int = 1,
                 utool: int = 1, uframe: int = 0,
                 speed_mm_s: int = 150,
                 term_type: str = "CNT", term_value: int = 100):
        # 延迟导入，避免循环依赖
        from .fanuc_communication import FRCUnifiedClient

        self.host       = host
        self.port       = port
        self.group      = group
        self.utool      = utool
        self.uframe     = uframe
        self.speed_mm_s = speed_mm_s
        self.term_type  = term_type
        self.term_value = term_value

        self.client = FRCUnifiedClient()
        self.logger = logging.getLogger(self.__class__.__name__)

    def _connect(self) -> None:
        self.logger.info(f"Connecting to {self.host}:{self.port} ...")
        self.client.connect(self.host, self.port, self.group)
        self.logger.info("✅ Connected")

    def _disconnect(self) -> None:
        self.client.disconnect()

    def _drain_acks(self, timeout: float = 10.0) -> None:
        """等待所有未 ACK 的指令执行完毕。"""
        deadline = time.perf_counter() + timeout
        pending  = set(range(1, self.client.seq_id))   # 所有已发 seq_id
        while pending:
            seq, err = self.client.check_ack()
            if seq is not None:
                pending.discard(seq)
                if err != 0:
                    self.logger.warning(f"ACK error: seq={seq} err=0x{err:06x}")
            if time.perf_counter() > deadline:
                self.logger.warning(f"Drain timeout, {len(pending)} ACKs pending")
                break
            time.sleep(0.005)

    def _send_gripper(self, close: bool, last_pose: Tuple) -> None:
        """安全切换夹爪：先断开当前阀，再打开目标阀。"""
        if close:
            cmds = [
                (RO_PORT_OPEN,  "OFF"),
                (RO_PORT_CLOSE, "ON"),
            ]
        else:
            cmds = [
                (RO_PORT_CLOSE, "OFF"),
                (RO_PORT_OPEN,  "ON"),
            ]
        for port, val in cmds:
            self.client.send_motion(
                last_pose,
                utool=self.utool, uframe=self.uframe,
                speed=self.speed_mm_s,
                term_type=self.term_type, term_value=self.term_value,
                lcb_type="TA", lcb_value=10,
                port_type=2, port_number=port, port_value=val,
            )
            time.sleep(VALVE_SWITCH_DELAY_MS)

    def _reset_gripper(self, last_pose: Optional[Tuple]) -> None:
        if last_pose is None:
            return
        self.logger.info("🔒 复位气阀...")
        for port in (RO_PORT_OPEN, RO_PORT_CLOSE):
            self.client.send_motion(
                last_pose,
                utool=self.utool, uframe=self.uframe,
                speed=self.speed_mm_s,
                term_type=self.term_type, term_value=self.term_value,
                lcb_type="TA", lcb_value=10,
                port_type=2, port_number=port, port_value="OFF",
            )
            time.sleep(0.06)

    def replay(
        self,
        episode_path: str,
        speed_scale:  float = 1.0,
        max_steps:    Optional[int] = None,
        confirm:      bool  = True,
    ) -> bool:
        """
        回放一个 episode。

        Args:
            episode_path: HDF5 文件路径
            speed_scale:  时间缩放因子（<1 慢放，>1 快放，0 = 尽可能快）
            max_steps:    最多回放多少步（None = 全部）
            confirm:      是否在开始前要求用户按回车确认

        Returns:
            True = 正常完成，False = 被中断或出错
        """
        ep = EpisodeReader(episode_path)
        intervals = ep.intervals()
        n = min(ep.n_steps, max_steps) if max_steps else ep.n_steps

        # ── 安全确认 ─────────────────────────────────────────────────────────
        if confirm:
            print("\n" + "="*60)
            print(f"  📋 Episode : {os.path.basename(episode_path)}")
            print(f"  Steps     : {n} / {ep.n_steps}")
            print(f"  Duration  : {sum(intervals[:n]):.2f}s  "
                  f"(×{speed_scale} = {sum(intervals[:n])/max(speed_scale,1e-6):.2f}s)")
            print(f"  First pose: {EpisodeReader._fmt_pose(ep.action_pose[0])}")
            print("="*60)
            input("  ⚠️  请确认机器人附近无障碍物，然后按 Enter 开始回放...")
            for i in range(3, 0, -1):
                print(f"  {i}...")
                time.sleep(1.0)
            print("  🚀 开始！\n")

        # ── 连接 ─────────────────────────────────────────────────────────────
        try:
            self._connect()
        except Exception as e:
            self.logger.error(f"❌ Connect failed: {e}")
            return False

        last_pose:    Optional[Tuple] = None
        last_gripper: bool            = bool(ep.action_gripper[0])
        pending:      set             = set()
        success:      bool            = True

        try:
            t_loop_start = time.perf_counter()

            for i in range(n):
                pose    = tuple(float(v) for v in ep.action_pose[i])
                gripper = bool(ep.action_gripper[i])
                dt      = float(intervals[i])

                # ── 背压控制 ─────────────────────────────────────────────────
                while len(pending) >= self.BUFFER_SIZE:
                    seq, err = self.client.check_ack()
                    if seq is not None:
                        pending.discard(seq)
                        if err != 0:
                            self.logger.warning(f"ACK err seq={seq} 0x{err:06x}")
                    else:
                        time.sleep(0.001)

                # ── 夹爪切换 ─────────────────────────────────────────────────
                if i > 0 and gripper != last_gripper:
                    self.logger.info(
                        f"[{i:4d}] 🦾 夹爪 → {'闭合' if gripper else '打开'}"
                    )
                    self._send_gripper(gripper, pose)
                    last_gripper = gripper

                # ── 发运动指令 ────────────────────────────────────────────────
                ok = self.client.send_motion(
                    pose,
                    utool=self.utool, uframe=self.uframe,
                    speed=self.speed_mm_s,
                    term_type=self.term_type, term_value=self.term_value,
                )
                if not ok:
                    self.logger.error(f"[{i:4d}] send_motion failed, aborting")
                    success = False
                    break

                seq_id = self.client.seq_id - 1
                pending.add(seq_id)
                last_pose = pose

                # ── 消费 ACK ────────────────────────────────────────────────
                while True:
                    seq, err = self.client.check_ack()
                    if seq is None:
                        break
                    pending.discard(seq)

                # ── 进度日志（每 50 步）────────────────────────────────────
                if i % 50 == 0 or i == n - 1:
                    elapsed = time.perf_counter() - t_loop_start
                    self.logger.info(
                        f"[{i+1:4d}/{n}]  {elapsed:.1f}s elapsed  "
                        f"pending={len(pending)}  "
                        f"pose={EpisodeReader._fmt_pose(ep.action_pose[i])}"
                    )

                # ── 定时：按原始间隔 × speed_scale ────────────────────────
                if speed_scale > 0 and dt > 0:
                    time.sleep(dt / speed_scale)

            # ── 等待最后一批 ACK ──────────────────────────────────────────
            if success:
                self.logger.info("⏳ 等待最后一批 ACK ...")
                self._drain_acks()
                self.logger.info("✅ 回放完成")

        except KeyboardInterrupt:
            self.logger.warning("\n⚠️  Ctrl+C — 中断回放")
            success = False

        except Exception as e:
            self.logger.error(f"💥 回放异常: {e}")
            import traceback; traceback.print_exc()
            success = False

        finally:
            self._reset_gripper(last_pose)
            self._disconnect()

        return success


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════
def _parse_args():
    p = argparse.ArgumentParser(
        description="Replay a recorded FANUC episode from HDF5 file"
    )
    p.add_argument("--file",       required=True,            help="HDF5 episode file path")
    p.add_argument("--host",       default="172.30.109.22",  help="Robot IP (default: 172.30.109.22)")
    p.add_argument("--port",       type=int, default=16001,  help="RMI port (default: 16001)")
    p.add_argument("--group",      type=int, default=1,      help="Control group (default: 1)")
    p.add_argument("--utool",      type=int, default=1,      help="UTool number (default: 1)")
    p.add_argument("--uframe",     type=int, default=0,      help="UFrame number (default: 0)")
    p.add_argument("--speed",      type=int, default=150,    help="Speed mm/s (default: 150)")
    p.add_argument("--speed-scale",type=float, default=1.0,
                   help="Playback speed multiplier (0=max speed, default: 1.0)")
    p.add_argument("--steps",      type=int, default=None,   help="Max steps to replay")
    p.add_argument("--loop",       type=int, default=1,      help="Repeat count (default: 1)")
    p.add_argument("--no-confirm", action="store_true",      help="Skip safety confirmation")
    p.add_argument("--term-type",  default="CNT",            help="TermType (default: CNT)")
    p.add_argument("--term-value", type=int, default=100,    help="TermValue (default: 100)")
    return p.parse_args()


def main():
    args = _parse_args()

    ctrl = FanucReplayController(
        host       = args.host,
        port       = args.port,
        group      = args.group,
        utool      = args.utool,
        uframe     = args.uframe,
        speed_mm_s = args.speed,
        term_type  = args.term_type,
        term_value = args.term_value,
    )

    for i in range(args.loop):
        if args.loop > 1:
            logger.info(f"\n{'='*60}")
            logger.info(f"  Loop {i+1} / {args.loop}")
            logger.info(f"{'='*60}")

        ok = ctrl.replay(
            episode_path = args.file,
            speed_scale  = args.speed_scale,
            max_steps    = args.steps,
            confirm      = (not args.no_confirm) and (i == 0),  # 只第一次确认
        )

        if not ok:
            logger.error("回放中断，退出循环")
            break

        if i < args.loop - 1:
            logger.info("⏸  下一次循环前等待 2 秒...")
            time.sleep(2.0)

    logger.info("👋 Replay 退出")


if __name__ == "__main__":
    main()