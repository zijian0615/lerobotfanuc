# """
# fanuc_record.py
# ===============
# 数据采集脚本，在 teleop 基础上记录 (state, obs, action) 三元组。

# 采集语义（标准 MDP）：
#     state[t]  ──►  action[t]  ──►  state[t+1]
#     即：先读当前机器人状态，再发出本步指令。

# 每步主循环顺序：
#     1. FRC_ReadCartesianPosition  （串行阻塞，读 state[t]）
#     2. camera.latest()            （非阻塞，从 ring buffer 取最新帧）
#     3. frc_sender.send_async()    （非阻塞，发 action[t]）
#     4. 记录 (state, obs, action, t_action)

# 为什么不用独立线程轮询 state：
#     FANUC 控制器 RMI 任务是单线程串行处理的，motion 指令高频发送时
#     同时高频轮询 FRC_ReadCartesianPosition 会导致 ITP 扫描超载
#     （SRVO-356 DCS ITP Scan Alarm 01）。串行读彻底避免了并发。

# 保存格式：HDF5
#     /action/pose        float32 [N, 6]   目标位姿 (x y z w p r)
#     /action/gripper     bool    [N]       夹爪状态
#     /action/t           float64 [N]       perf_counter 时间戳
#     /state/pose         float32 [N, 6]   读取到的实际位姿
#     /state/t            float64 [N]
#     /obs/frames         uint8   [N,H,W,3]
#     /obs/t              float64 [N]

# 依赖：
#     pip install h5py opencv-python numpy
# """

# import socket
# import json
# import time
# import math
# import threading
# import logging
# import os
# from collections import deque
# from datetime import datetime
# from typing import Optional, Tuple, List

# import cv2
# import numpy as np
# import h5py

# from .fanuc_config import TeleopConfig
# from .fanuc_transport import UDPTransport, optimize_tcp_socket, LineSocket
# from .fanuc_communication import FRCAsyncSender, FRCInitializer

# logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger(__name__)

# RO_PORT_OPEN          = 3
# RO_PORT_CLOSE         = 4
# VALVE_SWITCH_DELAY_MS = 80


# # ═══════════════════════════════════════════════════════════════════════════════
# # 1. UDP 数据接收器（不变）
# # ═══════════════════════════════════════════════════════════════════════════════
# class UDPDataReceiver:
#     def __init__(self, config):
#         self.config = config
#         self.transport = UDPTransport(config.host, config.port, config.buffer_size)
#         self.latest_frame: Optional[Tuple] = None
#         self.latest_grip: bool = False
#         self.grip_changed: bool = False
#         self.lock = threading.Lock()
#         self.running = False
#         self.thread = None
#         self.logger = logging.getLogger(self.__class__.__name__)

#     def start(self) -> None:
#         self.transport.bind()
#         self.running = True
#         self.thread = threading.Thread(target=self._recv_loop, daemon=True)
#         self.thread.start()
#         self.logger.info(f"✅ UDP Receiver on {self.config.host}:{self.config.port}")

#     def _recv_loop(self) -> None:
#         while self.running:
#             try:
#                 data, _ = self.transport.recv()
#                 payload = json.loads(data.decode("utf-8"))
#                 x = payload.get("x"); y = payload.get("y"); z = payload.get("z")
#                 w = payload.get("w"); p = payload.get("p"); r = payload.get("r")
#                 grip = bool(payload.get("gripButton", False))
#                 if None in (x, y, z, w, p, r):
#                     continue
#                 if any(math.isnan(v) or math.isinf(v) for v in (x, y, z, w, p, r)):
#                     continue
#                 with self.lock:
#                     self.latest_frame = (x, y, z, w, p, r)
#                     if grip != self.latest_grip:
#                         self.grip_changed = True
#                     self.latest_grip = grip
#             except json.JSONDecodeError:
#                 pass
#             except socket.timeout:
#                 continue
#             except Exception as e:
#                 if self.running:
#                     self.logger.error(f"UDP error: {e}")

#     def get_latest(self) -> Tuple[Optional[Tuple], bool, bool]:
#         with self.lock:
#             frame        = self.latest_frame
#             grip_state   = self.latest_grip
#             grip_changed = self.grip_changed
#             self.latest_frame  = None
#             self.grip_changed  = False
#             return frame, grip_state, grip_changed

