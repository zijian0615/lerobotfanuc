"""
Dual Camera Manager
===================
管理两个摄像头的同时采集。

特性：
    - 两个摄像头独立线程采集
    - 非阻塞 get_latest() 接口，供主循环快速取帧
    - 线程安全的环形缓冲
"""

import threading
import time
import logging
from collections import deque
from typing import Optional, Dict, Tuple
import cv2
import numpy as np


class DualCameraManager:
    """
    双摄像头管理器。
    
    Usage:
        manager = DualCameraManager(cam_ids=[0, 1], rate_hz=30.0)
        manager.start()
        
        # 在主循环中
        frames = manager.get_latest()  # {'camera_0': {...}, 'camera_1': {...}}
        
        manager.stop()
    """
    
    BUF_SIZE = 300  # 环形缓冲大小
    
    def __init__(self, 
                 cam_ids: list = None,
                 rate_hz: float = 30.0,
                 width: int = 640,
                 height: int = 480,
                 display: bool = False,
                 display_scale: float = 1.0):
        """
        初始化双摄像头管理器。
        
        Args:
            cam_ids: 摄像头 ID 列表，默认 [0, 1]
            rate_hz: 采集频率（Hz）
            width: 帧宽度
            height: 帧高度
            display: 是否启用实时显示
            display_scale: 显示缩放比例（0.0-1.0）
        """
        if cam_ids is None:
            cam_ids = [0, 1]
        
        self.cam_ids = cam_ids
        self.rate_hz = rate_hz
        self.width = width
        self.height = height
        self.display = display
        self.display_scale = display_scale
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # 为每个摄像头维护采集线程和环形缓冲
        self.buffers: Dict[int, deque] = {
            cam_id: deque(maxlen=self.BUF_SIZE) for cam_id in cam_ids
        }
        self.locks: Dict[int, threading.Lock] = {
            cam_id: threading.Lock() for cam_id in cam_ids
        }
        self.captures: Dict[int, cv2.VideoCapture] = {}
        self.threads: Dict[int, threading.Thread] = {}
        self.display_thread: Optional[threading.Thread] = None
        
        self.running = False
    
    def start(self) -> None:
        """启动所有摄像头采集线程。"""
        self.running = True
        
        # 启动每个摄像头的采集线程
        for cam_id in self.cam_ids:
            try:
                cap = cv2.VideoCapture(cam_id)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                
                if not cap.isOpened():
                    raise RuntimeError(f"Cannot open camera {cam_id}")
                
                self.captures[cam_id] = cap
                
                # 创建采集线程
                thread = threading.Thread(
                    target=self._capture_loop,
                    args=(cam_id,),
                    daemon=True,
                    name=f"CameraCapture-{cam_id}"
                )
                thread.start()
                self.threads[cam_id] = thread
                
                self.logger.info(f"✅ Camera {cam_id} started @ {self.rate_hz} Hz")
                
            except Exception as e:
                self.logger.error(f"❌ Failed to start camera {cam_id}: {e}")
                self.running = False
                return
    
    def _capture_loop(self, cam_id: int) -> None:
        """单个摄像头的采集循环。"""
        cap = self.captures[cam_id]
        interval = 1.0 / self.rate_hz
        
        while self.running:
            try:
                t0 = time.perf_counter()
                ret, frame = cap.read()
                t_cap = time.perf_counter()
                
                if ret and frame is not None:
                    with self.locks[cam_id]:
                        self.buffers[cam_id].append({
                            "t": t_cap,
                            "frame": frame,
                            "camera_id": cam_id
                        })
                
                elapsed = time.perf_counter() - t0
                time.sleep(max(0.0, interval - elapsed))
                
            except Exception as e:
                if self.running:
                    self.logger.error(f"Camera {cam_id} capture error: {e}")
                break
    
    def get_latest(self) -> Dict[str, Optional[dict]]:
        """
        非阻塞获取所有摄像头最新帧。
        
        Returns:
            dict: 格式为 {
                'camera_0': {'t': float, 'frame': ndarray, 'camera_id': int} or None,
                'camera_1': {'t': float, 'frame': ndarray, 'camera_id': int} or None,
                ...
            }
        """
        result = {}
        for cam_id in self.cam_ids:
            with self.locks[cam_id]:
                if self.buffers[cam_id]:
                    # 返回深拷贝，避免主循环修改缓冲
                    latest = self.buffers[cam_id][-1]
                    result[f'camera_{cam_id}'] = {
                        't': latest['t'],
                        'frame': latest['frame'].copy(),
                        'camera_id': cam_id
                    }
                else:
                    result[f'camera_{cam_id}'] = None
        
        return result
    
    def is_ready(self) -> bool:
        """检查是否所有摄像头都至少采集了一帧。"""
        for cam_id in self.cam_ids:
            with self.locks[cam_id]:
                if not self.buffers[cam_id]:
                    return False
        return True
    
    def stop(self) -> None:
        """停止所有采集和显示线程。"""
        self.running = False
        
        # 等待采集线程退出
        for cam_id, thread in self.threads.items():
            if thread.is_alive():
                thread.join(timeout=1.0)
            
            # 释放摄像头
            if cam_id in self.captures:
                self.captures[cam_id].release()
        
        # 等待显示线程退出
        if self.display_thread and self.display_thread.is_alive():
            self.display_thread.join(timeout=1.0)
        
        self.logger.info("✅ All cameras stopped")
