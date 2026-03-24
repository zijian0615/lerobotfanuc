# 多集数据存储系统 (Multi-Episode Data Store)

## 概述

新的数据存储系统改进了原有的单集 HDF5 方案，采用**一次运行、多集存储**的架构：

### 核心优势

| 特性 | 旧方案 (HDF5) | 新方案 (Parquet + MP4) |
|------|--------------|----------------------|
| 多集存储 | ❌ 单集一个文件 | ✅ 一次运行多集 |
| 存储效率 | ❌ 视频嵌入,空间浪费 | ✅ 独立存储,可复用MP4 |
| 数据读取 | ❌ 全加载 | ✅ 流式读取,支持sharding |
| 元数据 | ❌ 嵌入在HDF5 | ✅ 独立JSON/Parquet,规范化 |
| 统计信息 | ❌ 无 | ✅ 全局normalization stats |
| 任务管理 | ❌ 无 | ✅ 支持task descriptions |

---

## 目录结构

```
data_root/
├── meta/
│   ├── info.json                      # 规范化架构定义
│   ├── stats.json                     # 全局特征统计 (mean/std/min/max)
│   ├── tasks.jsonl                    # 任务描述 (可选)
│   └── episodes/                      # Episode 元数据 (chunked Parquet)
│       ├── episodes_0.parquet
│       ├── episodes_1.parquet
│       └── ...
│
├── data/                              # 帧级特征数据 (Parquet shards)
│   ├── data_shard_0.parquet
│   ├── data_shard_1.parquet
│   └── ...
│
└── videos/                            # 视频 (每摄像头一个MP4stream)
    ├── camera_0_shard_0.mp4
    ├── camera_0_shard_1.mp4
    └── ...
```

### 文件说明

#### `meta/info.json`
规范化的数据架构定义，包含：
- **schema**: 特征名称、数据类型、形状
- **fps**: 数据采样率
- **codebase_version**: 代码版本
- **created_at**: 创建时间戳

```json
{
  "schema": {
    "features": ["action_pose", "action_gripper", "t_action", ...],
    "action_pose_shape": [6],
    "action_pose_dtype": "float32"
  },
  "fps": 30.0,
  "codebase_version": "1.0.0",
  "created_at": "2026-03-24T10:30:00.123456"
}
```

#### `meta/stats.json`
全局特征统计，用于normalization：
- **mean**: 均值
- **std**: 标准差
- **min/max**: 最小/最大值
- **count**: 样本数

```json
{
  "action_pose": {
    "mean": [0.1, 0.2, ...],
    "std": [0.05, 0.06, ...],
    "min": [...],
    "max": [...],
    "count": 50000
  }
}
```

#### `meta/episodes/episodes_*.parquet`
每个 episode 的元数据：
| 字段 | 类型 | 说明 |
|------|------|------|
| `episode_id` | str | "episode_000001" |
| `task` | str | 任务描述 |
| `timestamp_start` | str | ISO 8601 时间戳 |
| `timestamp_end` | str | ISO 8601 时间戳 |
| `data_offset_start` | int | Parquet 中的行索引 |
| `data_offset_end` | int | Parquet 中的行索引 |
| `video_offset_start` | int | MP4 中的帧索引 |
| `video_offset_end` | int | MP4 中的帧索引 |
| `stats` | dict | Episode 统计 (可选) |

#### `data/data_shard_*.parquet`
帧级数据，**跨 episode sharding**：
| 列 | 类型 | 说明 |
|-----|-------|------|
| `t_action` | float64 | Action 时间戳 |
| `action_pose` | array[float32, 6] | 机器人目标姿态 |
| `action_gripper` | bool | 夹爪状态 |
| `t_state` | float64 | State 时间戳 |
| `state_pose` | array[float32, 6] | 机器人实际姿态 |
| `t_obs` | float64 | 观测时间戳 |
| `camera_id` | int | 摄像头ID |

#### `videos/camera_*.mp4`
编码的视频流，**跨 episode**：
- 支持多摄像头，每个摄像头一个独立的 MP4 文件
- 通过 `meta/episodes/` 中的偏移量可以定位每个 episode 的帧范围

---

## 使用说明

### 1. 训练数据采集