#     def stop(self) -> None:
#         self.running = False
#         self.transport.close()


# # ═══════════════════════════════════════════════════════════════════════════════
# # 2. FRC State Reader（同步，无独立线程）
# #
# #    持有一条独立的 TCP 长连接，每次调用 read() 完成一次串行读取。
# #    调用方（主循环）在发 action 前调用，不存在并发，不会触发 SRVO-356。
# # ═══════════════════════════════════════════════════════════════════════════════
# class FRCStateReader:
#     """
#     同步阻塞式 state 读取器。

#     连接流程：
#         FRC_Connect (16001) → 拿动态端口
#         直接连接动态端口
#         ★ 不发 FRC_Initialize（读取通道不需要）

#     使用方式：
#         reader = FRCStateReader(host, port=16001)
#         reader.connect()
#         pose, t = reader.read()   # 发 action 前调用
#         reader.close()
#     """

#     def __init__(self, host: str, connect_port: int = 16001, timeout: float = 0.5):
#         self.host         = host
#         self.connect_port = connect_port
#         self.timeout      = timeout
#         self._sock: Optional[socket.socket] = None
#         self._ls:   Optional[LineSocket]    = None
#         self._request_bytes = (
#             json.dumps({"Command": "FRC_ReadCartesianPosition", "Group": 1}) + "\r\n"
#         ).encode()
#         self.logger = logging.getLogger(self.__class__.__name__)

#     def connect(self) -> None:
#         dynamic_port = FRCInitializer.frc_connect(self.host, self.connect_port)
#         self.logger.info(f"StateReader: dynamic port = {dynamic_port}")
#         self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#         optimize_tcp_socket(self._sock)
#         self._sock.connect((self.host, dynamic_port))
#         self._sock.settimeout(self.timeout)
#         self._ls = LineSocket(self._sock)
#         self.logger.info(f"✅ StateReader connected to {self.host}:{dynamic_port}")

#     def read(self) -> Tuple[Optional[Tuple[float, ...]], Optional[float]]:
#         """
#         发送一次读取请求，阻塞等待响应。
#         时间戳在收到响应后立即打（反映读取完成的时刻）。

#         Returns:
#             (pose, t)  pose = (x, y, z, w, p, r)
#             失败时返回 (None, None)
#         """
#         try:
#             self._ls.sendall(self._request_bytes)
#             resp = self._ls.read_json()
#             t    = time.perf_counter()          # 收到响应后立即打时间戳

#             if resp.get("ErrorID", -1) != 0:
#                 self.logger.warning(f"StateReader ErrorID={resp.get('ErrorID')}")
#                 return None, None

#             pos  = resp.get("Position", {})
#             pose = (
#                 pos.get("X", 0.0), pos.get("Y", 0.0), pos.get("Z", 0.0),
#                 pos.get("W", 0.0), pos.get("P", 0.0), pos.get("R", 0.0),
#             )
#             return pose, t

#         except socket.timeout:
#             self.logger.warning("StateReader: read timeout")
#             return None, None
#         except Exception as e:
#             self.logger.error(f"StateReader read error: {e}")
#             return None, None

#     def close(self) -> None:
#         try:
#             if self._ls:
#                 self._ls.sendall(
#                     (json.dumps({"Communication": "FRC_Disconnect"}) + "\r\n").encode()
#                 )
#         except Exception:
#             pass
#         try:
#             if self._sock:
#                 self._sock.close()
#         except Exception:
#             pass
#         self.logger.info("StateReader closed")


# # ═══════════════════════════════════════════════════════════════════════════════
# # 3. Camera Capture（独立线程，ring buffer，主循环取最新帧）
# # ═══════════════════════════════════════════════════════════════════════════════
# class CameraCapture:
#     """
#     独立线程持续抓帧，存入 ring buffer。
#     主循环调用 latest() 取最近一帧，非阻塞。

#     时间戳在 read() 返回后立即打，不依赖相机硬件时间戳。
#     CAP_PROP_BUFFERSIZE=1 减少内部缓冲延迟。
#     """

#     BUF_SIZE = 300

