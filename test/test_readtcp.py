"""
test_frc_state_reader.py
========================
独立测试 FRCStateReader，不依赖项目其他模块。

用法：
    python test_frc_state_reader.py --host 192.168.1.100
    python test_frc_state_reader.py --host 192.168.1.100 --port 16001 --count 10 --verbose
"""

import socket
import json
import time
import argparse
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("test_frc_state_reader")


# ─── 最小化依赖：内联 LineSocket ──────────────────────────────────────────────
class LineSocket:
    """按行读取 TCP 数据（\\r\\n 或 \\n 分隔）。"""

    def __init__(self, sock: socket.socket, bufsize: int = 4096):
        self._sock   = sock
        self._bufsize = bufsize
        self._buf    = b""

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def read_line(self) -> bytes:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(self._bufsize)
            if not chunk:
                raise ConnectionError("Connection closed by remote")
            self._buf += chunk
        idx = self._buf.index(b"\n")
        line = self._buf[:idx].rstrip(b"\r")
        self._buf = self._buf[idx + 1:]
        return line

    def read_json(self) -> dict:
        line = self.read_line()
        logger.debug(f"  ← RAW: {line.decode(errors='replace')}")
        return json.loads(line)


def optimize_socket(sock: socket.socket) -> None:
    import socket as _s
    sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)
    try:
        sock.setsockopt(_s.SOL_SOCKET, _s.SO_KEEPALIVE, 1)
    except Exception:
        pass


