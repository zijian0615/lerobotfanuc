# import socket
# import json
# import time
# import math
# import threading
# import queue
# import statistics

# # ─────────────────────────────────────────────
# # 配置
# # ─────────────────────────────────────────────
# HOST              = "172.30.109.22"
# PORT_CONNECT      = 16001
# GROUP             = 1
# LOG_FILE          = "../recordings/teleop_log_1772263304.jsonl"

# START_UNITY_TS    = 3.1666667461395264

# UTOOL             = 1
# UFRAME            = 0         # 改为0试试，通常是基坐标系
# SPEED_MM_S        = 200       # 降低速度以确保安全
# CNT_VALUE         = 0         # 改为0试试精确停止，之前100可能太大了

# SKIP_NOT_TRACKING = True
# SKIP_DUPLICATE    = True
# MIN_DIST_MM       = 0.3

# ACK_TIMEOUT       = 10.0      # 等 ACK 最长时间（秒）
# TARGET_FPS        = 50        # 目标帧率（Hz）

# # ─────────────────────────────────────────────
# # LineSocket：解决 TCP 粘包
# # ─────────────────────────────────────────────
# class LineSocket:
#     def __init__(self, sock):
#         self.sock = sock
#         self._buf = b""

#     def sendall(self, data):
#         self.sock.sendall(data)

#     def settimeout(self, t):
#         self.sock.settimeout(t)

#     def readline(self):
#         while True:
#             for sep in (b"\r\n", b"\n"):
#                 idx = self._buf.find(sep)
#                 if idx != -1:
#                     line = self._buf[:idx].decode(errors="replace").strip()
#                     self._buf = self._buf[idx+len(sep):]
#                     if line:
#                         return line
#             chunk = self.sock.recv(4096)
#             if not chunk:
#                 raise ConnectionError("Socket closed")
#             self._buf += chunk

#     def read_json(self):
#         return json.loads(self.readline())

#     def drain(self, timeout=0.5):
#         self.sock.settimeout(timeout)
#         try:
#             while True:
#                 chunk = self.sock.recv(4096)
#                 if not chunk:
#                     break
#                 self._buf += chunk
#         except socket.timeout:
#             pass
#         self._buf = b""

#     def close(self):
#         self.sock.close()

# # ─────────────────────────────────────────────
# # TCP 优化函数
# # ─────────────────────────────────────────────
# def optimize_tcp_socket(sock):
#     """优化TCP连接以实现低延迟"""
#     try:
#         # 1. 禁用Nagle算法 - 立即发送小包
#         sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        
#         # 2. 设置缓冲区大小
#         sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
#         sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
        
#         # 3. 启用KeepAlive
#         sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        
#         print("✅ TCP socket optimized for low-latency")
#     except Exception as e:
#         print(f"⚠️ TCP optimization warning: {e}")

# # ─────────────────────────────────────────────
# # 异步发送器 - 核心优化
# # ─────────────────────────────────────────────
# class AsyncStreamingSender:
#     """
#     异步TCP发送器：分离数据发送和ACK接收，实现高频控制
#     - 主线程：发送指令（立即返回，不等响应）
#     - 后台线程：监听ACK（异步处理）
#     """
    
#     def __init__(self):
#         self.ls = None
#         self.ack_queue = queue.Queue()
#         self.recv_thread = None
#         self.running = False
#         self.seq_id = 1
#         self.ack_stats = {}  # 跟踪ACK统计
        
#     def connect(self, host, port, group=1):
#         """连接机器人并启动后台ACK监听"""
#         print("🔌 Connecting to Fanuc RMI...")
        
#         # 获取RMI端口
#         dynamic_port = frc_connect(host, port)
        
#         # 创建并优化socket
#         raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#         optimize_tcp_socket(raw)
#         raw.connect((host, dynamic_port))
#         raw.settimeout(5.0)
        
#         self.ls = LineSocket(raw)
        
