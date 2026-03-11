# Copyright 2024 FANUC Project
#
# 高层业务逻辑模块 - 滑动窗口版本 (Sliding Window)
#
# 基于FANUC RMI官方手册第52页的缓冲区处理机制
#
# 核心机制：
# 1. 初始填充：发送8条指令填满缓冲区（每条延迟2ms）
# 2. 滑动维持：每收到1个ACK就发送新的1条指令，无需强行限制FPS，ACK速度即为执行速度
# 3. 避免死锁：CNT必须在下一条指令到来前执行
#
# 这个文件只负责：
# - 控制流程（主循环）
# - 业务逻辑（UDP接收 + FANUC发送）
# - 性能统计和日志

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


# ==================== UDP 数据接收器 ====================
class UDPDataReceiver:
    """
    后台线程接收UDP数据并缓存最新帧。
    
    职责：只负责UDP数据接收，不涉及业务逻辑。
    """
    
    def __init__(self, config: UDPReceiverConfig):
        self.config = config
        self.transport = UDPTransport(config.host, config.port, config.buffer_size)
        self.latest_frame: Optional[Tuple] = None
        self.lock = threading.Lock()
        self.running = False
        self.thread = None
        self.recv_count = 0
        self.error_count = 0
        self.logger = logging.getLogger(self.__class__.__name__)
    
    def start(self) -> None:
        """启动UDP接收线程"""
        self.transport.bind()
        self.running = True
        self.thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.thread.start()
        self.logger.info(f"✅ UDP Receiver listening on {self.config.host}:{self.config.port}")
    
    def _recv_loop(self) -> None:
        """后台接收线程"""
        while self.running:
            try:
                data, addr = self.transport.recv()
                payload = json.loads(data.decode("utf-8"))
                
                fanuc = payload.get("fanuc", {})
                x = fanuc.get("x")
                y = fanuc.get("y")
                z = fanuc.get("z")
                w = fanuc.get("w")
                p = fanuc.get("p")
                r = fanuc.get("r")
                
                # buttons = payload.get("buttons", {})
                # b0_secondary = payload.get("b0_secondary") #B
                # b1_primary = payload.get("b1_primary") #A
                # b2_joystick = payload.get("b2_joystick") #pole
                # b3_trigger = payload.get("b3_trigger") #top
                # b4_grip = payload.get("b4_grip") #side
                # 数据验证
                if None in (x, y, z, w, p, r):
                    self.error_count += 1
                    continue
                if any(math.isnan(v) or math.isinf(v) for v in (x, y, z, w, p, r)):
                    self.error_count += 1
                    continue
                
                with self.lock:
                    self.latest_frame = (x, y, z, w, p, r)
                    self.recv_count += 1
            
            except json.JSONDecodeError as e:
                self.error_count += 1
                self.logger.warning(f"JSON parse error: {e}")
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.logger.error(f"UDP receive error: {e}")
    
    def get_latest(self) -> Optional[Tuple]:
        """取出最新帧（取走后清空）"""
        with self.lock:
            frame = self.latest_frame
            self.latest_frame = None
            return frame
    
    def stop(self) -> None:
        """停止接收"""
        self.running = False
        self.transport.close()