#     def __init__(self, cam_id: int = 0, rate_hz: float = 30.0,
#                  width: int = 640, height: int = 480):
#         self.cam_id  = cam_id
#         self.rate_hz = rate_hz
#         self.width   = width
#         self.height  = height
#         self._buf: deque = deque(maxlen=self.BUF_SIZE)
#         self._lock    = threading.Lock()
#         self._running = False
#         self._thread: Optional[threading.Thread] = None
#         self._cap:    Optional[cv2.VideoCapture]  = None
#         self.logger = logging.getLogger(self.__class__.__name__)

#     def start(self) -> None:
#         self._cap = cv2.VideoCapture(self.cam_id)
#         self._cap.set(cv2.CAP_PROP_FRAME_WIDTH,  self.width)
#         self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
#         self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
#         if not self._cap.isOpened():
#             raise RuntimeError(f"Cannot open camera {self.cam_id}")
#         self._running = True
#         self._thread  = threading.Thread(target=self._loop, daemon=True)
#         self._thread.start()
#         self.logger.info(f"✅ Camera {self.cam_id} @ {self.rate_hz} Hz")

#     def _loop(self) -> None:
#         interval = 1.0 / self.rate_hz
#         while self._running:
#             t0 = time.perf_counter()
#             ret, frame = self._cap.read()
#             t_cap = time.perf_counter()
#             if ret:
#                 with self._lock:
#                     self._buf.append({"t": t_cap, "frame": frame})
#             elapsed = time.perf_counter() - t0
#             time.sleep(max(0.0, interval - elapsed))

#     def latest(self) -> Optional[dict]:
#         """取最新帧（不 copy，由调用方 copy）"""
#         with self._lock:
#             return self._buf[-1] if self._buf else None

#     def stop(self) -> None:
#         self._running = False
#         if self._cap:
#             self._cap.release()
#         self.logger.info("Camera stopped")


# # ═══════════════════════════════════════════════════════════════════════════════
# # 4. Episode Writer（HDF5）
# # ═══════════════════════════════════════════════════════════════════════════════
# class EpisodeWriter:
#     def __init__(self, save_dir: str = "./episodes"):
#         os.makedirs(save_dir, exist_ok=True)
#         self.save_dir  = save_dir
#         self._records: List[dict] = []
#         self.logger = logging.getLogger(self.__class__.__name__)

#     def append(self, record: dict) -> None:
#         self._records.append(record)

#     def save(self) -> Optional[str]:
#         if not self._records:
#             self.logger.warning("No records to save")
#             return None

#         N = len(self._records)
#         sample_frame = self._records[0].get("obs_frame")
#         has_obs      = sample_frame is not None
#         if has_obs:
#             H, W, C = sample_frame.shape

#         action_pose    = np.zeros((N, 6), dtype=np.float32)
#         action_gripper = np.zeros(N,      dtype=bool)
#         action_t       = np.zeros(N,      dtype=np.float64)
#         state_pose     = np.zeros((N, 6), dtype=np.float32)
#         state_t        = np.zeros(N,      dtype=np.float64)
#         obs_t          = np.zeros(N,      dtype=np.float64)
#         if has_obs:
#             obs_frames = np.zeros((N, H, W, C), dtype=np.uint8)

#         for i, rec in enumerate(self._records):
#             action_pose[i]    = rec["action_pose"]
#             action_gripper[i] = rec["action_gripper"]
#             action_t[i]       = rec["t_action"]
#             if rec["state_pose"] is not None:
#                 state_pose[i] = rec["state_pose"]
#                 state_t[i]    = rec["t_state"]
#             if has_obs and rec["obs_frame"] is not None:
#                 obs_frames[i] = rec["obs_frame"]
#                 obs_t[i]      = rec["t_obs"]

#         ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
#         path = os.path.join(self.save_dir, f"episode_{ts}.h5")

#         with h5py.File(path, "w") as f:
#             f.attrs["n_steps"]    = N
#             f.attrs["created_at"] = ts

#             ag = f.create_group("action")
#             ag.create_dataset("pose",    data=action_pose,    compression="gzip")
#             ag.create_dataset("gripper", data=action_gripper)
#             ag.create_dataset("t",       data=action_t)

#             sg = f.create_group("state")
#             sg.create_dataset("pose",    data=state_pose,     compression="gzip")
#             sg.create_dataset("t",       data=state_t)

