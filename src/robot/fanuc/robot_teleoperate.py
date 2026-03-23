import socket
import json
import time
import math
import threading
import logging
import statistics
from datetime import datetime
from typing import Optional, Tuple

from .fanuc_config import TeleopConfig, FanucRobotConfig, UDPReceiverConfig, PerformanceConfig
from .fanuc_transport import UDPTransport
from .fanuc_communication import FRCAsyncSender

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# 气动夹爪端口配置 (双控)
# ==========================================
RO_PORT_OPEN = 3   # 控制打开的端口 (RO[3])
RO_PORT_CLOSE = 4  # 控制关闭的端口 (RO[4])

# ==========================================
# 夹爪安全参数
# ==========================================
VALVE_SWITCH_DELAY_MS = 80   # 关阀后等待气压释放的最小时间(ms)，根据实际气缸调整
MAX_LCB_QUEUE_DEPTH   = 2    # 队列最大深度 = 一次切换动作的指令数，防止堆积


# ==================== UDP 数据接收器 ====================
class UDPDataReceiver:
    def __init__(self, config: UDPReceiverConfig):
        self.config = config
        self.transport = UDPTransport(config.host, config.port, config.buffer_size)
        self.latest_frame: Optional[Tuple] = None
        self.latest_grip: bool = False  # track latest gripper
        self.grip_changed: bool = False  # 是否边缘变化
        self.lock = threading.Lock()
        self.running = False
        self.thread = None
        self.recv_count = 0
        self.error_count = 0
        self.logger = logging.getLogger(self.__class__.__name__)
    
    def start(self) -> None:
        self.transport.bind()
        #self._flush_socket_buffer() 
        self.running = True
        self.thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.thread.start()
        self.logger.info(f"✅ UDP Receiver listening on {self.config.host}:{self.config.port}")
    
    def _flush_socket_buffer(self) -> None:
        """启动前清空 socket 内核缓冲区，防止消费上一次会话的残留数据包。"""
        self.logger.info("🧹 清空 UDP socket 缓冲区...")
        flushed = 0
        # 临时设为非阻塞，把所有积压包全部读完丢弃
        self.transport.sock.setblocking(False)
        try:
            while True:
                try:
                    self.transport.sock.recv(65535)
                    flushed += 1
                except BlockingIOError:
                    break  # 缓冲区已空
        finally:
            # 恢复原来的超时设置
            self.transport.sock.setblocking(True)
            timeout = getattr(self.config, 'timeout', 1.0)
            self.transport.sock.settimeout(timeout)
        self.logger.info(f"🧹 已丢弃 {flushed} 个残留数据包")
    
    def _recv_loop(self) -> None:
        while self.running:
            try:
                data, addr = self.transport.recv()
                payload = json.loads(data.decode("utf-8"))
                
                x = payload.get("x")
                y = payload.get("y")
                z = payload.get("z")
                w = payload.get("w")
                p = payload.get("p")
                r = payload.get("r")

                # 夹爪按钮
                b4_grip_pressed = bool(payload.get("gripButton", False))

                if None in (x, y, z, w, p, r):
                    self.error_count += 1
                    continue
                if any(math.isnan(v) or math.isinf(v) for v in (x, y, z, w, p, r)):
                    self.error_count += 1
                    continue
                
                with self.lock:
                    self.latest_frame = (x, y, z, w, p, r)
                    #  在接收线程内做边缘检测，grip_changed 作为一次性标志
                    if b4_grip_pressed != self.latest_grip:
                        self.grip_changed = True
                    self.latest_grip = b4_grip_pressed
                    self.recv_count += 1
            
            except json.JSONDecodeError as e:
                self.error_count += 1
                self.logger.warning(f"JSON parse error: {e}")
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.logger.error(f"UDP receive error: {e}")
    
    def get_latest(self) -> Tuple[Optional[Tuple], bool, bool]:
        """
        取出最新帧。
        返回: (坐标帧, 当前夹爪状态, 夹爪是否发生了边缘跳变)
        grip_changed 读取后立即清除，保证每次切换只触发一次。
        """
        with self.lock:
            frame = self.latest_frame
            grip_state = self.latest_grip
            grip_changed = self.grip_changed
            self.latest_frame = None
            self.grip_changed = False  # ⭐ 读取后立即清除，防止重复触发
            return frame, grip_state, grip_changed
    
    def stop(self) -> None:
        self.running = False
        self.transport.close()


