using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Diagnostics;

namespace FanucStreaming
{
    // ─────────────────────────────────────────────
    // 配置
    // ─────────────────────────────────────────────
    public class Config
    {
        public const string HOST = "172.30.109.22";
        public const int PORT_CONNECT = 16001;
        public const int GROUP = 1;
        public const string LOG_FILE = "../recordings/teleop_log_1772263304.jsonl";

        public const double START_UNITY_TS = 3.1666667461395264;

        public const int UTOOL = 0;
        public const int UFRAME = 0;
        public const int SPEED_MM_S = 200;
        public const int CNT_VALUE = 0;

        public const bool SKIP_NOT_TRACKING = true;
        public const bool SKIP_DUPLICATE = true;
        public const float MIN_DIST_MM = 0.3f;

        public const float ACK_TIMEOUT = 10.0f;
        public const int TARGET_FPS = 50;
    }

    // ─────────────────────────────────────────────
    // LineSocket：解决 TCP 粘包
    // ─────────────────────────────────────────────
    public class LineSocket
    {
        private Socket _socket;
        private byte[] _buffer = new byte[65536];
        private int _bufferPos = 0;

        public LineSocket(Socket sock)
        {
            _socket = sock;
        }

        public void SendAll(byte[] data)
        {
            _socket.Send(data);
        }

        public void SetTimeout(int milliseconds)
        {
            _socket.ReceiveTimeout = milliseconds;
            _socket.SendTimeout = milliseconds;
        }

        public string ReadLine()
        {
            while (true)
            {
                // 查找换行符
                int idx = Array.IndexOf(_buffer, (byte)'\n', 0, _bufferPos);
                if (idx != -1)
                {
                    string line = Encoding.ASCII.GetString(_buffer, 0, idx).Trim();
                    // 移动缓冲区
                    _bufferPos -= idx + 1;
                    Array.Copy(_buffer, idx + 1, _buffer, 0, _bufferPos);
                    if (!string.IsNullOrEmpty(line))
                        return line;
                }

                // 接收更多数据
                try
                {
                    int bytesRead = _socket.Receive(_buffer, _bufferPos, _buffer.Length - _bufferPos);
                    if (bytesRead == 0)
                        throw new ConnectionException("Socket closed");
                    _bufferPos += bytesRead;
                }
                catch (SocketException ex)
                {
                    if (ex.SocketErrorCode != SocketError.TimedOut)
                        throw;
                }
            }
        }

        public JsonElement ReadJson()
        {
            string line = ReadLine();
            using (JsonDocument doc = JsonDocument.Parse(line))
            {
                return doc.RootElement.Clone();
            }
        }

        public void Drain(int timeoutMs = 500)
        {
            SetTimeout(timeoutMs);
            try
            {
                while (true)
                {
                    int bytesRead = _socket.Receive(_buffer, 0, _buffer.Length);
                    if (bytesRead == 0) break;
                }
            }
            catch (SocketException) { }
            _bufferPos = 0;
        }

        public void Close()
        {
            _socket?.Close();
            _socket?.Dispose();
        }
    }

