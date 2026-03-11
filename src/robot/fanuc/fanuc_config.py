# Copyright 2024 FANUC Project
#
# Configuration Module for FANUC Real-Time Teleoperation

from dataclasses import dataclass


@dataclass
class FanucRobotConfig:
    """FANUC机器人连接和运动配置"""
    
    # ==================== FANUC RMI 配置 ====================
    host: str = "172.30.109.22"      # FANUC机器人IP地址
    port: int = 16001                # RMI默认端口
    group: int = 1                   # 控制组号
    
    # ==================== 运动参数 ====================
    utool: int = 1                   # 工具号
    uframe: int = 0                  # 用户坐标系号 (0=世界坐标, 1=用户坐标)
    speed_mm_s: int = 250            # 移动速度 (mm/s)
    term_type: str = "CNT"           # 终止类型 ("FINE"=精确停止, "CNT"=连续运动)
    term_value: int = 100            # 终止值 (FINE=0, CNT=0-100, 100=最大连续)
    
    # ==================== 输入约束 ====================
    x_min: float = -2000.0           # X轴最小值 (mm)
    x_max: float = 2000.0            # X轴最大值 (mm)
    y_min: float = -2000.0           # Y轴最小值 (mm)
    y_max: float = 2000.0            # Y轴最大值 (mm)
    z_min: float = -2000.0           # Z轴最小值 (mm)
    z_max: float = 2000.0            # Z轴最大值 (mm)
    
    speed_min: int = 1               # 速度最小值 (mm/s)
    speed_max: int = 300            # 速度最大值 (mm/s)
    
    cnt_min: int = 0                 # CNT最小值
    cnt_max: int = 100               # CNT最大值
    
    # 角度自动规范化到 [-180, +180]
    angle_normalize: bool = True


@dataclass
class UDPReceiverConfig:
    """UDP接收器配置"""
    
    host: str = "0.0.0.0"            # 监听地址
    port: int = 9000                 # 监听端口
    buffer_size: int = 4096          # 接收缓冲区大小
    timeout: float = 1.0             # 套接字超时时间


@dataclass
class PerformanceConfig:
    """性能和实时性配置"""
    
    target_fps: int = 30              # 目标帧率 (Hz)
    print_interval_s: float = 2.0     # 性能打印间隔 (秒)
    frame_history_size: int = 60      # 保存最近N帧以计算平均FPS


@dataclass
class TeleopConfig:
    """遥操总体配置"""
    
    robot: FanucRobotConfig = None
    udp: UDPReceiverConfig = None
    performance: PerformanceConfig = None
    
    def __post_init__(self):
        if self.robot is None:
            self.robot = FanucRobotConfig()
        if self.udp is None:
            self.udp = UDPReceiverConfig()
        if self.performance is None:
            self.performance = PerformanceConfig()
    
    @property
    def frame_interval(self) -> float:
        """根据目标FPS计算帧间隔（秒）"""
        return 1.0 / self.performance.target_fps


# 默认全局配置实例
DEFAULT_CONFIG = TeleopConfig()
