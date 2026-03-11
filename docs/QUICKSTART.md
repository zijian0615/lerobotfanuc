# FANUC 遥操实现快速参考

## 🎯 核心概念

**🔴 问题**：指令堆积在机械臂导致控制不稳定
**🟢 解决方案**：异步发送 + 同步等待 = 有序执行

## 📚 文件对应关系

```
lerobotfanuc/src/robot/fanuc/
├── fanuc_config.py          ← 配置参数（TeleopConfig 等）
├── fanuc_transport.py       ← 网络层（UDP/TCP 传输）
├── fanuc_communication.py   ← 协议层（FANUC RMI 实现）
└── robot.py                 ← 业务层（主控制循环）
                               ↑ 这里实现 wait_until_executed 同步等待
```

## 🔑 关键 API

### FRCAsyncSender（协议库）

```python
from fanuc_communication import FRCAsyncSender

sender = FRCAsyncSender()

# 连接到 FANUC
sender.connect(host="172.30.109.22", port=16001, group=1)

# 方式 1: 异步发送（立即返回）
success = sender.send_async(
    target=(x, y, z, w, p, r),
    speed=200,
    cnt=100
)

# 方式 2: 等待指令完成（⭐ 关键的同步机制）
seq_id = sender.seq_id - 1  # 最后发送的指令 ID
success, err_id = sender.wait_until_executed(seq_id, timeout_s=2.0)

if success:
    if err_id == 0:
        print("✅ 指令成功执行")
    elif err_id == 2556956:
        print("⏳ 还在执行")
else:
    print(f"❌ 执行失败或超时")
```

### FanucTeleopController（业务库）

```python
from robot import FanucTeleopController
from fanuc_config import TeleopConfig

config = TeleopConfig()  # 使用默认配置
controller = FanucTeleopController(config)
controller.run()  # 启动主循环（已内置 wait_until_executed）
```

## 🔄 控制流程（主循环）

```
┌─────────────────────────────────────────┐
│ 初始化 FANUC 连接                        │
└────────────┬────────────────────────────┘
             │
             ▼
    ┌────────────────────┐
    │ last_seq_id = None │
    └────────┬───────────┘
             │
             ▼
    ┌─────────────────────────────────────┐
    │ 主循环 while True:                   │
    │                                      │
    │ 1️⃣  如果有上一条指令（last_seq_id）│
    │     wait_until_executed()            │
    │     └─ 阻塞直到完成                  │
    │                                      │
    │ 2️⃣  获取最新 UDP 目标位置            │
    │                                      │
    │ 3️⃣  异步发送到 FANUC                │
    │     send_async()                     │
    │     └─ 立即返回                      │
    │                                      │
    │ 4️⃣  记录本次 seq_id                 │
    │     last_seq_id = seq_id - 1         │
    │                                      │
    │ 5️⃣  帧率控制 sleep()                │
    └──────────┬──────────────────────────┘
             │
             ├─ UDP 更新？YES：重复
             ▼
    └─ 没有数据，等待...
```

## 🐛 常见问题排查

### 问题 1: ACK 错误码 2556956 (Robot still executing)

**原因**：上一条指令还在执行，却发送了新指令
**解决**：✅ 已修复！wait_until_executed() 会自动重试

```python
# wait_until_executed() 实现（伪代码）
while True:
    if ack_err == 0:
        return True, 0  # ✅ 成功
    elif ack_err == 2556956:
        continue  # ⏳ 继续等待
    else:
        return False, err_id  # ❌ 其他错误
```

### 问题 2: 帧率低于 30Hz

**预期行为**（❌ 不是问题！）：
```
实际 FPS = 1 / 机械臂执行时间
         = 1 / 0.1s ~ 0.5s
         ≈ 2 ~ 10 Hz
```

这是正常的。FANUC 工业机械臂的执行时间本来就长，远大于 Feetech 电机。

**如何优化**：
1. 增加机械臂速度：改 `FanucRobotConfig.speed_mm_s`
2. 缩短移动距离：给定更小的 Δx, Δy, Δz
3. 使用 `cnt_value=100`：连续运动，减少停顿

### 问题 3: ACK 超时 (timeout)

**原因**：
- 网络中断
- 机械臂离线
- TCP 连接丢失

**调试**：
```
查看日志中的：
[时间] ⏳ Seq XXX timeout
```

**恢复**：
```python
# robot.py 中的处理
if not success:
    if err_id is None:
        ack_timeout_count += 1  # 计数
        # 继续下一轮（不发送新指令，只等待）
```

### 问题 4: 指令超出工作范围 (error code 2556959)

**原因**：给定的 (x, y, z) 位置超出 FANUC 可达范围

**检查**：
- FANUC 的工作范围（机器人的规格书）
- 坐标系是否正确（World vs Tool frame）

