# import socket
# import json
# import time

# HOST = "172.30.109.22"
# PORT_CONNECT = 16001
# GROUP = 1  # 默认 Group 1
# TARGET_FPS = 20

# def frc_connect(host, port):
#     """建立 RMI 会话，返回动态端口"""
#     msg = b'{"Communication": "FRC_Connect"}\r\n'
#     with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
#         s.connect((host, port))
#         s.sendall(msg)
#         resp = s.recv(1024)
#     try:
#         data = json.loads(resp.decode())
#         return data.get("Port", 16002)
#     except json.JSONDecodeError:
#         return 16002

# def read_cartesian_position(host, port, group=1):
#     """读取机器人当前笛卡尔位置"""
#     packet = json.dumps({
#         "Command": "FRC_ReadCartesianPosition",
#         "Group": group
#     }) + "\r\n"

#     with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
#         s.connect((host, port))
#         s.sendall(packet.encode())
#         resp = s.recv(2048)

#     try:
#         data = json.loads(resp.decode())
#     except json.JSONDecodeError:
#         return None, "Invalid JSON response"

#     error_id = data.get("ErrorID", -1)
#     if error_id != 0:
#         return None, f"Controller returned error code {error_id}"

#     return {
#         "Position": data.get("Position", {}),
#         "Configuration": data.get("Configuration", {}),
#         "TimeTag": data.get("TimeTag")
#     }, None

# if __name__ == "__main__":
#     dynamic_port = frc_connect(HOST, PORT_CONNECT)
#     interval = 1.0 / TARGET_FPS

#     while True:
#         start_time = time.time()

#         cartesian_data, error = read_cartesian_position(HOST, dynamic_port, GROUP)
#         if error:
#             print("读取失败:", error)
#         else:
#             pos = cartesian_data["Position"]
#             config = cartesian_data["Configuration"]
#             print(f"X:{pos.get('X')} Y:{pos.get('Y')} Z:{pos.get('Z')} W:{pos.get('W')} P:{pos.get('P')} R:{pos.get('R')}")
#             print(f"Configuration: {config}")
#         elapsed = time.time() - start_time
#         sleep_time = max(0, interval - elapsed)
#         if sleep_time == 0:
#             # 请求耗时超过间隔，实际 FPS 会低于目标
#             print(f"Warning: request took longer than interval ({elapsed:.3f}s)")

#         time.sleep(sleep_time)
import socket
import json
import time

HOST = "172.30.109.22"
PORT_CONNECT = 16001
GROUP = 1  # 默认 Group 1
UTOOL = 1  # 您当前使用的工具号，请根据实际情况修改
TARGET_FPS = 20

def frc_connect(host, port):
    """建立 RMI 会话，获取动态分配的端口"""
    msg = b'{"Communication": "FRC_Connect"}\r\n'
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(5.0)
        s.connect((host, port))
        s.sendall(msg)
        resp = s.recv(1024)
    try:
        data = json.loads(resp.decode())
        # ⚠️ 注意：RMI 手册中返回的键名是 "PortNumber"，不是 "Port"
        return data.get("PortNumber", 16002)
    except json.JSONDecodeError:
        return 16002

def set_uframe_utool(sock, uframe, utool, group=1):
    """【核心新增】设置当前的 UFrame 和 UTool"""
    packet = json.dumps({
        "Command": "FRC_SetUFrameUTool",
        "UFrameNumber": int(uframe),
        "UToolNumber": int(utool),
        "Group": int(group)
    }) + "\r\n"
    
    sock.sendall(packet.encode())
    resp = sock.recv(1024)
    data = json.loads(resp.decode())
    if data.get("ErrorID", -1) != 0:
        raise RuntimeError(f"设置坐标系失败: {data}")
    return data

def read_cartesian_position(sock, group=1):
    """读取机器人当前笛卡尔位置"""
    packet = json.dumps({
        "Command": "FRC_ReadCartesianPosition",
        "Group": group
    }) + "\r\n"

    # 直接使用已建立的 socket 发送
    sock.sendall(packet.encode())
    resp = sock.recv(2048)

    try:
        data = json.loads(resp.decode())
    except json.JSONDecodeError:
        return None, "Invalid JSON response"

    error_id = data.get("ErrorID", -1)
    if error_id != 0:
        return None, f"Controller returned error code {error_id}"

    return {
        "Position": data.get("Position", {}),
        "Configuration": data.get("Configuration", {}),
        "TimeTag": data.get("TimeTag")
    }, None

if __name__ == "__main__":
    # 1. 握手获取动态端口
    dynamic_port = frc_connect(HOST, PORT_CONNECT)
    print(f"✅ RMI 握手成功，动态端口: {dynamic_port}")
    
    # 2. 建立长连接用于高频轮询 (不要在 while 循环里反复重连)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as rmi_sock:
        rmi_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) # 关闭 Nagle 算法，降低延迟
        rmi_sock.settimeout(2.0)
        rmi_sock.connect((HOST, dynamic_port))
        
        # ====================================================
        # 3. 强制将机器人的参考坐标系设置为 World 坐标系 (UFrame = 0)
        # ====================================================
        print("🌍 正在切换至 World 坐标系 (UFrame=0)...")
        #set_uframe_utool(rmi_sock, uframe=0, utool=UTOOL, group=GROUP)
        print("✅ 切换成功，开始高速读取 World 坐标系下的 TCP 位姿！")

        interval = 1.0 / TARGET_FPS

        try:
            while True:
                start_time = time.time()

                # 4. 执行读取
                cartesian_data, error = read_cartesian_position(rmi_sock, GROUP)
                if error:
                    print("❌ 读取失败:", error)
                else:
                    pos = cartesian_data["Position"]
                    config = cartesian_data["Configuration"]
                    
                    # 打印坐标和验证当前的 Frame 状态
                    print(f"🌍 World TCP -> X:{pos.get('X'):.3f}  Y:{pos.get('Y'):.3f}  Z:{pos.get('Z'):.3f}  "
                          f"W:{pos.get('W'):.3f}  P:{pos.get('P'):.3f}  R:{pos.get('R'):.3f} | "
                          f"[UF:{config.get('UFrameNumber')} UT:{config.get('UToolNumber')}]")
                
                # 5. 帧率控制
                elapsed = time.time() - start_time
                sleep_time = max(0, interval - elapsed)
                if sleep_time == 0:
                    print(f"⚠️ 警告: 请求耗时 ({elapsed:.3f}s) 超过了目标周期 ({interval:.3f}s)")

                time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\n🛑 用户停止读取。")
            # 退出前安全断开 RMI
            disconnect_msg = json.dumps({"Communication": "FRC_Disconnect"}) + "\r\n"
            rmi_sock.sendall(disconnect_msg.encode())
