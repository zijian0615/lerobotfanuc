# import socket
# import json
# import time
# import math
# import threading
# import queue
# import statistics
# from datetime import datetime

# # ─────────────────────────────────────────────
# # 配置
# # ─────────────────────────────────────────────
# # Fanuc 机器人
# FANUC_HOST        = "172.30.109.22"
# FANUC_PORT        = 16001
# GROUP             = 1

# UTOOL             = 1
# UFRAME            = 0
# SPEED_MM_S        = 200
# CNT_VALUE         = 0

# # Unity UDP 接收
# UDP_HOST          = "0.0.0.0"
# UDP_PORT          = 9000

# # 过滤参数
# MIN_DIST_MM       = 0.005       # 低于此距离的帧跳过（避免重复）
# ACK_TIMEOUT       = 10.0
# TARGET_FPS        = 30        # 目标发送帧率（Hz）

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
# # TCP 优化
# # ─────────────────────────────────────────────
# def optimize_tcp_socket(sock):
#     try:
#         sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
#         sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
#         sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
#         sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
#         print("✅ TCP socket optimized for low-latency")
#     except Exception as e:
#         print(f"⚠️ TCP optimization warning: {e}")

# # ─────────────────────────────────────────────
# # UDP 接收器（后台线程，将最新帧放入队列）
# # ─────────────────────────────────────────────
# class UDPReceiver:
#     """
#     后台线程持续接收 Unity UDP 数据包。
#     只保留最新一帧（latest_frame），避免积压延迟。
#     """
#     def __init__(self, host=UDP_HOST, port=UDP_PORT):
#         self.host = host
#         self.port = port
#         self.latest_frame = None      # 最新帧 (x, y, z, w, p, r)
#         self.lock = threading.Lock()
#         self.running = False
#         self.thread = None
#         self.recv_count = 0
#         self.error_count = 0

#     def start(self):
#         self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
#         self.sock.bind((self.host, self.port))
#         self.sock.settimeout(1.0)
#         self.running = True
#         self.thread = threading.Thread(target=self._recv_loop, daemon=True)
#         self.thread.start()
#         print(f"✅ UDP Receiver listening on {self.host}:{self.port}")

#     def _recv_loop(self):
#         while self.running:
#             try:
#                 data, addr = self.sock.recvfrom(4096)
#                 payload = json.loads(data.decode("utf-8"))

#                 fanuc = payload.get("fanuc", {})
#                 x = fanuc.get("x")
#                 y = fanuc.get("y")
#                 z = fanuc.get("z")
#                 w = fanuc.get("w")
#                 p = fanuc.get("p")
#                 r = fanuc.get("r")

#                 # 确保所有字段都存在且合法
#                 if None in (x, y, z, w, p, r):
#                     self.error_count += 1
#                     continue
#                 if any(math.isnan(v) or math.isinf(v) for v in (x, y, z, w, p, r)):
#                     self.error_count += 1
#                     continue

#                 with self.lock:
#                     self.latest_frame = (x, y, z, w, p, r)
#                     self.recv_count += 1

#             except json.JSONDecodeError as e:
#                 self.error_count += 1
#                 print(f"[警告] JSON 解析失败: {e}")
#             except socket.timeout:
#                 continue
#             except Exception as e:
#                 if self.running:
#                     print(f"[错误] UDP 接收异常: {e}")

#     def get_latest(self):
#         """取出最新帧，无新帧则返回 None"""
#         with self.lock:
#             frame = self.latest_frame
#             self.latest_frame = None   # 取走后清空，避免重复发送同一帧
#             return frame

#     def stop(self):
#         self.running = False
#         try:
#             self.sock.close()
#         except:
#             pass

# # ─────────────────────────────────────────────
# # 异步 Fanuc 发送器
# # ─────────────────────────────────────────────
# class AsyncStreamingSender:
#     def __init__(self):
#         self.ls = None
#         self.ack_queue = queue.Queue()
#         self.recv_thread = None
#         self.running = False
#         self.seq_id = 1

#     def connect(self, host, port, group=1):
#         print("🔌 Connecting to Fanuc RMI...")
#         dynamic_port = frc_connect(host, port)