**修复**：
```python
# 在 UDP 接收端做范围检查
x = max(-2000, min(2000, float(x)))  # mm 范围
y = max(-2000, min(2000, float(y)))
z = max(-2000, min(2000, float(z)))
```

## 📊 性能优化建议

### 1. 提交速度（mm/s）

| speed_mm_s | 执行时间 | 实际 FPS | 用途 |
|------------|---------|---------|------|
| 50 mm/s | ~400ms | 2.5 Hz | 精密操作，安全 |
| 200 mm/s | ~100ms | 10 Hz | 平衡性能 ⭐ |
| 500 mm/s | ~40ms | 25 Hz | 高速，风险 |

### 2. 连续运动参数 (cnt)

```python
cnt=100   # 连续运动，机械臂不停顿
cnt=0     # 点到点，每个指令机械臂完全停止
```

**建议**：使用 `cnt=100` 获得流畅运动

### 3. 网络优化

fanuc_transport.py 中已有：
```python
socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # 禁用 Nagle
socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)  # 大接收缓冲
socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)  # 大发送缓冲
```

无需调整。

## 📈 监控和日志

### 本地查看性能统计

```python
controller = FanucTeleopController(config)
controller.run()

# 实时输出：
# [12:34:56.789] 发送:  1234  ACK✅:  1234  ACK❌:    0  Timeout:    0
#              UDP总:  5678  fps:   8.5Hz
```

### 解读关键指标

```
发送 = 实际发送的指令数
ACK✅ = 成功执行的指令数（应等于"发送"）
ACK❌ = 执行失败的指令数（应为 0）
Timeout = 超时的指令数（应为 0）
UDP总 = 接收的 UDP 帧总数（会大于"发送"，因为有些帧没有发送新指令）
fps = 有效的指令执行率（不是 30Hz，而是 1/执行时间）
```

### 理想运行状态

```
✅ 发送 = ACK✅       （每条都成功）
✅ ACK❌ = 0          （没有错误）
✅ Timeout = 0       （没有超时）
✅ fps = 10 Hz左右   （FANUC 的正常速度）
```

## 🔌 与 LeRobot 的关键区别

| LeRobot (Feetech) | FANUC RMI | 原因 |
|------------------|-----------|------|
| `send_action()` 同步 | `send_async()` + `wait_until_executed()` | FANUC 是异步协议需要显式等待 |
| 30 Hz FPS | 10 Hz 左右实际 FPS | 机械臂执行时间长 |
| 无需等待ACK | 必须等待ACK | FANUC RMI 是异步需要确认 |

```python
# LeRobot
while True:
    obs = robot.get_observation()
    action = process(obs)
    robot.send_action(action)  # ← 同步返回
    sleep(1/fps)

# FANUC（改进后）
last_seq_id = None
while True:
    if last_seq_id:
        sender.wait_until_executed(last_seq_id)  # ← 异步等待
    
    target = get_latest_target()
    sender.send_async(target)  # ← 异步发送
    last_seq_id = sender.seq_id - 1
    
    sleep(remaining_time)
```

## 🎓 学习资源

1. **协议文档**：[FANUC RMI 规范](docs/command_ordering_design.md)
2. **可视化教程**：[打开 HTML 动画](docs/command_ordering_visualization.html)
3. **源代码注释**：
   - fanuc_communication.py - wait_until_executed() 详细注释
   - robot.py - FanucTeleopController.run() 主循环注释

## 🚀 快速开始

```bash
# 1. 启动 FANUC 遥操
python -m lerobotfanuc.src.robot.fanuc.robot

# 2. 在另一个终端发送 UDP 命令
# （实现你的 UDP 发送器）

# 3. 观察日志输出
# [时间] 发送: XXX  ACK✅: XXX  ACK❌: 0  Timeout: 0
```

## 📞 调试建议

```python
# 启用详细日志
import logging
logging.basicConfig(level=logging.DEBUG)

# 手动测试 wait_until_executed
from fanuc_communication import FRCAsyncSender
sender = FRCAsyncSender()
sender.connect("172.30.109.22", 16001, 1)

# 发送一条指令
sender.send_async((0, 0, 0, 0, 0, 0))
seq_id = sender.seq_id - 1

# 等待完成
success, err_id = sender.wait_until_executed(seq_id)
print(f"结果: {success}, 错误码: {err_id}")
```

## ✅ 验证清单

- [ ] FANUC 连接成功（无阿 FRC_Connect 错误）
- [ ] UDP Receiver 监听成功（无套接字错误）
- [ ] 能发送和接收 ACK（日志显示 ACK✅ > 0）
- [ ] 没有 ACK 错误（ACK❌ = 0）
- [ ] 没有超时（Timeout = 0）
- [ ] 机械臂平稳运动（无突然停顿）