#             og = f.create_group("obs")
#             og.create_dataset("t",       data=obs_t)
#             if has_obs:
#                 og.create_dataset(
#                     "frames", data=obs_frames,
#                     compression="gzip", chunks=(1, H, W, C),
#                 )

#         self.logger.info(f"💾 Saved {N} steps → {path}")
#         self._records.clear()
#         return path

#     def discard(self) -> None:
#         n = len(self._records)
#         self._records.clear()
#         self.logger.info(f"🗑  Discarded {n} steps")


# # ═══════════════════════════════════════════════════════════════════════════════
# # 5. 主 Record 控制器
# # ═══════════════════════════════════════════════════════════════════════════════
# class FanucRecordController:
#     """
#     每步主循环顺序（MDP 语义：state[t] → action[t]）：

#         1. FRC_ReadCartesianPosition  ← state[t]，串行阻塞，发 action 前
#         2. camera.latest()            ← obs[t]，非阻塞
#         3. frc_sender.send_async()    ← action[t]
#         4. writer.append(record)

#     Ctrl+C ×1 → 保存 episode
#     Ctrl+C ×2 → 丢弃 episode
#     """

#     BUFFER_SIZE        = 8
#     INTER_PACKET_DELAY = 0.002

#     def __init__(self, config: TeleopConfig,
#                  cam_id: int        = 0,
#                  cam_rate_hz: float = 30.0,
#                  save_dir: str      = "./episodes"):
#         self.config       = config
#         self.udp_receiver = UDPDataReceiver(config.udp)
#         self.frc_sender   = FRCAsyncSender()
#         self.state_reader = FRCStateReader(
#             host=config.robot.host,
#             connect_port=config.robot.port,
#         )
#         self.camera  = CameraCapture(cam_id=cam_id, rate_hz=cam_rate_hz)
#         self.writer  = EpisodeWriter(save_dir=save_dir)
#         self.pending_seq_ids: set = set()
#         self._interrupt_count = 0
#         self.logger = logging.getLogger(self.__class__.__name__)

#     def _build_safe_gripper_cmds(self, target_close: bool) -> list:
#         if target_close:
#             return [
#                 {"port": RO_PORT_OPEN,  "val": "OFF", "delay_before_ms": 0},
#                 {"port": RO_PORT_CLOSE, "val": "ON",  "delay_before_ms": VALVE_SWITCH_DELAY_MS},
#             ]
#         else:
#             return [
#                 {"port": RO_PORT_CLOSE, "val": "OFF", "delay_before_ms": 0},
#                 {"port": RO_PORT_OPEN,  "val": "ON",  "delay_before_ms": VALVE_SWITCH_DELAY_MS},
#             ]

#     def _reset_gripper_safe(self, last_known_pose: Optional[Tuple]) -> None:
#         if last_known_pose is None:
#             self.logger.warning("⚠️  无有效坐标，跳过气阀复位")
#             return
#         self.logger.info("🔒 安全复位：断开所有气阀...")
#         for port in (RO_PORT_OPEN, RO_PORT_CLOSE):
#             try:
#                 self.frc_sender.send_async(
#                     last_known_pose,
#                     utool=self.config.robot.utool,
#                     uframe=self.config.robot.uframe,
#                     speed=self.config.robot.speed_mm_s,
#                     term_type=self.config.robot.term_type,
#                     term_value=self.config.robot.term_value,
#                     lcb_type="TA", lcb_value=10,
#                     port_type=2, port_number=port, port_value="OFF",
#                 )
#                 time.sleep(0.06)
#             except Exception as e:
#                 self.logger.error(f"RO[{port}] 复位异常: {e}")
#         self.logger.info("🔒 气阀复位完成")

#     def run(self) -> None:
#         self.logger.info("=" * 60)
#         self.logger.info("🎬 FANUC Record Mode  (state → obs → action)")
#         self.logger.info("   Ctrl+C ×1 → save & quit")
#         self.logger.info("   Ctrl+C ×2 → discard & quit")
#         self.logger.info("=" * 60)

#         self.udp_receiver.start()