#         raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#         optimize_tcp_socket(raw)
#         raw.connect((host, dynamic_port))
#         raw.settimeout(5.0)
#         self.ls = LineSocket(raw)

#         init_cmd = json.dumps({"Command": "FRC_Initialize", "GroupMask": group}) + "\r\n"
#         self.ls.sendall(init_cmd.encode())
#         resp = self.ls.read_json()
#         print(f"FRC_Initialize: {resp}")
#         if resp.get("ErrorID", -1) != 0:
#             raise RuntimeError(f"FRC_Initialize failed: {resp}")
#         self.ls.drain()

#         self.running = True
#         self.recv_thread = threading.Thread(target=self._listen_acks, daemon=True)
#         self.recv_thread.start()
#         print("✅ Async sender initialized, ACK listener started")

#     def _listen_acks(self):
#         error_desc = {
#             2556952: "Configuration parameter error",
#             2556956: "Robot still executing (RMIT-028)",
#             2556957: "Invalid parameter or robot state",
#             2556959: "Position data invalid/out of range",
#         }
#         while self.running:
#             try:
#                 self.ls.settimeout(0.1)
#                 try:
#                     resp = self.ls.read_json()
#                     seq_id = resp.get("SequenceID", -1)
#                     err_id = resp.get("ErrorID", -1)
#                     self.ack_queue.put((seq_id, err_id, time.perf_counter()))
#                     if err_id != 0:
#                         desc = error_desc.get(err_id, "Unknown error")
#                         print(f"⚠️ ACK Error: seq={seq_id} err={err_id:7d} (0x{err_id:06x}) - {desc}")
#                 except socket.timeout:
#                     pass
#             except Exception as e:
#                 if self.running:
#                     print(f"❌ ACK listener error: {e}")
#                 break

#     def send_async(self, target, speed=SPEED_MM_S, cnt=CNT_VALUE):
#         try:
#             packet = make_packet(self.seq_id, target, speed, cnt)
#             self.ls.sendall(packet)
#             self.seq_id += 1
#             return True
#         except Exception as e:
#             print(f"❌ Send failed: {e}")
#             return False

#     def check_ack(self):
#         try:
#             seq_id, err_id, _ = self.ack_queue.get_nowait()
#             return seq_id, err_id
#         except queue.Empty:
#             return None, None

#     def disconnect(self):
#         self.running = False
#         try:
#             time.sleep(0.1)
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
#         print("🔌 Disconnected from Fanuc")

# # ─────────────────────────────────────────────
# # RMI 工具函数
# # ─────────────────────────────────────────────
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

# def make_packet(seq_id, target, speed=SPEED_MM_S, cnt=CNT_VALUE):
#     x, y, z, w, p, r = target
#     x = max(-2000, min(2000, float(x)))
#     y = max(-2000, min(2000, float(y)))
#     z = max(-2000, min(2000, float(z)))
#     w = float(w) % 360
#     p = float(p) % 360
#     r = float(r) % 360
#     speed = max(1, min(1000, int(speed)))
#     cnt   = max(0, min(100, int(cnt)))

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
# # 过滤
# # ─────────────────────────────────────────────
# def pos_dist(a, b):
#     return math.sqrt(sum((a[i] - b[i])**2 for i in range(3)))

# # ─────────────────────────────────────────────
# # 主流程
# # ─────────────────────────────────────────────
# def main():
#     print("=" * 60)
#     print("🤖 Fanuc Real-Time Teleoperation")
#     print(f"   UDP Input  : {UDP_HOST}:{UDP_PORT}")
#     print(f"   Fanuc RMI  : {FANUC_HOST}:{FANUC_PORT}")
#     print(f"   Target FPS : {TARGET_FPS} Hz")
#     print("=" * 60)

#     # 启动 UDP 接收
#     udp = UDPReceiver(UDP_HOST, UDP_PORT)
#     udp.start()

#     # 连接 Fanuc
#     sender = AsyncStreamingSender()
#     try:
#         sender.connect(FANUC_HOST, FANUC_PORT, GROUP)
#     except Exception as e:
#         print(f"❌ Connection failed: {e}")
#         udp.stop()
#         return

