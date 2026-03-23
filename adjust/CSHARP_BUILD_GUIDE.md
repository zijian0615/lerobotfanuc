# StreamLinear C# 版本 - 编译和运行指南

## 系统要求

- **.NET 6.0+** （推荐 .NET 7.0 或 .NET 8.0）
- **Windows / macOS / Linux** (跨平台支持)

## 安装 .NET SDK

### macOS
```bash
# 使用 Homebrew
brew install dotnet-sdk
```

### Windows
```
下载：https://dotnet.microsoft.com/en-us/download
或使用 Chocolatey：
choco install dotnet-sdk
```

### Ubuntu/Linux
```bash
sudo apt-get update
sudo apt-get install dotnet-sdk-8.0
```

## 验证安装
```bash
dotnet --version
```

---

## 编译

### 方式1：使用 csproj 文件（推荐）

```bash
# 进入项目目录
cd /Users/zhangzijian/Desktop/fanuc/adjust

# 构建项目
dotnet build

# 发布为独立可执行文件
dotnet publish -c Release -r osx-arm64  # macOS ARM64 (Apple Silicon)
# 或
dotnet publish -c Release -r osx-x64   # macOS Intel

# Windows
# dotnet publish -c Release -r win-x64

# Linux
# dotnet publish -c Release -r linux-x64
```

### 方式2：直接编译和运行
```bash
dotnet run
```

---

## 运行

### 方式1：直接运行
```bash
cd /Users/zhangzijian/Desktop/fanuc/adjust
dotnet run
```

### 方式2：编译后运行
```bash
# 编译
dotnet build

# 运行编译后的可执行文件
./bin/Debug/net6.0/StreamLinear

# 或发布版本
dotnet publish -c Release
./bin/Release/net6.0/publish/StreamLinear
```

### 方式3：作为独立程序（无需.NET运行时）
```bash
dotnet publish -c Release -r osx-arm64 --self-contained

# 运行发布的独立可执行文件
./bin/Release/net6.0/osx-arm64/publish/StreamLinear
```

---

## 配置参数修改

编辑 `StreamLinear.cs` 中的 `Config` 类：

```csharp
public class Config
{
    public const string HOST = "172.30.109.22";        // 机器人IP
    public const int SPEED_MM_S = 200;                 // 速度 (mm/s)
    public const int CNT_VALUE = 0;                    // 终止值
    public const int TARGET_FPS = 50;                  // 目标帧率
    // ... 其他配置 ...
}
```

修改后重新编译：
```bash
dotnet run
```

---

## Python 版本 vs C# 版本对比

| 特性 | Python | C# |
|------|--------|-----|
| **启动时间** | ~2秒 | ~50ms |
| **内存占用** | 50-100 MB | 30-50 MB |
| **性能** | 正常 | ⚡ 更快 |
| **多线程** | GIL限制 | 无限制 |
| **部署** | 需要Python运行时 | 可独立分发 |

---

## 常见问题

### Q: 编译时出错 "TargetFramework not found"
**A:** 确保已安装 .NET SDK：
```bash
dotnet --version
```

### Q: 找不到 JsonDocument
**A:** 已包含在 `System.Text.Json` 中，无需额外安装

### Q: 程序运行很慢
**A:** 使用发布版本而非调试版本：
```bash
dotnet publish -c Release
./bin/Release/net6.0/publish/StreamLinear
```

### Q: 文件路径问题
**A:** 确保 LOG_FILE 路径相对于程序运行目录正确：
```csharp
public const string LOG_FILE = "../recordings/teleop_log_1772263304.jsonl";
```

---

## 预期输出

```
Loaded 228 frames after filtering
🔌 Connecting to Fanuc RMI...
FRC_Connect: {...}
✅ TCP socket optimized for low-latency
FRC_Initialize: OK
✅ Async sender initialized, ACK listener started
📤 Sending seq=1: target=(x, y, z, w, p, r), speed=200, cnt=0
[   1/ 228] seq=   1 dt= 20.0ms fps= 50.0Hz
...
============================================================
📊 性能统计报告
============================================================
总发送帧数: 228
平均帧间隔: 20.05 ms
🎯 平均帧率: 49.9 Hz
✅ 达到目标帧率 50Hz!
============================================================
```

---

## 与 Unity 集成

如果要在 Unity 中使用此代码：

1. **复制核心类到 Unity**（Assets/Scripts）
2. **使用 System.Text.Json** 替代 Newtonsoft.Json
3. **调整命名空间** 以符合 Unity 约定
4. **在 MonoBehaviour 中调用**

```csharp
public class RobotController : MonoBehaviour
{
    private AsyncStreamingSender sender;
    
    void Start()
    {
        sender = new AsyncStreamingSender();
        sender.Connect(Config.HOST, Config.PORT_CONNECT);
    }
    
    void Update()
    {
        // 定期发送机器人指令
        if (Input.GetKeyDown(KeyCode.S))
        {
            double[] target = GetCurrentRobotTarget();
            sender.SendAsync(seqId++, target);
        }
    }
    
    void OnDestroy()
    {
        sender?.Disconnect();
    }
}
```

---

## 性能优化建议

1. **使用发布（Release）版本**（比调试版快 5-10 倍）
2. **启用 ReadyToRun**：
   ```bash
   dotnet publish -c Release -p:PublishReadyToRun=true
   ```
3. **启用分层编译**：
   ```bash
   dotnet publish -c Release -p:PublishTieredCompilation=true
   ```

---

## 调试

### Visual Studio Code
```bash
code .
# 按 F5 启动调试
```

### Visual Studio（Windows）
```bash
# 打开项目
open StreamLinear.csproj
# 按 F5 启动调试
```

---

## 故障排除

如遇到问题，运行调试版本查看详细输出：
```bash
dotnet run --configuration Debug
```

查看ACK错误详细信息（已在代码中集成）

