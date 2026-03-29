
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

保存格式：
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
from ...camera import DualCameraManager

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
# 2. 主 Record 控制器
# ═══════════════════════════════════════════════════════════════════════════════
class FanucRecordController:
    """
    主循环顺序（MDP 语义：state[t] → action[t]）：

        1. client.latest_state()  ← state[t]，非阻塞，由线程 B/C 异步维护
        2. camera.get_latest()    ← obs[t]，非阻塞，获取两个摄像头帧
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
                 cam_ids: list        = None,
                 cam_rate_hz: float = 30.0,
                 data_root: str     = "./data"):
        if cam_ids is None:
            cam_ids = [0, 1]
        
        self.config         = config
        self.udp_receiver   = UDPDataReceiver(config.udp)
        self.client         = FRCUnifiedClient()
        self.camera_manager = DualCameraManager(
            cam_ids=cam_ids,
            rate_hz=cam_rate_hz,
            display=True,
            display_scale=0.5
        )
        self.data_store     = MultiEpisodeDataStore(
            data_root=data_root,
            fps=cam_rate_hz,
            num_cameras=len(cam_ids),
            camera_names=[f"camera_{i}" for i in range(len(cam_ids))],
            chunk_size=1000
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
        display_enabled = True
        try:
            self.camera_manager.start()
            self.logger.info(f"⏳ 等待摄像头初始化...")
            
            # 等待所有摄像头就绪（最多等5秒）
            start_wait = time.perf_counter()
            while not self.camera_manager.is_ready() and time.perf_counter() - start_wait < 5.0:
                time.sleep(0.1)
            
            if not self.camera_manager.is_ready():
                self.logger.warning("⚠️  部分摄像头未就绪，继续开始录制")
            else:
                self.logger.info("✅ 所有摄像头已就绪")
                self.logger.info("📹 摄像头正在采集...")
                if self.camera_manager.display:
                    self.logger.info("🖥️  实时显示已启用（主线程）")
        except Exception as e:
            self.logger.error(f"❌ Camera start failed: {e}")
            self.logger.warning("⚠️  Obs will be None for all steps")
            obs_ok = False
            display_enabled = False

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

                        # 8b. 取 obs[t]：非阻塞，从两个摄像头获取帧
                        camera_frames = {}
                        if obs_ok:
                            all_frames = self.camera_manager.get_latest()
                            camera_frames = {k: v for k, v in all_frames.items() if v is not None}
                        
                        # 8b-display. 实时显示（主线程）
                        if display_enabled and self.camera_manager.display and camera_frames:
                            try:
                                display_h = int(self.camera_manager.height * self.camera_manager.display_scale)
                                display_w = int(self.camera_manager.width * self.camera_manager.display_scale)
                                
                                # 创建合成图像（并排显示）
                                combined = np.zeros((display_h, display_w * len(camera_frames), 3), dtype=np.uint8)
                                
                                for idx, (cam_key, cam_data) in enumerate(sorted(camera_frames.items())):
                                    frame = cam_data["frame"]
                                    resized = cv2.resize(frame, (display_w, display_h))
                                    combined[:, idx*display_w:(idx+1)*display_w] = resized
                                    
                                    # 添加摄像头 ID 标签
                                    cv2.putText(
                                        combined,
                                        f"Camera {cam_data['camera_id']}",
                                        (idx*display_w + 10, 30),
                                        cv2.FONT_HERSHEY_SIMPLEX,
                                        1.0,
                                        (0, 255, 0),
                                        2
                                    )
                                
                                # 显示（主线程安全）
                                cv2.imshow("Dual Camera Feed", combined)
                                
                                # 检查按键（1ms 超时）
                                key = cv2.waitKey(1) & 0xFF
                                if key == ord('q'):
                                    self.logger.info("🔌 用户关闭显示窗口")
                                    cv2.destroyAllWindows()
                                    display_enabled = False
                            except Exception as e:
                                self.logger.warning(f"⚠️  Display error: {e}")
                                display_enabled = False

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

                        # 8d. 记录到新的存储系统（支持多个摄像头）
                        # 为每个摄像头创建一条记录，如果摄像头无帧则为 None
                        if camera_frames:
                            # 有摄像头数据，为每个摄像头各创建一条记录
                            for cam_key, cam_data in camera_frames.items():
                                self.data_store.append({
                                    "t_action":       t_action,
                                    "action_pose":    np.array(target_pose, dtype=np.float32),
                                    "action_gripper": current_gripper_state,
                                    "t_state":        t_state,
                                    "state_pose":     np.array(state_pose_val, dtype=np.float32) if state_pose_val else None,
                                    "t_obs":          cam_data["t"],
                                    "obs_frame":      cam_data["frame"].copy(),
                                    "camera_id":      cam_data["camera_id"],
                                })
                        else:
                            # 没有摄像头数据，至少创建一条记录用于状态和动作同步
                            # （可选：如果不希望无摄像头数据的时刻被保存，删除这个分支）
                            self.data_store.append({
                                "t_action":       t_action,
                                "action_pose":    np.array(target_pose, dtype=np.float32),
                                "action_gripper": current_gripper_state,
                                "t_state":        t_state,
                                "state_pose":     np.array(state_pose_val, dtype=np.float32) if state_pose_val else None,
                                "t_obs":          None,
                                "obs_frame":      None,
                                "camera_id":      -1,  # 标记无效摄像头
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
                self.camera_manager.stop()
            
            # 关闭显示窗口
            if display_enabled:
                cv2.destroyAllWindows()
            
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