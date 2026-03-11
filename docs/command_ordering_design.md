# FANUC 指令有序执行设计

## 问题陈述

**核心问题**：确保发送给机械臂的指令不会在寄存器中堆积，而是被有序地执行一条接一条。

### 为什么这很重要？

- **控制稳定性**：如果指令堆积，机械臂会用延迟的历史轨迹执行，导致遥操不稳定
- **实时性**：堆积导致位置反馈滞后，无法进行实时控制
- **安全**：可能执行旧指令导致碰撞或危险动作

## 对比分析

### LeRobot (Feetech 电机) 的做法

```python
# lerobot_record.py 主循环
while True:
    obs = robot.get_observation()              # 读取当前状态（含位置反馈）
    action = process(obs)                      # 处理遥操输入
    robot.send_action(action)                  # 发送动作
        └─> bus.sync_write()                   # 非阻塞发送到电机
    
    sleep(1/fps - elapsed_time)                # ← 关键！帧率控制
```

**它如何避免堆积**：
1. `sync_write()` 是非阻塞的，立即返回
2. 但整个循环被限制在**目标 FPS (33ms)** 内
3. Feetech 电机通常在 10-20ms 内完成一条指令
4. 所以下一条指令发送时，上一条已经执行完了

**前提条件**：电机执行速度 ≤ FPS 周期

---

## FANUC RMI 的挑战

FANUC 工业机械臂不同于 Feetech 电机：
- **执行时间**：可能需要 50-500ms 执行一条运动指令
- **异步协议**：RMI 协议是异步的
  - 发送 `FRC_LinearMotion` 立即返回
  - 机械臂异步执行，执行完通过 ACK 报告
  - ACK 中 `ErrorID` 字段指示状态：
    - `0` = 执行成功
    - `2556956` = "Robot still executing" （还在执行）
    - 其他值 = 执行出错

### 原始错误的实现方式

```python
# 错误做法
while True:
    target = udp_receiver.get_latest()
    if target:
        sender.send_async(target)              # 立即发送
        ack_seq, ack_err = sender.check_ack()  # ← 非阻塞检查
        # ❌ 问题：没有等待！立即进入下一轮循环
```

**后果**：
- 第一条指令发送，机械臂开始执行（需要 100ms）
- 但 33ms 后，代码已经发送第二条指令
- 第二条指令在第一条执行完前就到达了
- 继续堆积...最后机械臂会报错 `2556956: Robot still executing`

---

## ✅ 正确的实现

### 核心机制：同步等待 + 有序执行

```python
# FRCAsyncSender.wait_until_executed() 新增方法
def wait_until_executed(self, seq_id, timeout_s=5.0):
    """
    ✅ 阻塞等待指定序列号的命令执行完成
    
    返回 (success, error_id)：
    - (True, 0): 执行成功 ✅
    - (False, err): 执行失败 ❌
    - (False, None): 超时 ⏳
    """
    deadline = time.perf_counter() + timeout_s
    while True:
        ack_seq, ack_err = self.check_ack()  # 非阻塞检查
        
        if ack_seq == seq_id:
            if ack_err == 0:
                return True, 0                   # ✅ 执行成功，可以发下一条
            elif ack_err == 2556956:
                continue                         # ⏳ 还在执行，继续等
            else:
                return False, ack_err           # ❌ 执行出错
        
        if time.perf_counter() > deadline:
            return False, None                  # ⏳ 超时
        
        time.sleep(0.01)  # 避免忙轮询
```

### 改进后的主循环

```python
# robot.py FanucTeleopController.run()
last_seq_id = None  # 跟踪上一条指令

while True:
    t_start = time.perf_counter()
    
    # ✅ 关键：等待上一条指令完成再继续
    if last_seq_id is not None:
        success, err_id = self.frc_sender.wait_until_executed(
            last_seq_id, 
            timeout_s=2.0
        )
        if not success:
            self.logger.warning(f"Seq {last_seq_id} failed/timeout")
    
    # 获取新目标
    target = self.udp_receiver.get_latest()
    
    if target:
        # 发送（异步，立即返回）
        ok = self.frc_sender.send_async(target)
        
        if ok:
            last_seq_id = self.frc_sender.seq_id - 1  # 记录本次 seq_id
    
    # 帧率控制
    sleep(remaining_time)
```

## 执行流程对比

### ❌ 无序执行（原始实现）

```
t=0ms   发送Cmd1 (需要100ms执行)
        ↓
t=33ms  发送Cmd2 (还没等Cmd1完成！) ← 堆积开始
        ↓         Cmd1 还在执行...
t=66ms  发送Cmd3 ← 继续堆积
        ↓         Cmd1 执行中...
t=100ms Cmd1 执行完，收到ACK
        但 Cmd2, Cmd3 已经堆积了，机械臂现在
        要按顺序执行 Cmd2→Cmd3，已经滞后了
```

