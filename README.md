# LocalP2PChat

局域网 P2P 聊天工具(无需服务器,直连/组网通信,支持加密传输、文件与音视频通话)。

## 目录结构

```
├── android/   # Android 版（Kotlin + Compose）
└── windows/   # Windows 版（PyQt6，已实现）
```

## Android 版

- Android 客户端源码位于 `android/`,基于 Gradle + Kotlin 构建。
- 功能:局域网成员发现、直接聊天、群组网状聊天(主机离线也可用)、离线历史、文件传输、音视频通话。聊天(群组+直连)、通话信令与媒体均全程 ECDH 握手加密;例外是文件字节流,采用 per-file 随机对称密钥的 AES-256-GCM 加密,该密钥本身经 ECDH 加密消息通道分发(下载握手行为 `file_download` 请求行含 uuid fileId 与可选下载 token=Base64(HMAC-SHA256(fileKey, "lc-file-dl-v1:"+fileId)),发送方恒时校验:token 存在但错误即拒绝,缺省(旧对端)才放行)。聊天与通话线路在同一连接内记录已见 nonce,发现重放即断开(线格式不变)。消息删除采用墓碑收敛:在线成员即时删除,离线成员重连后经 `join_ack`/`history_reply` 的 `deletedIds` 补齐(每群上限 200 条,单包上限同值)。注意:无密码群组的加密仅防被动嗅探、不防主动中间人,建议创建群组时设置密码;首次添加联系人时建议在两端核对安全码(指纹),防止首次接触被中间人;删除消息的授权以"持有群组口令的成员"为界(墓碑不携带作者校验),口令泄露即等于成员可让全群删除任意消息。
- 双模拟器 / 真机端到端调试脚本见 `android/tools/emulator_e2e.py`。

## Windows 版

- 完整 Windows 桌面版位于 `windows/`，基于 PyQt6，功能与 Android 端互通。

## 版本兼容说明

- 通话功能要求 **两端（Android 与 Windows）均为最新版本**：媒体通道建立时需完成 `call_media_hello` 校验，旧版本客户端无法与本版本通话（表现为"对方未接听/媒体通道校验失败"）；聊天、文件传输与群组功能不受影响。升级后如通话异常，请确认对方也已升级。
