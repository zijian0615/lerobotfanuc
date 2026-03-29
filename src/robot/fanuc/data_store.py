"""
data_store.py
==============
多集存储系统：采用 lerobot 标准格式。

目录结构（参考 lerobot）：
    data_root/
    ├── meta/
    │   ├── info.json               # schema, fps, version, data_path, video_path
    │   ├── stats.json              # global feature stats (mean/std/min/max)
    │   ├── tasks.parquet           # task descriptions
    │   └── episodes.parquet        # episode metadata
    ├── data/
    │   └── chunk-{chunk_id:03d}/
    │       └── file-{file_id:03d}.parquet
    └── videos/
        ├── {video_key}/
        │   └── chunk-{chunk_id:03d}/
        │       └── file-{file_id:03d}.mp4
"""

import json
import os
import logging
import threading
import time
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple
from collections import deque
from pathlib import Path
import queue
import subprocess

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
# 1. Video Encoder (MP4) - FFmpeg based
# ═══════════════════════════════════════════════════════════════════════════════
class MP4VideoEncoder:
    """
    FFmpeg 基础的 MP4 编码器：从 queue 中读取帧，通过 ffmpeg 管道编码为 MP4。
    支持多摄像头，每个摄像头独立编码线程。
    使用 H.264 编码器和标准参数以获得最佳兼容性。
    """
    def __init__(self, output_path: str, fps: float = 30.0, 
                 codec: str = "libx264", width: int = 640, height: int = 480):
        self.output_path = output_path
        self.fps = fps
        self.codec = codec  # 现在使用 libx264 而不是 mp4v
        self.width = width
        self.height = height
        self.frame_queue: queue.Queue = queue.Queue(maxsize=300)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen] = None
        
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        # 跟踪视频位置
        self._chunk_idx = 0
        self._file_idx = 0
        self._frame_count = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        """启动编码器线程"""
        self._running = True
        self._thread = threading.Thread(target=self._encode_loop, daemon=True)
        self._thread.start()
        logger.info(f"✅ MP4 Encoder started (FFmpeg + H.264): {self.output_path}")

    def _encode_loop(self) -> None:
        """后台编码线程 - 使用 ffmpeg 管道"""
        try:
            # FFmpeg 命令行参数（标准 H.264 编码参数）
            ffmpeg_cmd = [
                'ffmpeg',
                '-y',  # 覆盖输出文件
                '-f', 'rawvideo',  # 输入格式：原始视频
                '-pix_fmt', 'bgr24',  # OpenCV 使用 BGR 格式
                '-s', f'{self.width}x{self.height}',  # 分辨率
                '-r', str(self.fps),  # 帧率
                '-i', 'pipe:0',  # 从 stdin 读取
                # 视频编码参数
                '-c:v', self.codec,  # 视频编码器（libx264）
                '-profile:v', 'high',  # H.264 配置文件
                '-level', '4.0',  # H.264 等级
                '-crf', '23',  # 质量（0-51，越小越好）
                '-pix_fmt', 'yuv420p',  # 输出像素格式
                # 音频参数
                '-c:a', 'aac',  # 音频编码器
                '-b:a', '128k',  # 音频比特率
                # 优化参数
                '-movflags', '+faststart',  # 优化 MP4 元数据位置方便流式播放
                '-preset', 'medium',  # 编码速度 (ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow)
                self.output_path
            ]
            
            self._process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            
            frame_idx = 0
            while self._running:
                try:
                    frame = self.frame_queue.get(timeout=1.0)
                    if frame is None:  # 哨兵值：停止编码
                        break
                    
                    # 确保帧大小正确
                    if frame.shape[:2] != (self.height, self.width):
                        frame = cv2.resize(frame, (self.width, self.height))
                    
                    # 确保格式正确（BGR）
                    if len(frame.shape) != 3 or frame.shape[2] != 3:
                        logger.warning(f"⚠️  Frame shape mismatch: {frame.shape}, expected ({self.height}, {self.width}, 3)")
                        continue
                    
                    # 写入原始帧数据到 ffmpeg stdin
                    self._process.stdin.write(frame.tobytes())
                    
                    with self._lock:
                        self._frame_count += 1
                    frame_idx += 1
                    
                except queue.Empty:
                    if not self._running:
                        break
                except Exception as e:
                    logger.error(f"❌ Error writing frame to ffmpeg: {e}")
                    break
            
            # 关闭 ffmpeg 进程
            if self._process and self._process.stdin:
                self._process.stdin.close()
            if self._process:
                self._process.wait(timeout=10)
            
            logger.info(f"✅ MP4 encoded {frame_idx} frames: {self.output_path}")
            
        except FileNotFoundError:
            logger.error("❌ ffmpeg not found. Please install ffmpeg: brew install ffmpeg")
        except Exception as e:
            logger.error(f"❌ FFmpeg encoding error: {e}")
    
    def get_frame_count(self) -> int:
        """获取当前编码的帧数"""
        with self._lock:
            return self._frame_count

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
            self._thread.join(timeout=10)
        logger.info("✅ Frame encoding completed")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Parquet Data Writer (Chunk-based)
