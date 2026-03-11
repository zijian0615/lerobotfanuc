import socket
import json
 
HOST = "0.0.0.0"
PORT = 9001
 
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((HOST, PORT))
print(f"监听 {PORT} 端口，等待 Quest 数据...\n")
 
# kQFrame 验证时只打印一次
kqframe_printed = set()
 
while True:
    data, addr = sock.recvfrom(4096)
    try:
        msg = json.loads(data.decode())
        label = msg.get("label", "?")
        ex = msg.get("ex", 0)
        ey = msg.get("ey", 0)
        ez = msg.get("ez", 0)
 
        # kQFrame 验证包只打印一次
        if label.startswith("kQFrame"):
            if label not in kqframe_printed:
                kqframe_printed.add(label)
                print(f"[kQFrame验证] {label:25s}  ex={ex:7.2f}  ey={ey:7.2f}  ez={ez:7.2f}")
                if label == "kQFrame_UnityX90":
                    ok = abs(ey - 90) < 2 and abs(ex) < 2 and abs(ez) < 2
                    print(f"  期望 ey≈90, ex≈0, ez≈0  ->  {'✅ OK' if ok else '❌ 错误'}")
                elif label == "kQFrame_UnityY90":
                    ok = abs(ez - 90) < 2 and abs(ex) < 2 and abs(ey) < 2
                    print(f"  期望 ez≈90, ex≈0, ey≈0  ->  {'✅ OK' if ok else '❌ 错误'}")
                elif label == "kQFrame_UnityZ90":
                    ok = abs(ex - 270) < 2 and abs(ey) < 2 and abs(ez) < 2
                    print(f"  期望 ex≈270, ey≈0, ez≈0 ->  {'✅ OK' if ok else '❌ 错误'}")
                print()
 
        # 实时旋转增量：每帧打印
        elif label in ("deltaLocal", "deltaFanuc"):
            print(f"[{label:12s}]  ex={ex:7.2f}  ey={ey:7.2f}  ez={ez:7.2f}")
 
    except Exception as e:
        print(f"解析错误: {e}")
        