#     frame_interval = 1.0 / TARGET_FPS
#     frame_times    = []
#     sent_count     = 0
#     skip_count     = 0
#     prev_target    = None

#     print("\n▶️  开始实时遥操作，按 Ctrl+C 停止...\n")

#     try:
#         while True:
#             t_start = time.perf_counter()

#             # 取最新 UDP 帧
#             target = udp.get_latest()

#             if target is None:
#                 # 没有新数据，等一帧时间后继续
#                 time.sleep(frame_interval)
#                 continue

#             # 距离过滤：与上一帧太近则跳过
#             if prev_target is not None and pos_dist(target, prev_target) < MIN_DIST_MM:
#                 skip_count += 1
#                 time.sleep(frame_interval)
#                 continue

#             # 发送给 Fanuc
#             ok = sender.send_async(target)
#             if ok:
#                 prev_target = target
#                 sent_count += 1

#                 # 非阻塞检查 ACK
#                 ack_seq, ack_err = sender.check_ack()
#                 if ack_seq is not None and ack_err not in (0, None, 2556956):
#                     print(f"  ⚠️ ACK Error: seq={ack_seq} err={ack_err} (0x{ack_err:06x})")

#                 # 帧率控制
#                 t_elapsed = time.perf_counter() - t_start
#                 t_sleep   = max(0, frame_interval - t_elapsed)
#                 if t_sleep > 0:
#                     time.sleep(t_sleep)

#                 t_total = time.perf_counter() - t_start
#                 frame_times.append(t_total * 1000)

#                 # 每 50 帧打印一次状态
#                 if sent_count % 30 == 0:
#                     ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
#                     fps = 1000.0 / statistics.mean(frame_times[-50:]) if frame_times else 0
#                     print(f"[{ts}] 已发送: {sent_count:5d}帧  跳过: {skip_count:4d}帧  "
#                           f"UDP接收: {udp.recv_count:5d}帧  "
#                           f"fps: {fps:5.1f}Hz  "
#                           f"target: X={target[0]:+7.2f} Y={target[1]:+7.2f} Z={target[2]:+7.2f}")
#             else:
#                 print("❌ 发送失败，跳过本帧")

#     except KeyboardInterrupt:
#         print("\n⚠️  用户中断")
#     except Exception as e:
#         print(f"💥 异常: {e}")
#         import traceback
#         traceback.print_exc()
#     finally:
#         udp.stop()
#         sender.disconnect()

#         # 统计报告
#         print("\n" + "=" * 60)
#         print("📊 性能统计报告")
#         print("=" * 60)
#         print(f"  UDP 接收总帧数 : {udp.recv_count}")
#         print(f"  UDP 解析错误   : {udp.error_count}")
#         print(f"  实际发送帧数   : {sent_count}")
#         print(f"  距离过滤跳过   : {skip_count}")

#         if frame_times:
#             avg_ms  = statistics.mean(frame_times)
#             avg_fps = 1000.0 / avg_ms
#             print(f"  平均帧间隔     : {avg_ms:.2f} ms")
#             print(f"  平均帧率       : {avg_fps:.1f} Hz")
#             print(f"  最小 / 最大    : {min(frame_times):.2f} / {max(frame_times):.2f} ms")
#             if len(frame_times) > 1:
#                 print(f"  标准差         : {statistics.stdev(frame_times):.2f} ms")
#             status = "✅" if avg_fps >= TARGET_FPS * 0.9 else "⚠️"
#             print(f"  {status} 目标帧率 {TARGET_FPS}Hz → 实际 {avg_fps:.1f}Hz")
#         print("=" * 60)

# if __name__ == "__main__":
#     main()
import socket
import json
import time
import math
import threading
import queue
import statistics
from datetime import datetime

# ─────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────
FANUC_HOST        = "172.30.109.22"
FANUC_PORT        = 16001
GROUP             = 1

UTOOL             = 1
UFRAME            = 0
SPEED_MM_S        = 200
CNT_VALUE         = 100       # 100=连续运动，跟随更流畅

UDP_HOST          = "0.0.0.0"
UDP_PORT          = 9000

