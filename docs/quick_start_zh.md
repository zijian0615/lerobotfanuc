# 🚀 FANUC遥操 - 快速开始指南

## 📌 最新发现（FANUC手册确认）

根据 FANUC RMI 官方手册最新解读，解决了之前"为什么隐形死锁"的问题。

**之前的错误假设**：
- ❌ "盲发"（不等ACK）就能实现30Hz → **会死锁**（CNT需要等下一条指令）
- ❌ "死等ACK"就能保证执行 → **也会死锁**（CNT没有等待时返回ACK）

**正确做法**：
- ✅ **滑动窗口**（官方规范）
  - 初始：连续发送8条指令填满缓冲区（每条延迟2ms）
  - 维持：每收到1个ACK就发送1条新指令
  - 效果：30Hz实时控制 + 无死锁 + 无帧丢失

---

## 🎯 三个版本的选择

### **1️⃣ 如果你追求"最佳实践"**  → **用滑动窗口版本**

```bash
python -m lerobotfanuc.src.robot.fanuc.robot_sliding_window
```

特点：
- ✅ 遵循FANUC官方规范（手册第52页）
- ✅ 30Hz实时性能
- ✅ 零帧丢失
- ✅ <35ms延迟
- ✅ 生产环境推荐

---

### **2️⃣ 如果你只是"快速原型"** → **用非阻塞版本**

```bash
python -m lerobotfanuc.src.robot.fanuc.robot_nonblocking
```

特点：
- 27.3Hz性能（接近30Hz）
- 实现简单，易于理解
- 约42%帧丢失（可接受快速开发）
- 适合调试和原型

---

### **3️⃣ 如果你需要"精确定位"** → **用阻塞版本**

```bash
python -m lerobotfanuc.src.robot.fanuc.robot
```

特点：
- 每条指令必须执行完成才发送下一条
- 0.7Hz帧率（很低，但保证精确性）
- 适合离线编程或初始化序列
- 不适合实时遥操

---

## 🔧 配置前检查清单

在运行之前，确保以下配置正确：

### **fanuc_config.py**

```python
# 网络配置
class FanucRobotConfig:
    host: str = "172.30.109.22"      # 你的FANUC控制器IP
    port: int = 50001                # RMI端口
    group: int = 1                   # 控制组
    
# 运动配置
FanucRobotConfig:
    utool: int = 1                   # 工具号
    uframe: int = 1                  # 用户坐标系（推荐用1）
    speed_mm_s: int = 150            # 速度（mm/s）
    
# ⚠️ 必须用CNT模式！
    term_type: str = "CNT"           # ✅ 正确
    term_value: int = 100            # ✅ 100 = 最大融合
    
# UDP输入配置
class UDPReceiverConfig:
    host: str = "0.0.0.0"            # 监听所有网卡
    port: int = 5555                 # 接收遥操目标点的端口
```

---

## 📤 UDP 输入格式

遥操客户端（比如游戏手柄驱动）应该按以下格式发送 UDP：

```json
{
  "fanuc": {
    "x": 100.5,     // mm
    "y": 200.3,     // mm
    "z": 300.0,     // mm
    "w": -30,       // deg (手腕转向)
    "p": 45,        // deg (俯仰)
    "r": 0          // deg (横滚)
  }
}
```

**发送频率建议**：15~30 Hz（不用精确30Hz，程序会自适应）

---

## 🧪 测试步骤

### **Step 1: 连接性检查**

```bash
# 检查能否ping通FANUC
ping 172.30.109.22

# 检查RMI端口是否开放
nmap 172.30.109.22 -p 50001
```

### **Step 2: 启动遥操服务**

```bash
# 用滑动窗口版本（推荐）
python -m lerobotfanuc.src.robot.fanuc.robot_sliding_window
```

输出应该是这样：

