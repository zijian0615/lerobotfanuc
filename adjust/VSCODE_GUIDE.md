# VS Code C# 开发指南

## **1. 前置准备**

### **A. 安装 .NET SDK**

**macOS:**
```bash
brew install dotnet-sdk
dotnet --version  # 验证
```

**Windows:**
- 下载：https://dotnet.microsoft.com/en-us/download
- 或使用 Chocolatey：`choco install dotnet-sdk`

### **B. 安装 VS Code 扩展**

1. **打开 VS Code**
2. **按 `Cmd+Shift+X` 打开扩展市场**
3. **搜索并安装：**
   - ✅ **C# Dev Kit** (ms-dotnettools.csharp)
   - ✅ **.NET Runtime** (ms-dotnettools.vscode-dotnet-runtime)
   - ✅ **C#** (ms-dotnettools.csharp)

或终端一键安装：
```bash
code --install-extension ms-dotnettools.csharp
code --install-extension ms-dotnettools.vscode-dotnet-runtime
```

---

## **2. 打开项目**

```bash
# 进入项目目录
cd /Users/zhangzijian/Desktop/fanuc/adjust

# 用VS Code打开
code .
```

---

## **3. 编译和运行方式**

### **方式 A：使用快捷键（推荐）**

#### **编译调试版本**
```
按 Ctrl+Shift+B（或 Cmd+Shift+B on Mac）
选择 "build"
```

#### **运行程序**
```
按 F5
选择 "StreamLinear" → 按 Debug
```

#### **运行发布版本**
```
按 F5
选择 "StreamLinear (Release)" → 按 Debug
```

---

### **方式 B：使用命令面板**

1. **按 `Cmd+Shift+P` 打开命令面板**
2. **输入任务名称执行：**

| 任务 | 输入 | 功能 |
|------|------|------|
| 编译调试版 | `Tasks: Run Task` → `build` | 编译代码 |
| 运行调试版 | `Tasks: Run Task` → `run` | 直接运行 |
| 编译发布版 | `Tasks: Run Task` → `build-release` | 优化编译 |
| 运行发布版 | `Tasks: Run Task` → `run-release` | 快速运行 |
| 清理构建 | `Tasks: Run Task` → `clean` | 删除中间文件 |
| 发布独立程序 | `Tasks: Run Task` → `publish` | 生成可执行文件 |

---

### **方式 C：集成终端直接命令**

1. **按 `` Ctrl+` `` 打开VS Code内置终端**
2. **输入命令：**

```bash
# 编译
dotnet build

# 运行
dotnet run

# 调试运行
dotnet build && dotnet bin/Debug/net6.0/StreamLinear.dll

# 发布
dotnet publish -c Release -r osx-arm64 --self-contained
```

---

## **4. 调试技巧**

### **设置断点**
1. 在代码行号左边点击 → 出现红点
2. 按 F5 启动调试器
3. 程序会在断点处暂停

### **查看变量值**
- 调试时鼠标悬停在变量上查看值
- 或在 Debug Console 中输入变量名

### **单步执行**
- `F10` - 单步执行（跳过函数内部）
- `F11` - 单步进入（进入函数内部）
- `Shift+F11` - 单步退出（退出当前函数）
- `F6` - 继续执行

---

## **5. VS Code 界面说明**

### **左侧活动栏**
```
📁 浏览器    - 查看文件结构
🔍 搜索      - 搜索代码
📋 源代码管理 - Git管理
🐛 运行和调试 - 调试工具
📦 扩展      - 扩展管理
```

### **调试视图（F5 后）**
```
变量 (Variables)    - 查看当前变量值
监视 (Watch)        - 添加监视表达式
调用堆栈 (Call Stack) - 查看调用链
断点 (Breakpoints)  - 管理断点
```

---

## **6. 快速参考快捷键**

| 快捷键 | 功能 |
|--------|------|
| `F5` | 开始调试 |
| `Shift+F5` | 停止调试 |
| `Ctrl+Shift+B` | 运行构建任务 |
| `Cmd+K Cmd+M` | 切换集成终端焦点 |
| `Cmd+,` | 打开设置 |
| `Cmd+Shift+P` | 命令面板 |
| `Ctrl+``  | 打开/关闭终端 |
| `Cmd+L` | 清空终端 |

---

## **7. 完整的编译运行流程**

### **第一次运行**

```
1. 打开项目：code .
   ↓
2. 等待 C# 扩展加载完成（右下角显示"准备就绪"）
   ↓
3. 按 Cmd+Shift+B 编译
   ↓
4. 如果编译成功，按 F5 运行
   ↓
5. 在集成终端查看输出
```

### **后续运行**

```
只需按 F5 → 自动编译并运行
```

---

## **8. 常见问题**

### **Q: "dotnet not found"**
**A:** 重装 .NET SDK 并重启 VS Code

```bash
# 验证安装
dotnet --version
# 重启 VS Code
code .
```

### **Q: IntelliSense 不工作**
**A:** 
```bash
# 方法1: 重新加载窗口
按 Cmd+Shift+P → "Developer: Reload Window"

# 方法2: 重装扩展
# 卸载 C# Dev Kit，重新安装
```

### **Q: 编译很慢**
**A:** 使用发布版本更快
```bash
Tasks: Run Task → build-release
或
dotnet build -c Release
```

### **Q: 程序输出看不到**
**A:** 
1. 确保终端焦点在集成终端
2. 检查 `launch.json` 的 `console` 设置为 `"internalConsole"`

---

## **9. VS Code 扩展推荐配置**

文件：`.vscode/settings.json` 已自动配置：

```json
{
  "[csharp]": {
    "editor.defaultFormatter": "ms-dotnettools.csharp",
    "editor.formatOnSave": true
  },
  "omnisharp.enableEditorConfigSupport": true,
  "omnisharp.enableRoslynAnalyzers": true
}
```

---

## **10. 一键启动脚本**

创建 `debug.sh` 文件：

```bash
#!/bin/bash
cd "$(dirname "$0")"
echo "🔨 Compiling..."
dotnet build
echo ""
echo "🚀 Running..."
dotnet bin/Debug/net6.0/StreamLinear.dll
```

使用方法：
```bash
chmod +x debug.sh
./debug.sh
```

---

## **11. 项目结构**

```
/adjust/
├── StreamLinear.cs          ← 主程序
├── StreamLinear.csproj      ← 项目配置
├── .vscode/
│   ├── launch.json         ← 调试配置 ✓
│   ├── tasks.json          ← 任务配置 ✓
│   └── settings.json       ← 编辑器设置 ✓
└── bin/
    ├── Debug/              ← 调试输出
    └── Release/            ← 发布输出
```

---

## **12. 发布为可执行程序**

### **生成独立可执行文件（无需.NET）**

```bash
# 使用 Tasks: Run Task → publish
或
dotnet publish -c Release -r osx-arm64 --self-contained
```

输出位置：
```
./bin/Release/net6.0/osx-arm64/publish/StreamLinear
```

运行：
```bash
./bin/Release/net6.0/osx-arm64/publish/StreamLinear
```

---

## **立即开始**

```bash
# 1. 进入项目
cd /Users/zhangzijian/Desktop/fanuc/adjust

# 2. 打开VS Code
code .

# 3. 等待扩展加载

# 4. 按 Cmd+Shift+B 编译

# 5. 按 F5 运行调试

# 完成！🎉
```

