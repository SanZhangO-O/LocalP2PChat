# 修改代码注意点

本仓库是同一套局域网协议的两个独立实现（Windows = PyQt6 + Python，Android = Kotlin + Compose），
两端必须能在同一局域网内互通，且要兼容旧版本对端。改动前先读本文，避免重复踩过的坑。

## 1. 两套实现，一份协议

| 协议面 | Windows | Android |
| --- | --- | --- |
| 加密线层 / 握手 | `windows/localchat/securewire.py` | `android/app/src/main/java/com/zqr/localchat/network/SecureWire.kt` |
| 数据包模型 | `windows/localchat/models.py` (`NetworkPacket`) | `.../network/NetworkPacket.kt` |
| 直聊 / 群组 / 网状 | `windows/localchat/network.py` | `.../network/{P2PManager,DirectChat,GroupMesh}.kt` |
| 通话 / 文件 | `windows/localchat/call.py`、文件收发在 `network.py` | `.../call/CallManager.kt`、`.../network/FileTransfer.kt` |

- 任何协议改动（新增/修改字段、包类型、握手步骤、加密参数）必须**两端同步修改**，
  并用混合版本 E2E 验证（见第 7 节）。
- 跨平台字段名是 **camelCase JSON**（`groupId`/`hsMode`/`fileId`…），`NetworkPacket.to_dict()`
  与 Kotlin 序列化属性名必须一致；新增字段要在两端同时加。
- 固定参数不要单方面改动：PBKDF2-HMAC-SHA1 210k 迭代、HKDF info
  `localchat-session-v1` / `lc-direct-v1`、端口 9999、换行分帧 + 紧凑 JSON。

## 2. 向后兼容是硬约束

README「版本兼容说明」承诺：**聊天、文件、群组在版本不一致时仍可用**（只有通话要求两端同为最新版）。

- 新增字段必须可选、默认值保持旧行为；旧对端缺字段时要正常处理，而不是拒绝。
  Android 解码器已开 `ignoreUnknownKeys`，Python `from_dict` 忽略未知键。
- 新增校验一律「先观察后强制」，不要一上来就硬拒。参考 `seq`（防重放序号）的教训：
  - 发送端：`send_packet` / `sendPacket` 对每个加密包打 `seq`（1,2,3,…），不要移除。
  - 接收端：`securewire.py` 的 `recv_packet()`、`SecureWire.kt` 的 `recvPacket()`——
    旧对端（`seq` 缺失）始终放行；**对端首次携带 `seq` 之后**才按严格 `last+1` 校验，
    之后再缺 `seq` 按重放拒绝。旧流仍受每连接 nonce 缓存保护。
  - 历史事故：`a37413e` 把 `seq` 设为强制字段，旧手机 APK 的每个加密包都被新版 PC 拒绝
    （`direct_hello` 都进不来），表现为「手机电脑完全不互通」。
- 兼容只针对「缺失的新字段」，**不针对校验失败**：密码错误、TOFU 身份不符、签名不合法
  等必须继续拒绝（见第 5 节）。

## 3. 握手细节必须逐字节一致

- 群密码模式 transcript：`f"{mode}|{groupId}|{ephC}|{ephS}"`；Kotlin 的 `"$groupId"` 会把
  `null` 渲染成字面量 `"null"`，Windows 必须用 `_render_group_id()`（`securewire.py:149`）
  对齐。改任何一段字符串都要两端一起改并跑互通 E2E。
- 直聊/媒体走身份签名握手（`lc-direct-v1`），群密码握手与直聊握手是两条独立路径，别混用。
- 握手阶段的 `hs_*` 包是**明文** JSON，`activate()` 之后禁止明文（`send_raw`/`recv_raw`
  会抛异常），不要给旧包开降级口子。

## 4. 桌面端（PyQt6）UI 陷阱

- **`clicked` 会把 bool 注入 lambda 的默认参数**：

  ```python
  # 错误：rid 收到的是 clicked 的 bool（False），请求 id 被覆盖，点击静默无反应
  btn.clicked.connect(lambda rid=request.id: self.vm.accept_contact_request(rid))
  # 正确（也允许零参数 lambda）
  btn.clicked.connect(lambda checked=False, rid=request.id: self.vm.accept_contact_request(rid))
  ```

  回归测试：`windows/tests/test_functional.py::ViewModelFlowTest::test_contact_request_buttons_dispatch_request_id`。
- 列表页用 `setItemWidget`：刷新要重建条目并 `item.setSizeHint(widget.sizeHint())`；
  数据变化一律通过 ViewModel 的 `pyqtSignal` 触发 `refresh()`。