```python
from robot_record import FanucRecordController
from fanuc_config import TeleopConfig

config = TeleopConfig()
controller = FanucRecordController(config, data_root="./data")
controller.run()
```

**交互流程：**
```
🎬 FANUC Multi-Episode Record Mode
   Ctrl+C ×1 → end episode → continue
   Ctrl+C ×2 → end episode → exit
   Ctrl+C ×3 → discard episode → exit

Task description for episode_000001 (optional): task_name_1
[... recording ...]
^C
⚠️  Ctrl+C (×1)
   → 结束当前 episode...
✅ Episode 'episode_000001' ended (1234 steps)

Continue recording? (y/n): y

Task description for episode_000002 (optional): task_name_2
[... recording ...]
^C
⚠️  Ctrl+C (×1)
   → 结束当前 episode...
✅ Episode 'episode_000002' ended (5678 steps)

Continue recording? (y/n): n

✅ Recorded 2 episodes
📂 Data saved to: ./data
```

### 2. 数据验证

```bash
python inspect_data_store.py ./data
```

输出示例：
```
======================================================================
📂 Inspecting: ./data
======================================================================

📋 META FILES:
----------------------------------------------------------------------
✅ info.json:
   - Version: 1.0.0
   - FPS: 30
   - Created: 2026-03-24T10:30:00.123456
   - Features: action_pose, action_gripper, t_action, state_pose, t_state, t_obs, camera_id

✅ stats.json (7 features):
   - action_pose: mean=[0.123, 0.456, ...], std=[0.045, 0.067, ...]
   - ... and 6 more features

📊 PARQUET DATA SHARDS:
----------------------------------------------------------------------
✅ data_shard_0.parquet: 10000 rows, 7 columns
✅ data_shard_1.parquet: 6912 rows, 7 columns

📈 Total: 16912 data rows across 2 shards
Columns: t_action, action_pose, action_gripper, t_state, state_pose, t_obs, camera_id

🎬 EPISODE METADATA:
----------------------------------------------------------------------
✅ episodes_0.parquet: 2 episodes
   First episode: episode_000001
   - Task: task_name_1
   - Data offset: 0 - 1234
   - Started: 2026-03-24T10:30:00.123456

📊 Total: 2 episodes

🎥 VIDEO FILES:
----------------------------------------------------------------------
✅ camera_0_shard_0.mp4:
   - Size: 245.32 MB
   - Resolution: 640x480
   - Frames: 6912
   - FPS: 30.0
```

### 3. 读取数据

#### 读取所有数据

```python
import pandas as pd
import pyarrow.parquet as pq
import json
from pathlib import Path

data_root = Path("./data")

# 1. 加载 meta 信息
with open(data_root / "meta" / "info.json") as f:
    info = json.load(f)
print(f"Schema: {info['schema']['features']}")
print(f"FPS: {info['fps']}")

# 2. 加载 episode 元数据
episodes_df = pd.read_parquet(
    data_root / "meta" / "episodes" / "episodes_0.parquet"
)
print(f"\nEpisodes:")
print(episodes_df[["episode_id", "task", "data_offset_start", "data_offset_end"]])

# 3. 加载所有数据
data_shards = sorted(data_root.glob("data/*.parquet"))
tables = [pq.read_table(shard) for shard in data_shards]
merged_table = pq.concat_tables(tables)
df = merged_table.to_pandas()

print(f"\nTotal rows: {len(df)}")
print(f"Columns: {df.columns.tolist()}")
print(df.head())
```

#### 按 episode 读取

```python
import cv2

# 遍历每个 episode
for _, ep_row in episodes_df.iterrows():
    episode_id = ep_row["episode_id"]
    start_idx = ep_row["data_offset_start"]
    end_idx = ep_row["data_offset_end"]
    video_start = ep_row["video_offset_start"]
    video_end = ep_row["video_offset_end"]
    
    # 获取该 episode 的数据
    ep_data = df.iloc[start_idx:end_idx]
    
    # 读取对应的视频帧
    cap = cv2.VideoCapture(str(data_root / "videos" / "camera_0_shard_0.mp4"))
    frames = []
    for frame_idx in range(video_start, min(video_end, video_start + len(ep_data))):
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if ret:
            frames.append(frame)
    cap.release()
    
    print(f"\n{episode_id}:")
    print(f"  Task: {ep_row['task']}")
    print(f"  Steps: {len(ep_data)}")
    print(f"  Frames: {len(frames)}")
```