# ═══════════════════════════════════════════════════════════════════════════════
class ParquetDataWriter:
    """
    基于 chunk 的 Parquet 写入器：按 frames 写入，定期轮转 chunk。
    跟踪chunk/file索引以支持数据指针。
    """
    def __init__(self, output_dir: str, chunk_size: int = 1000, 
                 schema: Optional[pa.Schema] = None):
        self.output_dir = output_dir
        self.chunk_size = chunk_size
        self.schema = schema
        
        os.makedirs(output_dir, exist_ok=True)
        
        self._chunk_idx = 0
        self._file_idx = 0
        self._row_buffer: List[Dict] = []
        self._lock = threading.Lock()
        self._total_written_rows = 0  # 总共写入的行数

    def append(self, record: Dict) -> None:
        """追加单条记录"""
        with self._lock:
            self._row_buffer.append(record)
            if len(self._row_buffer) >= self.chunk_size:
                self._flush()

    def _flush(self) -> Tuple[int, int, int]:  # chunk_idx, file_idx, row_count
        """将缓冲区写入 Parquet 文件（需在 lock 内调用），返回写入位置"""
        if not self._row_buffer:
            return self._chunk_idx, self._file_idx, 0
        
        if not HAS_PARQUET:
            logger.error("❌ pyarrow not installed")
            return self._chunk_idx, self._file_idx, 0
        
        df = pd.DataFrame(self._row_buffer)
        row_count = len(self._row_buffer)
        
        # 创建 chunk 目录
        chunk_dir = os.path.join(self.output_dir, f"chunk-{self._chunk_idx:03d}")
        os.makedirs(chunk_dir, exist_ok=True)
        
        shard_path = os.path.join(chunk_dir, f"file-{self._file_idx:03d}.parquet")
        
        try:
            table = pa.Table.from_pandas(df, schema=self.schema)
            pq.write_table(table, shard_path, compression="snappy")
            logger.info(f"✅ Flushed {row_count} rows → chunk_{self._chunk_idx:03d}/file_{self._file_idx:03d}")
            
            # 保存当前位置
            current_chunk = self._chunk_idx
            current_file = self._file_idx
            self._total_written_rows += row_count
            
            self._file_idx += 1
            self._row_buffer.clear()
            
            return current_chunk, current_file, row_count
        except Exception as e:
            logger.error(f"❌ Parquet write failed: {e}")
            return self._chunk_idx, self._file_idx, 0
    
    def get_current_position(self) -> Tuple[int, int, int]:
        """获取当前写入位置（chunk, file, offset）"""
        with self._lock:
            return self._chunk_idx, self._file_idx, len(self._row_buffer)
    
    def get_total_written_rows(self) -> int:
        """获取总共写入的行数"""
        with self._lock:
            return self._total_written_rows + len(self._row_buffer)

    def finalize(self) -> Tuple[int, int, int]:
        """最后落盘，返回最后写入位置"""
        with self._lock:
            return self._flush()


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Global Stats Tracker
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
# 4. 主数据存储系统（lerobot 格式）
# ═══════════════════════════════════════════════════════════════════════════════
class MultiEpisodeDataStore:
    """
    完整的多集存储系统（lerobot 格式）：
    - 支持一次运行中多个 episode
    - 自动生成完整 meta 文件（info.json, stats.json, tasks.parquet, episodes.parquet）
    - 支持多摄像头（按 video_key 分类）
    - 分 chunk 存储数据
    """

    def __init__(self, data_root: str = "./data", 
                 fps: float = 30.0,
                 codebase_version: str = "v3.0",
                 num_cameras: int = 1,
                 camera_width: int = 640,
                 camera_height: int = 480,
                 camera_names: Optional[List[str]] = None,
                 chunk_size: int = 1000):
        self.data_root = data_root
        self.fps = fps
        self.codebase_version = codebase_version
        self.num_cameras = num_cameras
        self.camera_width = camera_width
        self.camera_height = camera_height
        # 修复：camera_names 应该只是 camera_0, camera_1，不包含前缀
        if camera_names is None:
            camera_names = [f"camera_{i}" for i in range(num_cameras)]
        self.camera_names = camera_names
        self.chunk_size = chunk_size
        
        # 初始化各组件
        self.data_writer = ParquetDataWriter(
            os.path.join(data_root, "data"),
            chunk_size=chunk_size
        )
        
        # 为每个摄像头创建视频编码器
        # 注意：视频目录名必须与 info.json 中的特性键匹配（observation.images.camera_N）
        self.video_encoders: Dict[str, MP4VideoEncoder] = {}
        for cam_idx, cam_name in enumerate(self.camera_names):
            # video_key 与 info.json 中的特性键一致
            video_key = f"observation.images.{cam_name}"
            video_dir = os.path.join(data_root, "videos", video_key)
            # 初始化第一个 chunk/file
            os.makedirs(os.path.join(video_dir, "chunk-000"), exist_ok=True)
            
            self.video_encoders[cam_name] = MP4VideoEncoder(
                os.path.join(video_dir, "chunk-000", "file-000.mp4"),
                fps=fps,
                width=camera_width,
                height=camera_height
            )
            self.video_encoders[cam_name].start()
        
        self.stats_tracker = StatsTracker()
        
        # 元数据跟踪
        self._episodes: List[Dict] = []
        self._tasks: set = set()
        self._episode_count = 0
        self._frame_count = 0
        self._current_episode: Optional[Dict] = None
        self._episode_start_frame = 0
        self._episode_start_data_idx = 0  # episode 开始时的数据索引
        self._task_index_counter = 0
        self._episode_stats: Dict[str, Any] = {}  # 每个episode的统计信息
        self._logger = logging.getLogger(self.__class__.__name__)

    def start_episode(self, episode_id: str, task_description: str = "") -> None:
        """开始新 episode"""
        self._episode_start_data_idx = self.data_writer.get_total_written_rows()
        
        self._current_episode = {
            "episode_index": self._episode_count,
            "episode_id": episode_id,
            "task": task_description,
            "task_index": self._task_index_counter,
            "timestamp_start": datetime.now().isoformat(),
            "frame_start": self._frame_count,
        }
        self._episode_start_frame = self._frame_count
        self._episodes.append(self._current_episode)
        
        # 记录任务（只有非空任务才添加）
        if task_description and task_description.strip():
            self._tasks.add(task_description.strip())
        
        # 重置episode统计
        self._episode_stats = {}
        
        self._episode_count += 1
        self._logger.info(f"🎬 Episode {self._episode_count} started (task: {task_description})")

    def append(self, record: Dict) -> None:
        """
        追加数据行（lerobot 格式）。
        record 应包含：
            - action_pose, action_gripper, t_action
            - state_pose, t_state
            - obs_frame (per camera), t_obs, camera_id
        """
        # 提取摄像头帧
        frame = record.get("obs_frame")
        camera_id = record.get("camera_id", 0)
        
        # 构建 lerobot 格式的记录
        lerobot_record = {}
        
        # 1. action 字段
        if "action_pose" in record:
            action_pose = record["action_pose"]
            if isinstance(action_pose, np.ndarray):
                action_pose = action_pose.tolist()
            lerobot_record["action"] = action_pose
        
        # 2. observation.state 字段
        if "state_pose" in record:
            state_pose = record["state_pose"]
            if isinstance(state_pose, np.ndarray):
                state_pose = state_pose.tolist()
            lerobot_record["observation.state"] = state_pose
        
        # 3. observation.images.{camera_name} 字段（此处为视频引用）
        # 在 lerobot 格式中，图像是通过 video_path 指向的，需要记录帧索引
        cam_name = self.camera_names[camera_id] if camera_id < len(self.camera_names) else f"camera_{camera_id}"
        if frame is not None:
            # 特性键应该是 observation.images.camera_N 格式
            feature_key = f"observation.images.{cam_name}"
            lerobot_record[feature_key] = self._frame_count
        
        # 4. 时间戳（相对于当前 episode 的起点）
        # 使用帧索引计算相对时间：frame_index_in_episode / fps
        if self._current_episode:
            frame_in_episode = self._frame_count - self._episode_start_frame
            lerobot_record["timestamp"] = frame_in_episode / self.fps
        else:
            lerobot_record["timestamp"] = 0.0
        
        # 5. 索引字段
        lerobot_record["frame_index"] = self._frame_count
        if self._current_episode:
            lerobot_record["episode_index"] = self._current_episode["episode_index"]
        lerobot_record["index"] = self._frame_count
        lerobot_record["task_index"] = self._task_index_counter
        
        # 追加到 Parquet
        self.data_writer.append(lerobot_record)
        self.stats_tracker.update(lerobot_record)
        
        # 编码视频帧
        if frame is not None and camera_id < len(self.camera_names):
            cam_name = self.camera_names[camera_id]
            frame_uint8 = frame
            if frame.dtype != np.uint8:
                frame_uint8 = (np.clip(frame, 0, 255)).astype(np.uint8)
            
            if cam_name in self.video_encoders:
                self.video_encoders[cam_name].append_frame(frame_uint8)
        
        self._frame_count += 1

    def end_episode(self) -> None:
        """结束当前 episode"""
        if not self._current_episode:
            self._logger.warning("⚠️  No active episode to end")
            return
        
        episode_id = self._current_episode["episode_id"]
        
        # 记录数据指针
        data_end_idx = self.data_writer.get_total_written_rows()
        chunk_idx, file_idx, row_offset = self.data_writer.get_current_position()
        
        self._current_episode["timestamp_end"] = datetime.now().isoformat()
        self._current_episode["frame_end"] = self._frame_count
        self._current_episode["frame_count"] = self._frame_count - self._episode_start_frame
        
        # 添加数据指针
        self._current_episode["data/chunk_index"] = 0  # 默认为chunk-000
        self._current_episode["data/file_index"] = 0  # 默认为file-000
        self._current_episode["dataset_from_index"] = self._episode_start_data_idx
        self._current_episode["dataset_to_index"] = data_end_idx
        
        # 添加视频指针（每个摄像头）
        # 注意：videoKey 必须与 info.json 中的特性键匹配（observation.images.camera_N）
        frame_in_episode_start = self._episode_start_frame
        frame_in_episode_end = self._frame_count
        ts_start = frame_in_episode_start / self.fps
        ts_end = frame_in_episode_end / self.fps
        
        for cam_name in self.camera_names:
            video_key = f"observation.images.{cam_name}"
            self._current_episode[f"videos/{video_key}/chunk_index"] = 0
            self._current_episode[f"videos/{video_key}/file_index"] = 0
            self._current_episode[f"videos/{video_key}/from_timestamp"] = float(ts_start)
            self._current_episode[f"videos/{video_key}/to_timestamp"] = float(ts_end)
        
        # 添加任务信息
        self._current_episode["length"] = self._current_episode["frame_count"]
        
        self._logger.info(f"✅ Episode '{episode_id}' ended ({self._current_episode['frame_count']} frames)")
        self._current_episode = None
        self._task_index_counter += 1

    def finalize(self) -> None:
        """完成数据保存，生成 meta 文件"""
        self._logger.info("🔒 Finalizing data store...")
        
        # 关闭所有编码器
        for encoder in self.video_encoders.values():
            encoder.stop()
        
        # 落盘所有 Parquet
        self.data_writer.finalize()
        
        # 生成 meta/ 目录
        meta_dir = os.path.join(self.data_root, "meta")
        os.makedirs(meta_dir, exist_ok=True)
        
        # 生成 meta/info.json（lerobot 格式）
        info = self._build_info_json()
        with open(os.path.join(meta_dir, "info.json"), "w") as f:
            json.dump(info, f, indent=2)
        
        # 生成 meta/stats.json
        stats = self.stats_tracker.finalize()
        with open(os.path.join(meta_dir, "stats.json"), "w") as f:
            json.dump(stats, f, indent=2)
        
        # 生成 meta/tasks.parquet
        self._write_tasks_parquet(meta_dir)
        
        # 生成 meta/episodes.parquet
        self._write_episodes_parquet(meta_dir)
        
        self._logger.info(f"✅ Data store finalized: {self.data_root}")
        self._logger.info(f"   - Data: data/chunk-*/file-*.parquet")
        self._logger.info(f"   - Videos: videos/{{camera}}/chunk-*/file-*.mp4")
        self._logger.info(f"   - Meta: meta/")

    def _build_info_json(self) -> Dict[str, Any]:
        """构建 info.json（lerobot 格式）"""
        video_keys = self.camera_names
        
        features = {
            "action": {
                "dtype": "float32",
                "shape": [6],
                "names": ["j0", "j1", "j2", "j3", "j4", "gripper"],
                "fps": self.fps
            },
            "observation.state": {
                "dtype": "float32",
                "shape": [6],
                "names": ["j0", "j1", "j2", "j3", "j4", "gripper"],
                "fps": self.fps
            },
        }
        
        # 为每个摄像头添加视频特性（特性键应该是 observation.images.{camera_name}）
        for cam_name in video_keys:
            feature_key = f"observation.images.{cam_name}"
            features[feature_key] = {
                "dtype": "video",
                "shape": [self.camera_height, self.camera_width, 3],
                "names": ["height", "width", "channels"],
                "info": {
                    "video.height": self.camera_height,
                    "video.width": self.camera_width,
                    "video.codec": "mp4v",
                    "video.pix_fmt": "yuv420p",
                    "video.is_depth_map": False,
                    "video.fps": self.fps,
                    "video.channels": 3,
                    "has_audio": False
                }
            }
        
        # 添加索引字段
        for idx_field in ["timestamp", "frame_index", "episode_index", "index", "task_index"]:
            features[idx_field] = {
                "dtype": "float32" if idx_field == "timestamp" else "int64",
                "shape": [1],
                "names": None,
                "fps": self.fps
            }
        
        info = {
            "codebase_version": self.codebase_version,
            "robot_type": "fanuc_m710",
            "total_episodes": self._episode_count,
            "total_frames": self._frame_count,
            "total_tasks": len(self._tasks) if self._tasks else 0,
            "chunks_size": self.chunk_size,
            "fps": self.fps,
            "splits": {
                "train": f"0:{self._episode_count}"
            },
            "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
            "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
            "features": features,
        }
        
        return info

    def _write_tasks_parquet(self, meta_dir: str) -> None:
        """生成 meta/tasks.parquet"""
        if not HAS_PARQUET or not self._tasks:
            return
        
        tasks_list = [{
            "task_index": idx,
            "task": task
        } for idx, task in enumerate(sorted(self._tasks))]
        
        df = pd.DataFrame(tasks_list)
        tasks_path = os.path.join(meta_dir, "tasks.parquet")
        
        try:
            table = pa.Table.from_pandas(df)
            pq.write_table(table, tasks_path, compression="snappy")
            self._logger.info(f"✅ Written tasks: {tasks_path}")
        except Exception as e:
            self._logger.error(f"❌ Failed to write tasks: {e}")

    def _write_episodes_parquet(self, meta_dir: str) -> None:
        """生成 meta/episodes/chunk-000/file-000.parquet（包含完整的指针和统计信息）"""
        if not HAS_PARQUET or not self._episodes:
            return
        
        # 计算全局统计
        global_stats = self.stats_tracker.finalize()
        
        # 为每个episode添加统计信息
        for ep_idx, ep_data in enumerate(self._episodes):
            # 添加统计字段（对所有特征）
            for feature_key, stats in global_stats.items():
                for stat_type in ['min', 'max', 'mean', 'std', 'count']:
                    ep_data[f"stats/{feature_key}/{stat_type}"] = stats.get(stat_type, 0)
            
            # 添加meta指针
            ep_data["meta/episodes/chunk_index"] = 0
            ep_data["meta/episodes/file_index"] = 0
        
        df = pd.DataFrame(self._episodes)
        # 创建 meta/episodes/chunk-000/ 目录
        episodes_chunk_dir = os.path.join(meta_dir, "episodes", "chunk-000")
        os.makedirs(episodes_chunk_dir, exist_ok=True)
        episodes_path = os.path.join(episodes_chunk_dir, "file-000.parquet")
        
        try:
            # 将DataFrame转换为Parquet，保留所有列
            table = pa.Table.from_pandas(df, preserve_index=False)
            pq.write_table(table, episodes_path, compression="snappy")
            self._logger.info(f"✅ Written episodes with complete metadata: {episodes_path}")
            self._logger.info(f"   Total episodes: {len(self._episodes)}")
            self._logger.info(f"   Columns: {len(df.columns)}")
        except Exception as e:
            self._logger.error(f"❌ Failed to write episodes: {e}")
