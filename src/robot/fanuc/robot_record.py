"""
robot_record.py
===============
数据采集脚本，在 teleop 基础上记录 (state, obs, action) 三元组。

采集语义（标准 MDP）：
    state[t]  ──►  action[t]  ──►  state[t+1]

架构：单 TCP 连接，ABC 三线程
    线程 A（主循环驱动）：发 FRC_LinearMotion，非阻塞
    线程 B（FRCUnifiedClient 内部）：定时发 FRC_ReadCartesianPosition，非阻塞
    线程 C（FRCUnifiedClient 内部）：死循环收包，按字段分发
        有 "Position"   → 更新 latest_state
        有 "SequenceID" → 放入 ack_queue

每步主循环顺序：
    1. client.latest_state()   非阻塞，取 ring buffer 最新 state
    2. camera.latest()         非阻塞，取相机最新帧
    3. client.send_motion()    非阻塞，发运动指令
    4. writer.append(record)

保存格式：HDF5
    /action/pose        float32 [N, 6]
    /action/gripper     bool    [N]
    /action/t           float64 [N]
    /state/pose         float32 [N, 6]
    /state/t            float64 [N]
    /obs/frames         uint8   [N,H,W,3]
    /obs/t              float64 [N]
"""

import socket
import json
import time
import math
import threading
import logging
import os
from collections import deque
from datetime import datetime
from typing import Optional, Tuple, List

import cv2
import numpy as np

from .fanuc_config import TeleopConfig
from .fanuc_transport import UDPTransport
from .fanuc_communication import FRCUnifiedClient
from .data_store import MultiEpisodeDataStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

RO_PORT_OPEN          = 3
RO_PORT_CLOSE         = 4
VALVE_SWITCH_DELAY_MS = 80


# ═══════════════════════════════════════════════════════════════════════════════
# 1. UDP 数据接收器（不变）
# ═══════════════════════════════════════════════════════════════════════════════
class UDPDataReceiver:
    def __init__(self, config):
        self.config = config
        self.transport = UDPTransport(config.host, config.port, config.buffer_size)
        self.latest_frame: Optional[Tuple] = None
        self.latest_grip: bool = False
        self.grip_changed: bool = False
        self.lock = threading.Lock()
        self.running = False
        self.thread = None
        self.logger = logging.getLogger(self.__class__.__name__)

    def start(self) -> None:
        self.transport.bind()
        self.running = True
        self.thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.thread.start()
        self.logger.info(f"✅ UDP Receiver on {self.config.host}:{self.config.port}")

    def _recv_loop(self) -> None:
        while self.running:
            try:
                data, _ = self.transport.recv()
                payload = json.loads(data.decode("utf-8"))
                x = payload.get("x"); y = payload.get("y"); z = payload.get("z")
                w = payload.get("w"); p = payload.get("p"); r = payload.get("r")
                grip = bool(payload.get("gripButton", False))
                if None in (x, y, z, w, p, r):
                    continue
                if any(math.isnan(v) or math.isinf(v) for v in (x, y, z, w, p, r)):
                    continue
                with self.lock:
                    self.latest_frame = (x, y, z, w, p, r)
                    if grip != self.latest_grip:
                        self.grip_changed = True
                    self.latest_grip = grip
            except json.JSONDecodeError:
                pass
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.logger.error(f"UDP error: {e}")

    def get_latest(self) -> Tuple[Optional[Tuple], bool, bool]:
        with self.lock:
            frame        = self.latest_frame
            grip_state   = self.latest_grip
            grip_changed = self.grip_changed
            self.latest_frame = None
            self.grip_changed = False
            return frame, grip_state, grip_changed

    def stop(self) -> None:
        self.running = False
        self.transport.close()


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Camera Capture（不变）
# ═══════════════════════════════════════════════════════════════════════════════
class CameraCapture:
    BUF_SIZE = 300

    def __init__(self, cam_id: int = 0, rate_hz: float = 30.0,
                 width: int = 640, height: int = 480):
        self.cam_id  = cam_id
        self.rate_hz = rate_hz
        self.width   = width
        self.height  = height
        self._buf: deque = deque(maxlen=self.BUF_SIZE)
        self._lock    = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._cap:    Optional[cv2.VideoCapture]  = None
        self.logger = logging.getLogger(self.__class__.__name__)

    def start(self) -> None:
        self._cap = cv2.VideoCapture(self.cam_id)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH,  self.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open camera {self.cam_id}")
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.logger.info(f"✅ Camera {self.cam_id} @ {self.rate_hz} Hz")

    def _loop(self) -> None:
        interval = 1.0 / self.rate_hz
        while self._running:
            t0 = time.perf_counter()
            ret, frame = self._cap.read()
            t_cap = time.perf_counter()
            if ret:
                with self._lock:
                    self._buf.append({"t": t_cap, "frame": frame})
            elapsed = time.perf_counter() - t0
            time.sleep(max(0.0, interval - elapsed))

    def latest(self) -> Optional[dict]:
        with self._lock:
            return self._buf[-1] if self._buf else None

    def stop(self) -> None:
        self._running = False
        if self._cap:
            self._cap.release()
        self.logger.info("Camera stopped")





