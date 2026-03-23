# StreamLinear 调试指南

## 常见错误码

### **2556957** - Invalid parameter or robot state
可能原因：
1. ❌ 机器人还在初始化状态，未准备好接收指令
2. ❌ UFRAME/UTOOL 设置不对
3. ❌ 位置数据超出工作空间

**解决方案：**
```python
# 尝试降低参数
UFRAME = 0        # 改为0（基坐标系）
UTOOL = 1         # 保持为1或改为0
SPEED_MM_S = 100  # 降低速度
CNT_VALUE = 0     # 改为0（精确停止）
```

### **2556952** - Configuration parameter error
可能原因：
1. ❌ UToolNumber 不存在
2. ❌ UFrameNumber 不存在
3. ❌ Configuration flags 不对

**解决方案：**
```python
# 检查机器人配置中实际存在的Tool/Frame编号
# 通常：
UTOOL = 0         # 试试改为0
UFRAME = 0        # 试试改为0
```

### **2556959** - Position data invalid/out of range
可能原因：
1. ❌ X, Y, Z 超出工作空间
2. ❌ W, P, R 姿态数据无效
3. ❌ 目标位置无法到达

**解决方案：**
- 检查 log 文件中的数据范围
- 添加位置约束（代码已自动处理）
- 检查机器人是否有障碍物

---

## 调试步骤

### 1. 检查数据格式
运行脚本后查看 `📤 Sending` 输出：
```
📤 Sending seq=1: target=(x, y, z, w, p, r), speed=200, cnt=0
```

### 2. 测试单个关键帧
修改 `load_and_filter()` 只加载前5帧：
```python
def load_and_filter(log_file):
    records = []
    # ... 现有逻辑 ...
    # 只加载前5帧用于测试
    print(f"Loaded {len(records)} frames after filtering")
    return records[:5]  # ← 添加这行
```

### 3. 检查机器人状态
在 Fanuc ROBOGUIDE 或示教器上：
- 确保机器人在 REMOTE 或允许的模式
- 检查紧急停止按钮未按下
- 确认 Frame 0 和 Tool 0 存在

### 4. 逐步调整参数
```python
# 第一步：完全静止不动（验证连接）
SPEED_MM_S = 1
CNT_VALUE = 0
# 视当前位置发送相同位置

# 第二步：小幅移动
# 修改 target 中的 X 增加 10mm

# 第三步：逐步增加速度
SPEED_MM_S = 50, 100, 200, ...
```

---

## 参数范围参考

| 参数 | 范围 | 建议 |
|------|------|------|
| X, Y, Z | -2000~2000 mm | 根据工作空间适配 |
| W, P, R | 0~360° | 自动约束到此范围 |
| Speed | 1~1000 mm/s | 调试时用50-100 |
| CNT | 0~100 | 0=精确，100=平滑 |
| UFRAME | 0~9 | 通常为0（基坐标系） |
| UTOOL | 0~9 | 通常为0或1 |

---

## 完整调试命令行

```bash
# 1. 只测试前5帧，加速调试
# 修改 load_and_filter() 返回 return records[:5]
python streamLinear.py

# 2. 查看详细输出
# 脚本会打印所有 ACK 错误和参数信息

# 3. 检查特定错误码含义
# 参考上方 error_desc 字典
```

---

## 如果步骤都试了还是出错

1. **验证网络连接**
   ```bash
   ping 172.30.109.22
   ```

2. **查看Fanuc日志**
   - 在示教器上查看错误日志
   - 记下错误信息和时间戳

3. **测试基础指令**
   ```python
   # 使用原始 send_and_ack() 测试单个指令
   # (保留在代码中未删除)
   ```

4. **联系Fanuc技术支持**
   - 提供错误码和参数配置
   - 提供机器人型号和RMI版本

---

## 预期输出（正常情况）

```
Loaded 228 frames after filtering
🔌 Connecting to Fanuc RMI...
✅ TCP socket optimized for low-latency
✅ Async sender initialized, ACK listener started
📤 Sending seq=1: target=(...), speed=200, cnt=0
[   1/ 228] seq=   1 dt= 20.0ms fps= 50.0Hz
...
✅ 达到目标帧率 50Hz!
```

