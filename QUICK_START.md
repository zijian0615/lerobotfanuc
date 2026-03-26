# 🚀 快速开始指南

## 5 分钟快速上手

### 1️⃣ 测试摄像头连接（可选但推荐）

在项目目录下运行：

```bash
cd lerobotfanuc/src/camera
python test_dual_camera.py
```

**预期输出：**
```
============================================================
🎥 Dual Camera Test
============================================================
摄像头 ID: [0, 1]
采集频率: 30 Hz
测试时长: 30 秒
显示: 启用
============================================================

✅ DualCameraManager 初始化成功
✅ 所有摄像头已启动

⏳ 等待摄像头初始化...
✅ 所有摄像头已就绪！

📊 开始采集测试（30秒）...
```

**成功标志：**
- 看到一个窗口显示两个摄像头画面（并排）
- 框中会显示 "Camera 0" 和 "Camera 1"
- 帧计数器持续增加

### 2️⃣ 启动录制

#### 方法 A：直接运行（快速）

编辑 `src/robot/fanuc/robot_record.py` 中的 `main()` 函数，确保使用新的参数：

```python
def main():
    from .fanuc_config import TeleopConfig

    config = TeleopConfig()
    try:
        # 新代码：使用双摄像头
        controller = FanucRecordController(
            config=config,
            cam_ids=[0, 1],      # 修改这里以改变摄像头 ID
            cam_rate_hz=30.0,
            data_root="./data"
        )
        controller.run()
    except KeyboardInterrupt:
        logger.info("Program interrupted")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
```

然后运行：
```bash
cd lerobotfanuc
python -m src.robot.fanuc.robot_record
```

#### 方法 B：使用示例脚本

```bash
cd lerobotfanuc/src/robot/fanuc
python example_dual_camera_record.py
```

### 3️⃣ 开始录制

启动脚本后，你会看到：

```
============================================================
🎬 FANUC Multi-Episode Record Mode
   (state → obs → action)
   Ctrl+C ×1 → end episode → continue
   Ctrl+C ×2 → end episode → exit
   Ctrl+C ×3 → discard episode → exit
============================================================
```

**输入 task 描述**（按 Enter 跳过）：
```
Task description for episode_000001 (optional): pick and place
```

**你会看到：**
1. ✅ 两个摄像头同时开始采集
2. 📺 桌面上显示两路摄像头的实时画面
3. 💾 数据开始保存到 `./data/` 目录

### 4️⃣ 停止录制

**结束当前 episode，继续下一个**：
```
Ctrl+C  →  ⚠️ Ctrl+C (×1)
        →  结束当前 episode...
        →  🔒 安全复位：断开所有气阀...
        →  Continue recording? (y/n):  [输入 y]
```

**结束所有录制**：
```
Ctrl+C  →  ⚠️ Ctrl+C (×1)
        →  结束当前 episode...
        →  Continue recording? (y/n):  [输入 n]
        →  👋 Record 退出
```

### 5️⃣ 查看录制数据

```bash
ls -lh data/episode_*/
```

你会看到如下结构：
```
data/
├── episode_000001/
│   ├── action_gripper.h5
│   ├── action_pose.h5
│   ├── state_pose.h5
│   └── obs_frames.h5  (包含两个摄像头的所有帧)
├── episode_000002/
└── ...
```

---

## ⚙️ 常见配置

### 修改摄像头 ID

如果你的摄像头不是 0 和 1，修改初始化代码：

```python
controller = FanucRecordController(
    config=config,
    cam_ids=[1, 2],   # ← 改成你的摄像头 ID
    cam_rate_hz=30.0,
    data_root="./data"
)
```

### 修改采集频率

```python
controller = FanucRecordController(
    config=config,
    cam_ids=[0, 1],
    cam_rate_hz=20.0,  # ← 改成 20Hz（推荐降低可减少 CPU 占用）
    data_root="./data"
)
```

### 改变数据保存位置

```python
controller = FanucRecordController(
    config=config,
    cam_ids=[0, 1],
    cam_rate_hz=30.0,
    data_root="/tmp/fanuc_data"  # ← 改成你的路径
)
```

### 禁用实时显示（无显示器环境）

在 `robot_record.py` 的 `FanucRecordController.__init__` 中修改：

```python
self.camera_manager = DualCameraManager(
    cam_ids=cam_ids,
    rate_hz=cam_rate_hz,
    display=False,  # ← 改成 False
    display_scale=0.5
)
```

---

## 🔧 高级配置

### 调整显示窗口大小

编辑 `dual_camera_manager.py`：