#         # 初始化RMI
#         init_cmd = json.dumps({"Command": "FRC_Initialize", "GroupMask": group}) + "\r\n"
#         self.ls.sendall(init_cmd.encode())
#         resp = self.ls.read_json()
#         print(f"FRC_Initialize: {resp}")
#         if resp.get("ErrorID", -1) != 0:
#             raise RuntimeError(f"FRC_Initialize failed: {resp}")
#         self.ls.drain()
        
#         # 🚀 启动后台ACK监听线程
#         self.running = True
#         self.recv_thread = threading.Thread(target=self._listen_acks, daemon=True)
#         self.recv_thread.start()
#         print("✅ Async sender initialized, ACK listener started")
    
#     def _listen_acks(self):
#         """
#         后台线程：持续监听ACK响应
#         不阻塞主线程的发送，提高吞吐量
#         """
#         error_desc = {
#             2556952: "Configuration parameter error",
#             2556956: "Robot still executing (RMIT-028)",
#             2556957: "Invalid parameter or robot state",
#             2556959: "Position data invalid/out of range",
#         }
        
#         while self.running:
#             try:
#                 self.ls.settimeout(0.1)  # 100ms超时（非阻塞式）
#                 try:
#                     resp = self.ls.read_json()
#                     seq_id = resp.get("SequenceID", -1)
#                     err_id = resp.get("ErrorID", -1)
                    
#                     # 放入队列，主线程可选择性处理
#                     self.ack_queue.put((seq_id, err_id, time.perf_counter()))
                    
#                     if err_id != 0:
#                         desc = error_desc.get(err_id, "Unknown error")
#                         print(f"⚠️ ACK Error: seq={seq_id} err={err_id:7d} (0x{err_id:06x}) - {desc}")
#                 except socket.timeout:
#                     # 正常超时，继续监听
#                     pass
#             except Exception as e:
#                 if self.running:
#                     print(f"❌ ACK listener error: {e}")
#                 break
    
#     def send_async(self, seq_id, target, speed=SPEED_MM_S, cnt=CNT_VALUE):
#         """
#         异步发送指令：立即返回，不等ACK
        
#         Args:
#             seq_id: 序列号
#             target: (x, y, z, w, p, r) 目标位置
#             speed: 速度值
#             cnt: CNT终止值
            
#         Returns:
#             True=发送成功, False=发送失败
#         """
#         try:
#             packet = make_packet(seq_id, target, speed, cnt)
#             # 调试：打印发送的数据（仅首次和出错时）
#             if seq_id <= 2 or seq_id % 100 == 0:
#                 print(f"📤 Sending seq={seq_id}: target={target}, speed={speed}, cnt={cnt}")
#             self.ls.sendall(packet)
#             return True
#         except Exception as e:
#             print(f"❌ Send failed: {e}")
#             return False
    
#     def check_ack(self):
#         """
#         检查队列中是否有待处理的ACK
#         非阻塞式，用于监控ACK状态
#         """
#         try:
#             seq_id, err_id, timestamp = self.ack_queue.get_nowait()
#             return seq_id, err_id
#         except queue.Empty:
#             return None, None
    
#     def disconnect(self):
#         """断开连接"""
#         self.running = False
#         try:
#             time.sleep(0.1)  # 等后台线程处理最后的ACK
#             if self.recv_thread:
#                 self.recv_thread.join(timeout=1)
#         except:
#             pass
        
#         try:
#             rmi_abort(self.ls)
#         except:
#             pass
        
#         try:
#             self.ls.close()
#         except:
#             pass
        
#         print("🔌 Disconnected")


# def frc_connect(host, port):
#     with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
#         s.connect((host, port))
#         s.sendall(b'{"Communication": "FRC_Connect"}\r\n')
#         resp = s.recv(4096)
#     data = json.loads(resp.decode())
#     print("FRC_Connect:", data)
#     if data.get("ErrorID", -1) != 0:
#         raise RuntimeError(f"FRC_Connect failed: {data}")
#     return data["PortNumber"]

