using UnityEngine;
using UnityEngine.XR;
using System;
using System.Net;
using System.Net.Sockets;
using System.Text;

[Serializable]
public struct FanucTCP
{
    public float x, y, z;
    public float w, p, r;

    public FanucTCP(float x, float y, float z, float w, float p, float r)
    {
        this.x = x;
        this.y = y;
        this.z = z;
        this.w = w;
        this.p = p;
        this.r = r;
    }
}

[Serializable]
internal class UDPPacket
{
    public float px, py, pz;
    public float qx, qy, qz, qw;
    public float x, y, z, w, p, r;
    public bool primaryButton;
    public bool secondaryButton;
    public bool triggerButton;
    public bool gripButton;
}

public class VRHandCubeTest : MonoBehaviour
{
    [Header("XR")]
    public XRNode handNode = XRNode.RightHand;

    [Header("Scale")]
    public float scale = 1000f;

    [Header("Robot Home")]
    public float robotHomeX = 408.913f;
    public float robotHomeY = 40.853f;
    public float robotHomeZ = -24.032f;
    public float robotHomeW = 149.716f;
    public float robotHomeP = 11.3f;
    public float robotHomeR = 125.261f;

    [Header("Workspace")]
    public float maxRadius = 400f;
    public float minZ = -200f;
    public float maxZ = 300f;

    [Header("Filter")]
    [Range(0.01f, 1f)]
    public float posAlpha = 0.2f;
    [Range(0.01f, 1f)]
    public float rotAlpha = 0.15f;

    [Header("Velocity Limit mm/s")]
    public float maxVel = 600f;

    [Header("UDP")]
    public string targetIP = "192.168.137.170";
    public int targetPort = 9000;

    // ── 新增：供 RobotIKVisualizer 读取的公开属性 ──
    private FanucTCP currentTCP;
    public FanucTCP currentFanucPos => currentTCP;

    private InputDevice handDevice;
    private Vector3 questHomePos;
    private Quaternion questHomeRot;
    private Quaternion questHomeYaw;
    private bool homeSet = false;
    private bool triggerHeld = false;
    private Vector3 filteredPos;
    private Quaternion filteredRot;
    private Vector3 prevPos;
    private bool initialized = false;
    private UdpClient udpClient;
    private IPEndPoint remoteEndPoint;

    void Start()
    {
        udpClient = new UdpClient();
        remoteEndPoint = new IPEndPoint(IPAddress.Parse(targetIP), targetPort);
    }

    void Update()
    {
        handDevice = InputDevices.GetDeviceAtXRNode(handNode);
        if (!handDevice.isValid)
            return;

        if (!handDevice.TryGetFeatureValue(CommonUsages.devicePosition, out Vector3 handPos))
            return;

        if (!handDevice.TryGetFeatureValue(CommonUsages.deviceRotation, out Quaternion handRot))
            return;

        handDevice.TryGetFeatureValue(CommonUsages.trigger, out float trigger);
        triggerHeld = trigger > 0.5f;

        handDevice.TryGetFeatureValue(CommonUsages.primaryButton, out bool primary);
        handDevice.TryGetFeatureValue(CommonUsages.secondaryButton, out bool secondary);
        handDevice.TryGetFeatureValue(CommonUsages.gripButton, out bool grip);

        if (primary)
        {
            questHomePos = handPos;
            questHomeRot = handRot;
            Vector3 forward = handRot * Vector3.forward;
            forward.y = 0;
            forward.Normalize();
            float yaw = Mathf.Atan2(forward.x, forward.z) * Mathf.Rad2Deg;
            questHomeYaw = Quaternion.AngleAxis(yaw, Vector3.up);
            homeSet = true;
        }

        if (!homeSet)
            return;

        if (!triggerHeld)
            return;

        Vector3 deltaPosWorld = handPos - questHomePos;
        Vector3 deltaPosAligned = Quaternion.Inverse(questHomeYaw) * deltaPosWorld;
        Quaternion deltaRotWorld = handRot * Quaternion.Inverse(questHomeRot);
        Quaternion deltaRotAligned = Quaternion.Inverse(questHomeYaw) * deltaRotWorld * questHomeYaw;

        FanucTCP target = ComputeTarget(deltaPosAligned, deltaRotAligned);
        SendUDP(handPos, handRot, target, triggerHeld, primary, secondary, grip);
    }

    // ────────────────────────────
    // Position mapping
    // ────────────────────────────
    Vector3 MapUnityPosition(Vector3 delta)
    {
        return new Vector3(
            -delta.z,
            delta.x,
            delta.y
        );
    }

    // ────────────────────────────
    // Rotation mapping
    // ────────────────────────────
    Quaternion MapUnityRotation(Quaternion q)
    {
        return new Quaternion(
            -q.z,
            -q.x,
            q.y,
            q.w
        );
    }

