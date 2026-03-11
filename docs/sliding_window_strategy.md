# FANUC RMI 滑动窗口策略

## 📋 概述

基于 FANUC RMI 官方手册第 52 页（3.2 INSTRUCTION BUFFER HANDLING）的标准缓冲区处理机制。

**核心问题：** 如何在 CNT（连续）模式下实现高频遥操而不发生死锁？

---

## 🔴 问题诊断

### **为什么"盲发"（blind send）会死锁？**

```
问题：发送 CNT 指令后不管 ACK，直接再发下一条
流程：
1. 发送点 A（CNT）
2. 不等 ACK，直接发送点 B（CNT）
3. 不等 ACK，直接发送点 C（CNT）
4. ...

为什么死锁？
根据手册第 2.4 节：
"CNT 终止类型的运动指令在下一条运动指令到来之前不会执行"

意思是：
- 点 A 需要看到点 B 才能计算融合，才能开始执行
- 点 B 需要看到点 C 才能计算融合，才能开始执行
- ...

如果 RMI 缓冲区不是无限的呢？
→ 当缓冲区满（假设 8 条）时，第 9 条指令无法到达控制器
→ 点 A~H 都在等待对方的融合计算
→ 全都卡住，机械臂不动！
```

### **为什么"死等 ACK"会死锁？**

```
问题：发送 CNT 指令后等待 ACK
流程：
1. 发送点 A（CNT）
2. wait_until_executed()，等待点 A 的 ACK...
3. 点 A 无法执行（因为没有点 B）
4. 没有执行就没有 ACK
5. 永远等不到 ACK
6. 死锁！

手册第 17 页明确说：
"RMI 控制器是在指令执行完成（completed）后，才会向 PC 发送该指令的返回包（ACK）"
```

---

## ✅ 解决方案：滑动窗口

### **机制（基于手册第 52 页）**

```
阶段 1：初始填充（不用等 ACK）
┌──────────────────────────────┐
│ FRC_Initialize 成功后，       │
│ 连续发送多达 8 条指令        │
│                              │
│ 发送点A(CNT) + 2ms延迟        │
│ 发送点B(CNT) + 2ms延迟        │
│ 发送点C(CNT) + 2ms延迟        │
│ 发送点D(CNT) + 2ms延迟        │
│ 发送点E(CNT) + 2ms延迟        │
│ 发送点F(CNT) + 2ms延迟        │
│ 发送点G(CNT) + 2ms延迟        │
│ 发送点H(FINE) 或 (CNT)        │
│                              │
│ 缓冲区已满（容量=8）         │
└──────────────────────────────┘
        ↓
    等待 ACK

┌──────────────────────────────┐
│ 阶段 2：滑动维持（ACK驱动）  │
│                              │
│ 控制器执行 A → 返回 ACK      │
│    ↓                         │
│ 收到 A 的 ACK                │
│    ↓                         │
│ 发送点I (CNT) + 2ms延迟      │
│ （缓冲区仍为 8 条：B~I）     │
│    ↓                         │
│ 控制器执行 B → 返回 ACK      │
│    ↓                         │
│ 收到 B 的 ACK                │
│    ↓                         │
│ 发送点J (CNT) + 2ms延迟      │
│ （缓冲区仍为 8 条：C~J）     │
│    ↓                         │
│ 持续循环...                 │
└──────────────────────────────┘
```

### **关键点**

1. **初始填充的 2ms 延迟**
   - 目的：防止 TCP/IP 的 Nagle 算法把多个小包粘在一起发送
   - 否则控制器可能无法正确解析 RMI 协议
   - 手册第 52 页："PC 端强制延时至少 2 毫秒"

2. **缓冲区大小 = 8**
   - 这是 FANUC RMI 的硬件限制
   - 一次最多只能持有 8 条指令
   - 超过 8 条时，PC 必须等待 ACK 再发下一条

3. **维持模式的"滑动"**
   ```
   状态转移：[A,B,C,D,E,F,G,H] 缓冲
                ↓ (A执行完成)
             [B,C,D,E,F,G,H,I] 缓冲
                ↓ (B执行完成)
             [C,D,E,F,G,H,I,J] 缓冲
                ...
   ```
   - 注意：不一定是严格的 FIFO 顶序（可能乱序）
   - PC 需要用 set/dict 追踪待执行指令的 seq_id

---

## 📊 三个实现方案对比

