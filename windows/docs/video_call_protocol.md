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

- 视频：最大边 640px、JPEG 质量约 70；Windows 端约 12.5fps（发送间隔 `VIDEO_INTERVAL=0.08s`），Android 端约 10fps（发送限流 100ms）。合计约 150~400 KB/s，局域网可接受。
- 音频：20ms 一帧（32 KB/s）。**静音时仍发送静音帧**，保证连接活跃，防止误判掉线。
- 读超时 15 秒：超时视为通话中断。
- 每次发送在锁内 `sendall` 保证帧不交错；接收循环单线程解析。

### 2.3 媒体帧加密（AES-256-GCM）

媒体连接在承载任何帧之前先完成 **direct 身份安全握手**（与直连聊天同一套 ECDH +
长期身份密钥签名握手；被叫拨号主叫媒体端口，作为握手发起方），协商出的会话密钥
用于加密此后**每一帧**。加密后的帧格式：

```
[1 byte 通道][4 bytes 大端长度][12 bytes nonce][AES-256-GCM 密文 || 16 bytes tag]
```

- 4B 长度计的是 `nonce||密文||tag` 整体；每帧使用独立随机 nonce。
- 解密失败（被篡改或密钥不符）视为通道不可信，直接结束通话。

### 2.4 媒体通道校验（call_media_hello）

被叫在安全握手完成后发送 `call_media_hello`（复用 JSON 行协议包，`call` 携带
`callId/callerId/calleeId`），主叫校验 **type + 三元绑定**：

- `type == call_media_hello`，且 `callId == 本次通话`、`callerId == 自己`、`calleeId == 对端`；
- 任一不符即判“媒体通道校验失败”，结束通话。

动机：媒体端口是 TCP 公开的，仅完成首次身份握手不足以证明“来者就是本次通话的对方”；
`callId` 只经加密的 `call_offer` 传递，能给出正确三元组者必然是收到 offer 的一端。
两端任一为旧版本（无安全握手与该校验）时无法建立通话——即 README“版本兼容”
所称旧版无法通话的机制。

## 3. 状态机（每端一个通话）

```
Idle
 ├─ start_call(peer) ──────────────► Outgoing(callId)  开启媒体服务器+发 call_offer
 │                                    ├─ 媒体连接建立 ─► Active（call_answer 仅为冗余确认）
 │                                    ├─ 收到 call_reject/call_failed ─► Idle(提示)
 │                                    ├─ 媒体 accept 超时(45s) ─► 发 call_hangup ─► Idle
 │                                    └─ 用户取消 ─► 发 call_hangup ─► Idle
 ├─ 收到 call_offer ─► Incoming(callId)
 │                        ├─ accept：连接媒体服务器（安全握手+call_media_hello 完成即 Active）→发 call_answer─► Active
 │                        ├─ reject ─► 发 call_reject ─► Idle
 │                        └─ 收到 call_hangup（主叫取消）─► Idle
 └─ Active：媒体连接断开 / 收到 call_hangup / 用户挂断 ─► Idle
```

同一时刻只允许一个通话；忙时收到 offer 自动回 `call_reject`。

## 4. 端侧实现要点

- **Windows**：摄像头用 OpenCV（cv2.VideoCapture）采集 → JPEG；音频**优先探测
  sounddevice（PortAudio）**，QtMultimedia QAudioSource/QAudioSink **逐侧兜底**
  （sounddevice 未安装或该侧采集/播放不可用时）；无摄像头时用合成测试图案兜底。
- **Android**：CameraX ImageAnalysis 采集（JPEG 输出优先，YUV 转换兜底）→ 旋转/缩放 →
  JPEG；音频 AudioRecord/AudioTrack。

## 5. 直连成员通话（信令不经群组主机）

除经群组主机中继信令外，群组成员之间的一对一通话也支持在**直连成员会话**上发起
（两端成员在线且已建立直连会话时）：

- 主叫通过直连会话发送 `call_offer`（Windows `CallManager.start_direct_call` /
  Android `CallManager.startDirectCall`），此后全部信令
  （`call_offer`/`call_answer`/`call_reject`/`call_failed`/`call_hangup`）均沿该直连
  会话的**加密 socket** 双向投递，**不经过群组主机**；两端入口为
  `handle_direct_signal`（Windows）/ `handleDirectSignal`（Android）。
- 直连会话本身已完成 direct 身份安全握手（§2.3 同一套 ECDH + 长期身份密钥签名），
  信令全程加密；§1.3 的定向路由与身份校验由直连会话的点对点性质天然保证。
- 媒体连接与群组通话完全一致（§2）：仍由主叫开启媒体端口、被叫拨号并作为握手
  发起方，完成安全握手与 `call_media_hello` 后双向传输加密帧。

## 6. 被叫激活时机与 call_answer 的冗余性

被叫接受邀请后主动连接主叫媒体端口；**媒体连接建立（安全握手 + `call_media_hello`
完成）即进入 Active**，随即开始收发媒体，`call_answer` 在此之后补发，仅作为**冗余
确认**：即使该应答包在信令通道中丢失，主叫也已因媒体连接到达而立即激活
（§3 状态机中主叫侧同样是"媒体连接建立 ─► Active"），不会停留在 Outgoing 等待。
任一端为旧版本（无安全握手与 `call_media_hello`）时仍按"版本兼容"规则无法建立通话。