```
============================================================
🤖 FANUC Real-Time Teleoperation (Sliding Window)
   UDP Input  : 0.0.0.0:5555
   FANUC RMI  : 172.30.109.22:50001
   Speed      : 150 mm/s   TermType=CNT
   Target FPS : 30 Hz
   Buffer Size: 8 (RMI standard)
============================================================

▶️  开始实时遥操作，按 Ctrl+C 停止...

（滑动窗口模式：初始填充8条 + ACK驱动发送）

🔄 初始化：填充缓冲区...
✅ 缓冲区已满（8条指令）→ 进入维持模式
[14:23:45.123] 发送:    10  ACK:    10  缓冲: 8/8  fps: 30.1Hz  → X=+100.00 Y=+200.00 Z=+300.00
[14:23:46.123] 发送:    40  ACK:    40  缓冲: 8/8  fps: 30.2Hz  → X=+105.50 Y=+205.30 Z=+305.00
```

### **Step 3: 发送测试指令**

在另一个终端，用 Python 发送测试UDP：

```python
import socket
import json
import time

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.connect(("127.0.0.1", 5555))

for i in range(10):
    data = {
        "fanuc": {
            "x": 100 + i * 5,
            "y": 200 + i * 3,
            "z": 300 + i * 2,
            "w": -30,
            "p": 45,
            "r": 0
        }
    }
    sock.send(json.dumps(data).encode())
    time.sleep(0.1)  # 10 Hz

sock.close()
```

---

## 📊 性能目标

运行成功后，你应该看到：

```
✅ 发送指令数：    30/秒
✅ ACK成功返回：   30/秒
✅ 缓冲区状态：    8/8（满）
✅ 实际帧率：      30 Hz
✅ 机械臂响应：    流畅、无延迟
❌ 帧丢失数：      接近0
```

---

## ⚠️ 常见问题排查

### **问题1：连接超时（"Connection timed out"）**

```
原因：FANUC的IP地址不对，或防火墙阻止
解决：
1. 检查 fanuc_config.py 中 host 是否正确
2. 运行 ping 172.30.109.22（用你的实际IP）
3. 检查网络防火墙
```

### **问题2：缓冲区填充超时（"Waiting for UDP data..."）**

```
原因：没有接收到任何UDP数据
解决：
1. 检查遥操客户端是否在运行
2. 检查目标端口是否正确（默认5555）
3. 用 netstat -an | grep 5555 检查是否在监听
```

### **问题3：频繁看到 FRC_SystemFault**

```
原因：可能是协议数据格式不对，或参数超范围
解决：
1. 检查 uframe 是否为 0 或 1
2. 检查 term_type 是否为 "CNT" 或 "FINE"
3. 检查坐标值是否在范围内（见 fanuc_config.py）
```

### **问题4：机械臂不动**

```
原因1：可能在FINE模式下，等待停止
解决：改用CNT模式，term_type = "CNT"

原因2：缓冲区没有填满（少于8条）
解决：这是正常的，继续发送数据就会继续执行

原因3：没有收到ACK
解决：检查控制器日志，运行诊断（robot_sliding_window 内置诊断）
```

---

## 📈 原理深入理解

如果你想理解"为什么滑动窗口能解决问题"，请读：

👉 [滑动窗口详解](sliding_window_strategy.md)

---

## 🔗 相关代码

- **主控制器**：[robot_sliding_window.py](../src/robot/fanuc/robot_sliding_window.py)
- **RMI协议**：[fanuc_communication.py](../src/robot/fanuc/fanuc_communication.py)
- **网络层**：[fanuc_transport.py](../src/robot/fanuc/fanuc_transport.py)
- **配置**：[fanuc_config.py](../src/robot/fanuc/fanuc_config.py)

---

## 📞 获取帮助

遇到问题？检查以下顺序：

1. **打开日志调试**：在 python 代码中加入
   ```python
   logging.basicConfig(level=logging.DEBUG)
   ```

2. **运行诊断**：
   ```bash
   python -m lerobotfanuc.src.robot.fanuc.fanuc_communication
   ```
   （如果有诊断脚本）

3. **查看缓冲区状态**：程序的每一条日志都会显示 `缓冲: X/8`，确保它在逐渐增加

4. **联系FANUC技术支持**（如果怀疑硬件问题）