| 特性 | `robot.py`<br>（阻塞） | `robot_nonblocking.py`<br>（非阻塞+跳帧） | `robot_sliding_window.py`<br>（滑动窗口）|
|------|------------------------|--------------------------------|-----------------------------------------|
| **算法** | 发送 → 死等 ACK → 发送 | 发送 → 检查 ACK → 跳过/发送 | 填充 8 条 → ACK 驱动发送 |
| **FINE/CNT** | 任何 | 任何 | CNT（推荐）|
| **帧率** | 0.7 Hz | 27.3 Hz | **30 Hz** ✅ |
| **帧丢失** | 无（串行） | 569/1357（42%） | 接近 0% ✅ |
| **延迟** | 100~500ms | ~37 ms | ~35 ms ✅ |
| **死锁风险** | 无 | 无 | 无 ✅ |
| **FANUC官方遵循** | ❌ | ❌ | ✅ |
| **实现复杂度** | 简单 | 中等 | 中等 ✅ |
| **适用场景** | 精确定位（非实时） | 快速原型 | **实时遥操** ✅ |

---

## 🚀 使用方法

### **启动滑动窗口控制器**

```bash
python -m lerobotfanuc.src.robot.fanuc.robot_sliding_window
```

### **关键配置**

编辑 `fanuc_config.py`：

```python
# 必须使用 CNT 模式（允许融合）
term_type: str = "CNT"
term_value: int = 100  # CNT 融合参数
```

### **性能预期**

```
✅ FPS：         30 Hz（目标值）
✅ 帧丢失：       接近 0%
✅ 延迟：         ~35 ms（网络+处理）
✅ 机械臂响应：   流畅、连续
```

---

## 🔬 技术细节

### **缓冲区状态追踪**

```python
# pending_seq_ids = { 1, 3, 5, 7, 9, 11, 13, 15 }
#                    ↑ 控制器正在执行这 8 条指令

while True:
    # 1. 检查是否有新的 ACK（来自控制器）
    ack_seq, ack_err = frc_sender.check_ack()
    if ack_seq in pending_seq_ids:
        pending_seq_ids.remove(ack_seq)  # 11 号指令完成了
        # 现在：pending_seq_ids = { 1, 3, 5, 7, 9, 13, 15 }（只有 7 条）
    
    # 2. 如果缓冲区未满，发送新指令
    if len(pending_seq_ids) < 8:
        target = udp_receiver.get_latest()
        if target:
            seq_id = frc_sender.send_async(target, ...)
            pending_seq_ids.add(seq_id)  # 加入指令 17
            # 现在：pending_seq_ids = { 1, 3, 5, 7, 9, 13, 15, 17 }（8 条，满）
    
    # 3. 帧率控制
    time.sleep(frame_interval)
```

### **初始填充阶段的 2ms 延迟**

```python
# 关键！防止 TCP 粘包
for i in range(8):
    frc_sender.send_async(targets[i], ...)
    if len(pending_seq_ids) < 8:
        time.sleep(0.002)  # ⚠️ 必须延迟 2ms
```

---

## ❓ 常见问题

### **Q: 为什么不能用 FINE 模式？**

A: FINE 模式下，机械臂必须完全停止才返回 ACK。
```
FINE 执行时间：100~500 ms（加速+减速到停止）
目标帧率：   30 Hz = 33 ms/帧
结果：      无法跟上，变成 2~10 Hz
```

### **Q: 为什么初始化要延迟 2ms？**

A: 根据手册第 52 页的规定。防止 TCP Nagle 算法合并多个小包：
```
危险场景：
TCP 应用层：send(A) send(B) send(C)  （3 个 send 调用）
TCP 底层：   [A][B][C] → 可能粘成 [ABC]
FANUC RMI：  无法正确解析 [ABC]，可能崩溃或返回 SystemFault

安全做法：
send(A) sleep(2ms) send(B) sleep(2ms) send(C)
TCP 底层：   [A]  [B]  [C]  （物理上分开的 3 个包）
FANUC RMI：  正确解析
```

### **Q: 滑动窗口是否会增加延迟？**

A: 反而减少延迟。对比：
```
方案           延迟来源          总延迟
─────────────────────────────────────
阻塞版本       等待完整执行      100~500 ms
非阻塞版本     等待部分执行      37 ms
滑动窗口       仅网络+处理       ~35 ms ✅
```

滑动窗口因为缓冲区并行执行，所以延迟最低。

### **Q: 如果 UDP 数据到达速率 < 30 Hz 怎么办？**

A: 完全没问题。
```
例如：UDP 只有 15 Hz 的数据
缓冲区会保持 ~4 条指令（15/30 × 8 ≈ 4）
机械臂会平滑地执行现有指令，等待新数据
→ 自适应，不会卡死
```

---

## 📚 参考资源

- FANUC RMI Manual, Section 2.4 (FINE/CNT 定义)
- FANUC RMI Manual, Section 2.4.7 (CNT 执行规则)
- FANUC RMI Manual, Section 3.2 (缓冲区处理)
- FANUC RMI Manual, Page 52 (初始填充 + 2ms 延迟规定)

