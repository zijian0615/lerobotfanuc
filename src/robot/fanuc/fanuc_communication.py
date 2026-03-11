# Copyright 2024 FANUC Project
#
# Communication Layer: FANUC RMI Protocol Implementation
#
# 职责：实现FANUC RMI协议的具体细节，包括：
# - RMI握手和初始化
# - 异步发送和ACK接收
# - 数据包构造

import json
import logging
import threading
import queue
import time
import socket
from typing import Tuple, Optional

from .fanuc_transport import TCPTransport

logger = logging.getLogger(__name__)


class FRCInitializer:
    """
    FANUC RMI初始化器。
    
    负责FRC_Connect和FRC_Initialize的握手过程。
    """
    
    @staticmethod
    def frc_connect(host: str, port: int) -> int:
        """
        执行FRC_Connect握手，获取动态端口号。
        
        Args:
            host: FANUC机器人IP
            port: RMI初始端口 (通常16001)
        
        Returns:
            动态端口号
        
        Raises:
            RuntimeError: 握手失败
        """
        import socket
        
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((host, port))
            s.sendall(b'{"Communication": "FRC_Connect"}\r\n')
            resp = s.recv(4096)
        
        data = json.loads(resp.decode())
        logger.info(f"FRC_Connect response: {data}")
        
        if data.get("ErrorID", -1) != 0:
            raise RuntimeError(f"FRC_Connect failed: {data}")
        
        return data["PortNumber"]


class FRCPacketFactory:
    """
    FANUC RMI数据包工厂。
    
    负责构造符合FANUC RMI协议的数据包。
    """
    
    @staticmethod
    def normalize_angle(angle: float) -> float:
        """
        将任意角度规范化到[-180, +180]范围内。
        FANUC RMI要求角度在此范围内。
        """
        angle = float(angle) % 360.0
        if angle > 180.0:
            angle -= 360.0
        return angle
    
    @staticmethod
    def linear_motion_packet(
        seq_id: int,
        target: Tuple[float, float, float, float, float, float],
        utool: int = 1,
        uframe: int = 1,
        speed: int = 150,
        term_type: str = "FINE",
        term_value: int = 0,
    ) -> bytes:
        """
        构造FRC_LinearMotion数据包。
        
        Args:
            seq_id: 序列号
            target: (x, y, z, w, p, r)位置目标
            utool: 工具号 (default=1)
            uframe: 用户坐标系号 (default=1)
            speed: 移动速度 mm/s (default=150)
            term_type: 终止类型 "FINE"(精确停止) 或 "CNT"(连续) (default="FINE")
            term_value: 终止值 (FINE时=0, CNT时=0-100) (default=0)
        
        Returns:
            JSON编码的数据包（包含\r\n结尾）
        """
        x, y, z, w, p, r = target
        
        # 边界约束
        x = max(-2000, min(2000, float(x)))
        y = max(-2000, min(2000, float(y)))
        z = max(-2000, min(2000, float(z)))
        w = FRCPacketFactory.normalize_angle(w)
        p = FRCPacketFactory.normalize_angle(p)
        r = FRCPacketFactory.normalize_angle(r)
        speed = max(1, min(1000, int(speed)))
        
        # 验证 term_type 并约束 term_value
        term_type = str(term_type).upper()
        if term_type not in ("FINE", "CNT"):
            term_type = "FINE"
        if term_type == "FINE":
            term_value = 0
        else:
            term_value = max(0, min(100, int(term_value)))
        
        packet_dict = {
            "Instruction": "FRC_LinearMotion",
            "SequenceID": int(seq_id),
            "Configuration": {
                "UToolNumber": int(utool),
                "UFrameNumber": int(uframe),
                "Front": 1, "Up": 1, "Left": 0,
                "Flip": 0, "Turn4": 0, "Turn5": 0, "Turn6": 0
            },
            "Position": {
                "X": x, "Y": y, "Z": z,
                "W": w, "P": p, "R": r,
                "Ext1": 0.0, "Ext2": 0.0, "Ext3": 0.0
            },
            "SpeedType": "mmSec",
            "Speed": speed,
            "TermType": term_type,
            "TermValue": term_value
        }
        
        return (json.dumps(packet_dict) + "\r\n").encode()