#         try:
#             self.frc_sender.connect(
#                 self.config.robot.host,
#                 self.config.robot.port,
#                 self.config.robot.group,
#             )
#         except Exception as e:
#             self.logger.error(f"❌ Action sender connect failed: {e}")
#             self.udp_receiver.stop()
#             return

#         state_ok = True
#         try:
#             self.state_reader.connect()
#         except Exception as e:
#             self.logger.error(f"❌ State reader connect failed: {e}")
#             self.logger.warning("⚠️  State will be None for all steps")
#             state_ok = False

#         obs_ok = True
#         try:
#             self.camera.start()
#         except Exception as e:
#             self.logger.error(f"❌ Camera start failed: {e}")
#             self.logger.warning("⚠️  Obs will be None for all steps")
#             obs_ok = False

#         last_sent_pose: Optional[Tuple] = None
#         last_gripper_state = False
#         lcb_queue: list    = []
#         MIN_DIST_MM        = 0.01
#         save_path: Optional[str] = None

#         try:
#             while True:
#                 # 1. 处理 ACK ──────────────────────────────────────────────────
#                 while True:
#                     ack_seq, ack_err = self.frc_sender.check_ack()
#                     if ack_seq is None:
#                         break
#                     if ack_seq in self.pending_seq_ids:
#                         self.pending_seq_ids.remove(ack_seq)
#                         if ack_err != 0:
#                             self.logger.warning(f"⚠️ Seq {ack_seq} err {ack_err}")

#                 # 2. 背压控制 ──────────────────────────────────────────────────
#                 if len(self.pending_seq_ids) >= self.BUFFER_SIZE:
#                     time.sleep(0.001)
#                     continue

#                 # 3. 获取 UDP 数据 ─────────────────────────────────────────────
#                 target_pose, current_gripper_state, grip_changed = \
#                     self.udp_receiver.get_latest()

#                 # 4. 夹爪边缘检测 ─────────────────────────────────────────────
#                 if grip_changed:
#                     self.logger.info(
#                         f"🦾 夹爪切换 → {'闭合' if current_gripper_state else '打开'}"
#                     )
#                     if lcb_queue:
#                         lcb_queue.clear()
#                     lcb_queue.extend(self._build_safe_gripper_cmds(current_gripper_state))
#                     last_gripper_state = current_gripper_state

#                 # 5. pose 为空时用上一帧坐标补发 IO ───────────────────────────
#                 if target_pose is None:
#                     if lcb_queue and last_sent_pose is not None:
#                         target_pose = last_sent_pose
#                     else:
#                         time.sleep(0.001)
#                         continue

#                 # 6. 取下一条 IO 指令 ─────────────────────────────────────────
#                 current_lcb = lcb_queue.pop(0) if lcb_queue else None
#                 if current_lcb and current_lcb.get("delay_before_ms", 0) > 0:
#                     time.sleep(current_lcb["delay_before_ms"] / 1000.0)

#                 # 7. 空间滤波 ──────────────────────────────────────────────────
#                 if last_sent_pose is not None:
#                     dx = target_pose[0] - last_sent_pose[0]
#                     dy = target_pose[1] - last_sent_pose[1]
#                     dz = target_pose[2] - last_sent_pose[2]
#                     if math.sqrt(dx**2 + dy**2 + dz**2) < MIN_DIST_MM and not current_lcb:
#                         time.sleep(0.001)
#                         continue

#                 # ── 顺序）────────────────────────────────

#                 # 8a. 读 state[t]：发 action 前，串行阻塞
#                 #     RTT 约 5-15ms，这是主循环唯一增加的延迟
#                 state_pose_val, t_state = (None, None)
#                 if state_ok:
#                     state_pose_val, t_state = self.state_reader.read()

#                 # 8b. 读 obs[t]：非阻塞，取 ring buffer 最新帧
#                 obs_snap = self.camera.latest() if obs_ok else None
#                 t_obs    = obs_snap["t"]         if obs_snap else None

#                 # 8c. 发 action[t]
#                 ok = self.frc_sender.send_async(
#                     target_pose,
#                     utool=self.config.robot.utool,
#                     uframe=self.config.robot.uframe,
#                     speed=self.config.robot.speed_mm_s,
#                     term_type=self.config.robot.term_type,
#                     term_value=self.config.robot.term_value,
#                     lcb_type="TA" if current_lcb else None,
#                     lcb_value=10  if current_lcb else 0,
#                     port_type=2   if current_lcb else None,
#                     port_number=current_lcb["port"] if current_lcb else None,
#                     port_value=current_lcb["val"]   if current_lcb else None,
#                 )