TARGET_FPS        = 30
PRINT_INTERVAL_S  = 2.0

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
                    self._buf = self._buf[idx + len(sep):]
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
# TCP 优化
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
# UDP 接收器（后台线程，只保留最新帧）
# ─────────────────────────────────────────────
class UDPReceiver:
    def __init__(self, host=UDP_HOST, port=UDP_PORT):
        self.host = host
        self.port = port
        self.latest_frame = None
        self.lock = threading.Lock()
        self.running = False
        self.recv_count = 0
        self.error_count = 0

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.settimeout(1.0)
        self.running = True
        self.thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.thread.start()
        print(f"✅ UDP Receiver listening on {self.host}:{self.port}")

    def _recv_loop(self):
        while self.running:
            try:
                data, _ = self.sock.recvfrom(4096)
                payload = json.loads(data.decode("utf-8"))
                fanuc = payload.get("fanuc", {})
                x = fanuc.get("x")
                y = fanuc.get("y")
                z = fanuc.get("z")
                w = fanuc.get("w")
                p = fanuc.get("p")
                r = fanuc.get("r")

                if None in (x, y, z, w, p, r):
                    self.error_count += 1
                    continue
                if any(math.isnan(v) or math.isinf(v) for v in (x, y, z, w, p, r)):
                    self.error_count += 1
                    continue

                with self.lock:
                    self.latest_frame = (x, y, z, w, p, r)
                    self.recv_count += 1

            except json.JSONDecodeError as e:
                self.error_count += 1
                print(f"[警告] JSON 解析失败: {e}")
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"[错误] UDP 接收异常: {e}")

    def get_latest(self):
        with self.lock:
            frame = self.latest_frame
            self.latest_frame = None
            return frame

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except:
            pass

# ─────────────────────────────────────────────
# 异步发送器
# ─────────────────────────────────────────────
class AsyncStreamingSender:
    def __init__(self):
        self.ls        = None
        self.ack_queue = queue.Queue()
        self.recv_thread = None
        self.running   = False
        self.seq_id    = 1

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
                    resp   = self.ls.read_json()
                    seq_id = resp.get("SequenceID", -1)
                    err_id = resp.get("ErrorID", -1)
                    self.ack_queue.put((seq_id, err_id, time.perf_counter()))
                    if err_id != 0:
                        desc = error_desc.get(err_id, "Unknown error")
                        print(f"⚠️ ACK Error: seq={seq_id} err={err_id} (0x{err_id:06x}) - {desc}")
                except socket.timeout:
                    pass
            except Exception as e:
                if self.running:
                    print(f"❌ ACK listener error: {e}")
                break

    def send_async(self, target):
        try:
            packet = make_packet(self.seq_id, target)
            self.ls.sendall(packet)
            self.seq_id += 1
            return True
        except Exception as e:
            print(f"❌ Send failed: {e}")
            return False

    def check_ack(self):
        try:
            seq_id, err_id, _ = self.ack_queue.get_nowait()
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
        print("🔌 Disconnected from Fanuc")

# ─────────────────────────────────────────────
# RMI 工具函数
# ─────────────────────────────────────────────
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
# ✅ 修复：角度规范化到 FANUC 要求的 -180~+180
# ─────────────────────────────────────────────
def normalize_angle(a):
    """将任意角度规范化到 -180.0 ~ +180.0 度（FANUC RMI 要求范围）"""
    a = float(a) % 360.0
    if a > 180.0:
        a -= 360.0
    return a