# def rmi_abort(ls):
#     try:
#         ls.settimeout(5.0)
#         ls.sendall((json.dumps({"Command": "FRC_Abort"}) + "\r\n").encode())
#         resp = ls.read_json()
#         print("FRC_Abort:", resp)
#     except Exception as e:
#         print(f"Abort failed: {e}")

# def rmi_initialize(host, port, group=1):
#     raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#     raw.connect((host, port))
#     raw.settimeout(5.0)
#     ls = LineSocket(raw)
#     ls.sendall((json.dumps({"Command": "FRC_Initialize", "GroupMask": group}) + "\r\n").encode())
#     resp = ls.read_json()
#     print("FRC_Initialize:", resp)
#     if resp.get("ErrorID", -1) != 0:
#         raise RuntimeError(f"FRC_Initialize failed: {resp}")
#     ls.drain()
#     return ls

# # ─────────────────────────────────────────────
# # 运动包
# # ─────────────────────────────────────────────
# def make_packet(seq_id, target, speed=SPEED_MM_S, cnt=CNT_VALUE):
#     x, y, z, w, p, r = target
    
#     # 参数检查和约束
#     # 位置：通常在合理范围内（根据机器人外形尺寸）
#     x = max(-2000, min(2000, float(x)))
#     y = max(-2000, min(2000, float(y)))
#     z = max(-2000, min(2000, float(z)))
    
#     # 姿态：0-360
#     w = float(w) % 360
#     p = float(p) % 360
#     r = float(r) % 360
    
#     # 速度：1-1000 mm/s
#     speed = max(1, min(1000, int(speed)))
    
#     # CNT：0-100（0=最精确停止，100=最平滑）
#     cnt = max(0, min(100, int(cnt)))
    
#     return (json.dumps({
#         "Instruction": "FRC_LinearMotion",
#         "SequenceID": int(seq_id),
#         "Configuration": {
#             "UToolNumber": int(UTOOL),
#             "UFrameNumber": int(UFRAME),
#             "Front": 1, "Up": 1, "Left": 0,
#             "Flip": 0, "Turn4": 0, "Turn5": 0, "Turn6": 0
#         },
#         "Position": {
#             "X": x, "Y": y, "Z": z,
#             "W": w, "P": p, "R": r,
#             "Ext1": 0.0, "Ext2": 0.0, "Ext3": 0.0
#         },
#         "SpeedType": "mmSec",
#         "Speed": speed,
#         "TermType": "CNT",
#         "TermValue": cnt
#     }) + "\r\n").encode()

# # ─────────────────────────────────────────────
# # 发送并等 ACK，带重试
# # 返回 True=成功, False=跳过
# # ─────────────────────────────────────────────
# def send_and_ack(ls, seq_id, target, label=""):
#     ls.settimeout(ACK_TIMEOUT)
#     for attempt in range(3):
#         ls.sendall(make_packet(seq_id, target))
#         try:
#             resp = ls.read_json()
#             err  = resp.get("ErrorID", -1)
#             if err == 0:
#                 return True
#             elif err == 2556956:  # RMIT-028: wait for instruction done
#                 print(f"{label} RMIT-028, waiting...")
#                 time.sleep(0.1)
#                 # 不重发，继续下一帧
#                 return False
#             else:
#                 print(f"{label} ErrorID={err} (hex={hex(err)}) attempt={attempt+1}")
#                 time.sleep(0.05)
#         except socket.timeout:
#             print(f"{label} ACK timeout attempt={attempt+1}")
#     return False

# # ─────────────────────────────────────────────
# # 过滤
# # ─────────────────────────────────────────────
# def pos_dist(a, b):
#     return math.sqrt(sum((a[i] - b[i])**2 for i in range(3)))