class FRCAsyncSender:
    """
    FANUC RMI异步发送器。
    
    职责：
    - 异步发送运动命令
    - 后台接收ACK并排队
    - 非阻塞地检查ACK状态
    - 记录指令执行时间用于性能分析
    """
    
    def __init__(self):
        self.tcp = None
        self.ack_queue = queue.Queue()
        self.ack_listener_thread = None
        self.running = False
        self.seq_id = 1
        self.send_times = {}  # 记录发送时间：{seq_id: send_time}
        self.logger = logging.getLogger(self.__class__.__name__)
    
    def connect(self, host: str, port: int, group: int = 1) -> None:
        """
        连接FANUC并初始化FRC。
        
        Args:
            host: FANUC机器人IP
            port: RMI端口
            group: 控制组号
        """
        # 第1步：FRC_Connect握手获取动态端口
        dynamic_port = FRCInitializer.frc_connect(host, port)
        self.logger.info(f"FRC_Connect returned dynamic port: {dynamic_port}")
        
        # 第2步：连接到动态端口
        self.tcp = TCPTransport(host, dynamic_port)
        self.tcp.connect()
        
        # 第3步：FRC_Initialize初始化
        init_cmd = {
            "Command": "FRC_Initialize",
            "GroupMask": group
        }
        self.tcp.send_json(init_cmd)
        resp = self.tcp.recv_json()
        self.logger.info(f"FRC_Initialize response: {resp}")
        
        if resp.get("ErrorID", -1) != 0:
            raise RuntimeError(f"FRC_Initialize failed: {resp}")
        
        # 清空接收缓冲区
        self.tcp.ls.drain()
        
        # 第4步：启动ACK监听线程
        self.running = True
        self.ack_listener_thread = threading.Thread(target=self._listen_acks, daemon=True)
        self.ack_listener_thread.start()
        
        self.logger.info("✅ FRC initialized, ACK listener started")
    
    def _listen_acks(self) -> None:
        """后台线程：持续监听ACK"""
        error_desc = {
            2556952: "Configuration parameter error",
            2556956: "Robot still executing (RMIT-028)",
            2556957: "Invalid parameter or robot state",
            2556959: "Position data invalid/out of range",
        }
        
        first_ack = True  # 标记第一个ACK用于调试
        ack_count = 0
        
        while self.running:
            try:
                self.tcp.ls.settimeout(0.1)
                try:
                    resp = self.tcp.recv_json()
                    
                    ack_count += 1
                    
                    # 🔍 调试：检查是否是系统故障
                    if resp.get("Communication") == "FRC_SystemFault":
                        seq_id = resp.get("SequenceID", -1)
                        if first_ack:
                            self.logger.error(f"🚨 FIRST ACK IS SYSTEM FAULT!")
                            self.logger.error(f"   Full ACK: {resp}")
                            self.logger.error(f"   Possible causes:")
                            self.logger.error(f"   1. Robot not enabled (check teach pendant)")
                            self.logger.error(f"   2. Coordinates out of reach")
                            self.logger.error(f"   3. Missing initialization handshake")
                            first_ack = False
                        
                        # 系统故障当作error_id=-2来处理
                        self.ack_queue.put((seq_id, -2, time.perf_counter()))
                        continue
                    
                    # 🔍 调试：打印所有响应内容（帮助诊断CNT模式问题）
                    if first_ack or ack_count <= 5:
                        self.logger.info(f"📥 ACK #{ack_count}: {resp}")
                        first_ack = False
                    
                    seq_id = resp.get("SequenceID", -1)
                    err_id = resp.get("ErrorID", -1)
                    self.ack_queue.put((seq_id, err_id, time.perf_counter()))
                    
                    if err_id != 0:
                        desc = error_desc.get(err_id, "Unknown error")
                        self.logger.warning(
                            f"ACK Error: seq={seq_id} err={err_id} (0x{err_id:06x}) - {desc}"
                        )
                except socket.timeout:
                    # 非阻塞轮询超时，继续等待
                    pass
            except Exception as e:
                if self.running:
                    self.logger.error(f"ACK listener error: {e}")
                break
    
    # def send_async(
    #     self,
    #     target: Tuple[float, float, float, float, float, float],
    #     utool: int = 1,
    #     uframe: int = 1,
    #     speed: int = 150,
    #     term_type: str = "FINE",
    #     term_value: int = 0,
    # ) -> bool:
    #     """
    #     异步发送一条运动命令。
        
    #     Args:
    #         target: (x, y, z, w, p, r)位置目标
    #         utool: 工具号 (default=1)
    #         uframe: 用户坐标系号 (default=1)
    #         speed: 速度 mm/s (default=150)
    #         term_type: 终止类型 "FINE" 或 "CNT" (default="FINE")
    #         term_value: 终止值 (default=0)
        
    #     Returns:
    #         True if send succeeded, False otherwise
    #     """
    #     try:
    #         current_seq_id = self.seq_id
    #         packet = FRCPacketFactory.linear_motion_packet(
    #             current_seq_id, target, utool, uframe, speed, term_type, term_value
    #         )
            
    #         # 🔍 调试：打印发送的数据包（仅第一条指令）
    #         if current_seq_id == 1:
    #             self.logger.info(f"📤 FIRST PACKET DEBUG: {packet.decode().strip()}")
            
    #         self.tcp.ls.sendall(packet)
            
    #         # ✅ 记录发送时间，用于后续计算执行时间
    #         self.send_times[current_seq_id] = time.perf_counter()
            
    #         self.seq_id += 1
    #         return True
    #     except Exception as e:
    #         self.logger.error(f"Send failed: {e}")
    #         return False
    def send_async(self, pos: tuple, utool=1, uframe=1, speed=150, term_type="CNT", term_value=100,
                   lcb_type=None, lcb_value=0, port_type=None, port_number=None, port_value=None) -> bool:
        """
        发送运动指令（非阻塞）并附带可选的 LCB IO 控制
        """
        if not self.tcp:
            return False
            
        x, y, z, w, p, r = pos
        packet = {
            "Instruction": "FRC_LinearMotion",
            "SequenceID": self.seq_id,
            "Configuration": {
                "UToolNumber": utool, "UFrameNumber": uframe,
                "Front": 1, "Up": 1, "Left": 0, "Flip": 0, "Turn4": 0, "Turn5": 0, "Turn6": 0
            },
            "Position": {
                "X": float(x), "Y": float(y), "Z": float(z),
                "W": float(w), "P": float(p), "R": float(r),
                "Ext1": 0.0, "Ext2": 0.0, "Ext3": 0.0
            },
            "SpeedType": "mmSec",
            "Speed": int(speed),
            "TermType": str(term_type),
            "TermValue": int(term_value)
        }

        # 如果传入了 LCB 夹爪控制参数，追加进 packet
        if lcb_type and port_type and port_number and port_value:
            packet.update({
                "LCBType": str(lcb_type),
                "LCBValue": int(lcb_value),
                "PortType": int(port_type),
                "PortNumber": int(port_number),
                "PortValue": str(port_value)
            })

        msg = (json.dumps(packet) + "\r\n").encode('utf-8')
        try:
            self.tcp.ls.sendall(msg)
            self.seq_id += 1
            return True
        except Exception as e:
            self.logger.error(f"Failed to send async: {e}")
            return False
        
    def check_ack(self) -> Tuple[Optional[int], Optional[int]]:
        """
        非阻塞地检查是否有待处理的ACK。
        
        Returns:
            (seq_id, error_id) 如果有ACK，否则 (None, None)
        """
        try:
            seq_id, err_id, _ = self.ack_queue.get_nowait()
            return seq_id, err_id
        except queue.Empty:
            return None, None
    
    def wait_until_executed(self, seq_id: int, timeout_s: float = 5.0) -> Tuple[bool, Optional[int]]:
        """
        阻塞等待指定序列号的命令执行完成（ACK ErrorID == 0）。
        
        这是关键的同步机制，确保指令不会堆积在机械臂寄存器中。
        每次发送新指令前，必须等待上一条指令执行完成。
        
        Args:
            seq_id: 要等待的序列号
            timeout_s: 超时时间（秒）
        
        Returns:
            (success, error_id)
            - success=True, error_id=0: 指令成功执行
            - success=True, error_id=2556956: 还在执行（应重试）
            - success=False, error_id=error_code: 执行出错
            - success=False, error_id=None: 超时
        """
        deadline = time.perf_counter() + timeout_s
        error_desc = {
            2556952: "Configuration parameter error",
            2556956: "Robot still executing (keep waiting)",
            2556957: "Invalid parameter or robot state",
            2556959: "Position data invalid/out of range",
        }
        
        while True:
            try:
                # 非阻塞检查，等待100ms后重试
                ack_seq, ack_err = self.check_ack()
                
                if ack_seq == seq_id:
                    # 找到对应的ACK
                    if ack_err == 0:
                        # ✅ 执行成功
                        return True, 0
                    elif ack_err == 2556956:
                        # ⏳ 还在执行，继续等待
                        if time.perf_counter() > deadline:
                            self.logger.warning(f"Seq {seq_id} timeout while executing")
                            return False, None
                        time.sleep(0.01)  # 小睡眠避免忙轮询
                        continue
                    else:
                        # ❌ 执行出错
                        desc = error_desc.get(ack_err, "Unknown error")
                        self.logger.error(f"Seq {seq_id} execution failed: err={ack_err} - {desc}")
                        return False, ack_err
                
                # 还没收到这个seq_id的ACK，继续等待
                if time.perf_counter() > deadline:
                    self.logger.warning(f"Seq {seq_id} timeout waiting for ACK")
                    return False, None
                
                time.sleep(0.01)  # 避免忙轮询
            
            except Exception as e:
                self.logger.error(f"Error waiting for seq {seq_id}: {e}")
                return False, None
    
    def frc_get_status(self) -> dict:
        """
        发送FRC_GetStatus命令获取机器人状态。
        用于诊断为什么FRC_SystemFault会出现。
        """
        try:
            self.tcp.ls.settimeout(2.0)
            self.tcp.send_json({"Command": "FRC_GetStatus"})
            resp = self.tcp.recv_json()
            self.logger.info(f"🔍 FRC_GetStatus response: {resp}")
            return resp
        except Exception as e:
            self.logger.warning(f"FRC_GetStatus failed: {e}")
            return {}
    
    def frc_abort(self) -> None:
        """发送FRC_Abort命令中止运动"""
        try:
            self.tcp.ls.settimeout(5.0)
            self.tcp.send_json({"Command": "FRC_Abort"})
            resp = self.tcp.recv_json()
            self.logger.info(f"FRC_Abort response: {resp}")
        except Exception as e:
            self.logger.warning(f"FRC_Abort failed: {e}")
    
    def disconnect(self) -> None:
        """断开连接"""
        self.running = False
        
        try:
            time.sleep(0.1)
            if self.ack_listener_thread:
                self.ack_listener_thread.join(timeout=1)
        except:
            pass
        
        try:
            self.frc_abort()
        except:
            pass
        
        if self.tcp:
            self.tcp.close()
        
        self.logger.info("🔌 Disconnected from FANUC")
