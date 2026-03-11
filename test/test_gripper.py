import socket
import json
import time

HOST = "172.30.109.22"
PORT_CONNECT = 16001
GROUP = 1

# ==========================================
# 气动夹爪端口配置 (双控)
# ==========================================
RO_PORT_OPEN = 3   # 控制打开的端口 (RO[3])
RO_PORT_CLOSE = 4  # 控制关闭的端口 (RO[4])

def frc_connect(host, port):
    msg = b'{"Communication": "FRC_Connect"}\r\n'
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((host, port))
        s.sendall(msg)
        resp = s.recv(1024)
    data = json.loads(resp.decode())
    if data.get("ErrorID", -1) != 0:
        raise RuntimeError(f"FRC_Connect failed: {data}")
    return data.get("PortNumber")

def rmi_abort(sock):
    abort_msg = json.dumps({"Command": "FRC_Abort"}) + "\r\n"
    sock.sendall(abort_msg.encode())
    sock.recv(1024)

def rmi_initialize(host, port, group=1):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((host, port))
    s.settimeout(5.0)
    init_msg = json.dumps({"Command": "FRC_Initialize", "GroupMask": group}) + "\r\n"
    s.sendall(init_msg.encode())
    s.recv(1024)
    return s

def get_current_pose(sock):
    """动态读取机器人的当前位置和配置"""
    msg = json.dumps({"Command": "FRC_ReadCartesianPosition"}) + "\r\n"
    sock.sendall(msg.encode())
    resp = sock.recv(2048)
    data = json.loads(resp.decode())
    if data.get("ErrorID", -1) != 0:
        raise RuntimeError(f"获取位置失败: {data}")
    return data.get("Configuration"), data.get("Position")

def send_in_place_motion_with_lcb(sock, sequence_id, config, pos, port_number, port_value):
    """发送原地运动指令，并附带 LCB IO操作"""
    packet = {
        "Instruction": "FRC_LinearMotion",
        "SequenceID": int(sequence_id),
        "Configuration": config,   # 直接使用读取到的当前配置
        "Position": pos,           # 直接使用读取到的当前坐标
        "SpeedType": "mmSec",
        "Speed": 100,              # 速度无所谓，因为距离是0
        "TermType": "FINE",        # 原地触发必须用 FINE
        "TermValue": 0,
        
        # LCB 触发逻辑 (到达后10ms触发)
        "LCBType": "TA",
        "LCBValue": 10,
        "PortType": 2,             # 2 = ROUT
        "PortNumber": int(port_number),
        "PortValue": str(port_value)
    }

    msg = (json.dumps(packet) + "\r\n").encode()
    print(f"[{sequence_id}] 原地触发 -> RO[{port_number}] = {port_value}")
    sock.sendall(msg)
    
    resp = sock.recv(4096)
    data = json.loads(resp.decode())
    if data.get("ErrorID", -1) != 0:
        print(f"❌ 指令执行报错: {data}")
    return data

if __name__ == "__main__":
    dynamic_port = frc_connect(HOST, PORT_CONNECT)
    rmi_sock = rmi_initialize(HOST, dynamic_port, GROUP)

    try:
        # 1. 动态获取当前真实的坐标
        print("\n获取机器人当前坐标中...")
        current_config, current_pos = get_current_pose(rmi_sock)
        print(f"✅ 当前位置: Z={current_pos['Z']:.2f}")

        seq = 1

        print("\n" + "="*50)
        print("▶️ 动作 1：打开夹爪")
        print("="*50)
        # 双控安全逻辑：先关 CLOSE 线圈，再开 OPEN 线圈
        send_in_place_motion_with_lcb(rmi_sock, seq, current_config, current_pos, RO_PORT_CLOSE, "OFF")
        seq += 1
        send_in_place_motion_with_lcb(rmi_sock, seq, current_config, current_pos, RO_PORT_OPEN, "ON")
        seq += 1
        
        print("\n⏳ 夹爪已打开，等待 3 秒...")
        time.sleep(3.0)

        print("\n" + "="*50)
        print("▶️ 动作 2：关闭夹爪")
        print("="*50)
        # 双控安全逻辑：先关 OPEN 线圈，再开 CLOSE 线圈
        send_in_place_motion_with_lcb(rmi_sock, seq, current_config, current_pos, RO_PORT_OPEN, "OFF")
        seq += 1
        send_in_place_motion_with_lcb(rmi_sock, seq, current_config, current_pos, RO_PORT_CLOSE, "ON")
        seq += 1

        print("\n✅ 原地测试流程执行完毕！")

    except Exception as e:
        print(f"💥 发生异常: {e}")
    finally:
        print("\n🧹 清理：中止 RMI 程序并断开...")
        if rmi_sock:
            try:
                rmi_abort(rmi_sock)
                rmi_sock.close()
            except:
                pass