# def load_and_filter(log_file):
#     records = []
#     prev_target = None
#     with open(log_file, "r") as f:
#         for line in f:
#             line = line.strip()
#             if not line:
#                 continue
#             try:
#                 rec = json.loads(line)
#             except:
#                 continue
#             if rec.get("unity_data", {}).get("timestamp", 0.0) < START_UNITY_TS:
#                 continue
#             if SKIP_NOT_TRACKING and not rec.get("is_tracking", False):
#                 continue
#             target = rec.get("robot_target")
#             if not target or len(target) != 6:
#                 continue
#             if SKIP_DUPLICATE and prev_target == target:
#                 continue
#             if prev_target is not None and pos_dist(target, prev_target) < MIN_DIST_MM:
#                 continue
#             records.append((float(rec["ts"]), target))
#             prev_target = target
#     print(f"Loaded {len(records)} frames after filtering")
#     return records

# # ─────────────────────────────────────────────
# # 主流程
# # ─────────────────────────────────────────────
# def main():
#     records = load_and_filter(LOG_FILE)
#     if not records:
#         print("No records, exiting.")
#         return

#     sender = AsyncStreamingSender()
    
#     try:
#         sender.connect(HOST, PORT_CONNECT, GROUP)
#     except Exception as e:
#         print(f"❌ Connection failed: {e}")
#         return
    
#     seq_id = 1
#     frame_times = []
#     ack_errors = []
#     frame_interval = 1.0 / TARGET_FPS  # 帧间隔（秒）
    
#     try:
#         for idx, (ts, target) in enumerate(records):
#             t_frame_start = time.perf_counter()
            
#             # 🚀 发送指令（异步，立即返回）
#             ok = sender.send_async(seq_id, target)
            
#             if ok:
#                 seq_id += 1
                
#                 # 间歇地检查ACK（非阻塞）
#                 ack_seq, ack_err = sender.check_ack()
#                 if ack_seq is not None:
#                     if ack_err == 0:
#                         ack_errors.append((ack_seq, 0))
#                     elif ack_err == 2556956:  # RMIT-028
#                         pass  # 机器人还在执行上一条指令，正常
#                     else:
#                         ack_errors.append((ack_seq, ack_err))
#                         print(f"  ⚠️ ACK Error: seq={ack_seq} err={ack_err} (hex={hex(ack_err)})")
                
#                 # 帧率控制：维持TARGET_FPS
#                 t_elapsed = time.perf_counter() - t_frame_start
#                 t_sleep = max(0, frame_interval - t_elapsed)
#                 if t_sleep > 0:
#                     time.sleep(t_sleep)
                
#                 t_total = time.perf_counter() - t_frame_start
#                 frame_times.append(t_total * 1000)  # 转换为ms
                
#                 # 定期打印进度
#                 if (idx + 1) % 20 == 0 or idx == 0:
#                     current_fps = 1000.0 / statistics.mean(frame_times[-20:]) if frame_times else 0
#                     print(f"[{idx+1:4d}/{len(records):4d}] seq={seq_id-1:4d} "
#                           f"dt={t_total*1000:5.1f}ms fps={current_fps:5.1f}Hz")
#             else:
#                 print(f"[{idx+1}/{len(records)}] ❌ Send FAILED, skipping")
    
#     except KeyboardInterrupt:
#         print("\n⚠️ 用户中断")
#     except Exception as e:
#         print(f"💥 异常: {e}")
#         import traceback
#         traceback.print_exc()
#     finally:
#         sender.disconnect()
        
#         # 📊 打印最终统计
#         print("\n" + "="*60)
#         print("📊 性能统计报告")
#         print("="*60)
        
#         if frame_times:
#             print(f"总发送帧数: {len(frame_times)}")
#             print(f"平均帧间隔: {statistics.mean(frame_times):.2f} ms")
#             print(f"中位数间隔: {statistics.median(frame_times):.2f} ms")
#             print(f"最小间隔: {min(frame_times):.2f} ms")
#             print(f"最大间隔: {max(frame_times):.2f} ms")
#             print(f"标准差: {statistics.stdev(frame_times):.2f} ms" if len(frame_times) > 1 else "")
            
