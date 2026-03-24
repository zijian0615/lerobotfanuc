"""
data_store.py
==============
多集存储系统：一次运行中保存多个 episode，采用 Parquet + MP4 架构。

目录结构：
    data_root/
    ├── meta/
    │   ├── info.json               # schema, fps, version, paths
    │   ├── stats.json              # global feature stats (mean/std/min/max)
    │   ├── tasks.jsonl             # task descriptions (for RL)
    │   └── episodes/               # episode metadata (chunked Parquet)
    │       ├── episodes_0.parquet
    │       ├── episodes_1.parquet
    │       └── ...
    ├── data/                       # frame-by-frame Parquet shards
    │   ├── data_shard_0.parquet
    │   └── ...
    └── videos/                     # MP4 video per camera
        ├── camera_0_shard_0.mp4
        └── ...

状态转移：
    episode.start(episode_id)  --> 标记开始点
    writer.append(record)      --> 多条
    episode.end(episode_id)    --> 标记结束点，记录 meta

数据流：
    record = {"obs_frame", "action_pose", "state_pose", "t_action", "t_state", "t_obs", ...}
                              ↓
                    append(record)
                              ↓
    frame queue → MP4 encoder → videos/camera_0_shard_*.mp4
    record rows → Parquet writer → data/data_shard_*.parquet
"""

import json
import os
import logging
import threading
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple
from collections import deque
from pathlib import Path
import queue

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    HAS_PARQUET = True
except ImportError:
    HAS_PARQUET = False


logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Video Encoder (MP4)
# ═══════════════════════════════════════════════════════════════════════════════
class MP4VideoEncoder:
    """
    异步 MP4 编码器：从 queue 中读取帧，编码为 MP4。
    支持多摄像头，每个摄像头独立编码线程。
    """
    def __init__(self, output_path: str, fps: float = 30.0, 
                 codec: str = "mp4v", width: int = 640, height: int = 480):
        self.output_path = output_path
        self.fps = fps
        self.codec = codec
        self.width = width
        self.height = height
        self.frame_queue: queue.Queue = queue.Queue(maxsize=300)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._writer = None
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

    def start(self) -> None:
        if not HAS_CV2:
            logger.error("❌ cv2 not installed, video encoding disabled")
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._encode_loop, daemon=True)
        self._thread.start()
        logger.info(f"✅ MP4 Encoder started: {self.output_path}")

    def _encode_loop(self) -> None:
        """后台编码线程"""
        fourcc = cv2.VideoWriter_fourcc(*self.codec)
        self._writer = cv2.VideoWriter(
            self.output_path, fourcc, self.fps, (self.width, self.height)
        )
        
        if not self._writer.isOpened():
            logger.error(f"❌ Failed to open VideoWriter: {self.output_path}")
            return
        
        frame_count = 0
        while self._running:
            try:
                frame = self.frame_queue.get(timeout=1.0)
                if frame is None:  # 哨兵值：停止编码
                    break
                
                # 确保帧大小正确
                if frame.shape[:2] != (self.height, self.width):
                    frame = cv2.resize(frame, (self.width, self.height))
                
                self._writer.write(frame)
                frame_count += 1
            except queue.Empty:
                if not self._running:
                    break
        
        if self._writer:
            self._writer.release()
        logger.info(f"✅ MP4 encoded {frame_count} frames: {self.output_path}")

    def append_frame(self, frame: np.ndarray, timeout: float = 0.1) -> bool:
        """追加帧到编码队列（非阻塞）"""
        if not self._running:
            return False
        try:
            self.frame_queue.put(frame, timeout=timeout)
            return True
        except queue.Full:
            logger.warning("⚠️  Video frame queue full, dropping frame")
            return False

    def stop(self) -> None:
        """停止编码器"""
        self._running = False
        try:
            self.frame_queue.put(None, timeout=1.0)  # 哨兵
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("Frame queue cleared")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Parquet Data Writer
# ═══════════════════════════════════════════════════════════════════════════════
class ParquetDataWriter:
    """
    分片 Parquet 写入器：按行写入，定期轮转文件。
    """
    def __init__(self, output_dir: str, shard_size: int = 10000, 
                 schema: Optional[pa.Schema] = None):
        self.output_dir = output_dir
        self.shard_size = shard_size
        self.schema = schema
        
        os.makedirs(output_dir, exist_ok=True)
        
        self._shard_idx = 0
        self._row_buffer: List[Dict] = []
        self._lock = threading.Lock()

    def append(self, record: Dict) -> None:
        """追加单条记录"""
        with self._lock:
            self._row_buffer.append(record)
            if len(self._row_buffer) >= self.shard_size:
                self._flush()

    def _flush(self) -> None:
        """将缓冲区写入 Parquet 文件（需在 lock 内调用）"""
        if not self._row_buffer:
            return
        
        if not HAS_PARQUET:
            logger.error("❌ pyarrow not installed")
            return
        
        df = pd.DataFrame(self._row_buffer)
        shard_path = os.path.join(
            self.output_dir, f"data_shard_{self._shard_idx}.parquet"
        )
        
        try:
            table = pa.Table.from_pandas(df, schema=self.schema)
            pq.write_table(table, shard_path, compression="snappy")
            logger.info(f"✅ Flushed {len(self._row_buffer)} rows → {shard_path}")
            self._shard_idx += 1
            self._row_buffer.clear()
        except Exception as e:
            logger.error(f"❌ Parquet write failed: {e}")

    def finalize(self) -> None:
        """最后落盘"""
        with self._lock:
            self._flush()


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Episode Metadata Manager
# ═══════════════════════════════════════════════════════════════════════════════
class EpisodeMetaManager:
    """
    管理每个 episode 的元数据：
    - 开始/结束时间戳
    - 数据偏移（在 Parquet/MP4 中的位置）
    - 任务描述
    - 统计信息
    """
    def __init__(self, output_dir: str, shard_size: int = 100):
        self.output_dir = os.path.join(output_dir, "meta", "episodes")
        self.shard_size = shard_size
        os.makedirs(self.output_dir, exist_ok=True)
        
        self._episodes: List[Dict] = []
        self._shard_idx = 0
        self._lock = threading.Lock()

    def start_episode(self, episode_id: str, task_description: str = "") -> Dict:
        """标记 episode 开始"""
        return {
            "episode_id": episode_id,
            "task": task_description,
            "timestamp_start": datetime.now().isoformat(),
            "data_offset_start": 0,  # 后续填充
            "video_offset_start": 0,
        }

    def end_episode(self, episode_data: Dict, 
                    data_offset_end: int, 
                    video_offset_end: int,
                    stats: Optional[Dict] = None) -> None:
        """标记 episode 结束并记录元数据"""
        with self._lock:
            episode_data["timestamp_end"] = datetime.now().isoformat()
            episode_data["data_offset_end"] = data_offset_end
            episode_data["video_offset_end"] = video_offset_end
            
            if stats:
                episode_data["stats"] = stats
            
            self._episodes.append(episode_data)
            
            # 定期轮转 parquet 文件
            if len(self._episodes) >= self.shard_size:
                self._flush()

    def _flush(self) -> None:
        """将元数据写入 Parquet（需在 lock 内调用）"""
        if not self._episodes or not HAS_PARQUET:
            return
        
        df = pd.DataFrame(self._episodes)
        shard_path = os.path.join(
            self.output_dir, f"episodes_{self._shard_idx}.parquet"
        )
        
        try:
            table = pa.Table.from_pandas(df)
            pq.write_table(table, shard_path, compression="snappy")
            logger.info(f"✅ Flushed {len(self._episodes)} episodes → {shard_path}")
            self._shard_idx += 1
            self._episodes.clear()
        except Exception as e:
            logger.error(f"❌ Episode meta write failed: {e}")

    def finalize(self) -> None:
        """最后落盘"""
        with self._lock:
            self._flush()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Global Stats Tracker