### ✅ 有序执行（改进实现）

```
t=0ms   发送Cmd1 (需要100ms执行)
        进入 wait_until_executed(Cmd1)
        
t=0-100ms  阻塞等待中...
           ...Cmd1 执行...
           
t=100ms 收到 ACK(ErrorID=0) ✅
        wait_until_executed() 返回
        ↓
        发送Cmd2 (新目标距离遥操输入最近)
        进入 wait_until_executed(Cmd2)
        
t=100-200ms 阻塞等待中...
            ...Cmd2 执行...
            
t=200ms 收到 ACK(ErrorID=0) ✅
        wait_until_executed() 返回
        ↓
        发送Cmd3 (最新目标)
        
结果：每条指令执行完后立即发送下一条
      延迟：(100ms 机械臂 + 33ms UDP更新) ≈ 133ms
      但指令是有序的，控制是稳定的
```

## 性能特征

### 延迟分析

| 操作 | 耗时 |
|------|------|
| UDP 接收 | ~5-10ms |
| JSON 处理 | ~2-5ms |
| TCP 发送 | ~1-2ms |
| **机械臂执行** | **50-500ms** ← 主要延迟 |
| **总延迟** | **~100-600ms** |

### 帧率限制

原始设计目标：30 Hz (33ms 周期)

**实际受限**：
```
Effective FPS = 1 / (wait_time + UDP_poll_time)
              = 1 / (mecanum_exec_time + overhead)
```

如果机械臂平均执行 100ms：
```
Actual FPS = 1 / 0.1s ≈ 10 Hz
```

### 性能统计监控

```
[12:34:56.789] 发送:  1234  ACK✅:  1234  ACK❌:    0  Timeout:    0
             UDP总:  5678  fps:   8.5Hz  → X=+123.45 Y=+456.78 Z=+789.01
```

- **发送 > ACK✅**: 有等待中的指令
- **ACK❌ > 0**: 机械臂报错
- **Timeout > 0**: 与机械臂通信超时
- **fps**: 有效的指令执行率

## 配置调优

### fanuc_config.py 中的相关参数

```python
@dataclass
class FanucRobotConfig:
    speed_mm_s: int = 200           # 运动速度 mm/s
                                     # 更快 → 执行时间更短 → FPS 更高
    
    cnt_value: int = 100            # 连续运动参数 (0-100)
                                     # 100 = 连续不停，减少停顿
                                     # 0 = 每条指令完全停止

@dataclass  
class PerformanceConfig:
    target_fps: int = 30            # 目标 FPS (参考值，实际受机械臂限制)
    print_interval_s: float = 2.0   # 统计打印间隔
```

## 错误恢复策略

### 2556956: Robot Still Executing

**原因**：上一条指令还在执行时收到新指令
**当前处理**：自动重试（wait_until_executed 中的 continue）
**监控**：会在日志中警告

### 超时

**原因**：机械臂没有回复 ACK（通信故障）
**当前处理**：中止该指令，继续下一条
**建议**：检查网络连接，确认机械臂状态

### 其他错误 (2556952/2556957/2556959)

**原因**：配置错误、参数越界、机械臂错误
**当前处理**：记录错误，继续循环
**建议**：检查日志，验证给定位置是否在工作范围内

## 与 LeRobot 的区别总结

| 方面 | LeRobot (Feetech) | FANUC RMI |
|------|------------------|-----------|
| 发送方式 | 同步写入 (sync_write) | 异步发送 (send_async) |
| 执行确认 | 隐式（硬件立即执行） | 显式（ACK 回复） |
| 指令堆积防止 | 帧率控制（FPS） | **显式等待（wait_until_executed）** |
| 执行时间 | 10-20ms | 50-500ms |
| 实际 FPS | ~30 Hz | ~5-10 Hz |
| 代码复杂度 | 简单 | **需要同步控制** |

## 测试建议

1. **验证有序性**：
   ```
   发送 Z+100, Z+100, Z+100（三条相同指令）
   验证日志中显示每条指令都有 ACK✅
   ```

2. **验证延迟**：
   ```
   记录发送时间戳和 ACK 接收时间
   Δt = 机械臂执行时间 + 通信延迟
   ```

3. **压力测试**：
   ```
   快速变化目标位置（UDP 30Hz 快速更新）
   验证 Timeout 和 ACK❌ 计数保持为 0
   ```

## 参考资源

- FANUC RMI 协议：[specification]
- TCP 粘包处理：fanuc_transport.py LineSocket
- ACK 异步监听：fanuc_communication.py _listen_acks()
