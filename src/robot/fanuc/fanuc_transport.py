# Copyright 2024 FANUC Project
#
# Transport Layer: Low-level Network Communication
# 
# 职责：处理TCP/UDP的原始数据传输，不涉及业务逻辑

import socket
import json
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# ==================== TCP 粘包处理 ====================
class LineSocket:
    """
    TCP套接字包装器，自动处理粘包问题。
    
    FANUC RMI协议使用\r\n或\n分隔的JSON行格式，
    这个类负责缓冲和分行读取。
    """
    
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buf = b""
        self.logger = logging.getLogger(self.__class__.__name__)
    
    def sendall(self, data: bytes) -> None:
        """发送数据"""
        self.sock.sendall(data)
    
    def settimeout(self, timeout: float) -> None:
        """设置套接字超时"""
        self.sock.settimeout(timeout)
    
    def readline(self) -> str:
        """读取一行（\r\n或\n分隔）"""
        while True:
            for sep in (b"\r\n", b"\n"):
                idx = self._buf.find(sep)
                if idx != -1:
                    line = self._buf[:idx].decode(errors="replace").strip()
                    self._buf = self._buf[idx + len(sep):]
                    if line:
                        return line
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Socket closed")
            self._buf += chunk
    
    def read_json(self) -> dict:
        """读取一行并解析为JSON"""
        line = self.readline()
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse JSON: {line}")
            raise
    
    def drain(self, timeout: float = 0.5) -> None:
        """清空接收缓冲区（忽略未读数据）"""
        self.sock.settimeout(timeout)
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
        except socket.timeout:
            pass
        self._buf = b""
    
    def close(self) -> None:
        """关闭连接"""
        self.sock.close()


# ==================== TCP 优化 ====================
def optimize_tcp_socket(sock: socket.socket) -> None:
    """
    优化TCP套接字以降低延迟。
    
    - TCP_NODELAY: 禁用Nagle算法，立即发送小包
    - SO_RCVBUF/SO_SNDBUF: 增大缓冲区以避免丢包
    - SO_KEEPALIVE: 检测死连接
    """
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        logger.debug("✅ TCP socket optimized for low-latency")
    except Exception as e:
        logger.warning(f"TCP optimization warning: {e}")


# ==================== UDP 接收 ====================
class UDPTransport:
    """
    低层UDP接收器。
    
    职责：
    - 监听UDP端口
    - 接收原始数据包
    - 不涉及解析或缓存逻辑
    """
    
    def __init__(self, host: str = "0.0.0.0", port: int = 9000, buffer_size: int = 4096):
        self.host = host
        self.port = port
        self.buffer_size = buffer_size
        self.sock = None
        self.logger = logging.getLogger(self.__class__.__name__)
    
    def bind(self) -> None:
        """绑定到指定地址和端口"""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.settimeout(1.0)
        self.logger.info(f"UDP transport bound to {self.host}:{self.port}")
    
    def recv(self) -> Tuple[bytes, Tuple[str, int]]:
        """接收一个数据包，返回(数据, 发送者地址)"""
        data, addr = self.sock.recvfrom(self.buffer_size)
        return data, addr
    
    def close(self) -> None:
        """关闭UDP套接字"""
        if self.sock:
            try:
                self.sock.close()
            except:
                pass


# ==================== TCP 连接 ====================
class TCPTransport:
    """
    低层TCP连接管理。
    
    职责：
    - 建立TCP连接
    - 维护连接状态
    - 处理发送/接收的原始字节流
    """
    
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.ls = None
        self.logger = logging.getLogger(self.__class__.__name__)
    
    def connect(self, timeout: float = 5.0) -> None:
        """建立TCP连接（使用LineSocket包装以处理粘包）"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            optimize_tcp_socket(sock)
            sock.connect((self.host, self.port))
            sock.settimeout(timeout)
            self.ls = LineSocket(sock)
            self.logger.info(f"Connected to {self.host}:{self.port}")
        except Exception as e:
            self.logger.error(f"Failed to connect: {e}")
            raise
    
    def send_json(self, data: dict) -> None:
        """发送JSON数据"""
        try:
            packet = (json.dumps(data) + "\r\n").encode()
            self.ls.sendall(packet)
        except Exception as e:
            self.logger.error(f"Failed to send: {e}")
            raise
    
    def recv_json(self) -> dict:
        """接收JSON数据"""
        try:
            return self.ls.read_json()
        except socket.timeout:
            # 非阻塞轮询超时，正常现象，不记录日志
            raise
        except Exception as e:
            self.logger.error(f"Failed to receive: {e}")
            raise
    
    def close(self) -> None:
        """关闭连接"""
        if self.ls:
            try:
                self.ls.close()
            except:
                pass
            self.logger.info("Connection closed")