#                 if not ok:
#                     continue

#                 t_action = time.perf_counter()
#                 seq_id   = self.frc_sender.seq_id - 1
#                 self.pending_seq_ids.add(seq_id)
#                 last_sent_pose = target_pose

#                 # 8d. 记录 record
#                 self.writer.append({
#                     "t_action":       t_action,
#                     "action_pose":    list(target_pose),
#                     "action_gripper": current_gripper_state,
#                     "t_state":        t_state,
#                     "state_pose":     list(state_pose_val) if state_pose_val else None,
#                     "t_obs":          t_obs,
#                     "obs_frame":      obs_snap["frame"].copy() if obs_snap else None,
#                 })

#                 if len(self.pending_seq_ids) < self.BUFFER_SIZE:
#                     time.sleep(self.INTER_PACKET_DELAY)

#         except KeyboardInterrupt:
#             self._interrupt_count += 1
#             if self._interrupt_count == 1:
#                 self.logger.warning("\n⚠️  Ctrl+C — 保存 episode...")
#                 self._reset_gripper_safe(last_sent_pose)
#                 save_path = self.writer.save()
#             else:
#                 self.logger.warning("\n⚠️  Ctrl+C ×2 — 丢弃 episode")
#                 self._reset_gripper_safe(last_sent_pose)
#                 self.writer.discard()

#         except Exception as e:
#             self.logger.error(f"💥 异常: {e}")
#             import traceback; traceback.print_exc()
#             self._reset_gripper_safe(last_sent_pose)
#             self.writer.discard()

#         finally:
#             self.udp_receiver.stop()
#             if state_ok:
#                 self.state_reader.close()
#             if obs_ok:
#                 self.camera.stop()
#             if save_path:
#                 self.logger.info(f"✅ Episode saved: {save_path}")
#             self.logger.info("👋 Record 退出")

# # ═══════════════════════════════════════════════════════════════════════════════
# # Main Entry Point
# # ═══════════════════════════════════════════════════════════════════════════════
# def main():
#     from .fanuc_config import TeleopConfig
    
#     config = TeleopConfig()
    
#     try:
#         controller = FanucRecordController(config)
#         controller.run()
#     except KeyboardInterrupt:
#         logger.info("Program interrupted")
#     except Exception as e:
#         logger.error(f"Fatal error: {e}")
#         import traceback
#         traceback.print_exc()