# ==================== 主业务流程 ====================
class FanucTeleopControllerSlidingWindow:
    """
    FANUC遥操主控制器 - 滑动窗口版本。
    
    基于FANUC RMI官方手册第52页缓冲区处理机制
    """
    
    BUFFER_SIZE = 8  # RMI缓冲区大小
    INTER_PACKET_DELAY = 0.002  # 2ms，防止TCP粘包
    
    def __init__(self, config: TeleopConfig):
        self.config = config
        self.udp_receiver = UDPDataReceiver(config.udp)
        self.frc_sender = FRCAsyncSender()
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # 滑动窗口状态
        self.pending_seq_ids = set()  # 待执行的seq_id集合
    
    def run(self) -> None:
        """运行遥操主循环（滑动窗口模式）"""
        self.logger.info("=" * 60)
        self.logger.info("🤖 FANUC Real-Time Teleoperation (Sliding Window)")
        self.logger.info(f"   UDP Input  : {self.config.udp.host}:{self.config.udp.port}")
        self.logger.info(f"   FANUC RMI  : {self.config.robot.host}:{self.config.robot.port}")
        self.logger.info(f"   Speed      : {self.config.robot.speed_mm_s} mm/s   TermType={self.config.robot.term_type}")
        self.logger.info(f"   Target FPS : {self.config.performance.target_fps} Hz (ACK Driven)")
        self.logger.info(f"   Buffer Size: 8 (RMI standard)")
        self.logger.info("=" * 60)
        
        # 启动UDP接收
        self.udp_receiver.start()
        
        # 连接FANUC
        try:
            self.frc_sender.connect(
                self.config.robot.host,
                self.config.robot.port,
                self.config.robot.group
            )
            # ⚠️ 极度重要：请确保你在 FRCAsyncSender 类的 connect 方法里设置了：
            # self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception as e:
            self.logger.error(f"❌ Connection failed: {e}")
            self.udp_receiver.stop()
            return
        
        # 初始化统计
        frame_times =[]
        sent_count = 0
        ack_count = 0
        none_count = 0
        last_print_time = time.perf_counter()
        buffer_histogram = {}  # 缓冲区大小分布
        
        self.logger.info("\n▶️  开始实时遥操作，按 Ctrl+C 停止...\n")
        self.logger.info("（滑动窗口模式：初始填充8条 + 纯ACK驱动发送）\n")
        
        try:
            self.logger.info("🔄 初始化：等待 UDP 数据并开始填充缓冲区...")
            buffer_full = False
            
            # --- 新增：用于记录上一次实际发送的坐标 ---
            last_sent_pose = None  
            MIN_DIST_MM = 2.0  # 空间滤波阈值：两点距离小于 2mm 则不发送（可根据实际情况调大到 5.0）
            
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
                target = self.udp_receiver.get_latest()
                if target is None:
                    none_count += 1
                    time.sleep(0.001)  
                    continue
                
                # =======================================================
                # ⭐ 新增：空间滤波（防抖与规避 RMI Short Motion 惩罚）
                # =======================================================
                if last_sent_pose is not None:
                    # 计算欧氏距离 (只算XYZ位置，暂不计姿态，如有需要可加上姿态变化量)
                    dx = target[0] - last_sent_pose[0]
                    dy = target[1] - last_sent_pose[1]
                    dz = target[2] - last_sent_pose[2]
                    dist = math.sqrt(dx**2 + dy**2 + dz**2)
                    
                    if dist < MIN_DIST_MM:
                        # 距离太近，当做噪音或微小抖动忽略掉，不要去占用宝贵的 8 个缓冲区槽位
                        none_count += 1
                        time.sleep(0.001)
                        continue
                # =======================================================
                
                # 4. 发送指令
                ok = self.frc_sender.send_async(
                    target,
                    utool=self.config.robot.utool,
                    uframe=self.config.robot.uframe,
                    speed=self.config.robot.speed_mm_s,
                    term_type=self.config.robot.term_type,  # ⚠️ 确保配置文件里这里是 "CNT"
                    term_value=self.config.robot.term_value # ⚠️ 确保配置文件里这里是 100
                )
                
                if ok:
                    seq_id = self.frc_sender.seq_id - 1
                    self.pending_seq_ids.add(seq_id)
                    sent_count += 1
                    last_sent_pose = target  # 记录这次成功下发的点位
                    
                    if not buffer_full and len(self.pending_seq_ids) < self.BUFFER_SIZE:
                        time.sleep(self.INTER_PACKET_DELAY)
        
        except KeyboardInterrupt:
            self.logger.warning("\n⚠️  用户中断")
        except Exception as e:
            self.logger.error(f"💥 异常: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._shutdown(frame_times, sent_count, ack_count, none_count, buffer_histogram)
    
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
        
        # 缓冲区大小分布
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


# ==================== 快速启动 ====================
def main():
    """快速启动遥操控制器，使用默认配置"""
    config = TeleopConfig()
    controller = FanucTeleopControllerSlidingWindow(config)
    controller.run()


if __name__ == "__main__":
    main()