# ==================== 主业务流程 ====================
class FanucTeleopControllerSlidingWindow:
    BUFFER_SIZE = 8  
    INTER_PACKET_DELAY = 0.002  
    
    def __init__(self, config: TeleopConfig):
        self.config = config
        self.udp_receiver = UDPDataReceiver(config.udp)
        self.frc_sender = FRCAsyncSender()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.pending_seq_ids = set()

    # ==========================================
    # ⭐ 新增：生成安全的双阀切换指令序列
    # ==========================================
    def _build_safe_gripper_cmds(self, target_close: bool) -> list:
        """
        规则：先断开当前阀，等待气压释放(delay_before_ms)，再得电新阀。
        两条指令之间强制插入 VALVE_SWITCH_DELAY_MS 延时，保证硬件互锁。
        """
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

    # ==========================================
    # ⭐ 新增：程序退出时强制复位两路气阀
    # ==========================================
    def _reset_gripper_safe(self, last_known_pose: Optional[Tuple]) -> None:
        """退出前强制断开两路气阀，确保气缸处于已知安全状态。"""
        if last_known_pose is None:
            self.logger.warning("⚠️  无有效坐标，跳过气阀复位（请手动检查气阀！）")
            return
        self.logger.info("🔒 安全复位：断开所有气阀...")
        for port in (RO_PORT_OPEN, RO_PORT_CLOSE):
            try:
                ok = self.frc_sender.send_async(
                    last_known_pose,
                    utool=self.config.robot.utool,
                    uframe=self.config.robot.uframe,
                    speed=self.config.robot.speed_mm_s,
                    term_type=self.config.robot.term_type,
                    term_value=self.config.robot.term_value,
                    lcb_type="TA",
                    lcb_value=10,
                    port_type=2,
                    port_number=port,
                    port_value="OFF",
                )
                if ok:
                    self.logger.info(f"   RO[{port}] → OFF ✅")
                else:
                    self.logger.error(f"   RO[{port}] → OFF 发送失败 ❌")
                time.sleep(0.06)  # 等待 ACK 及气压稳定
            except Exception as e:
                self.logger.error(f"   RO[{port}] 复位异常: {e}")
        self.logger.info("🔒 气阀复位完成")

    def run(self) -> None:
        self.logger.info("=" * 60)
        self.logger.info("🤖 FANUC Real-Time Teleoperation (With Gripper Control)")
        self.logger.info(f"   Target FPS : {self.config.performance.target_fps} Hz")
        self.logger.info("=" * 60)
        
        self.udp_receiver.start()
        
        try:
            self.frc_sender.connect(self.config.robot.host, self.config.robot.port, self.config.robot.group)
        except Exception as e:
            self.logger.error(f"❌ Connection failed: {e}")
            self.udp_receiver.stop()
            return
        
        frame_times = []
        sent_count = 0
        ack_count = 0
        none_count = 0
        last_print_time = time.perf_counter()
        
        # --- 夹爪状态机变量 ---
        last_gripper_state = False
        lcb_queue = []  # 用于存放将要发送的 IO 动作
        
        try:
            self.logger.info("🔄 初始化：等待 UDP 数据并开始填充缓冲区...")
            buffer_full = False
            last_sent_pose = None
            MIN_DIST_MM = 0.01  
            
            while True:
                t_start = time.perf_counter()
                
                # 1. 处理 ACK
                while True:
                    ack_seq, ack_err = self.frc_sender.check_ack()
                    if ack_seq is None:
                        break
                    if ack_seq in self.pending_seq_ids:
                        self.pending_seq_ids.remove(ack_seq)
                        ack_count += 1
                        if ack_err != 0:
                            self.logger.warning(f"⚠️ Seq {ack_seq} error {ack_err}")
                
                # 2. 检查缓冲是否满
                if len(self.pending_seq_ids) >= self.BUFFER_SIZE:
                    if not buffer_full:
                        buffer_full = True
                    time.sleep(0.001)
                    continue
                
                # 3. 获取UDP数据
                # ⭐ 返回值新增了 grip_changed，边缘检测已在接收线程完成
                target_pose, current_gripper_state, grip_changed = self.udp_receiver.get_latest()

                # =======================================================
                # ⭐ 夹爪边缘检测：必须在 pose 判空之前执行
                #    保证即使坐标帧为空，夹爪指令也不会被漏掉
                # =======================================================
                if grip_changed:
                    self.logger.info(
                        f"🦾 检测到夹爪指令切换！ 目标状态: "
                        f"{'闭合/抓取' if current_gripper_state else '打开/松开'}"
                    )
                    # ⭐ 安全：清空未执行的旧队列，防止反复冲击气缸
                    if lcb_queue:
                        self.logger.warning(
                            f"⚠️  旧夹爪队列未清空({len(lcb_queue)}条)，强制清空以防气缸连续冲击！"
                        )
                        lcb_queue.clear()
                    # ⭐ 用安全序列替代原来的直接 append
                    lcb_queue.extend(self._build_safe_gripper_cmds(current_gripper_state))
                    last_gripper_state = current_gripper_state

                # pose 为空时：若队列有 IO 任务则用上一帧坐标强制发送，否则跳过
                if target_pose is None:
                    if lcb_queue and last_sent_pose is not None:
                        target_pose = last_sent_pose  # 原地不动，只发 IO
                    else:
                        none_count += 1
                        time.sleep(0.001)
                        continue

                # 提取下一个需要发送的 IO 动作
                current_lcb = lcb_queue.pop(0) if lcb_queue else None

                # ⭐ 如果该指令要求延时（等待气压释放），在此阻塞等待
                if current_lcb and current_lcb.get("delay_before_ms", 0) > 0:
                    delay_s = current_lcb["delay_before_ms"] / 1000.0
                    self.logger.info(f"⏳ 等待气压释放 {current_lcb['delay_before_ms']}ms ...")
                    time.sleep(delay_s)

                # =======================================================
                # 空间滤波判断 (带强制下发逻辑)
                # =======================================================
                if last_sent_pose is not None:
                    dx = target_pose[0] - last_sent_pose[0]
                    dy = target_pose[1] - last_sent_pose[1]
                    dz = target_pose[2] - last_sent_pose[2]
                    dist = math.sqrt(dx**2 + dy**2 + dz**2)
                    
                    if dist < MIN_DIST_MM and not current_lcb:
                        none_count += 1
                        time.sleep(0.001)
                        continue
                
                # 4. 发送指令 (附带夹爪参数)
                ok = self.frc_sender.send_async(
                    target_pose,
                    utool=self.config.robot.utool,
                    uframe=self.config.robot.uframe,
                    speed=self.config.robot.speed_mm_s,
                    term_type=self.config.robot.term_type,  
                    term_value=self.config.robot.term_value,
                    lcb_type="TA" if current_lcb else None,
                    lcb_value=10 if current_lcb else 0,
                    port_type=2 if current_lcb else None,
                    port_number=current_lcb["port"] if current_lcb else None,
                    port_value=current_lcb["val"] if current_lcb else None
                )
                
                if ok:
                    seq_id = self.frc_sender.seq_id - 1
                    self.pending_seq_ids.add(seq_id)
                    sent_count += 1
                    last_sent_pose = target_pose  
                    
                    if not buffer_full and len(self.pending_seq_ids) < self.BUFFER_SIZE:
                        time.sleep(self.INTER_PACKET_DELAY)
        
        except KeyboardInterrupt:
            self.logger.warning("\n⚠️  用户中断")
        except Exception as e:
            self.logger.error(f"💥 异常: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # ⭐ 退出前强制复位气阀
            self._reset_gripper_safe(last_sent_pose)
            self._shutdown(frame_times, sent_count, ack_count, none_count, {})
    
    def _shutdown(self, frame_times, sent_count, ack_count, none_count, buffer_histogram) -> None:
        """清理和统计"""
        self.udp_receiver.stop()
        self.frc_sender.disconnect()
        
        self.logger.info("\n" + "=" * 60)
        self.logger.info("📊 性能统计报告 (滑动窗口模式)")
        self.logger.info("=" * 60)
        self.logger.info(f"  UDP 接收总帧数 : {self.udp_receiver.recv_count}")
        self.logger.info(f"  UDP 解析错误   : {self.udp_receiver.error_count}")
        self.logger.info(f"  实际发送指令   : {sent_count}")
        self.logger.info(f"  ACK成功返回    : {ack_count}")
        self.logger.info(f"  发送覆盖率     : {100*sent_count/max(1,self.udp_receiver.recv_count):.1f}%")
        self.logger.info(f"  无数据跳过     : {none_count} (正常现象，代表轮询速度快于UDP)")
        
        if buffer_histogram:
            self.logger.info("\n  📊 缓冲区状态占比（正常应维持在 7/8 或 8/8）：")
            for size in sorted(buffer_histogram.keys()):
                count = buffer_histogram[size]
                percentage = 100 * count / sum(buffer_histogram.values())
                bar = "█" * int(percentage / 5)
                self.logger.info(f"      {size}/8: {count:6d} 次 ({percentage:5.1f}%) {bar}")
        
        if frame_times:
            avg_ms = statistics.mean(frame_times)
            loop_fps = 1000.0 / avg_ms
            self.logger.info(f"\n  底层循环平均耗时: {avg_ms:.2f} ms")
            self.logger.info(f"  底层循环频率    : {loop_fps:.1f} Hz (代表程序响应极快)")
            self.logger.info(f"  最小 / 最大耗时 : {min(frame_times):.2f} / {max(frame_times):.2f} ms")
        self.logger.info("=" * 60)

def main():
    config = TeleopConfig()
    controller = FanucTeleopControllerSlidingWindow(config)
    controller.run()

if __name__ == "__main__":
    main()
# import socket
# import json
# import time
# import math
# import threading
# import logging
# import statistics
# from collections import deque
# from datetime import datetime
# from typing import Optional, Tuple

# from .fanuc_config import TeleopConfig, FanucRobotConfig, UDPReceiverConfig, PerformanceConfig
# from .fanuc_transport import UDPTransport
# from .fanuc_communication import FRCAsyncSender

# logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger(__name__)

# # ==========================================
# # 气动夹爪端口配置 (双控)
# # ==========================================
# RO_PORT_OPEN  = 3   # 控制打开的端口 (RO[3])
# RO_PORT_CLOSE = 4   # 控制关闭的端口 (RO[4])

# # ==========================================
# # 夹爪安全参数
# # ==========================================
# VALVE_SWITCH_DELAY_MS = 80   # 关阀后等待气压释放的最小时间(ms)
# MAX_LCB_QUEUE_DEPTH   = 2    # 一次切换动作的指令数，防止堆积

# # ==========================================
# # ⭐ 帧队列深度上限
# #    设为 BUFFER_SIZE 的 2~3 倍即可。
# #    太大 → 延迟感增加；太小 → 丢帧。
# #    建议从 16 开始调，跟手感好后可酌情降低。
# # ==========================================
# UDP_FRAME_QUEUE_MAXLEN = 16


# # ==================== UDP 数据接收器 ====================
# class UDPDataReceiver:
#     def __init__(self, config: UDPReceiverConfig):
#         self.config = config
#         self.transport = UDPTransport(config.host, config.port, config.buffer_size)

#         # ⭐ 核心改动：从单值 latest_frame 改为有界 deque
#         #    maxlen 保证队列不会无限增长（旧帧自动从左侧丢弃）
#         self._frame_queue: deque = deque(maxlen=UDP_FRAME_QUEUE_MAXLEN)

#         self.latest_grip: bool = False
#         self.grip_changed: bool = False
#         self.lock = threading.Lock()
#         self.running = False
#         self.thread = None
#         self.recv_count  = 0
#         self.error_count = 0
#         # ⭐ 新增：队列满时因 maxlen 自动丢弃的帧数（用于诊断）
#         self.queue_overflow_count = 0
#         self.logger = logging.getLogger(self.__class__.__name__)

#     def start(self) -> None:
#         self.transport.bind()
#         self.running = True
#         self.thread = threading.Thread(target=self._recv_loop, daemon=True)
#         self.thread.start()
#         self.logger.info(
#             f"✅ UDP Receiver listening on {self.config.host}:{self.config.port} "
#             f"(frame_queue maxlen={UDP_FRAME_QUEUE_MAXLEN})"
#         )

#     def _flush_socket_buffer(self) -> None:
#         """启动前清空 socket 内核缓冲区，防止消费上一次会话的残留数据包。"""
#         self.logger.info("🧹 清空 UDP socket 缓冲区...")
#         flushed = 0
#         self.transport.sock.setblocking(False)
#         try:
#             while True:
#                 try:
#                     self.transport.sock.recv(65535)
#                     flushed += 1
#                 except BlockingIOError:
#                     break
#         finally:
#             self.transport.sock.setblocking(True)
#             timeout = getattr(self.config, "timeout", 1.0)
#             self.transport.sock.settimeout(timeout)
#         self.logger.info(f"🧹 已丢弃 {flushed} 个残留数据包")

#     def _recv_loop(self) -> None:
#         while self.running:
#             try:
#                 data, addr = self.transport.recv()
#                 payload = json.loads(data.decode("utf-8"))

#                 x = payload.get("x")
#                 y = payload.get("y")
#                 z = payload.get("z")
#                 w = payload.get("w")
#                 p = payload.get("p")
#                 r = payload.get("r")
#                 b4_grip_pressed = bool(payload.get("gripButton", False))

#                 if None in (x, y, z, w, p, r):
#                     self.error_count += 1
#                     continue
#                 if any(math.isnan(v) or math.isinf(v) for v in (x, y, z, w, p, r)):
#                     self.error_count += 1
#                     continue

#                 with self.lock:
#                     # ⭐ 检测队列是否已满（将要溢出）
#                     if len(self._frame_queue) == self._frame_queue.maxlen:
#                         self.queue_overflow_count += 1

#                     # ⭐ append 到队列尾部；若满，deque 自动丢弃最老的帧（左侧）
#                     self._frame_queue.append((x, y, z, w, p, r))

#                     # 边缘检测放在接收线程内
#                     if b4_grip_pressed != self.latest_grip:
#                         self.grip_changed = True
#                     self.latest_grip = b4_grip_pressed
#                     self.recv_count += 1

#             except json.JSONDecodeError as e:
#                 self.error_count += 1
#                 self.logger.warning(f"JSON parse error: {e}")
#             except socket.timeout:
#                 continue
#             except Exception as e:
#                 if self.running:
#                     self.logger.error(f"UDP receive error: {e}")

#     def get_next(self) -> Tuple[Optional[Tuple], bool, bool]:
#         """
#         ⭐ 从队列头部取出最老的一帧（FIFO，不丢帧）。
#         返回: (坐标帧 or None, 当前夹爪状态, 夹爪是否发生了边缘跳变)
#         grip_changed 读取后立即清除。
#         """
#         with self.lock:
#             frame = self._frame_queue.popleft() if self._frame_queue else None
#             grip_state   = self.latest_grip
#             grip_changed = self.grip_changed
#             if grip_changed:
#                 self.grip_changed = False   # 读后清除，防止重复触发
#             return frame, grip_state, grip_changed

#     @property
#     def queue_size(self) -> int:
#         with self.lock:
#             return len(self._frame_queue)

#     def stop(self) -> None:
#         self.running = False
#         self.transport.close()


# # ==================== 主业务流程 ====================
# class FanucTeleopControllerSlidingWindow:
#     BUFFER_SIZE = 8
#     INTER_PACKET_DELAY = 0.002

#     def __init__(self, config: TeleopConfig):
#         self.config = config
#         self.udp_receiver = UDPDataReceiver(config.udp)
#         self.frc_sender   = FRCAsyncSender()
#         self.logger = logging.getLogger(self.__class__.__name__)
#         self.pending_seq_ids: set = set()

#     # --------------------------------------------------
#     # 生成安全的双阀切换指令序列
#     # --------------------------------------------------
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

#     # --------------------------------------------------
#     # 程序退出时强制复位两路气阀
#     # --------------------------------------------------
#     def _reset_gripper_safe(self, last_known_pose: Optional[Tuple]) -> None:
#         if last_known_pose is None:
#             self.logger.warning("⚠️  无有效坐标，跳过气阀复位（请手动检查气阀！）")
#             return
#         self.logger.info("🔒 安全复位：断开所有气阀...")
#         for port in (RO_PORT_OPEN, RO_PORT_CLOSE):
#             try:
#                 ok = self.frc_sender.send_async(
#                     last_known_pose,
#                     utool=self.config.robot.utool,
#                     uframe=self.config.robot.uframe,
#                     speed=self.config.robot.speed_mm_s,
#                     term_type=self.config.robot.term_type,
#                     term_value=self.config.robot.term_value,
#                     lcb_type="TA", lcb_value=10,
#                     port_type=2, port_number=port, port_value="OFF",
#                 )
#                 self.logger.info(f"   RO[{port}] → OFF {'✅' if ok else '❌'}")
#                 time.sleep(0.06)
#             except Exception as e:
#                 self.logger.error(f"   RO[{port}] 复位异常: {e}")
#         self.logger.info("🔒 气阀复位完成")

#     # --------------------------------------------------
#     # 主循环
#     # --------------------------------------------------
#     def run(self) -> None:
#         self.logger.info("=" * 60)
#         self.logger.info("🤖 FANUC Real-Time Teleoperation (With Gripper Control)")
#         self.logger.info(f"   Target FPS : {self.config.performance.target_fps} Hz")
#         self.logger.info("=" * 60)

#         self.udp_receiver.start()

#         try:
#             self.frc_sender.connect(
#                 self.config.robot.host,
#                 self.config.robot.port,
#                 self.config.robot.group,
#             )
#         except Exception as e:
#             self.logger.error(f"❌ Connection failed: {e}")
#             self.udp_receiver.stop()
#             return

#         frame_times = []
#         sent_count  = 0
#         ack_count   = 0
#         none_count  = 0          # 队列为空时的跳过次数
#         buffer_full_skip = 0     # ⭐ 缓冲区满时的跳过次数（新增，诊断用）
#         last_print_time = time.perf_counter()

#         lcb_queue: list = []
#         last_sent_pose: Optional[Tuple] = None
#         MIN_DIST_MM = 0.01

#         try:
#             self.logger.info("🔄 初始化：等待 UDP 数据并开始填充缓冲区...")

#             while True:
#                 t_start = time.perf_counter()

#                 # ── 1. 排空 ACK ──────────────────────────────────────
#                 while True:
#                     ack_seq, ack_err = self.frc_sender.check_ack()
#                     if ack_seq is None:
#                         break
#                     if ack_seq in self.pending_seq_ids:
#                         self.pending_seq_ids.remove(ack_seq)
#                         ack_count += 1
#                         if ack_err != 0:
#                             self.logger.warning(f"⚠️ Seq {ack_seq} error {ack_err}")

#                 # ── 2. 缓冲区满检查 ───────────────────────────────────
#                 # ⭐ 关键改动：满时不再 continue 跳过整个循环。
#                 #    而是把 UDP 帧消费掉（避免队列积压），然后稍等再重试。
#                 if len(self.pending_seq_ids) >= self.BUFFER_SIZE:
#                     buffer_full_skip += 1
#                     # 消费一帧以防队列溢出，但本轮不发送
#                     self.udp_receiver.get_next()
#                     time.sleep(0.001)
#                     continue

#                 # ── 3. 取帧（FIFO，不丢帧）────────────────────────────
#                 target_pose, current_gripper_state, grip_changed = self.udp_receiver.get_next()

#                 # ── 4. 夹爪边缘检测（在 pose 判空之前）───────────────
#                 if grip_changed:
#                     self.logger.info(
#                         f"🦾 夹爪切换 → {'闭合/抓取' if current_gripper_state else '打开/松开'}"
#                     )
#                     if lcb_queue:
#                         self.logger.warning(
#                             f"⚠️  旧夹爪队列未清空({len(lcb_queue)}条)，强制清空！"
#                         )
#                         lcb_queue.clear()
#                     lcb_queue.extend(self._build_safe_gripper_cmds(current_gripper_state))

#                 # ── 5. pose 为空时：若有 IO 任务则用上一帧坐标补发 ──
#                 if target_pose is None:
#                     if lcb_queue and last_sent_pose is not None:
#                         target_pose = last_sent_pose   # 原地不动，只发 IO
#                     else:
#                         none_count += 1
#                         time.sleep(0.001)
#                         continue

#                 # ── 6. 取出下一条 IO 动作 ─────────────────────────────
#                 current_lcb = lcb_queue.pop(0) if lcb_queue else None

#                 # ── 7. 气阀切换前延时 ─────────────────────────────────
#                 if current_lcb and current_lcb.get("delay_before_ms", 0) > 0:
#                     delay_s = current_lcb["delay_before_ms"] / 1000.0
#                     self.logger.info(f"⏳ 等待气压释放 {current_lcb['delay_before_ms']}ms ...")
#                     time.sleep(delay_s)

#                 # ── 8. 空间滤波（有 IO 任务时强制下发）──────────────
#                 if last_sent_pose is not None and not current_lcb:
#                     dx = target_pose[0] - last_sent_pose[0]
#                     dy = target_pose[1] - last_sent_pose[1]
#                     dz = target_pose[2] - last_sent_pose[2]
#                     if math.sqrt(dx**2 + dy**2 + dz**2) < MIN_DIST_MM:
#                         none_count += 1
#                         time.sleep(0.001)
#                         continue

#                 # ── 9. 发送 ──────────────────────────────────────────
#                 ok = self.frc_sender.send_async(
#                     target_pose,
#                     utool=self.config.robot.utool,
#                     uframe=self.config.robot.uframe,
#                     speed=self.config.robot.speed_mm_s,
#                     term_type=self.config.robot.term_type,
#                     term_value=self.config.robot.term_value,
#                     lcb_type="TA" if current_lcb else None,
#                     lcb_value=10   if current_lcb else 0,
#                     port_type=2    if current_lcb else None,
#                     port_number=current_lcb["port"] if current_lcb else None,
#                     port_value=current_lcb["val"]   if current_lcb else None,
#                 )

#                 if ok:
#                     seq_id = self.frc_sender.seq_id - 1
#                     self.pending_seq_ids.add(seq_id)
#                     sent_count += 1
#                     last_sent_pose = target_pose

#                     if len(self.pending_seq_ids) < self.BUFFER_SIZE:
#                         time.sleep(self.INTER_PACKET_DELAY)

#                 # ── 10. 循环耗时采样（可选诊断）─────────────────────
#                 t_end = time.perf_counter()
#                 frame_times.append((t_end - t_start) * 1000)

#                 # 每 5 秒打印一次实时统计
#                 if t_end - last_print_time >= 5.0:
#                     q = self.udp_receiver.queue_size
#                     ov = self.udp_receiver.queue_overflow_count
#                     self.logger.info(
#                         f"📈 sent={sent_count}  ack={ack_count}  "
#                         f"buf_full_skip={buffer_full_skip}  "
#                         f"udp_queue={q}  queue_overflow={ov}"
#                     )
#                     last_print_time = t_end

#         except KeyboardInterrupt:
#             self.logger.warning("\n⚠️  用户中断")
#         except Exception as e:
#             self.logger.error(f"💥 异常: {e}")
#             import traceback
#             traceback.print_exc()
#         finally:
#             self._reset_gripper_safe(last_sent_pose)
#             self._shutdown(frame_times, sent_count, ack_count, none_count,
#                            buffer_full_skip, {})

#     # --------------------------------------------------
#     # 统计报告
#     # --------------------------------------------------
#     def _shutdown(self, frame_times, sent_count, ack_count,
#                   none_count, buffer_full_skip, buffer_histogram) -> None:
#         self.udp_receiver.stop()
#         self.frc_sender.disconnect()

#         self.logger.info("\n" + "=" * 60)
#         self.logger.info("📊 性能统计报告 (滑动窗口模式)")
#         self.logger.info("=" * 60)
#         self.logger.info(f"  UDP 接收总帧数    : {self.udp_receiver.recv_count}")
#         self.logger.info(f"  UDP 解析错误      : {self.udp_receiver.error_count}")
#         self.logger.info(f"  实际发送指令      : {sent_count}")
#         self.logger.info(f"  ACK 成功返回      : {ack_count}")
#         self.logger.info(
#             f"  发送覆盖率        : "
#             f"{100*sent_count/max(1,self.udp_receiver.recv_count):.1f}%"
#         )
#         self.logger.info(f"  无数据跳过        : {none_count}")
#         self.logger.info(
#             f"  缓冲区满跳过      : {buffer_full_skip}  "
#             f"← ⭐ 这个数字高说明机器人ACK慢是主要瓶颈"
#         )
#         self.logger.info(
#             f"  UDP队列溢出丢帧   : {self.udp_receiver.queue_overflow_count}  "
#             f"← 如果此值大，请增大 UDP_FRAME_QUEUE_MAXLEN"
#         )

#         if buffer_histogram:
#             self.logger.info("\n  📊 缓冲区状态占比：")
#             for size in sorted(buffer_histogram.keys()):
#                 count = buffer_histogram[size]
#                 pct = 100 * count / sum(buffer_histogram.values())
#                 bar = "█" * int(pct / 5)
#                 self.logger.info(f"      {size}/8: {count:6d} 次 ({pct:5.1f}%) {bar}")

#         if frame_times:
#             avg_ms = statistics.mean(frame_times)
#             self.logger.info(f"\n  底层循环平均耗时  : {avg_ms:.2f} ms")
#             self.logger.info(f"  底层循环频率      : {1000/avg_ms:.1f} Hz")
#             self.logger.info(
#                 f"  最小 / 最大耗时   : "
#                 f"{min(frame_times):.2f} / {max(frame_times):.2f} ms"
#             )
#         self.logger.info("=" * 60)


# def main():
#     config = TeleopConfig()
#     controller = FanucTeleopControllerSlidingWindow(config)
#     controller.run()


# if __name__ == "__main__":
#     main()