# if __name__ == "__main__":
#     main()
"""
fanuc_record.py
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
import h5py

from .fanuc_config import TeleopConfig
from .fanuc_transport import UDPTransport
from .fanuc_communication import FRCUnifiedClient

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
# 3. Episode Writer（HDF5，不变）
# ═══════════════════════════════════════════════════════════════════════════════
class EpisodeWriter:
    def __init__(self, save_dir: str = "./episodes"):
        os.makedirs(save_dir, exist_ok=True)
        self.save_dir  = save_dir
        self._records: List[dict] = []
        self.logger = logging.getLogger(self.__class__.__name__)

    def append(self, record: dict) -> None:
        self._records.append(record)

    def save(self) -> Optional[str]:
        if not self._records:
            self.logger.warning("No records to save")
            return None

        N = len(self._records)
        sample_frame = self._records[0].get("obs_frame")
        has_obs      = sample_frame is not None
        if has_obs:
            H, W, C = sample_frame.shape

        action_pose    = np.zeros((N, 6), dtype=np.float32)
        action_gripper = np.zeros(N,      dtype=bool)
        action_t       = np.zeros(N,      dtype=np.float64)
        state_pose     = np.zeros((N, 6), dtype=np.float32)
        state_t        = np.zeros(N,      dtype=np.float64)
        obs_t          = np.zeros(N,      dtype=np.float64)
        if has_obs:
            obs_frames = np.zeros((N, H, W, C), dtype=np.uint8)

        for i, rec in enumerate(self._records):
            action_pose[i]    = rec["action_pose"]
            action_gripper[i] = rec["action_gripper"]
            action_t[i]       = rec["t_action"]
            if rec["state_pose"] is not None:
                state_pose[i] = rec["state_pose"]
                state_t[i]    = rec["t_state"]
            if has_obs and rec["obs_frame"] is not None:
                obs_frames[i] = rec["obs_frame"]
                obs_t[i]      = rec["t_obs"]

        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.save_dir, f"episode_{ts}.h5")

        with h5py.File(path, "w") as f:
            f.attrs["n_steps"]    = N
            f.attrs["created_at"] = ts

            ag = f.create_group("action")
            ag.create_dataset("pose",    data=action_pose,    compression="gzip")
            ag.create_dataset("gripper", data=action_gripper)
            ag.create_dataset("t",       data=action_t)

            sg = f.create_group("state")
            sg.create_dataset("pose",    data=state_pose,     compression="gzip")
            sg.create_dataset("t",       data=state_t)

            og = f.create_group("obs")
            og.create_dataset("t",       data=obs_t)
            if has_obs:
                og.create_dataset(
                    "frames", data=obs_frames,
                    compression="gzip", chunks=(1, H, W, C),
                )

        self.logger.info(f"💾 Saved {N} steps → {path}")
        self._records.clear()
        return path

    def discard(self) -> None:
        n = len(self._records)
        self._records.clear()
        self.logger.info(f"🗑  Discarded {n} steps")


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

    Ctrl+C ×1 → 保存 episode
    Ctrl+C ×2 → 丢弃 episode
    """

    BUFFER_SIZE        = 8
    INTER_PACKET_DELAY = 0.002

    def __init__(self, config: TeleopConfig,
                 cam_id: int        = 0,
                 cam_rate_hz: float = 30.0,
                 save_dir: str      = "./episodes"):
        self.config       = config
        self.udp_receiver = UDPDataReceiver(config.udp)
        self.client       = FRCUnifiedClient()          # 单连接，ABC 三线程
        self.camera       = CameraCapture(cam_id=cam_id, rate_hz=cam_rate_hz)
        self.writer       = EpisodeWriter(save_dir=save_dir)
        self.pending_seq_ids: set = set()
        self._interrupt_count = 0
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
        self.logger.info("🎬 FANUC Record Mode  (state → obs → action)")
        self.logger.info("   Ctrl+C ×1 → save & quit")
        self.logger.info("   Ctrl+C ×2 → discard & quit")
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
        save_path: Optional[str] = None

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

                # 8d. 记录
                self.writer.append({
                    "t_action":       t_action,
                    "action_pose":    list(target_pose),
                    "action_gripper": current_gripper_state,
                    "t_state":        t_state,
                    "state_pose":     list(state_pose_val) if state_pose_val else None,
                    "t_obs":          t_obs,
                    "obs_frame":      obs_snap["frame"].copy() if obs_snap else None,
                })

                if len(self.pending_seq_ids) < self.BUFFER_SIZE:
                    time.sleep(self.INTER_PACKET_DELAY)

        except KeyboardInterrupt:
            self._interrupt_count += 1
            if self._interrupt_count == 1:
                self.logger.warning("\n⚠️  Ctrl+C — 保存 episode...")
                self._reset_gripper_safe(last_sent_pose)
                save_path = self.writer.save()
            else:
                self.logger.warning("\n⚠️  Ctrl+C ×2 — 丢弃 episode")
                self._reset_gripper_safe(last_sent_pose)
                self.writer.discard()

        except Exception as e:
            self.logger.error(f"💥 异常: {e}")
            import traceback; traceback.print_exc()
            self._reset_gripper_safe(last_sent_pose)
            self.writer.discard()

        finally:
            self.udp_receiver.stop()
            self.client.disconnect()
            if obs_ok:
                self.camera.stop()
            if save_path:
                self.logger.info(f"✅ Episode saved: {save_path}")
            self.logger.info("👋 Record 退出")


# ═══════════════════════════════════════════════════════════════════════════════
# Main Entry Point
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    from .fanuc_config import TeleopConfig

    config = TeleopConfig()
    try:
        controller = FanucRecordController(config)
        controller.run()
    except KeyboardInterrupt:
        logger.info("Program interrupted")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()