# ═══════════════════════════════════════════════════════════════════════════════
# 4. 主 Record 控制器
# ═══════════════════════════════════════════════════════════════════════════════
class FanucRecordController:
    """
    主循环顺序（MDP 语义：state[t] → action[t]）：

        1. client.latest_state()  ← state[t]，非阻塞，由线程 B/C 异步维护
        2. camera.latest()        ← obs[t]，非阻塞
        3. client.send_motion()   ← action[t]，非阻塞
        4. writer.append(record)

    多集模式：
        Ctrl+C ×1 → 结束当前 episode，询问是否继续
        Ctrl+C ×2 → 结束当前 episode，终止程序
        Ctrl+C ×3 → 丢弃当前 episode，终止程序
    """

    BUFFER_SIZE        = 8
    INTER_PACKET_DELAY = 0.002

    def __init__(self, config: TeleopConfig,
                 cam_id: int        = 0,
                 cam_rate_hz: float = 30.0,
                 data_root: str     = "./data"):
        self.config         = config
        self.udp_receiver   = UDPDataReceiver(config.udp)
        self.client         = FRCUnifiedClient()
        self.camera         = CameraCapture(cam_id=cam_id, rate_hz=cam_rate_hz)
        self.data_store     = MultiEpisodeDataStore(
            data_root=data_root,
            fps=cam_rate_hz,
            num_cameras=1
        )
        self.pending_seq_ids: set = set()
        self._interrupt_count = 0
        self._episode_count = 0
        self._continue_mode = True
        self.logger = logging.getLogger(self.__class__.__name__)

    def _build_safe_gripper_cmds(self, target_close: bool) -> list:
        if target_close:
            return [
                {"port": RO_PORT_OPEN,  "val": "OFF", "delay_before_ms": 0},
                {"port": RO_PORT_CLOSE, "val": "ON",  "delay_before_ms": VALVE_SWITCH_DELAY_MS},
            ]
        else:
            return [
                {"port": RO_PORT_CLOSE, "val": "OFF", "delay_before_ms": 0},
                {"port": RO_PORT_OPEN,  "val": "ON",  "delay_before_ms": VALVE_SWITCH_DELAY_MS},
            ]

    def _reset_gripper_safe(self, last_known_pose: Optional[Tuple]) -> None:
        if last_known_pose is None:
            self.logger.warning("⚠️  无有效坐标，跳过气阀复位")
            return
        self.logger.info("🔒 安全复位：断开所有气阀...")
        for port in (RO_PORT_OPEN, RO_PORT_CLOSE):
            try:
                self.client.send_motion(
                    last_known_pose,
                    utool=self.config.robot.utool,
                    uframe=self.config.robot.uframe,
                    speed=self.config.robot.speed_mm_s,
                    term_type=self.config.robot.term_type,
                    term_value=self.config.robot.term_value,
                    lcb_type="TA", lcb_value=10,
                    port_type=2, port_number=port, port_value="OFF",
                )
                time.sleep(0.06)
            except Exception as e:
                self.logger.error(f"RO[{port}] 复位异常: {e}")
        self.logger.info("🔒 气阀复位完成")

    def run(self) -> None:
        self.logger.info("=" * 60)
        self.logger.info("🎬 FANUC Multi-Episode Record Mode")
        self.logger.info("   (state → obs → action)")
        self.logger.info("   Ctrl+C ×1 → end episode → continue")
        self.logger.info("   Ctrl+C ×2 → end episode → exit")
        self.logger.info("   Ctrl+C ×3 → discard episode → exit")
        self.logger.info("=" * 60)

        self.udp_receiver.start()

        # 建立单连接，启动线程 B、C
        try:
            self.client.connect(
                self.config.robot.host,
                self.config.robot.port,
                self.config.robot.group,
            )
        except Exception as e:
            self.logger.error(f"❌ Client connect failed: {e}")
            self.udp_receiver.stop()
            return

        obs_ok = True
        try:
            self.camera.start()
        except Exception as e:
            self.logger.error(f"❌ Camera start failed: {e}")
            self.logger.warning("⚠️  Obs will be None for all steps")
            obs_ok = False

        last_sent_pose: Optional[Tuple] = None
        last_gripper_state = False
        lcb_queue: list    = []
        MIN_DIST_MM        = 0.01

        try:
            while self._continue_mode:
                # 新 episode
                self._episode_count += 1
                episode_id = f"episode_{self._episode_count:06d}"
                task_desc = input(f"Task description for {episode_id} (optional): ")
                self.data_store.start_episode(episode_id, task_description=task_desc)
                self._interrupt_count = 0
                
                try:
                    while True:
                        # 1. 处理 ACK ──────────────────────────────────────────────────
                        while True:
                            ack_seq, ack_err = self.client.check_ack()
                            if ack_seq is None:
                                break
                            if ack_seq in self.pending_seq_ids:
                                self.pending_seq_ids.remove(ack_seq)
                                if ack_err != 0:
                                    self.logger.warning(f"⚠️ Seq {ack_seq} err {ack_err}")

                        # 2. 背压控制 ──────────────────────────────────────────────────
                        if len(self.pending_seq_ids) >= self.BUFFER_SIZE:
                            time.sleep(0.001)
                            continue

                        # 3. 获取 UDP 数据 ─────────────────────────────────────────────
                        target_pose, current_gripper_state, grip_changed = \
                            self.udp_receiver.get_latest()

                        # 4. 夹爪边缘检测 ─────────────────────────────────────────────
                        if grip_changed:
                            self.logger.info(
                                f"🦾 夹爪切换 → {'闭合' if current_gripper_state else '打开'}"
                            )
                            if lcb_queue:
                                lcb_queue.clear()
                            lcb_queue.extend(self._build_safe_gripper_cmds(current_gripper_state))
                            last_gripper_state = current_gripper_state

                        # 5. pose 为空时用上一帧坐标补发 IO ───────────────────────────
                        if target_pose is None:
                            if lcb_queue and last_sent_pose is not None:
                                target_pose = last_sent_pose
                            else:
                                time.sleep(0.001)
                                continue

                        # 6. 取下一条 IO 指令 ─────────────────────────────────────────
                        current_lcb = lcb_queue.pop(0) if lcb_queue else None
                        if current_lcb and current_lcb.get("delay_before_ms", 0) > 0:
                            time.sleep(current_lcb["delay_before_ms"] / 1000.0)

                        # 7. 空间滤波 ──────────────────────────────────────────────────
                        if last_sent_pose is not None:
                            dx = target_pose[0] - last_sent_pose[0]
                            dy = target_pose[1] - last_sent_pose[1]
                            dz = target_pose[2] - last_sent_pose[2]
                            if math.sqrt(dx**2 + dy**2 + dz**2) < MIN_DIST_MM and not current_lcb:
                                time.sleep(0.001)
                                continue

                        # 8a. 取 state[t]：非阻塞，线程 C 异步维护
                        state_pose_val, t_state = self.client.latest_state()

                        # 8b. 取 obs[t]：非阻塞
                        obs_snap = self.camera.latest() if obs_ok else None
                        t_obs    = obs_snap["t"]         if obs_snap else None

                        # 8c. 发 action[t]：非阻塞
                        ok = self.client.send_motion(
                            target_pose,
                            utool=self.config.robot.utool,
                            uframe=self.config.robot.uframe,
                            speed=self.config.robot.speed_mm_s,
                            term_type=self.config.robot.term_type,
                            term_value=self.config.robot.term_value,
                            lcb_type="TA" if current_lcb else None,
                            lcb_value=10  if current_lcb else 0,
                            port_type=2   if current_lcb else None,
                            port_number=current_lcb["port"] if current_lcb else None,
                            port_value=current_lcb["val"]   if current_lcb else None,
                        )

                        if not ok:
                            continue

                        t_action = time.perf_counter()
                        seq_id   = self.client.seq_id - 1
                        self.pending_seq_ids.add(seq_id)
                        last_sent_pose = target_pose

                        # 8d. 记录到新的存储系统
                        self.data_store.append({
                            "t_action":       t_action,
                            "action_pose":    np.array(target_pose, dtype=np.float32),
                            "action_gripper": current_gripper_state,
                            "t_state":        t_state,
                            "state_pose":     np.array(state_pose_val, dtype=np.float32) if state_pose_val else None,
                            "t_obs":          t_obs,
                            "obs_frame":      obs_snap["frame"].copy() if obs_snap else None,
                            "camera_id":      0,
                        })

                        if len(self.pending_seq_ids) < self.BUFFER_SIZE:
                            time.sleep(self.INTER_PACKET_DELAY)

                except KeyboardInterrupt:
                    self._interrupt_count += 1
                    self.logger.warning(f"\n⚠️  Ctrl+C (×{self._interrupt_count})")
                    
                    if self._interrupt_count == 1:
                        self.logger.info("   → 结束当前 episode...")
                        self._reset_gripper_safe(last_sent_pose)
                        self.data_store.end_episode()
                        
                        # 询问是否继续
                        response = input("Continue recording? (y/n): ").strip().lower()
                        if response != "y":
                            self._continue_mode = False
                            
                    elif self._interrupt_count == 2:
                        self.logger.info("   → 丢弃当前 episode，退出")
                        self._reset_gripper_safe(last_sent_pose)
                        # 不调用 end_episode，直接 break
                        self._continue_mode = False
                    else:
                        self.logger.warning("   → 强制退出")
                        self._continue_mode = False

        except Exception as e:
            self.logger.error(f"💥 异常: {e}")
            import traceback
            traceback.print_exc()
            self._reset_gripper_safe(last_sent_pose)
            self._continue_mode = False

        finally:
            self.udp_receiver.stop()
            self.client.disconnect()
            if obs_ok:
                self.camera.stop()
            
            # 最终落盘所有数据
            self.data_store.finalize()
            self.logger.info(f"✅ Recorded {self._episode_count} episodes")
            self.logger.info(f"📂 Data saved to: {self.data_store.data_root}")
            self.logger.info("👋 Record 退出")


# ═══════════════════════════════════════════════════════════════════════════════
# Main Entry Point
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    from .fanuc_config import TeleopConfig

    config = TeleopConfig()
    try:
        controller = FanucRecordController(config, data_root="./data")
        controller.run()
    except KeyboardInterrupt:
        logger.info("Program interrupted")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()