    // ────────────────────────────
    // Target computation
    // ────────────────────────────
    FanucTCP ComputeTarget(Vector3 deltaPos, Quaternion deltaRot)
    {
        Vector3 mapped = MapUnityPosition(deltaPos) * scale;
        Vector3 rawPos = new Vector3(
            robotHomeX + mapped.x,
            robotHomeY + mapped.y,
            robotHomeZ + mapped.z
        );

        rawPos = ClampWorkspace(rawPos);
        //rawPos = FilterPosition(rawPos);
        rawPos = LimitVelocity(rawPos);

        Quaternion rawRot = ComputeRotation(deltaRot);
        //rawRot = FilterRotation(rawRot);

        initialized = true;

        // ── 修复：手动反解 Fanuc ZYX 外旋欧拉角 ──────────────────
        // Fanuc W=绕X, P=绕Y, R=绕Z，外旋顺序 Z→Y→X
        // 对应四元数分量公式：
        float qw = rawRot.w;
        float qx = rawRot.x;
        float qy = rawRot.y;
        float qz = rawRot.z;

        // P（绕Y轴）—— 先算，用于万向锁判断
        float sinP = Mathf.Clamp(2f * (qw * qy - qz * qx), -1f, 1f);
        float fanucP = Mathf.Asin(sinP) * Mathf.Rad2Deg;
        float fanucW, fanucR;

        if (Mathf.Abs(sinP) > 0.9999f) // 万向锁
        {
            fanucW = Mathf.Atan2(2f * (qw * qx - qy * qz), 1f - 2f * (qx * qx + qz * qz)) * Mathf.Rad2Deg;
            fanucR = 0f;
        }
        else
        {
            // W（绕X轴）
            fanucW = Mathf.Atan2(2f * (qw * qx + qy * qz), 1f - 2f * (qx * qx + qy * qy)) * Mathf.Rad2Deg;
            // R（绕Z轴）
            fanucR = Mathf.Atan2(2f * (qw * qz + qx * qy), 1f - 2f * (qy * qy + qz * qz)) * Mathf.Rad2Deg;
        }

        currentTCP = new FanucTCP(
            rawPos.x,
            rawPos.y,
            rawPos.z,
            fanucW,
            fanucP,
            fanucR
        );

        return currentTCP;
    }

    // ────────────────────────────
    // rotation composition
    // ────────────────────────────
    Quaternion ComputeRotation(Quaternion deltaRot)
    {
        Quaternion qW = Quaternion.AngleAxis(robotHomeW, Vector3.right);
        Quaternion qP = Quaternion.AngleAxis(robotHomeP, Vector3.up);
        Quaternion qR = Quaternion.AngleAxis(robotHomeR, Vector3.forward);
        Quaternion homeQ = qR * qP * qW;
        Quaternion deltaFanuc = MapUnityRotation(deltaRot);
        return deltaFanuc * homeQ;
    }

    // ────────────────────────────
    // workspace clamp
    // ────────────────────────────
    Vector3 ClampWorkspace(Vector3 p)
    {
        Vector3 center = new Vector3(robotHomeX, robotHomeY, robotHomeZ);
        Vector3 offset = p - center;

        if (offset.magnitude > maxRadius)
            offset = offset.normalized * maxRadius;

        float z = Mathf.Clamp(p.z, minZ, maxZ);

        return new Vector3(
            center.x + offset.x,
            center.y + offset.y,
            z
        );
    }

    // ────────────────────────────
    // velocity limiter
    // ────────────────────────────
    Vector3 LimitVelocity(Vector3 target)
    {
        float dt = Time.deltaTime;
        float maxStep = maxVel * dt;
        Vector3 delta = target - prevPos;

        if (delta.magnitude > maxStep)
            delta = delta.normalized * maxStep;

        prevPos += delta;
        return prevPos;
    }

    // ────────────────────────────
    // filters
    // ────────────────────────────
    Vector3 FilterPosition(Vector3 raw)
    {
        if (!initialized)
        {
            filteredPos = raw;
            prevPos = raw;
            return raw;
        }

        filteredPos = Vector3.Lerp(filteredPos, raw, posAlpha);
        return filteredPos;
    }

    Quaternion FilterRotation(Quaternion raw)
    {
        if (!initialized)
        {
            filteredRot = raw;
            return raw;
        }

        filteredRot = Quaternion.Slerp(filteredRot, raw, rotAlpha);
        return filteredRot;
    }

    // ────────────────────────────
    float Normalize(float a)
    {
        if (a > 180f)
            a -= 360f;
        return a;
    }

    // ────────────────────────────
    // UDP send
    // ────────────────────────────
    void SendUDP(Vector3 questPos, Quaternion questRot, FanucTCP tcp, bool trigger, bool primary, bool secondary, bool grip)
    {
        try
        {
            UDPPacket p = new UDPPacket();
            p.px = questPos.x;
            p.py = questPos.y;
            p.pz = questPos.z;
            p.qx = questRot.x;
            p.qy = questRot.y;
            p.qz = questRot.z;
            p.qw = questRot.w;
            p.x = tcp.x;
            p.y = tcp.y;
            p.z = tcp.z;
            p.w = tcp.w;
            p.p = tcp.p;
            p.r = tcp.r;
            p.triggerButton = trigger;
            p.primaryButton = primary;
            p.secondaryButton = secondary;
            p.gripButton = grip;

            string json = JsonUtility.ToJson(p);
            byte[] data = Encoding.UTF8.GetBytes(json);
            udpClient.Send(data, data.Length, remoteEndPoint);
        }
        catch (Exception e)
        {
            Debug.LogWarning(e.Message);
        }
    }

    void OnDestroy()
    {
        udpClient?.Close();
    }
}