#             avg_fps = 1000.0 / statistics.mean(frame_times)
#             print(f"\n🎯 平均帧率: {avg_fps:.1f} Hz")
            
#             if avg_fps >= TARGET_FPS:
#                 print(f"✅ 达到目标帧率 {TARGET_FPS}Hz!")
#             else:
#                 print(f"⚠️ 未达到目标帧率 {TARGET_FPS}Hz (实际{avg_fps:.1f}Hz)")
        
#         if ack_errors:
#             error_count = sum(1 for seq, err in ack_errors if err != 0)
#             print(f"\n📨 ACK错误统计:")
#             print(f"  总错误数: {error_count}")
#             if error_count > 0:
#                 print(f"  错误率: {100*error_count/len(ack_errors):.2f}%")
        
#         print("="*60)

# if __name__ == "__main__":
#     main()
import socket
import json
import time
import math
import threading
import queue
import statistics

# ─────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────
HOST              = "172.30.109.22"
PORT_CONNECT      = 16001
GROUP             = 1
LOG_FILE          = "/Users/zhangzijian/Desktop/fanuc/Fanuc_teleop/received_data.jsonl"

UTOOL             = 1
UFRAME            = 0
SPEED_MM_S        = 200
CNT_VALUE         = 0

SKIP_NOT_TRACKING = True
SKIP_DUPLICATE    = True
MIN_DIST_MM       = 0.3

ACK_TIMEOUT       = 10.0
TARGET_FPS        = 50

# ─────────────────────────────────────────────
# LineSocket：解决 TCP 粘包
# ─────────────────────────────────────────────
class LineSocket:
    def __init__(self, sock):
        self.sock = sock
        self._buf = b""

    def sendall(self, data):
        self.sock.sendall(data)

    def settimeout(self, t):
        self.sock.settimeout(t)

    def readline(self):
        while True:
            for sep in (b"\r\n", b"\n"):
                idx = self._buf.find(sep)
                if idx != -1:
                    line = self._buf[:idx].decode(errors="replace").strip()
                    self._buf = self._buf[idx+len(sep):]
                    if line:
                        return line
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Socket closed")
            self._buf += chunk

    def read_json(self):
        return json.loads(self.readline())

    def drain(self, timeout=0.5):
        self.sock.settimeout(timeout)
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                self._buf += chunk
        except socket.timeout:
            pass
        self._buf = b""

    def close(self):
        self.sock.close()

