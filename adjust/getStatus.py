import socket
import json

HOST = "172.30.109.22"
PORT_CONNECT = 16001
GROUP = 1

def frc_connect(host, port):
    """建立基础 RMI 会话并返回动态端口"""
    msg = b'{"Communication": "FRC_Connect"}\r\n'
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((host, port))
        s.sendall(msg)
        resp = s.recv(1024)
    data = json.loads(resp.decode())
    return data.get("PortNumber")

def rmi_session(host, port, group=1):
    """初始化 RMI_MOVE TP 程序"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((host, port))
    s.settimeout(5.0)
    init_msg = json.dumps({
        "Command": "FRC_Initialize",
        "GroupMask": group
    }) + "\r\n"
    s.sendall(init_msg.encode())
    resp = s.recv(1024)
    data = json.loads(resp.decode())
    if data.get("ErrorID") not in (0, 7015):
        raise RuntimeError(f"Initialize failed: {data}")
    return s

def get_current_tool_frame(s):
    """读取当前 TP Tool/Frame 和姿态 Orientation"""
    packet = {"Command": "FRC_GetUFrameUTool"}
    s.sendall((json.dumps(packet) + "\r\n").encode())
    resp = s.recv(1024)
    data = json.loads(resp.decode())
    # 返回 Tool/Frame 编号 + Orientation 参数
    config = {
        "UToolNumber": data.get("UToolNumber", 1),
        "UFrameNumber": data.get("UFrameNumber", 0),
        "Front": data.get("Front", 1),
        "Up": data.get("Up", 1),
        "Left": data.get("Left", 0),
        "Flip": data.get("Flip", 0),
        "Turn4": data.get("Turn4", 0),
        "Turn5": data.get("Turn5", 0),
        "Turn6": data.get("Turn6", 0)
    }
    return config

def send_linear_motion(s, sequence_id, x, y, z, w, p, r, config, speed_type="mmSec", speed=100, term_type="FINE", term_value=0):
    """发送 Linear Motion 指令"""
    packet = {
        "Instruction": "FRC_LinearMotion",
        "SequenceID": sequence_id,
        "Configuration": config,
        "Position": {
            "X": float(x),
            "Y": float(y),
            "Z": float(z),
            "W": float(w),
            "P": float(p),
            "R": float(r),
            "Ext1": 0.0,
            "Ext2": 0.0,
            "Ext3": 0.0
        },
        "SpeedType": str(speed_type),
        "Speed": int(speed),
        "TermType": str(term_type),
        "TermValue": int(term_value)
    }
    s.sendall((json.dumps(packet) + "\r\n").encode())
    resp = s.recv(1024)
    return json.loads(resp.decode())

if __name__ == "__main__":
    dynamic_port = frc_connect(HOST, PORT_CONNECT)
    print(f"Dynamic port: {dynamic_port}")

    rmi_sock = rmi_session(HOST, dynamic_port, GROUP)

    try:
        # 获取当前 Tool/Frame 配置
        config = get_current_tool_frame(rmi_sock)
        print("Current Tool/Frame Configuration:", config)

        # 发送线性运动
        resp = send_linear_motion(
            rmi_sock,
            sequence_id=1,
            x=-384.052,
            y=507.995,
            z=159.512,
            w=171.317,
            p=-2.911,
            r=144.527,
            config=config,
            speed_type="mmSec",
            speed=150,
            term_type="CNT",
            term_value=100
        )

        err = resp.get("ErrorID", -1)
        if err == 0:
            print("✅ Linear Motion 成功执行！")
        else:
            print(f"❌ ErrorID: {err} (hex: {hex(err)})")
            print("请在示教器 ALARM 页面查看详细报警信息")

    finally:
        rmi_sock.close()