
# import socket
# import json
# from datetime import datetime

# HOST = "0.0.0.0"
# PORT = 9000        # Unity

# JSONL_FILE = "received_data.jsonl"

# def main():
#     sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
#     sock.bind((HOST, PORT))
#     print(f"[UDP Receiver] 监听 {HOST}:{PORT} ...")
#     print(f"[UDP Receiver] 数据将保存至 {JSONL_FILE}")
#     print("-" * 70)

#     while True:
#         try:
#             data, addr = sock.recvfrom(1024)
#             timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]

#             payload = json.loads(data.decode("utf-8"))

#             quest = payload["quest"]
#             fanuc = payload["fanuc"]
#             buttons = payload["buttons"]

#             # 构建带时间戳和来源信息的记录
#             record = {
#                 "timestamp": timestamp,
#                 "source_ip": addr[0],
#                 "source_port": addr[1],
#                 "quest": quest,
#                 "fanuc": fanuc
#             }

#             # 追加写入 JSONL 文件（每条记录一行）
#             with open(JSONL_FILE, "a", encoding="utf-8") as f:
#                 f.write(json.dumps(record, ensure_ascii=False) + "\n")

#             print(f"[{timestamp}] 来自 {addr[0]}:{addr[1]}")
#             print(f"  Quest  | Pos: ({quest['px']:+.4f}, {quest['py']:+.4f}, {quest['pz']:+.4f})"
#                   f"  Quat: ({quest['qx']:+.4f}, {quest['qy']:+.4f}, {quest['qz']:+.4f}, {quest['qw']:+.4f})")
#             print(f"  Fanuc  | Pos: X={fanuc['x']:+.2f}mm  Y={fanuc['y']:+.2f}mm  Z={fanuc['z']:+.2f}mm"
#                   f"  Rot: W={fanuc['w']:+.2f}°  P={fanuc['p']:+.2f}°  R={fanuc['r']:+.2f}°")
#             print(f"  Buttons | {buttons}")
#             print("-" * 70)

#         except json.JSONDecodeError as e:
#             print(f"[警告] JSON 解析失败: {e} | 原始数据: {data}")
#         except KeyboardInterrupt:
#             print("\n[UDP Receiver] 已停止。")
#             break
#         except Exception as e:
#             print(f"[错误] {e}")

#     sock.close()

# if __name__ == "__main__":
#     main()
import socket

import json

from datetime import datetime

import matplotlib.pyplot as plt

from matplotlib.animation import FuncAnimation

from collections import deque

import threading

import queue
 
HOST = "0.0.0.0"

PORT = 9000
 
BUFFER_SIZE = 300
 
timestamps = deque(maxlen=BUFFER_SIZE)

quest_data = {

    'px': deque(maxlen=BUFFER_SIZE), 'py': deque(maxlen=BUFFER_SIZE), 'pz': deque(maxlen=BUFFER_SIZE),

    'qx': deque(maxlen=BUFFER_SIZE), 'qy': deque(maxlen=BUFFER_SIZE),

    'qz': deque(maxlen=BUFFER_SIZE), 'qw': deque(maxlen=BUFFER_SIZE)

}

fanuc_data = {

    'x': deque(maxlen=BUFFER_SIZE), 'y': deque(maxlen=BUFFER_SIZE), 'z': deque(maxlen=BUFFER_SIZE),

    'w': deque(maxlen=BUFFER_SIZE), 'p': deque(maxlen=BUFFER_SIZE), 'r': deque(maxlen=BUFFER_SIZE)

}
 
data_queue = queue.Queue()
 
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

sock.bind((HOST, PORT))

sock.setblocking(False)
 
print(f"[UDP Receiver] 监听 {HOST}:{PORT} ...")

print("-" * 70)
 
 
def udp_listener():

    while True:

        try:

            data, addr = sock.recvfrom(4096)

            timestamp = datetime.now()

            # ── 修复1：Unity 发的是扁平 JSON，直接解析整包 ──

            payload = json.loads(data.decode("utf-8"))

            data_queue.put({'timestamp': timestamp, 'payload': payload})

        except (socket.error, BlockingIOError):

            pass

        except json.JSONDecodeError:

            pass

        except Exception as e:

            print(f"[Error] {e}")
 
 