    // ─────────────────────────────────────────────
    // TCP 优化函数
    // ─────────────────────────────────────────────
    public class TcpOptimizer
    {
        public static void OptimizeSocket(Socket sock)
        {
            try
            {
                // 禁用Nagle算法
                sock.NoDelay = true;

                // 设置缓冲区大小
                sock.ReceiveBufferSize = 65536;
                sock.SendBufferSize = 65536;

                // 启用KeepAlive
                sock.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.KeepAlive, true);

                Console.WriteLine("✅ TCP socket optimized for low-latency");
            }
            catch (Exception ex)
            {
                Console.WriteLine($"⚠️ TCP optimization warning: {ex.Message}");
            }
        }
    }

    // ─────────────────────────────────────────────
    // RMI 通信函数
    // ─────────────────────────────────────────────
    public class RmiCommunication
    {
        public static int FrcConnect(string host, int port)
        {
            using (Socket s = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp))
            {
                s.Connect(host, port);
                string connectCmd = "{\"Communication\": \"FRC_Connect\"}\r\n";
                s.Send(Encoding.ASCII.GetBytes(connectCmd));

                byte[] resp = new byte[4096];
                int bytesRead = s.Receive(resp);
                string respStr = Encoding.ASCII.GetString(resp, 0, bytesRead);
                
                using (JsonDocument doc = JsonDocument.Parse(respStr))
                {
                    var root = doc.RootElement;
                    Console.WriteLine($"FRC_Connect: {respStr}");
                    int errId = root.GetProperty("ErrorID").GetInt32();
                    if (errId != 0)
                        throw new Exception($"FRC_Connect failed: {errId}");
                    return root.GetProperty("PortNumber").GetInt32();
                }
            }
        }

        public static void RmiAbort(LineSocket ls)
        {
            try
            {
                ls.SetTimeout(5000);
                string abortCmd = "{\"Command\": \"FRC_Abort\"}\r\n";
                ls.SendAll(Encoding.ASCII.GetBytes(abortCmd));
                var resp = ls.ReadJson();
                Console.WriteLine($"FRC_Abort: OK");
            }
            catch (Exception ex)
            {
                Console.WriteLine($"⚠️ Abort failed: {ex.Message}");
            }
        }

        public static LineSocket RmiInitialize(string host, int port, int group = 1)
        {
            Socket raw = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
            TcpOptimizer.OptimizeSocket(raw);
            raw.Connect(host, port);
            raw.ReceiveTimeout = 5000;

            LineSocket ls = new LineSocket(raw);
            
            var initCmd = new { Command = "FRC_Initialize", GroupMask = group };
            string cmdStr = JsonSerializer.Serialize(initCmd) + "\r\n";
            ls.SendAll(Encoding.ASCII.GetBytes(cmdStr));

            var resp = ls.ReadJson();
            Console.WriteLine($"FRC_Initialize: OK");
            
            if (resp.GetProperty("ErrorID").GetInt32() != 0)
                throw new Exception($"FRC_Initialize failed");
            
            ls.Drain();
            return ls;
        }
    }

    // ─────────────────────────────────────────────
    // 运动包生成
    // ─────────────────────────────────────────────
    public class MotionPacket
    {
        public static byte[] MakePacket(int seqId, double[] target, int speed = Config.SPEED_MM_S, int cnt = Config.CNT_VALUE)
        {
            double x = target[0], y = target[1], z = target[2];
            double w = target[3], p = target[4], r = target[5];

            // 参数约束
            x = Math.Max(-2000, Math.Min(2000, x));
            y = Math.Max(-2000, Math.Min(2000, y));
            z = Math.Max(-2000, Math.Min(2000, z));

            w = w % 360;
            p = p % 360;
            r = r % 360;

            speed = Math.Max(1, Math.Min(1000, speed));
            cnt = Math.Max(0, Math.Min(100, cnt));

            var packet = new
            {
                Instruction = "FRC_LinearMotion",
                SequenceID = seqId,
                Configuration = new
                {
                    UToolNumber = Config.UTOOL,
                    UFrameNumber = Config.UFRAME,
                    Front = 1,
                    Up = 1,
                    Left = 0,
                    Flip = 0,
                    Turn4 = 0,
                    Turn5 = 0,
                    Turn6 = 0
                },
                Position = new
                {
                    X = x,
                    Y = y,
                    Z = z,
                    W = w,
                    P = p,
                    R = r,
                    Ext1 = 0.0,
                    Ext2 = 0.0,
                    Ext3 = 0.0
                },
                SpeedType = "mmSec",
                Speed = speed,
                TermType = "CNT",
                TermValue = cnt
            };

            string json = JsonSerializer.Serialize(packet) + "\r\n";
            return Encoding.ASCII.GetBytes(json);
        }
    }

    // ─────────────────────────────────────────────
    // 异步发送器 - 核心优化
    // ─────────────────────────────────────────────
    public class AsyncStreamingSender
    {
        private LineSocket _ls;
        private Queue<(int, int, long)> _ackQueue;
        private Thread _recvThread;
        private bool _running = false;
        private int _seqId = 1;

        private readonly Dictionary<int, string> _errorDesc = new()
        {
            { 2556952, "Configuration parameter error" },
            { 2556956, "Robot still executing (RMIT-028)" },
            { 2556957, "Invalid parameter or robot state" },
            { 2556959, "Position data invalid/out of range" }
        };

        public AsyncStreamingSender()
        {
            _ackQueue = new Queue<(int, int, long)>();
        }

        public void Connect(string host, int port, int group = 1)
        {
            Console.WriteLine("🔌 Connecting to Fanuc RMI...");

            int dynamicPort = RmiCommunication.FrcConnect(host, port);
            _ls = RmiCommunication.RmiInitialize(host, dynamicPort, group);

            // 🚀 启动后台ACK监听线程
            _running = true;
            _recvThread = new Thread(ListenAcks)
            {
                IsBackground = true,
                Name = "ACK Listener"
            };
            _recvThread.Start();
            Console.WriteLine("✅ Async sender initialized, ACK listener started");
        }

        private void ListenAcks()
        {
            while (_running)
            {
                try
                {
                    _ls.SetTimeout(100);
                    try
                    {
                        var resp = _ls.ReadJson();
                        int seqId = resp.GetProperty("SequenceID").GetInt32();
                        int errId = resp.GetProperty("ErrorID").GetInt32();

                        lock (_ackQueue)
                        {
                            _ackQueue.Enqueue((seqId, errId, Stopwatch.GetTimestamp()));
                        }

                        if (errId != 0)
                        {
                            string desc = _errorDesc.ContainsKey(errId) ? _errorDesc[errId] : "Unknown error";
                            Console.WriteLine($"⚠️ ACK Error: seq={seqId,7} err={errId,7} (0x{errId:06x}) - {desc}");
                        }
                    }
                    catch (SocketException) { }
                }
                catch (Exception ex)
                {
                    if (_running)
                        Console.WriteLine($"❌ ACK listener error: {ex.Message}");
                    break;
                }
            }
        }

        public bool SendAsync(int seqId, double[] target, int speed = Config.SPEED_MM_S, int cnt = Config.CNT_VALUE)
        {
            try
            {
                byte[] packet = MotionPacket.MakePacket(seqId, target, speed, cnt);
                
                // 调试：打印前5帧
                if (seqId <= 2 || seqId % 100 == 0)
                    Console.WriteLine($"📤 Sending seq={seqId}: target=({target[0]}, {target[1]}, {target[2]}, " +
                        $"{target[3]}, {target[4]}, {target[5]}), speed={speed}, cnt={cnt}");
                
                _ls.SendAll(packet);
                return true;
            }
            catch (Exception ex)
            {
                Console.WriteLine($"❌ Send failed: {ex.Message}");
                return false;
            }
        }

        public (int?, int?) CheckAck()
        {
            lock (_ackQueue)
            {
                if (_ackQueue.TryDequeue(out var result))
                    return (result.Item1, result.Item2);
            }
            return (null, null);
        }

        public void Disconnect()
        {
            _running = false;
            try
            {
                Thread.Sleep(100);
                if (_recvThread != null)
                    _recvThread.Join(1000);
            }
            catch { }

            try
            {
                RmiCommunication.RmiAbort(_ls);
            }
            catch { }

            try
            {
                _ls?.Close();
            }
            catch { }

            Console.WriteLine("🔌 Disconnected");
        }
    }

    // ─────────────────────────────────────────────
    // 数据处理
    // ─────────────────────────────────────────────
    public class DataProcessor
    {
        public static double Distance(double[] a, double[] b)
        {
            return Math.Sqrt(Math.Pow(b[0] - a[0], 2) + 
                             Math.Pow(b[1] - a[1], 2) + 
                             Math.Pow(b[2] - a[2], 2));
        }

        public static List<(double, double[])> LoadAndFilter(string logFile)
        {
            var records = new List<(double, double[])>();
            double[]? prevTarget = null;

            try
            {
                foreach (var line in File.ReadLines(logFile))
                {
                    line = line.Trim();
                    if (string.IsNullOrEmpty(line))
                        continue;

                    try
                    {
                        using (var doc = JsonDocument.Parse(line))
                        {
                            var root = doc.RootElement;

                            double ts = root.GetProperty("unity_data")
                                .GetProperty("timestamp").GetDouble();
                            if (ts < Config.START_UNITY_TS)
                                continue;

                            if (Config.SKIP_NOT_TRACKING && !root.GetProperty("is_tracking").GetBoolean())
                                continue;

                            var targetJson = root.GetProperty("robot_target");
                            if (targetJson.ValueKind != JsonValueKind.Array || targetJson.GetArrayLength() != 6)
                                continue;

                            double[] target = new double[6];
                            for (int i = 0; i < 6; i++)
                                target[i] = targetJson[i].GetDouble();

                            if (Config.SKIP_DUPLICATE && prevTarget != null && 
                                target.SequenceEqual(prevTarget))
                                continue;

                            if (prevTarget != null && 
                                Distance(target, prevTarget) < Config.MIN_DIST_MM)
                                continue;

                            records.Add((ts, target));
                            prevTarget = target;
                        }
                    }
                    catch { }
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"Error loading file: {ex.Message}");
            }

            Console.WriteLine($"Loaded {records.Count} frames after filtering");
            return records;
        }
    }

    // ─────────────────────────────────────────────
    // 主程序 - 异步高频发送
    // ─────────────────────────────────────────────
    public class Program
    {
        public static void Main(string[] args)
        {
            var records = DataProcessor.LoadAndFilter(Config.LOG_FILE);
            if (records.Count == 0)
            {
                Console.WriteLine("No records, exiting.");
                return;
            }

            var sender = new AsyncStreamingSender();

            try
            {
                sender.Connect(Config.HOST, Config.PORT_CONNECT, Config.GROUP);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"❌ Connection failed: {ex.Message}");
                return;
            }

            int seqId = 1;
            var frameTimes = new List<double>();
            var ackErrors = new List<(int, int)>();
            double frameInterval = 1.0 / Config.TARGET_FPS;
            var stopwatch = Stopwatch.StartNew();

            try
            {
                foreach (int idx in Enumerable.Range(0, records.Count))
                {
                    var frameStart = Stopwatch.StartNew();
                    var (_, target) = records[idx];

                    // 🚀 发送指令（异步，立即返回）
                    bool ok = sender.SendAsync(seqId, target);

                    if (ok)
                    {
                        seqId++;

                        // 间歇地检查ACK（非阻塞）
                        var (ackSeq, ackErr) = sender.CheckAck();
                        if (ackSeq.HasValue)
                        {
                            if (ackErr == 0)
                                ackErrors.Add((ackSeq.Value, 0));
                            else if (ackErr == 2556956)
                            {
                                // 正常：机器人还在执行上一条指令
                            }
                            else
                                ackErrors.Add((ackSeq.Value, ackErr.Value));
                        }

                        // 帧率控制
                        double tElapsed = frameStart.Elapsed.TotalSeconds;
                        double tSleep = Math.Max(0, frameInterval - tElapsed);
                        if (tSleep > 0)
                            Thread.Sleep((int)(tSleep * 1000));

                        double tTotal = frameStart.Elapsed.TotalSeconds;
                        frameTimes.Add(tTotal * 1000);

                        // 定期打印
                        if ((idx + 1) % 20 == 0 || idx == 0)
                        {
                            double recentAvg = frameTimes.TakeLast(Math.Min(20, frameTimes.Count)).Average();
                            double currentFps = recentAvg > 0 ? 1000.0 / recentAvg : 0;
                            Console.WriteLine(
                                $"[{idx + 1,4}/{records.Count,4}] seq={seqId - 1,4} " +
                                $"dt={tTotal * 1000,6:F1}ms fps={currentFps,6:F1}Hz");
                        }
                    }
                    else
                    {
                        Console.WriteLine($"[{idx + 1}/{records.Count}] ❌ Send FAILED, skipping");
                    }
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"💥 异常: {ex.Message}");
            }
            finally
            {
                sender.Disconnect();

                // 📊 打印最终统计
                Console.WriteLine("\n" + new string('=', 60));
                Console.WriteLine("📊 性能统计报告");
                Console.WriteLine(new string('=', 60));

                if (frameTimes.Count > 0)
                {
                    double avg = frameTimes.Average();
                    double median = frameTimes.OrderBy(x => x).ElementAt(frameTimes.Count / 2);
                    double min = frameTimes.Min();
                    double max = frameTimes.Max();
                    double stdev = Math.Sqrt(frameTimes.Sum(x => Math.Pow(x - avg, 2)) / frameTimes.Count);

                    Console.WriteLine($"总发送帧数: {frameTimes.Count}");
                    Console.WriteLine($"平均帧间隔: {avg:F2} ms");
                    Console.WriteLine($"中位数间隔: {median:F2} ms");
                    Console.WriteLine($"最小间隔: {min:F2} ms");
                    Console.WriteLine($"最大间隔: {max:F2} ms");
                    Console.WriteLine($"标准差: {stdev:F2} ms");

                    double avgFps = 1000.0 / avg;
                    Console.WriteLine($"\n🎯 平均帧率: {avgFps:F1} Hz");

                    if (avgFps >= Config.TARGET_FPS)
                        Console.WriteLine($"✅ 达到目标帧率 {Config.TARGET_FPS}Hz!");
                    else
                        Console.WriteLine($"⚠️ 未达到目标帧率 {Config.TARGET_FPS}Hz (实际{avgFps:F1}Hz)");
                }

                if (ackErrors.Count > 0)
                {
                    int errorCount = ackErrors.Count(x => x.Item2 != 0);
                    Console.WriteLine($"\n📨 ACK错误统计:");
                    Console.WriteLine($"  总错误数: {errorCount}");
                    if (errorCount > 0)
                        Console.WriteLine($"  错误率: {100.0 * errorCount / ackErrors.Count:F2}%");
                }

                Console.WriteLine(new string('=', 60));
            }
        }
    }
}
