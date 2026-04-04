#!/usr/bin/env python3
"""
FANUC 控制器健康检查工具
检查网络连接、RMI 服务、设备状态等
"""

import socket
import json
import subprocess
import sys
import time
from datetime import datetime

HOST = "172.30.109.22"
PORT_CONNECT = 16001

def check_network_connectivity():
    """检查网络连通性"""
    print("\n" + "="*60)
    print("🔍 1. 网络连通性检查")
    print("="*60)
    
    # ARP 检查
    print("\n📡 检查 ARP 缓存...")
    result = subprocess.run(["arp", "-a"], capture_output=True, text=True)
    if "172.30.109.22" in result.stdout:
        print(f"✅ FANUC 设备在 ARP 缓存中")
        for line in result.stdout.split("\n"):
            if "172.30.109.22" in line:
                print(f"   {line}")
    else:
        print(f"❌ FANUC 设备不在 ARP 缓存中（可能已掉线）")
    
    # Traceroute 检查
    print("\n📊 检查路由...")
    result = subprocess.run(["traceroute", "-m", "5", HOST], 
                          capture_output=True, text=True, timeout=10)
    if "172.30.109.22" in result.stdout:
        print(f"✅ 可以到达 FANUC 设备")
    else:
        print(f"❌ 无法路由到 FANUC 设备")

def check_tcp_port():
    """检查 TCP 端口连接"""
    print("\n" + "="*60)
    print("🔍 2. TCP 端口检查")
    print("="*60)
    
    print(f"\n尝试连接 {HOST}:{PORT_CONNECT}...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex((HOST, PORT_CONNECT))
        
        if result == 0:
            print(f"✅ 端口 {PORT_CONNECT} 开放 - 可以连接")
            sock.close()
            return True
        else:
            sock.close()
            error_msgs = {
                61: "Connection refused - 端口关闭或拒绝连接",
                65: "No route to host - 无路由到达主机",
                111: "Connection refused - 连接被拒绝",
                110: "Connection timed out - 连接超时"
            }
            msg = error_msgs.get(result, f"未知错误 {result}")
            print(f"❌ 连接失败: {msg}")
            print(f"   错误码: {result}")
            return False
    except socket.timeout:
        print(f"❌ 连接超时")
        return False
    except Exception as e:
        print(f"❌ 异常: {e}")
        return False

def try_frc_connect():
    """尝试 FRC 握手"""
    print("\n" + "="*60)
    print("🔍 3. FRC 握手检查")
    print("="*60)
    
    print(f"\n发送 FRC_Connect 请求...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((HOST, PORT_CONNECT))
        
        msg = b'{"Communication": "FRC_Connect"}\r\n'
        print(f"   请求: {msg.decode().strip()}")
        
        sock.sendall(msg)
        resp = sock.recv(1024)
        
        print(f"   响应: {resp.decode().strip()}")
        
        try:
            data = json.loads(resp.decode())
            error_id = data.get("ErrorID", -1)
            
            if error_id == 0:
                port = data.get("PortNumber")
                print(f"✅ FRC_Connect 成功！动态端口: {port}")
                sock.close()
                return True, port
            else:
                print(f"❌ FRC_Connect 返回错误: ErrorID={error_id}")
                print(f"   错误码详情: {data}")
                sock.close()
                return False, None
        except json.JSONDecodeError:
            print(f"❌ 响应格式无效（非 JSON）")
            sock.close()
            return False, None
            
    except socket.timeout:
        print(f"❌ FRC_Connect 超时")
        return False, None
    except ConnectionRefusedError:
        print(f"❌ 连接被拒绝 - FANUC RMI 服务可能未启动")
        return False, None
    except Exception as e:
        print(f"❌ 异常: {e}")
        return False, None

def try_rmi_initialize(port):
    """尝试 RMI 初始化"""
    print("\n" + "="*60)
    print("🔍 4. RMI 初始化检查")
    print("="*60)
    
    print(f"\n发送 FRC_Initialize 请求...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((HOST, port))
        
        init_msg = json.dumps({
            "Command": "FRC_Initialize",
            "GroupMask": 1
        }) + "\r\n"
        
        print(f"   请求: {init_msg.strip()}")
        sock.sendall(init_msg.encode())
        
        resp = sock.recv(1024)
        print(f"   响应: {resp.decode().strip()}")
        
        try:
            data = json.loads(resp.decode())
            error_id = data.get("ErrorID", -1)
            
            if error_id in (0, 7015):
                print(f"✅ RMI 初始化成功！")
                sock.close()
                return True
            else:
                print(f"❌ RMI 初始化返回错误: ErrorID={error_id}")
                error_msg = {
                    2556943: "Invalid Controller State - 控制器状态无效",
                    2556942: "Group in use - 组已被占用",
                    2556941: "Invalid Group - 无效的组号"
                }
                if error_id in error_msg:
                    print(f"   含义: {error_msg[error_id]}")
                print(f"   完整响应: {data}")
                sock.close()
                return False
        except json.JSONDecodeError:
            print(f"❌ 响应格式无效")
            sock.close()
            return False
            
    except Exception as e:
        print(f"❌ 异常: {e}")
        return False

def print_summary():
    """打印诊断摘要"""
    print("\n" + "="*60)
    print("📋 诊断摘要与建议")
    print("="*60)
    
    print("""
可能的问题与解决方案：

1️⃣  如果网络连不通 (No route to host)
   → 检查 FANUC 控制器是否通电
   → 检查网线是否正确插入
   → 检查交换机/路由器是否正常
   → 重启网络: sudo ifconfig en11 down && up

2️⃣  如果 TCP 连接被拒绝 (Connection refused)
   → FANUC RMI 服务未启动
   → 在示教器上: Menu > Network Settings > RMI > Enable
   → 检查防火墙设置
   → 重启 FANUC 控制器

3️⃣  如果 FRC_Connect 返回错误
   → 查看错误代码含义
   → 在示教器 ALARM 页面查看详细信息
   → 可能需要联系 FANUC 技术支持

4️⃣  如果 RMI 初始化返回 "Invalid Controller State"
   → 可能是上一次连接未正确断开
   → 在示教器重启 RMI 服务
   → 清空所有待处理的 RMI 任务

✅ 快速恢复步骤:
   1. 示教器上按 [EMERGENCY STOP] 检查
   2. Menu > System Information 查看网络状态
   3. 重启 FANUC 控制器（关闭 -> 等 30 秒 -> 打开）
   4. 重新运行本诊断脚本
""")

def main():
    print("\n" + "="*60)
    print("🤖 FANUC 控制器健康检查工具")
    print("="*60)
    print(f"目标: {HOST}:{PORT_CONNECT}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 1. 网络检查
    check_network_connectivity()
    
    time.sleep(1)
    
    # 2. TCP 检查
    tcp_ok = check_tcp_port()
    
    if not tcp_ok:
        print("\n❌ TCP 连接失败，无法继续检查")
        print_summary()
        return 1
    
    time.sleep(1)
    
    # 3. FRC 握手
    frc_ok, dynamic_port = try_frc_connect()
    
    if not frc_ok:
        print_summary()
        return 1
    
    time.sleep(1)
    
    # 4. RMI 初始化
    rmi_ok = try_rmi_initialize(dynamic_port)
    
    print("\n" + "="*60)
    if rmi_ok:
        print("✅ 所有检查通过！FANUC 控制器正常运行")
        print("   现在可以执行运动命令")
    else:
        print("⚠️  部分检查失败，请参考建议")
    print("="*60 + "\n")
    
    print_summary()
    
    return 0 if rmi_ok else 1

if __name__ == "__main__":
    sys.exit(main())