# ─────────────────────────────────────────────
# TCP 优化函数
# ─────────────────────────────────────────────
def optimize_tcp_socket(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        print("✅ TCP socket optimized for low-latency")
    except Exception as e:
        print(f"⚠️ TCP optimization warning: {e}")

# ─────────────────────────────────────────────
# 异步发送器
# ─────────────────────────────────────────────
class AsyncStreamingSender:
    def __init__(self):
        self.ls = None
        self.ack_queue = queue.Queue()
        self.recv_thread = None
        self.running = False
        self.seq_id = 1
        self.ack_stats = {}

    def connect(self, host, port, group=1):
        print("🔌 Connecting to Fanuc RMI...")
        dynamic_port = frc_connect(host, port)

        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        optimize_tcp_socket(raw)
        raw.connect((host, dynamic_port))
        raw.settimeout(5.0)

        self.ls = LineSocket(raw)

        init_cmd = json.dumps({"Command": "FRC_Initialize", "GroupMask": group}) + "\r\n"
        self.ls.sendall(init_cmd.encode())
        resp = self.ls.read_json()
        print(f"FRC_Initialize: {resp}")
        if resp.get("ErrorID", -1) != 0:
            raise RuntimeError(f"FRC_Initialize failed: {resp}")
        self.ls.drain()

        self.running = True
        self.recv_thread = threading.Thread(target=self._listen_acks, daemon=True)
        self.recv_thread.start()
        print("✅ Async sender initialized, ACK listener started")

    def _listen_acks(self):
        error_desc = {
            2556952: "Configuration parameter error",
            2556956: "Robot still executing (RMIT-028)",
            2556957: "Invalid parameter or robot state",
            2556959: "Position data invalid/out of range",
        }
        while self.running:
            try:
                self.ls.settimeout(0.1)
                try:
                    resp = self.ls.read_json()
                    seq_id = resp.get("SequenceID", -1)
                    err_id = resp.get("ErrorID", -1)
                    self.ack_queue.put((seq_id, err_id, time.perf_counter()))
                    if err_id != 0:
                        desc = error_desc.get(err_id, "Unknown error")
                        print(f"⚠️ ACK Error: seq={seq_id} err={err_id:7d} (0x{err_id:06x}) - {desc}")
                except socket.timeout:
                    pass
            except Exception as e:
                if self.running:
                    print(f"❌ ACK listener error: {e}")
                break

    def send_async(self, seq_id, target, speed=SPEED_MM_S, cnt=CNT_VALUE):
        try:
            packet = make_packet(seq_id, target, speed, cnt)
            if seq_id <= 2 or seq_id % 100 == 0:
                print(f"📤 Sending seq={seq_id}: target={target}, speed={speed}, cnt={cnt}")
            self.ls.sendall(packet)
            return True
        except Exception as e:
            print(f"❌ Send failed: {e}")
            return False

    def check_ack(self):
        try:
            seq_id, err_id, timestamp = self.ack_queue.get_nowait()
            return seq_id, err_id
        except queue.Empty:
            return None, None

    def disconnect(self):
        self.running = False
        try:
            time.sleep(0.1)
            if self.recv_thread:
                self.recv_thread.join(timeout=1)
        except:
            pass
        try:
            rmi_abort(self.ls)
        except:
            pass
        try:
            self.ls.close()
        except:
            pass
        print("🔌 Disconnected")


def frc_connect(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((host, port))
        s.sendall(b'{"Communication": "FRC_Connect"}\r\n')
        resp = s.recv(4096)
    data = json.loads(resp.decode())
    print("FRC_Connect:", data)
    if data.get("ErrorID", -1) != 0:
        raise RuntimeError(f"FRC_Connect failed: {data}")
    return data["PortNumber"]

def rmi_abort(ls):
    try:
        ls.settimeout(5.0)
        ls.sendall((json.dumps({"Command": "FRC_Abort"}) + "\r\n").encode())
        resp = ls.read_json()
        print("FRC_Abort:", resp)
    except Exception as e:
        print(f"Abort failed: {e}")

# ─────────────────────────────────────────────
# 运动包
# ─────────────────────────────────────────────
def make_packet(seq_id, target, speed=SPEED_MM_S, cnt=CNT_VALUE):
    x, y, z, w, p, r = target
    x = max(-2000, min(2000, float(x)))
    y = max(-2000, min(2000, float(y)))
    z = max(-2000, min(2000, float(z)))
    w = float(w) % 360
    p = float(p) % 360
    r = float(r) % 360
    speed = max(1, min(1000, int(speed)))
    cnt = max(0, min(100, int(cnt)))

    return (json.dumps({
        "Instruction": "FRC_LinearMotion",
        "SequenceID": int(seq_id),
        "Configuration": {
            "UToolNumber": int(UTOOL),
            "UFrameNumber": int(UFRAME),
            "Front": 1, "Up": 1, "Left": 0,
            "Flip": 0, "Turn4": 0, "Turn5": 0, "Turn6": 0
        },
        "Position": {
            "X": x, "Y": y, "Z": z,
            "W": w, "P": p, "R": r,
            "Ext1": 0.0, "Ext2": 0.0, "Ext3": 0.0
        },
        "SpeedType": "mmSec",
        "Speed": speed,
        "TermType": "CNT",
        "TermValue": cnt
    }) + "\r\n").encode()

# ─────────────────────────────────────────────
# 过滤
# ─────────────────────────────────────────────
def pos_dist(a, b):
    return math.sqrt(sum((a[i] - b[i])**2 for i in range(3)))

def load_and_filter(log_file):
    records = []
    prev_target = None
    with open(log_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except:
                continue
            # 从 fanuc 字段提取目标位置
            fanuc = rec.get("fanuc")
            if not fanuc:
                continue
            try:
                target = [
                    fanuc["x"], fanuc["y"], fanuc["z"],
                    fanuc["w"], fanuc["p"], fanuc["r"]
                ]
            except KeyError:
                continue
            if SKIP_DUPLICATE and prev_target == target:
                continue
            if prev_target is not None and pos_dist(target, prev_target) < MIN_DIST_MM:
                continue
            ts = rec.get("ts", rec.get("timestamp", 0.0))
            records.append((ts, target))
            prev_target = target
    print(f"Loaded {len(records)} frames after filtering")
    return records

# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────
def main():
    records = load_and_filter(LOG_FILE)
    if not records:
        print("No records, exiting.")
        return

    sender = AsyncStreamingSender()
    try:
        sender.connect(HOST, PORT_CONNECT, GROUP)
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return

    seq_id = 1
    frame_times = []
    ack_errors = []
    frame_interval = 1.0 / TARGET_FPS

    try:
        for idx, (ts, target) in enumerate(records):
            t_frame_start = time.perf_counter()

            ok = sender.send_async(seq_id, target)

            if ok:
                seq_id += 1

                ack_seq, ack_err = sender.check_ack()
                if ack_seq is not None:
                    if ack_err == 0:
                        ack_errors.append((ack_seq, 0))
                    elif ack_err == 2556956:
                        pass
                    else:
                        ack_errors.append((ack_seq, ack_err))
                        print(f"  ⚠️ ACK Error: seq={ack_seq} err={ack_err} (hex={hex(ack_err)})")

                t_elapsed = time.perf_counter() - t_frame_start
                t_sleep = max(0, frame_interval - t_elapsed)
                if t_sleep > 0:
                    time.sleep(t_sleep)

                t_total = time.perf_counter() - t_frame_start
                frame_times.append(t_total * 1000)

                if (idx + 1) % 20 == 0 or idx == 0:
                    current_fps = 1000.0 / statistics.mean(frame_times[-20:]) if frame_times else 0
                    print(f"[{idx+1:4d}/{len(records):4d}] seq={seq_id-1:4d} "
                          f"dt={t_total*1000:5.1f}ms fps={current_fps:5.1f}Hz")
            else:
                print(f"[{idx+1}/{len(records)}] ❌ Send FAILED, skipping")

    except KeyboardInterrupt:
        print("\n⚠️ 用户中断")
    except Exception as e:
        print(f"💥 异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        sender.disconnect()

        print("\n" + "="*60)
        print("📊 性能统计报告")
        print("="*60)

        if frame_times:
            print(f"总发送帧数: {len(frame_times)}")
            print(f"平均帧间隔: {statistics.mean(frame_times):.2f} ms")
            print(f"中位数间隔: {statistics.median(frame_times):.2f} ms")
            print(f"最小间隔: {min(frame_times):.2f} ms")
            print(f"最大间隔: {max(frame_times):.2f} ms")
            if len(frame_times) > 1:
                print(f"标准差: {statistics.stdev(frame_times):.2f} ms")
            avg_fps = 1000.0 / statistics.mean(frame_times)
            print(f"\n🎯 平均帧率: {avg_fps:.1f} Hz")
            if avg_fps >= TARGET_FPS:
                print(f"✅ 达到目标帧率 {TARGET_FPS}Hz!")
            else:
                print(f"⚠️ 未达到目标帧率 {TARGET_FPS}Hz (实际{avg_fps:.1f}Hz)")

        if ack_errors:
            error_count = sum(1 for _, err in ack_errors if err != 0)
            print(f"\n📨 ACK错误统计:")
            print(f"  总错误数: {error_count}")
            if error_count > 0:
                print(f"  错误率: {100*error_count/len(ack_errors):.2f}%")

        print("="*60)

if __name__ == "__main__":
    main()