# ═══════════════════════════════════════════════════════════════════════════════
class StatsTracker:
    """
    跟踪全局特征统计（用于 normalization）。
    """
    def __init__(self):
        self._stats_dict: Dict[str, Dict] = {}
        self._lock = threading.Lock()

    def update(self, record: Dict) -> None:
        """增量更新统计"""
        with self._lock:
            for key, value in record.items():
                if isinstance(value, (int, float, np.number)):
                    if key not in self._stats_dict:
                        self._stats_dict[key] = {
                            "count": 0,
                            "sum": 0.0,
                            "sum_sq": 0.0,
                            "min": float("inf"),
                            "max": float("-inf"),
                        }
                    
                    stat = self._stats_dict[key]
                    stat["count"] += 1
                    stat["sum"] += float(value)
                    stat["sum_sq"] += float(value) ** 2
                    stat["min"] = min(stat["min"], float(value))
                    stat["max"] = max(stat["max"], float(value))

    def finalize(self) -> Dict[str, Dict]:
        """计算最终统计（mean/std）"""
        with self._lock:
            result = {}
            for key, stat in self._stats_dict.items():
                count = stat["count"]
                if count == 0:
                    continue
                
                mean = stat["sum"] / count
                variance = (stat["sum_sq"] / count) - (mean ** 2)
                std = np.sqrt(max(0.0, variance))
                
                result[key] = {
                    "mean": float(mean),
                    "std": float(std),
                    "min": float(stat["min"]),
                    "max": float(stat["max"]),
                    "count": count,
                }
            return result