- 后台线程不碰 UI，全部经信号回到主线程。
- 离屏 GUI 测试要沿真实 UI 路径操作（点击行、`page.open_chat()`）。
  直接调 `vm.open_direct_chat()` 不会设置 `page._peer_id`，页面不刷新，会造成假失败。

## 5. 安全校验不要为兼容放宽

- 首次接触的地址一致性检查（`network.py:3120` 起）：loopback 来源豁免、身份已通过 TOFU
  证明的对端豁免。**不要**给未知身份的地址不一致开口子，那正是中间人入口。
- TOFU（`DeviceIdentity`）、移除标记、消息发送者校验、通话参与者校验保持原样；
  「兼容」永远只施加于新字段缺失，而不是校验失败。
- 对端安全码（指纹）与首次接触提示是刻意设计，UI 改动不要隐藏。

## 6. Android / 模拟器注意事项

- `targetSdk = 36`：`ACCESS_LOCAL_NETWORK` 是运行时权限，`MainActivity.kt` 已有请求与兜底
  流程，改导航/启动流程时别把它绕掉。
- 模拟器 NAT：每台 guest 都报 `10.0.2.15`，互相不可达。测试统一用 adb forward：
  宿主 `tcp:10099 → guest:9999`，guest 经 `10.0.2.2:9999` 回宿主。
- 测试流程内不要 `force-stop`（会掐断既有 TCP，表现为「消息永远收不到」）；只有流程开头的
  干净启动允许重启应用。`pm clear` 会清掉运行时权限，之后要重新 `pm grant`。
- 更多实测坑（UI 驱动、`/proc/net/tcp` 诊断、启动模拟器）见
  `android/.agents/skills/localchat-adb-e2e/SKILL.md`。

## 7. 测试与验证

```powershell
# Windows 全量单测（约 3 分钟，200+ 项）
cd windows; python -m pytest tests -q

# Android 单测 + 构建 APK
cd android; .\gradlew.bat testDebugUnitTest
.\gradlew.bat assembleDebug   # 产物: app/build/outputs/apk/debug/app-debug.apk
```

互通 E2E（需先启动模拟器并安装 APK）：

| 脚本 | 覆盖 |
| --- | --- |
| `windows/tests/win_emu_e2e.py` | 引擎层：按 IP 加联系人、双向消息、离线补发 |
| `windows/tests/win_emu_gui_e2e.py` | Windows 真实 UI：添加成员对话框 → 聊天页双向消息 |
| `windows/tests/win_emu_accept_e2e.py` | 手机先拨 → PC 请求卡片点「接受」→ 会话建立 + 双向聊天 |
| `android/tools/emulator_e2e.py` | 双 Android 模拟器全流程 |

**改协议必须做混合版本验证**（老对端 + 新代码）：

1. `git worktree add <临时目录> <旧提交>`，复制 `android/local.properties` 后构建旧 APK 并在
   模拟器覆盖安装；
2. 用当前 Windows 代码跑 `win_emu_accept_e2e.py`，应全过；
3. 负面对照：用旧提交的 Windows 代码跑同一脚本，修复前应复现失败（证明测试有效）。
4. 验证完 `git worktree remove --force <临时目录>`；Android 构建目录路径过长，必要时用
   `cmd /c rmdir /s /q` 清理。

测试文件约定：Windows 测试保持纯 ASCII（中文写 `\uXXXX` 转义）；辅助脚本写成 UTF-8 文件再
执行，不要用 PowerShell here-string 传中文（会按 GBK 进管道）。

## 8. 环境

- `adb` / `emulator` 不在 PATH：`%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe`、
  `%LOCALAPPDATA%\Android\Sdk\emulator\emulator.exe`。
- 启动长驻进程（模拟器）时输出重定向到日志文件，不要用管道截断。
- GitHub 推送依赖本机代理 `socks5://127.0.0.1:1081`（见 `git config` 的
  `http.https://github.com.proxy`）；代理未启动时直连会被重置。

## 9. 本地功能与存储

- 消息正文与「最后一条预览」是加密静态存储的（`enc1:` 前缀）：搜索/过滤**不能**对 `content`
  列直接做 SQL `LIKE`（对密文恒不匹配，且不报错）；只查静态明文列
  （`senderName`/`relativePath`/`folderName`）或用解密后的值匹配，`%`/`_`/`\` 要转义为字面量。
  详见 `docs/LESSONS.md`，回归测试：`windows/tests/test_functional.py::MessageSearchStoreTest`、
  Android `MessageSearchTest`。
- 本地 UI 功能（消息搜索、表情面板、Android 通知快捷回复）不涉及协议改动，两端各自实现、
  无互通要求；全量测试前先结束重负载构建，避免时序假失败（见 `docs/LESSONS.md`）。
