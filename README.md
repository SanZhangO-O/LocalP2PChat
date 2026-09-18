# LocalP2PChat

局域网 P2P 聊天工具(无需服务器,直连/组网通信,支持加密传输、文件与音视频通话)。

## 目录结构

```
├── android/   # Android 版（Kotlin + Compose）
└── windows/   # Windows 版（PyQt6，已实现）
```

## Android 版

- Android 客户端源码位于 `android/`,基于 Gradle + Kotlin 构建。
- 功能:局域网成员发现、直接聊天、群组网状聊天(主机离线也可用)、离线历史、文件传输、音视频通话。聊天(群组+直连)、通话信令与媒体均全程 ECDH 握手加密;例外是文件字节流,采用 per-file 随机对称密钥的 AES-256-GCM 加密,该密钥本身经 ECDH 加密消息通道分发(下载握手行为 `file_download` 请求行含 uuid fileId 与可选下载 token=Base64(HMAC-SHA256(fileKey, "lc-file-dl-v1:"+fileId)),发送方恒时校验:token 存在但错误即拒绝,缺省(旧对端)才放行)。聊天与通话线路在同一连接内记录已见 nonce,发现重放即断开;加密包内附加逐方向严格递增的 `seq`(接收端要求恰好加一,拒绝重放/乱序/注入),对旧版本(不携带 `seq` 的)对端仍兼容:其报文持续放行,直到对端首次携带 `seq` 后才强制校验。消息删除采用墓碑收敛:在线成员即时删除,离线成员重连后经 `join_ack`/`history_reply` 的 `deletedIds` 补齐(每群上限 200 条,单包上限同值)。注意:无密码群组的加密仅防被动嗅探、不防主动中间人,建议创建群组时设置密码;首次添加联系人时建议在两端核对安全码(指纹),防止首次接触被中间人;删除消息的授权以"持有群组口令的成员"为界(墓碑不携带作者校验),口令泄露即等于成员可让全群删除任意消息。
- 双模拟器 / 真机端到端调试脚本见 `android/tools/emulator_e2e.py`。

## Windows 版

- 完整 Windows 桌面版位于 `windows/`，基于 PyQt6，功能与 Android 端互通。

## 文件断点续传与进度详情

- 下载握手 `file_download` 请求行现在**必带 `offset` 字段**（已收字节数，非负整数）：0 表示全新下载，大于 0 表示从中断处续传；`offset` 超过发送方声明的大小时直接拒绝（不返回 meta、不发送任何字节）。发送方从该偏移 seek 后流式发送；`offset == 文件大小` 时只回 meta 与 EOF 标记，接收端据此完成落盘。
- 接收端把解密分片追加写入临时文件（Windows/桌面为 `<目标>.part`；Android 因 SAF 文档没有同目录临时文件，统一写入应用私有 `downloads/<fileId>_<文件名>.part` 暂存文件），完成后才原子落盘：桌面端 `os.replace`，Android 媒体文件 `renameTo`、用户选择的 SAF 文档则复制后删除暂存文件。
- 下载中断（连接断开/取消/应用退出）**保留暂存文件与已收字节数**：状态持久化在本地（Windows 存于加密的设置项 `file_download_resume_v1`，Android 存于 Keystore 包裹的 prefs `file_resume_<fileId>`，其中包含续传所需的下载地址与 per-file 密钥），应用重启后文件卡片仍显示「已暂停 xx%（点击续传）」，再次点击自动从断点继续；文件夹传输逐文件适用（已完成条目会跳过）。
- 取消语义两端一致：**取消 = 保留断点**（显示「已暂停」，再次点击续传），不删除暂存文件。
- 下载中的卡片实时显示「下载中 N%」与进度详情「已传/总大小 · 速度 · 剩余时间」；刷新节流保证至少 4 次/秒（250ms 或 256KB 或 5% 阈值，任一触发）。
- 加密线层不变：每个分片仍是独立的 AES-256-GCM 报文，格式为 `nonce(12B) || ciphertext || tag(16B)` 外加 4 字节大端长度前缀，**nonce 每片随机生成**，与偏移无关，因此续传不改变任何 GCM 参数（两端一致）。校验失败（token 错误、GCM 标签不符、大小不符等）与安全底线保持不变，不因兼容放宽。

## 版本兼容说明

- 通话功能要求 **两端（Android 与 Windows）均为最新版本**：媒体通道建立时需完成 `call_media_hello` 校验，旧版本客户端无法与本版本通话（表现为"对方未接听/媒体通道校验失败"）。文件断点续传同理：`file_download` 的 `offset` 为必需字段，新旧版本混用时文件下载会失败（项目尚未发布，未保留旧版兜底）；请两端同步升级。聊天与群组功能不受影响。

## 构建

### Windows 版

```bat
cd windows
pip install -r requirements.txt
python main.py
```

### Android 版

用 Android Studio 打开 `android/` 目录构建安装，或：

```bat
cd android
gradlew assembleDebug
```

## 许可证

本项目基于 [MIT License](LICENSE) 开源，免费使用。