# ─── Step 1: FRC_Connect → 获取动态端口 ──────────────────────────────────────
def frc_connect(host: str, port: int = 16001, timeout: float = 5.0) -> int:
    """
    向 FANUC 控制器发送 FRC_Connect，拿到动态工作端口。
    """
    logger.info(f"[1] FRC_Connect → {host}:{port}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    optimize_socket(sock)
    sock.settimeout(timeout)
    sock.connect((host, port))
    ls = LineSocket(sock)

    req = json.dumps({"Communication": "FRC_Connect"}) + "\r\n"
    logger.debug(f"  → {req.strip()}")
    ls.sendall(req.encode())

    resp = ls.read_json()
    logger.info(f"  FRC_Connect response: {resp}")

    dynamic_port = resp.get("PortNumber") or resp.get("Port")
    if dynamic_port is None:
        raise RuntimeError(f"FRC_Connect: no 'PortNumber' in response: {resp}")

    sock.close()
    logger.info(f"  Dynamic port = {dynamic_port}")
    return int(dynamic_port)


# ─── Step 2: 连接动态端口（不发 FRC_Initialize）────────────────────────────────
def connect_dynamic(host: str, dynamic_port: int, timeout: float = 5.0):
    logger.info(f"[2] Connecting to dynamic port {host}:{dynamic_port}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    optimize_socket(sock)
    sock.settimeout(timeout)
    sock.connect((host, dynamic_port))
    ls = LineSocket(sock)
    logger.info("  Connected.")
    return sock, ls


# ─── Step 3: 读一次 FRC_ReadCartesianPosition ─────────────────────────────────
def read_cartesian(ls: LineSocket) -> dict:
    req = json.dumps({"Command": "FRC_ReadCartesianPosition", "Group": 1}) + "\r\n"
    logger.debug(f"  → {req.strip()}")
    ls.sendall(req.encode())
    resp = ls.read_json()
    return resp


# ─── 备用：试发一次 FRC_Initialize 再读 ──────────────────────────────────────
def try_with_initialize(host: str, dynamic_port: int, timeout: float = 5.0) -> dict:
    """
    有些固件版本要求先发 FRC_Initialize 才能读取。
    这里单独测一下。
    """
    logger.info("[ALT] 尝试先发 FRC_Initialize ...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    optimize_socket(sock)
    sock.settimeout(timeout)
    sock.connect((host, dynamic_port))
    ls = LineSocket(sock)

    # FRC_Initialize
    init_req = json.dumps({
        "Communication": "FRC_Initialize",
        "ControlGroup":  [1],
        "ControlType":   "MONITOR",     # 只读，不需要 MOTION 权限
    }) + "\r\n"
    logger.debug(f"  → {init_req.strip()}")
    ls.sendall(init_req.encode())
    init_resp = ls.read_json()
    logger.info(f"  FRC_Initialize response: {init_resp}")

    # ReadCartesian
    resp = read_cartesian(ls)
    sock.close()
    return resp


# ─── 主测试流程 ───────────────────────────────────────────────────────────────
def run_test(host: str, port: int, count: int, interval: float, verbose: bool):
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    print("\n" + "="*60)
    print(f"  FRCStateReader 独立测试")
    print(f"  Target : {host}:{port}")
    print(f"  Samples: {count}  interval={interval}s")
    print("="*60 + "\n")

    # ── 1. FRC_Connect ────────────────────────────────────────────────────────
    try:
        dynamic_port = frc_connect(host, port)
    except Exception as e:
        logger.error(f"❌ FRC_Connect failed: {e}")
        logger.error("   → 检查：控制器 IP/端口是否正确？RMI 任务是否启动？防火墙？")
        return

    # ── 2. 连动态端口 ─────────────────────────────────────────────────────────
    try:
        sock, ls = connect_dynamic(host, dynamic_port)
    except Exception as e:
        logger.error(f"❌ connect_dynamic failed: {e}")
        return

    # ── 3. 连续读取 ──────────────────────────────────────────────────────────
    print(f"\n[3] 开始读取 {count} 次 FRC_ReadCartesianPosition ...\n")
    success = 0
    for i in range(count):
        t0 = time.perf_counter()
        try:
            resp = read_cartesian(ls)
            rtt  = (time.perf_counter() - t0) * 1000  # ms

            err = resp.get("ErrorID", -1)
            if err == 0:
                pos = resp.get("Position", {})
                x, y, z = pos.get("X"), pos.get("Y"), pos.get("Z")
                w, p, r = pos.get("W"), pos.get("P"), pos.get("R")
                print(f"  [{i+1:3d}] ✅ RTT={rtt:6.1f}ms  "
                      f"X={x:8.2f}  Y={y:8.2f}  Z={z:8.2f}  "
                      f"W={w:7.2f}  P={p:7.2f}  R={r:7.2f}")
                success += 1
            else:
                print(f"  [{i+1:3d}] ⚠️  ErrorID={err}  全响应={resp}")

        except socket.timeout:
            print(f"  [{i+1:3d}] ❌ 读取超时（>500ms）")
        except Exception as e:
            print(f"  [{i+1:3d}] ❌ 异常: {e}")

        if i < count - 1:
            time.sleep(interval)

    sock.close()

    print(f"\n{'='*60}")
    print(f"  结果: {success}/{count} 成功")

    # ── 4. 如果全部失败，试试先发 FRC_Initialize ──────────────────────────────
    if success == 0:
        print("\n[4] 全部失败，尝试备用方案：先发 FRC_Initialize 再读...\n")
        try:
            dport2 = frc_connect(host, port)   # 重新拿一个端口
            resp   = try_with_initialize(host, dport2)
            err    = resp.get("ErrorID", -1)
            if err == 0:
                pos = resp.get("Position", {})
                print(f"  ✅ FRC_Initialize 方案成功！Position={pos}")
                print("  → FRCStateReader 需要加上 FRC_Initialize 步骤")
            else:
                print(f"  ❌ 仍然失败，ErrorID={err}，全响应={resp}")
                print("  → 请检查控制器侧 RMI 日志，或确认 Command 名称拼写")
        except Exception as e:
            print(f"  ❌ 备用方案异常: {e}")

    print("="*60 + "\n")


# ─── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone FRCStateReader test")
    parser.add_argument("--host",     required=True,       help="FANUC controller IP")
    parser.add_argument("--port",     type=int, default=16001, help="FRC connect port (default 16001)")
    parser.add_argument("--count",    type=int, default=5,    help="Number of reads (default 5)")
    parser.add_argument("--interval", type=float, default=0.2, help="Interval between reads in seconds (default 0.2)")
    parser.add_argument("--verbose",  action="store_true",    help="Enable DEBUG logging")
    args = parser.parse_args()

    run_test(
        host     = args.host,
        port     = args.port,
        count    = args.count,
        interval = args.interval,
        verbose  = args.verbose,
    )