# ═══════════════════════════════════════════════════════════════════════════════
# 5. 主数据存储系统
# ═══════════════════════════════════════════════════════════════════════════════
class MultiEpisodeDataStore:
    """
    完整的多集存储系统：
    - 支持一次运行中多个 episode
    - 自动生成 meta 文件（info.json, stats.json）
    - 支持多摄像头
    """

    def __init__(self, data_root: str = "./data", 
                 fps: float = 30.0,
                 codebase_version: str = "1.0.0",
                 num_cameras: int = 1,
                 camera_width: int = 640,
                 camera_height: int = 480):
        self.data_root = data_root
        self.fps = fps
        self.codebase_version = codebase_version
        self.num_cameras = num_cameras
        self.camera_width = camera_width
        self.camera_height = camera_height
        
        os.makedirs(data_root, exist_ok=True)
        
        # 初始化各组件
        self.data_writer = ParquetDataWriter(
            os.path.join(data_root, "data"),
            shard_size=10000
        )
        
        self.video_encoders: Dict[int, MP4VideoEncoder] = {}
        for cam_id in range(num_cameras):
            self.video_encoders[cam_id] = MP4VideoEncoder(
                os.path.join(data_root, "videos", f"camera_{cam_id}_shard_0.mp4"),
                fps=fps,
                width=camera_width,
                height=camera_height
            )
            self.video_encoders[cam_id].start()
        
        self.episode_meta = EpisodeMetaManager(data_root)
        self.stats_tracker = StatsTracker()
        
        self._episode_row_count = 0
        self._current_episode: Optional[Dict] = None
        self._logger = logging.getLogger(self.__class__.__name__)

    def start_episode(self, episode_id: str, task_description: str = "") -> None:
        """开始新 episode"""
        self._current_episode = self.episode_meta.start_episode(episode_id, task_description)
        self._episode_row_count = 0
        self._logger.info(f"🎬 Episode '{episode_id}' started (task: {task_description})")

    def append(self, record: Dict) -> None:
        """
        追加数据行。
        record 应包含：
            - action_pose, action_gripper, t_action
            - state_pose, t_state
            - obs_frame (per camera), t_obs
        """
        # 视频编码（如果存在）
        if "obs_frame" in record and record["obs_frame"] is not None:
            cam_id = record.get("camera_id", 0)
            if cam_id in self.video_encoders:
                frame = record["obs_frame"]
                if frame.dtype != np.uint8:
                    frame = (np.clip(frame, 0, 255)).astype(np.uint8)
                self.video_encoders[cam_id].append_frame(frame)
        
        # Parquet 数据行（去掉图片，保留标量）
        parquet_record = {
            k: v for k, v in record.items() 
            if k not in ["obs_frame"]
        }
        
        # 转换 np array 为列表
        for k, v in parquet_record.items():
            if isinstance(v, np.ndarray):
                parquet_record[k] = v.tolist()
        
        self.data_writer.append(parquet_record)
        self.stats_tracker.update(parquet_record)
        
        self._episode_row_count += 1

    def end_episode(self) -> None:
        """结束当前 episode"""
        if not self._current_episode:
            self._logger.warning("⚠️  No active episode to end")
            return
        
        episode_id = self._current_episode["episode_id"]
        
        # 记录偏移量（简化：row count）
        self._current_episode["data_offset_start"] = 0
        self._current_episode["data_offset_end"] = self._episode_row_count
        
        # 计算 episode 统计
        episode_stats = {}  # 可按需计算
        
        self.episode_meta.end_episode(
            self._current_episode,
            data_offset_end=self._episode_row_count,
            video_offset_end=0,  # 简化
            stats=episode_stats
        )
        
        self._logger.info(f"✅ Episode '{episode_id}' ended ({self._episode_row_count} steps)")
        self._current_episode = None
        self._episode_row_count = 0

    def finalize(self) -> None:
        """完成数据保存，生成 meta 文件"""
        self._logger.info("🔒 Finalizing data store...")
        
        # 关闭所有编码器
        for encoder in self.video_encoders.values():
            encoder.stop()
        
        # 落盘所有 Parquet
        self.data_writer.finalize()
        self.episode_meta.finalize()
        
        # 生成 meta/info.json
        info = {
            "schema": {
                "features": [
                    "action_pose", "action_gripper", "t_action",
                    "state_pose", "t_state",
                    "t_obs", "camera_id"
                ],
                "action_pose_shape": [6],
                "action_pose_dtype": "float32",
                "state_pose_shape": [6],
                "state_pose_dtype": "float32",
            },
            "fps": self.fps,
            "codebase_version": self.codebase_version,
            "data_dir": "data/",
            "videos_dir": "videos/",
            "created_at": datetime.now().isoformat(),
        }
        
        meta_dir = os.path.join(self.data_root, "meta")
        os.makedirs(meta_dir, exist_ok=True)
        
        with open(os.path.join(meta_dir, "info.json"), "w") as f:
            json.dump(info, f, indent=2)
        
        # 生成 meta/stats.json
        stats = self.stats_tracker.finalize()
        with open(os.path.join(meta_dir, "stats.json"), "w") as f:
            json.dump(stats, f, indent=2)
        
        self._logger.info(f"✅ Data store finalized: {self.data_root}")
        self._logger.info(f"   - Parquet: data/")
        self._logger.info(f"   - Videos: videos/")
        self._logger.info(f"   - Meta: meta/")