```python
manager = DualCameraManager(
    cam_ids=[0, 1],
    rate_hz=30.0,
    width=640,           # ← 修改分辨率
    height=480,
    display=True,
    display_scale=0.3    # ← 改小会更快（0.3 = 30% 缩放）
)
```

### 调整缓冲区大小

编辑 `dual_camera_manager.py` 中的 `BUF_SIZE`：

```python
class DualCameraManager:
    BUF_SIZE = 300  # ← 改成 500 为更多缓冲、更多内存占用
```

### 使用单个摄像头调试

如果遇到问题，用单个摄像头测试：

```python
controller = FanucRecordController(
    config=config,
    cam_ids=[0],        # ← 只用摄像头 0
    cam_rate_hz=30.0,
    data_root="./data"
)
```

---

## 📊 性能指标

| 配置 | CPU | 内存 | 延迟 |
|------|-----|------|------|
| 2×摄像头 30Hz 640×480 | ~5-10% | ~200MB | ~30ms |
| 2×摄像头 20Hz 640×480 | ~3-5% | ~150MB | ~50ms |
| 显示禁用 | -30% | -50MB | N/A |

**优化建议：**
- 如果 CPU 占用过高，降低 `cam_rate_hz` 到 20Hz
- 如果显示卡顿，降低 `display_scale` 到 0.3
- 如果内存不足，禁用 `display=False`

---

## 🐛 常见问题

### Q1: 摄像头打不开怎么办？

**A:**
```bash
# Linux: 列出所有摄像头
ls /dev/video*

# macOS: 与 Finder 中检查权限
# 设置 → 安全与隐私 → 摄像头

# Windows: 设备管理器 → 成像设备
```

尝试用不同的摄像头 ID：
```python
cam_ids=[0]    # 先用一个
cam_ids=[1]
cam_ids=[0, 2]  # 可能有其他设备占用 ID 1
```

### Q2: 实时显示很卡怎么办？

**A:** 减小显示尺寸：
```python
manager = DualCameraManager(
    cam_ids=[0, 1],
    rate_hz=30.0,
    display_scale=0.3   # 降低到 30%
)
```

或禁用显示，后期查看数据：
```python
display=False
```

### Q3: 数据采集不完整怎么办？

**A:** 
- 等待 `is_ready()` 返回 True
- 确保磁盘空间充足
- 检查写入权限：`chmod 755 ./data/`

### Q4: 如何修改摄像头分辨率？

**A:**
```python
manager = DualCameraManager(
    cam_ids=[0, 1],
    rate_hz=30.0,
    width=320,       # 改成更小
    height=240,
    display=True
)
```

更小的分辨率 = 更快的采集和显示

### Q5: 如何查看录制的数据？

**A:**
```bash
# 列出所有 episode
ls data/

# 查看某个 episode 的详情
h5ls data/episode_000001/obs_frames.h5

# Python 中读取
import h5py
with h5py.File("data/episode_000001/obs_frames.h5", "r") as f:
    frames = f["obs_frames"][:]  # 获取所有帧
    camera_ids = f["camera_id"][:]  # 摄像头 ID
    timestamps = f["obs_t"][:]  # 时间戳
    print(f"总帧数: {len(frames)}")
    print(f"分辨率: {frames.shape[1:]}") # (H, W, 3)
```

---

## 📚 更多资源

| 文件 | 内容 |
|------|------|
| [README.md](README.md) | 详细架构和 API 文档 |
| [IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md) | 实现细节 |
| [dual_camera_manager.py](dual_camera_manager.py) | 源代码（有详细注释） |
| [test_dual_camera.py](test_dual_camera.py) | 测试脚本和诊断工具 |
| [example_dual_camera_record.py](../robot/fanuc/example_dual_camera_record.py) | 完整的使用示例 |

---

## ✅ 检查清单

使用前确保：

- [ ] 两个摄像头已正确连接
- [ ] `test_dual_camera.py` 能成功运行
- [ ] 能看到实时摄像头画面
- [ ] 磁盘空间 > 1GB（用于录制）
- [ ] 摄像头权限正确（如需 sudo）
- [ ] FANUC 机器人已连接
- [ ] UDP 遥控器已准备好

---

## 🎉 开始使用！

```bash
# 1. 进入项目目录
cd lerobotfanuc

# 2. 测试摄像头（可选）
cd src/camera
python test_dual_camera.py --duration 10

# 3. 启动录制
cd ../robot/fanuc
python example_dual_camera_record.py

# 4. 开始操作机器人！
```

**祝你录制顺利！** 🤖🎬

---

**最后更新**：2025-03-25  
**版本**：1.0
