"""
ACK 延迟诊断工具
================
独立运行，不依赖 UDP 数据源。
直接向 FANUC 发送固定位置指令，精确测量每条指令从发出到 ACK 回来的时间。

用法：
    python diagnose_ack_latency.py

输出示例：
    [#1] sent → ack  latency = 23.4 ms
    [#2] sent → ack  latency = 18.7 ms
    ...
    ── 统计 (N=20) ──────────────────────────
    平均   : 21.3 ms
    中位数 : 20.9 ms
    最小   : 15.2 ms
    最大   : 48.6 ms
    > 50ms : 0 次
    > 100ms: 0 次
    > 200ms: 0 次
"""

import sys
import time
import statistics
import logging

# ── 把项目根目录加到 path（按实际情况修改）──────────────────
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lerobotfanuc.src.robot.fanuc.fanuc_communication import FRCAsyncSender
from lerobotfanuc.src.robot.fanuc.fanuc_config import FanucRobotConfig

logging.basicConfig(level=logging.WARNING)   # 关掉 INFO 避免干扰计时输出

# ── 配置（改成你自己的机器人 IP 和一个安全的静止位置）────────
cfg = FanucRobotConfig()
SAFE_POSE = (cfg.host, cfg.port, cfg.group)  # 连接参数

# 发送到这个固定位置（确保机器人当前在附近，不会碰撞）
FIXED_X, FIXED_Y, FIXED_Z = 471.0, -182.0, -32.0
FIXED_W, FIXED_P, FIXED_R =   149.0, 0.0,   103.0
POSE = (FIXED_X, FIXED_Y, FIXED_Z, FIXED_W, FIXED_P, FIXED_R)

N_PROBES      = 20      # 发送多少条指令
PROBE_DELAY_S = 0.05    # 每条指令之间的间隔（s），给机器人缓冲时间
ACK_POLL_INTERVAL = 0.0005   # ACK 轮询间隔（s）
ACK_TIMEOUT_S     = 5.0      # 单条指令最长等待时间（s）


def main():
    sender = FRCAsyncSender()
    host, port, group = SAFE_POSE

    print(f"连接 {host}:{port} group={group} ...")
    sender.connect(host, port, group)
    print("连接成功，开始 ACK 延迟探测...\n")

    latencies = []
    pending = {}   # seq_id → send_time

    for i in range(N_PROBES):
        # 发送指令
        ok = sender.send_async(
            POSE,
            utool=cfg.utool,
            uframe=cfg.uframe,
            speed=cfg.speed_mm_s,
            term_type=cfg.term_type,
            term_value=cfg.term_value,
        )
        if not ok:
            print(f"[#{i+1}] send_async 失败，跳过")
            continue

        seq_id    = sender.seq_id - 1
        send_time = time.perf_counter()
        pending[seq_id] = send_time

        # 等 ACK（超时保护）
        deadline = send_time + ACK_TIMEOUT_S
        acked = False
        while time.perf_counter() < deadline:
            ack_seq, ack_err = sender.check_ack()
            if ack_seq is not None and ack_seq in pending:
                latency_ms = (time.perf_counter() - pending.pop(ack_seq)) * 1000
                latencies.append(latency_ms)
                marker = ""
                if latency_ms > 200:
                    marker = "  ⚠️ 极慢！"
                elif latency_ms > 100:
                    marker = "  ⚠️ 较慢"
                elif latency_ms > 50:
                    marker = "  ℹ️ 偏慢"
                print(f"[#{i+1:02d}] seq={ack_seq}  latency = {latency_ms:7.2f} ms{marker}")
                acked = True
                break
            time.sleep(ACK_POLL_INTERVAL)

        if not acked:
            print(f"[#{i+1:02d}] seq={seq_id}  ⚠️ 超时（>{ACK_TIMEOUT_S}s），未收到 ACK！")

        time.sleep(PROBE_DELAY_S)

    sender.disconnect()

    if not latencies:
        print("\n没有收到任何 ACK，请检查连接和 FANUC 配置。")
        return

    print(f"\n── 统计 (N={len(latencies)}) {'─'*40}")
    print(f"  平均值   : {statistics.mean(latencies):.1f} ms")
    print(f"  中位数   : {statistics.median(latencies):.1f} ms")
    print(f"  标准差   : {statistics.stdev(latencies):.1f} ms" if len(latencies) > 1 else "")
    print(f"  最小值   : {min(latencies):.1f} ms")
    print(f"  最大值   : {max(latencies):.1f} ms")
    print(f"  > 50ms   : {sum(1 for l in latencies if l > 50):3d} 次")
    print(f"  > 100ms  : {sum(1 for l in latencies if l > 100):3d} 次")
    print(f"  > 200ms  : {sum(1 for l in latencies if l > 200):3d} 次")
    print()

    avg = statistics.mean(latencies)
    print("── 结论 " + "─"*40)
    if avg < 30:
        print(f"  ✅ ACK 延迟正常（{avg:.1f} ms）")
        print("     瓶颈不在 FANUC，请检查 Python 主循环逻辑。")
    elif avg < 80:
        print(f"  ℹ️  ACK 延迟偏高（{avg:.1f} ms），机器人处理稍慢。")
        print("     建议：检查 term_type/term_value，或降低 speed_mm_s。")
    else:
        print(f"  ❌ ACK 延迟严重偏高（{avg:.1f} ms）！")
        print("     可能原因：")
        print("       1. FRCAsyncSender.check_ack() 内部有阻塞（如 recv with timeout）")
        print("       2. FANUC RMI 配置问题（Motion Group 未启用 RMI Fast Mode）")
        print("       3. 网络问题（ping 延迟、交换机抖动）")
        print("       4. term_type='FINE' 导致等待精确到位")
        print(f"\n     当前配置: term_type={cfg.term_type!r}, term_value={cfg.term_value}")
        print(f"               speed_mm_s={cfg.speed_mm_s}")


if __name__ == "__main__":
    main()