# LocalP2PChat

局域网 P2P 聊天工具(无需服务器,直连/组网通信,支持加密传输、文件与音视频通话)。

## 目录结构

```
├── android/   # Android 版（Kotlin + Compose）
└── windows/   # Windows 版（PyQt6，已实现）
```

## Android 版

- Android 客户端源码位于 `android/`,基于 Gradle + Kotlin 构建。
- 功能:局域网成员发现、直接聊天、群组网状聊天(主机离线也可用)、离线历史、文件传输、音视频通话,所有传输均带 ECDH 握手加密。
- 双模拟器 / 真机端到端调试脚本见 `android/tools/emulator_e2e.py`。

## Windows 版

- 完整 Windows 桌面版位于 `windows/`，基于 PyQt6，功能与 Android 端互通。

## 版本兼容说明

- 通话功能要求 **两端（Android 与 Windows）均为最新版本**：媒体通道建立时需完成 `call_media_hello` 校验，旧版本客户端无法与本版本通话（表现为"对方未接听/媒体通道校验失败"）；聊天、文件传输与群组功能不受影响。升级后如通话异常，请确认对方也已升级。
