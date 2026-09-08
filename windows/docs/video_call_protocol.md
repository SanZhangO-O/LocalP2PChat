# 视频通话协议设计（LocalChat / LocalChatWin 互通）

视频通话在现有群组消息通道之上增加「信令」，媒体数据通过通话双方之间的**直连 TCP** 传输，不经过群组主机。

## 1. 信令（复用 JSON 行协议，TCP 9999 组通道）

### 1.1 新增字段

`NetworkPacket` 增加两个可选字段（kotlinx/Python 两侧一致，null/默认值不序列化）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `targetId` | String? | 定向投递目标成员 id。为 null 时按原有广播语义处理 |
| `call` | CallInfo? | 通话信息 |

`CallInfo`（camelCase 序列化）：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `callId` | String | — | 本次通话唯一 id（UUID） |
| `callerId` | String | — | 主叫方成员 id |
| `callerName` | String | — | 主叫方昵称 |
| `calleeId` | String | — | 被叫方成员 id |
| `mediaPort` | Int | 0 | 主叫方媒体服务器端口（offer 中携带） |
| `accepted` | Boolean | true | 应答是否接受（默认接受；拒绝用 `call_reject`） |
| `audioEnabled` | Boolean | true | 是否启用音频 |

### 1.2 新增包类型

| type | 方向 | 载荷 | 语义 |
|---|---|---|---|
| `call_offer` | 主叫 → 被叫 | `call`（含 mediaPort） | 发起通话邀请 |
| `call_answer` | 被叫 → 主叫 | `call` | 接受邀请（accepted=true） |
| `call_reject` | 被叫 → 主叫 | `call`（errorMessage 可选） | 拒绝邀请 / 忙 |
| `call_failed` | 被叫 → 主叫 | `call`（errorMessage） | 媒体直连失败 |
| `call_hangup` | 任意 → 对方 | `call` | 结束通话 / 取消呼叫 |

### 1.3 定向路由规则（主机中继）

- 客户端发送带 `targetId` 的包 → 发送给主机；主机收到后：
  - `targetId == 主机自己` → 主机本地处理（主机是被叫/主叫）；
  - `targetId` 是某个在线成员 → 仅转发给该成员的 socket（不广播、不回给发送者）；
  - 目标不存在 → 丢弃。
- 主机直接向成员 socket 发送带 `targetId` 的包。
- 客户端收到的带 `targetId` 的包必须满足 `targetId == 自己`，否则忽略。

身份校验：主机转发前校验 `call.callerId == 发送者`（offer/failed）或
`call.calleeId == 发送者`（answer/reject），不符则丢弃。

## 2. 媒体（直连 TCP，二进制分帧）

### 2.1 连接建立

- 主叫发起时开启 `ServerSocket(0)`，把端口放进 `call_offer.mediaPort`。
- 被叫接受邀请后主动连接 `主叫IP:mediaPort`（主叫 IP 取自成员列表）。
- 单条 TCP 连接承载**双向**媒体（全双工）。

### 2.2 帧格式

```
[1 byte 通道][4 bytes 大端长度][payload]
```

| 通道 | 内容 |
|---|---|
| 0 | 视频：JPEG 帧 |
| 1 | 音频：PCM 16bit 小端，单声道，16kHz（20ms=640 字节/帧） |

- 视频：最大边 640px、JPEG 质量约 70、约 10fps（150~400 KB/s，局域网可接受）。
- 音频：20ms 一帧（32 KB/s）。**静音时仍发送静音帧**，保证连接活跃，防止误判掉线。
- 读超时 15 秒：超时视为通话中断。
- 每次发送在锁内 `sendall` 保证帧不交错；接收循环单线程解析。

## 3. 状态机（每端一个通话）

```
Idle
 ├─ start_call(peer) ──────────────► Outgoing(callId)  开启媒体服务器+发 call_offer
 │                                    ├─ 收到 call_answer ─► Active
 │                                    ├─ 收到 call_reject/call_failed ─► Idle(提示)
 │                                    ├─ 媒体 accept 超时(45s) ─► 发 call_hangup ─► Idle
 │                                    └─ 用户取消 ─► 发 call_hangup ─► Idle
 ├─ 收到 call_offer ─► Incoming(callId)
 │                        ├─ accept：连接媒体服务器→发 call_answer─► Active
 │                        ├─ reject ─► 发 call_reject ─► Idle
 │                        └─ 收到 call_hangup（主叫取消）─► Idle
 └─ Active：媒体连接断开 / 收到 call_hangup / 用户挂断 ─► Idle
```

同一时刻只允许一个通话；忙时收到 offer 自动回 `call_reject`。

## 4. 端侧实现要点

- **Windows**：摄像头用 OpenCV（cv2.VideoCapture）采集 → JPEG；音频用 QtMultimedia
  QAudioSource/QAudioSink（无需 ffmpeg 后端）；无摄像头时用合成测试图案兜底。
- **Android**：CameraX ImageAnalysis 采集（JPEG 输出优先，YUV 转换兜底）→ 旋转/缩放 →
  JPEG；音频 AudioRecord/AudioTrack。
