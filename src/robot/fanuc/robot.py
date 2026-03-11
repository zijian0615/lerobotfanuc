# Copyright 2024 FANUC Project
#
# 高层业务逻辑模块
#
# 这个文件只负责：
# - 控制流程（主循环）
# - 业务逻辑（UDP接收 + FANUC发送）
# - 性能统计和日志
#
# 底层细节已分离到：
# - fanuc_config.py      (配置)
# - fanuc_transport.py   (网络传输)
# - fanuc_communication.py (FANUC协议)

# if __name__ == "__main__":
#     main()
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
                
                # 数据验证
                if None in (x, y, z, w, p, r):
                    self.error_count += 1
                    continue
                if any(math.isnan(v) or math.isinf(v) for v in (x, y, z, w, p, r)):
                    self.error_count += 1
                    continue
                
                with self.lock:
                    # ✅ 修正坐标映射：反向 X 和 Y 轴（左/右、前/后反向问题）
                    # X轴:  向左 -> -x（反向）
                    # Y轴:  向前 -> -y（反向）
                    # Z轴:  向上 -> z（保持）
                    self.latest_frame = (-x, -y, z, w, p, r)
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
class FanucTeleopController:
    """
    FANUC遥操主控制器。
    
    协调UDP接收和FANUC发送，实现闭环遥操流程。
    """
    
    def __init__(self, config: TeleopConfig):
        self.config = config
        self.udp_receiver = UDPDataReceiver(config.udp)
        self.frc_sender = FRCAsyncSender()
        self.logger = logging.getLogger(self.__class__.__name__)
    
    def run(self) -> None:
        """运行遥操主循环"""
        self.logger.info("=" * 60)
        self.logger.info("🤖 FANUC Real-Time Teleoperation")
        self.logger.info(f"   UDP Input  : {self.config.udp.host}:{self.config.udp.port}")
        self.logger.info(f"   FANUC RMI  : {self.config.robot.host}:{self.config.robot.port}")
        self.logger.info(f"   Speed      : {self.config.robot.speed_mm_s} mm/s   TermType={self.config.robot.term_type} TermValue={self.config.robot.term_value}")
        self.logger.info(f"   Target FPS : {self.config.performance.target_fps} Hz")
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
        except Exception as e:
            self.logger.error(f"❌ Connection failed: {e}")
            self.udp_receiver.stop()
            return
        
        # 初始化统计
        frame_interval = self.config.frame_interval
        frame_times = []
        sent_count = 0
        ack_ok = 0
        ack_err_count = 0
        ack_timeout_count = 0
        none_count = 0
        last_print_time = time.perf_counter()
        last_seq_id = None  # 跟踪上一条发送的指令
        
        self.logger.info("\n▶️  开始实时遥操作，按 Ctrl+C 停止...\n")
        
        try:
            while True:
                t_start = time.perf_counter()
                
                # 如果有上一条指令，必须等待它执行完成再发送下一条
                # 这是防止指令堆积的关键！
                if last_seq_id is not None:
                    success, err_id = self.frc_sender.wait_until_executed(last_seq_id, timeout_s=2.0)
                    if success:
                        if err_id == 0:
                            ack_ok += 1
                        elif err_id == 2556956:
                            # 还在执行，这不应该发生因为我们在等待
                            pass
                    else:
                        if err_id is None:
                            ack_timeout_count += 1
                            self.logger.warning(f"Seq {last_seq_id} timeout")
                        else:
                            ack_err_count += 1
                
                # 获取最新目标位置
                target = self.udp_receiver.get_latest()
                
                if target is None:
                    none_count += 1
                    # 定期打印等待状态
                    if time.perf_counter() - last_print_time >= self.config.performance.print_interval_s:
                        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        self.logger.info(
                            f"[{ts}] ⏳ 等待 UDP 数据...  UDP接收: {self.udp_receiver.recv_count}  已发送: {sent_count}"
                        )
                        last_print_time = time.perf_counter()
                    time.sleep(frame_interval)
                    continue
                
                # 发送到FANUC（异步发送）
                ok = self.frc_sender.send_async(
                    target,
                    utool=self.config.robot.utool,
                    uframe=self.config.robot.uframe,
                    speed=self.config.robot.speed_mm_s,
                    term_type=self.config.robot.term_type,
                    term_value=self.config.robot.term_value
                )
                
                if ok:
                    sent_count += 1
                    last_seq_id = self.frc_sender.seq_id - 1  # 记录本次发送的seq_id
                
                # 帧率控制
                t_elapsed = time.perf_counter() - t_start
                t_sleep = max(0, frame_interval - t_elapsed)
                if t_sleep > 0:
                    time.sleep(t_sleep)
                
                t_total = time.perf_counter() - t_start
                frame_times.append(t_total * 1000)
                
                # 定期打印性能统计
                if time.perf_counter() - last_print_time >= self.config.performance.print_interval_s:
                    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    fps = (
                        1000.0 / statistics.mean(
                            frame_times[-self.config.performance.frame_history_size:]
                        )
                        if frame_times
                        else 0
                    )
                    self.logger.info(
                        f"[{ts}] "
                        f"发送: {sent_count:5d}  "
                        f"ACK✅: {ack_ok:5d}  ACK❌: {ack_err_count:3d}  Timeout: {ack_timeout_count:3d}  "
                        f"UDP总: {self.udp_receiver.recv_count:5d}  "
                        f"fps: {fps:5.1f}Hz  "
                        f"→ X={target[0]:+7.2f} Y={target[1]:+7.2f} Z={target[2]:+7.2f}  "
                        f"W={target[3]:+6.2f} P={target[4]:+6.2f} R={target[5]:+6.2f}"
                    )
                    last_print_time = time.perf_counter()
        
        except KeyboardInterrupt:
            self.logger.warning("\n⚠️  用户中断")
        except Exception as e:
            self.logger.error(f"💥 异常: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._shutdown(frame_times, sent_count, ack_ok, ack_err_count, ack_timeout_count, none_count)
    
    def _shutdown(self, frame_times, sent_count, ack_ok, ack_err_count, ack_timeout_count, none_count) -> None:
        """清理和统计"""
        self.udp_receiver.stop()
        self.frc_sender.disconnect()
        
        self.logger.info("\n" + "=" * 60)
        self.logger.info("📊 性能统计报告")
        self.logger.info("=" * 60)
        self.logger.info(f"  UDP 接收总帧数 : {self.udp_receiver.recv_count}")
        self.logger.info(f"  UDP 解析错误   : {self.udp_receiver.error_count}")
        self.logger.info(f"  实际发送帧数   : {sent_count}")
        self.logger.info(f"  ACK 成功       : {ack_ok}")
        self.logger.info(f"  ACK 错误       : {ack_err_count}")
        self.logger.info(f"  ACK 超时       : {ack_timeout_count}")
        self.logger.info(f"  无数据跳过     : {none_count}")
        
        if frame_times:
            avg_ms = statistics.mean(frame_times)
            avg_fps = 1000.0 / avg_ms
            self.logger.info(f"  平均帧间隔     : {avg_ms:.2f} ms")
            self.logger.info(f"  平均帧率       : {avg_fps:.1f} Hz")
            self.logger.info(f"  最小 / 最大    : {min(frame_times):.2f} / {max(frame_times):.2f} ms")
            if len(frame_times) > 1:
                self.logger.info(f"  标准差         : {statistics.stdev(frame_times):.2f} ms")
            status = "✅" if avg_fps >= self.config.performance.target_fps * 0.9 else "⚠️"
            self.logger.info(
                f"  {status} 目标帧率 {self.config.performance.target_fps}Hz → 实际 {avg_fps:.1f}Hz"
            )
        self.logger.info("=" * 60)


# ==================== 快速启动 ====================
def main():
    """快速启动遥操控制器，使用默认配置"""
    config = TeleopConfig()  # 使用dataclass默认值
    controller = FanucTeleopController(config)
    controller.run()


if __name__ == "__main__":
    main()