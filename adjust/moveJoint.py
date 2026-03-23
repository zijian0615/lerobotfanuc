import socket
import json
import time

HOST = "172.30.109.22"
PORT_CONNECT = 16001
GROUP = 1

def frc_connect(host, port):
    """建立 RMI 基础连接并获取动态端口"""
    msg = b'{"Communication": "FRC_Connect"}\r\n'
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((host, port))
        s.sendall(msg)
        resp = s.recv(1024)
    print("FRC_Connect:", repr(resp))
    data = json.loads(resp.decode())
    if data.get("ErrorID", -1) != 0:
        raise RuntimeError(f"FRC_Connect failed: {data}")
    return data.get("PortNumber")

def rmi_abort(sock):
    """发送 FRC_Abort 清空之前的 RMI_MOVE 程序"""
    abort_msg = json.dumps({"Command": "FRC_Abort"}) + "\r\n"
    sock.sendall(abort_msg.encode())
    resp = sock.recv(1024)
    print("FRC_Abort response:", resp.decode())
    data = json.loads(resp.decode())
    if data.get("ErrorID", -1) != 0:
        print("⚠️ Abort returned ErrorID:", data.get("ErrorID"))

def rmi_initialize(host, port, group=1):
    """初始化 RMI_MOVE，失败自动检查并 Abort"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((host, port))
    s.settimeout(5.0)

    init_msg = json.dumps({
        "Command": "FRC_Initialize",
        "GroupMask": group
    }) + "\r\n"

    s.sendall(init_msg.encode())
    resp = s.recv(1024)
    print("FRC_Initialize:", repr(resp))
    data = json.loads(resp.decode())

    # if data.get("ErrorID") == 2556943:  # Invalid Controller State
    #     print("⚠️ Controller busy, sending Abort and retrying...")
    #     rmi_abort(s)
    #     time.sleep(0.5)
    #     s.sendall(init_msg.encode())
    #     resp = s.recv(1024)
    #     data = json.loads(resp.decode())
    #     print("Retry FRC_Initialize:", data)

    # if data.get("ErrorID") not in (0, 7015):
    #     raise RuntimeError(f"FRC_Initialize failed: {data}")
    return s
def send_joint_motion(
        sock,
        sequence_id,
        j1, j2, j3, j4, j5, j6,
        speed_type="Percent",
        speed=10,
        term_type="FINE",
        term_value=0):
    
    # 按照 FANUC RMI 协议构造 Joint 报文
    packet = {
        "Instruction": "FRC_JointMotionJRep",
        "SequenceID": int(sequence_id),
        "JointAngles": {
            "J1": float(j1),
            "J2": float(j2),
            "J3": float(j3),
            "J4": float(j4),
            "J5": float(j5),
            "J6": float(j6)
            # "J7": 0.0,
            # "J8": 0.0,
            # "J9": 0.0
        },
        "SpeedType": str(speed_type),
        "Speed": int(speed),
        "TermType": str(term_type),
        "TermValue": int(term_value)
    }

    # 必须添加 \r\n 结束符
    msg = (json.dumps(packet) + "\r\n").encode()
    
    print("\n🚀 Sending Joint Motion:")
    print(json.dumps(packet, indent=2))

    sock.sendall(msg)
    resp = sock.recv(1024)
    
    print("Response:", resp.decode())
    return json.loads(resp.decode())

if __name__ == "__main__":
    rmi_sock = None
    try:
        # 1. 连接并初始化 (使用你之前的函数)
        dynamic_port = frc_connect(HOST, PORT_CONNECT)
        print(f"Dynamic port: {dynamic_port}")
        rmi_sock = rmi_initialize(HOST, dynamic_port, GROUP)

        # 2. 调用关节运动
        # 使用你提供的接近当前状态的数据：
        # J1:-0.618, J2:30.882, J3:-22.106, J4:-5.338, J5:-48.715, J6:98.509
        resp = send_joint_motion(
            rmi_sock,
            sequence_id=1001,
            j1=-0.618,
            j2=30.882,
            j3=-22.106,
            j4=-5.338,
            j5=-48.715,
            j6=98.200,    
            speed_type="Percent",
            speed=10,      # 关节运动常用 Percent，10% 比较安全
            term_type="FINE",
            term_value=0
        )

        # 3. 结果判断
        if resp:
            err = resp.get("ErrorID", -1)
            if err == 0:
                print("✅ Joint motion executed successfully")
            else:
                print(f"❌ ErrorID: {err} (hex: {hex(err)})")
                print("报错提示：如果仍报 Invalid Position Data，请检查 J1-J6 是否在机器人限位内。")

    except Exception as e:
        print(f"💥 发生异常: {e}")
        if rmi_sock:
            try:
                rmi_abort(rmi_sock)
            except:
                pass
    finally:
        if rmi_sock:
            rmi_sock.close()
            print("🔌 连接已关闭")