# LocalP2PChat

局域网 P2P 聊天工具(无需服务器,直连/组网通信,支持加密传输、文件与音视频通话)。

## 目录结构

```
├── android/   # Android 版(本仓库当前代码)
└── windows/   # Windows 版(预留位置)
```

## Android 版

- Android 客户端源码位于 `android/`,基于 Gradle + Kotlin 构建。
- 功能:局域网成员发现、直接聊天、群组网状聊天(主机离线也可用)、离线历史、文件传输、音视频通话,所有传输均带 ECDH 握手加密。
- 双模拟器 / 真机端到端调试脚本见 `android/tools/emulator_e2e.py`。

## Windows 版

- 预留位置 `windows/`,待放入 Windows 桌面版本。