def process_incoming_data():

    while not data_queue.empty():

        try:

            item = data_queue.get_nowait()

            timestamp = item['timestamp']

            # ── 修复2：直接从扁平 payload 取字段 ──

            p = item['payload']
 
            timestamps.append(timestamp)
 
            for key in quest_data:

                quest_data[key].append(p.get(key, 0.0))
 
            for key in fanuc_data:

                fanuc_data[key].append(p.get(key, 0.0))
 
            ts = timestamp.strftime("%H:%M:%S.%f")[:-3]

            print(

                f"[{ts}]  "

                f"Quest pos=({p['px']:+.3f}, {p['py']:+.3f}, {p['pz']:+.3f})  |  "
                f"Fanuc X={p['x']:+.1f} Y={p['y']:+.1f} Z={p['z']:+.1f}  "
                f"W={p['w']:+.1f} P={p['p']:+.1f} R={p['r']:+.1f}"
                f"triggerButton ={p['triggerButton']} gripButton={p['gripButton']}"

            )
 
        except queue.Empty:

            break
 
 
# ── 绘图 ──────────────────────────────────────────────

plt.style.use('dark_background')

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8))

fig.suptitle('Real-time UDP Data', fontsize=14, fontweight='bold')
 
 
def update_plot(frame):

    process_incoming_data()
 
    if not timestamps:

        return
 
    t0 = timestamps[0]

    time_seconds = [(t - t0).total_seconds() for t in timestamps]
 
    ax1.clear()

    ax2.clear()
 
    # ── Quest ──

    ax1.set_title('Quest Hand Tracker', fontweight='bold')

    ax1.set_ylabel('Position (m) / Quaternion')

    ax1.grid(True, alpha=0.3, linestyle='--')
 
    if len(time_seconds) > 1:

        ax1.plot(time_seconds, list(quest_data['px']), 'r-',  lw=1.5, label='px', alpha=0.9)

        ax1.plot(time_seconds, list(quest_data['py']), 'g-',  lw=1.5, label='py', alpha=0.9)

        ax1.plot(time_seconds, list(quest_data['pz']), 'b-',  lw=1.5, label='pz', alpha=0.9)

        ax1.plot(time_seconds, list(quest_data['qx']), 'r--', lw=1,   label='qx', alpha=0.5)

        ax1.plot(time_seconds, list(quest_data['qy']), 'g--', lw=1,   label='qy', alpha=0.5)

        ax1.plot(time_seconds, list(quest_data['qz']), 'b--', lw=1,   label='qz', alpha=0.5)

        ax1.plot(time_seconds, list(quest_data['qw']), 'y--', lw=1,   label='qw', alpha=0.5)
 
    ax1.legend(loc='upper right', ncol=4, fontsize=8)

    ax1.set_xlim(time_seconds[0], max(time_seconds[-1], time_seconds[0] + 1))
 
    # ── Fanuc ──

    ax2.set_title('Fanuc TCP', fontweight='bold')

    ax2.set_xlabel('Time (s)')

    ax2.set_ylabel('Position (mm) / Angle (deg)')

    ax2.grid(True, alpha=0.3, linestyle='--')
 
    if len(time_seconds) > 1:

        ax2.plot(time_seconds, list(fanuc_data['x']), 'r-',  lw=1.5, label='X mm', alpha=0.9)

        ax2.plot(time_seconds, list(fanuc_data['y']), 'g-',  lw=1.5, label='Y mm', alpha=0.9)

        ax2.plot(time_seconds, list(fanuc_data['z']), 'b-',  lw=1.5, label='Z mm', alpha=0.9)

        ax2.plot(time_seconds, list(fanuc_data['w']), 'r--', lw=1,   label='W°',   alpha=0.5)

        ax2.plot(time_seconds, list(fanuc_data['p']), 'g--', lw=1,   label='P°',   alpha=0.5)

        ax2.plot(time_seconds, list(fanuc_data['r']), 'b--', lw=1,   label='R°',   alpha=0.5)
 
    ax2.legend(loc='upper right', ncol=3, fontsize=8)

    ax2.set_xlim(time_seconds[0], max(time_seconds[-1], time_seconds[0] + 1))
 
    plt.tight_layout()
 
 
def main():

    t = threading.Thread(target=udp_listener, daemon=True)

    t.start()
 
    print("等待数据中...")
 
    ani = FuncAnimation(fig, update_plot, interval=50, cache_frame_data=False)
 
    try:

        plt.show()

    except KeyboardInterrupt:

        print("\n[UDP Receiver] 已停止。")

    finally:

        sock.close()
 
 
if __name__ == "__main__":

    main()
 