#### 计算 normalization 参数

```python
# 加载全局统计
with open(data_root / "meta" / "stats.json") as f:
    stats = json.load(f)

# 用于 normalization
for feature_name, feature_stats in stats.items():
    mean = feature_stats["mean"]
    std = feature_stats["std"]
    print(f"{feature_name}: mean={mean}, std={std}")

# 应用 normalization
df_normalized = df.copy()
for col in stats.keys():
    if col in df.columns:
        mean = stats[col]["mean"]
        std = stats[col]["std"]
        df_normalized[col] = (df[col] - mean) / (std + 1e-6)
```

---

## 存储效率对比

假设：
- 100 episodes
- 每 episode 750 steps (25 秒 @ 30fps)
- 每帧 640x480 RGB (约 900KB 压缩后)

| 指标 | HDF5 方案 | Parquet + MP4 方案 |
|------|---------|------------------|
| 存储大小 | ~67 GB | ~22 GB |
| 单数据读取 | ~67 GB | ~150 MB |
| 并行处理 | ❌ 困难 | ✅ 易于分片 |
| 版本控制 | ❌ 整个重新生成 | ✅ 增量添加 |

---

## 配置选项

### MultiEpisodeDataStore 初始化参数

```python
data_store = MultiEpisodeDataStore(
    data_root="./data",           # 数据根目录
    fps=30.0,                     # 采样频率
    codebase_version="1.0.0",     # 代码版本标记
    num_cameras=1,                # 摄像头数量
    camera_width=640,             # 视频宽度
    camera_height=480,            # 视频高度
)
```

### Parquet Shard 大小

```python
# ParquetDataWriter 中
shard_size = 10000  # 每 10000 行写入一个 shard
```

调整 `shard_size` 可以平衡：
- **更小**: 更好的并行性，更多文件
- **更大**: 更少文件，读取效率更高

---

## 故障排除

### 问题：没有 Parquet 或 MP4 文件

**原因**：未安装依赖库

```bash
pip install pyarrow pandas
pip install opencv-python  # For MP4 encoding
```

### 问题：视频文件大小异常

**原因**：MP4 编码器参数不匹配

检查：
```python
# 确保帧分辨率与编码器初始化参数一致
encoder = MP4VideoEncoder(..., width=640, height=480)
frame.shape  # 应为 (480, 640, 3)
```

### 问题：Parquet 读取慢

**解决**：使用列选择和过滤

```python
# 只读取需要的列
table = pq.read_table(
    "data_shard_0.parquet",
    columns=["action_pose", "t_action"]
)

# 使用 filters 进行预过滤（推送给 Parquet）
filters = [("camera_id", "==", 0)]
table = pq.read_table(
    "data_shard_0.parquet",
    filters=filters
)
```

---

## 进阶功能

### 多摄像头支持

```python
data_store = MultiEpisodeDataStore(num_cameras=2)

# 追加不同摄像头的数据
data_store.append({
    "camera_id": 0,
    "obs_frame": frame_from_cam0,
    # ... other fields
})

data_store.append({
    "camera_id": 1,
    "obs_frame": frame_from_cam1,
    # ... other fields
})
```

### 任务管理

```python
# 开始 episode 时指定任务
data_store.start_episode(
    episode_id="episode_000001",
    task_description="pick and place from bin A to bin B"
)

# 生成 tasks.jsonl (可选)
tasks = {
    "tasks": {
        "0": "pick and place",
        "1": "push object",
        "2": "stack blocks"
    }
}
```

### 自定义统计

```python
# 在 end_episode 时计算自定义统计
episode_stats = {
    "mean_speed": 0.45,
    "max_force": 12.3,
    "grasp_success_rate": 0.95
}

data_store.episode_meta.end_episode(
    episode_data=current_episode,
    data_offset_end=step_count,
    video_offset_end=frame_count,
    stats=episode_stats
)
```

---

## 参考实现

完整代码位置：
- [data_store.py](./data_store.py) - 核心实现
- [robot_record.py](./robot_record.py) - 集成示例
- [inspect_data_store.py](./inspect_data_store.py) - 验证工具

---

## License

见上层 LICENSE 文件。