def make_packet(seq_id, target, speed=SPEED_MM_S, cnt=CNT_VALUE):
    x, y, z, w, p, r = target
    x = max(-2000, min(2000, float(x)))
    y = max(-2000, min(2000, float(y)))
    z = max(-2000, min(2000, float(z)))
    w = normalize_angle(w)   # ✅ 修复：原来 % 360 会产生 0~360，FANUC 要求 -180~+180
    p = normalize_angle(p)   # ✅ 修复
    r = normalize_angle(r)   # ✅ 修复
    speed = max(1, min(1000, int(speed)))
    cnt   = max(0, min(100, int(cnt)))

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
# 主流程
# ─────────────────────────────────────────────
def main():
    print("=" * 60)
    print("🤖 Fanuc Real-Time Teleoperation")
    print(f"   UDP Input  : {UDP_HOST}:{UDP_PORT}")
    print(f"   Fanuc RMI  : {FANUC_HOST}:{FANUC_PORT}")
    print(f"   Speed      : {SPEED_MM_S} mm/s   CNT={CNT_VALUE}")
    print(f"   Target FPS : {TARGET_FPS} Hz")
    print("=" * 60)

    udp = UDPReceiver(UDP_HOST, UDP_PORT)
    udp.start()

    sender = AsyncStreamingSender()
    try:
        sender.connect(FANUC_HOST, FANUC_PORT, GROUP)
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        udp.stop()
        return

    frame_interval  = 1.0 / TARGET_FPS
    sent_count      = 0
    none_count      = 0
    ack_ok          = 0
    ack_err_count   = 0
    frame_times     = []
    last_print_time = time.perf_counter()

    print("\n▶️  开始实时遥操作，按 Ctrl+C 停止...\n")

    try:
        while True:
            t_start = time.perf_counter()

            target = udp.get_latest()

            if target is None:
                none_count += 1
                if time.perf_counter() - last_print_time >= PRINT_INTERVAL_S:
                    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    print(f"[{ts}] ⏳ 等待 UDP 数据...  "
                          f"UDP接收: {udp.recv_count}  已发送: {sent_count}")
                    last_print_time = time.perf_counter()
                time.sleep(frame_interval)
                continue

            ok = sender.send_async(target)
            if ok:
                sent_count += 1

            ack_seq, ack_err = sender.check_ack()
            if ack_seq is not None:
                if ack_err == 0:
                    ack_ok += 1
                elif ack_err == 2556956:
                    pass
                else:
                    ack_err_count += 1

            t_elapsed = time.perf_counter() - t_start
            t_sleep   = max(0, frame_interval - t_elapsed)
            if t_sleep > 0:
                time.sleep(t_sleep)

            t_total = time.perf_counter() - t_start
            frame_times.append(t_total * 1000)

            if time.perf_counter() - last_print_time >= PRINT_INTERVAL_S:
                ts  = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                fps = 1000.0 / statistics.mean(frame_times[-60:]) if frame_times else 0
                print(f"[{ts}] "
                      f"发送: {sent_count:5d}  "
                      f"ACK✅: {ack_ok:5d}  ACK❌: {ack_err_count:3d}  "
                      f"UDP总: {udp.recv_count:5d}  "
                      f"fps: {fps:5.1f}Hz  "
                      f"→ X={target[0]:+7.2f} Y={target[1]:+7.2f} Z={target[2]:+7.2f}  "
                      f"W={target[3]:+6.2f} P={target[4]:+6.2f} R={target[5]:+6.2f}")
                last_print_time = time.perf_counter()

    except KeyboardInterrupt:
        print("\n⚠️  用户中断")
    except Exception as e:
        print(f"💥 异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        udp.stop()
        sender.disconnect()

        print("\n" + "=" * 60)
        print("📊 性能统计报告")
        print("=" * 60)
        print(f"  UDP 接收总帧数 : {udp.recv_count}")
        print(f"  UDP 解析错误   : {udp.error_count}")
        print(f"  实际发送帧数   : {sent_count}")
        print(f"  ACK 成功       : {ack_ok}")
        print(f"  ACK 错误       : {ack_err_count}")
        print(f"  无数据跳过     : {none_count}")
        if frame_times:
            avg_ms  = statistics.mean(frame_times)
            avg_fps = 1000.0 / avg_ms
            print(f"  平均帧间隔     : {avg_ms:.2f} ms")
            print(f"  平均帧率       : {avg_fps:.1f} Hz")
            print(f"  最小 / 最大    : {min(frame_times):.2f} / {max(frame_times):.2f} ms")
            if len(frame_times) > 1:
                print(f"  标准差         : {statistics.stdev(frame_times):.2f} ms")
            status = "✅" if avg_fps >= TARGET_FPS * 0.9 else "⚠️"
            print(f"  {status} 目标帧率 {TARGET_FPS}Hz → 实际 {avg_fps:.1f}Hz")
        print("=" * 60)

if __name__ == "__main__":
    main()