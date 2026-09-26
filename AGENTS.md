# 修改代码注意点

本仓库是同一套局域网协议的独立实现（Windows = PyQt6 + Python，Android = Kotlin + Compose），
各端必须能在同一局域网内互通，且要兼容旧版本对端。另有 `server/localchat_server.py` =
Python 一体化**服务器**（信令/打洞/中继 + Web 多用户聊天，HTTP+WS 中转与存储 + 浏览器 UI，
范围见 `web/README.md`）：Web 聊天只提供网络服务器聊天，**不实现局域网协议、
不与 Windows / Android 端互通**，不在协议同步范围内。
改动前先读本文，避免重复踩过的坑。
**协议改动必须同步所有已实现该协议面的端**（Windows / Android）。

## 1. 两套实现，一份协议

| 协议面 | Windows | Android |
| --- | --- | --- |
| 加密线层 / 握手 | `windows/localchat/securewire.py` | `android/app/src/main/java/com/zqr/localchat/network/SecureWire.kt` |
| 数据包模型 | `windows/localchat/models.py` (`NetworkPacket`) | `.../network/NetworkPacket.kt` |
| 直聊 / 群组 / 网状 | `windows/localchat/network.py` | `.../network/{P2PManager,DirectChat,GroupMesh}.kt` |
| 通话 / 文件 | `windows/localchat/call.py`、文件收发在 `network.py` | `.../call/CallManager.kt`、`.../network/FileTransfer.kt` |
| 群消息设备签名 | `windows/localchat/groupauth.py` | `.../groupauth/GroupAuth.kt` |

- 任何协议改动（新增/修改字段、包类型、握手步骤、加密参数）必须**两端同步修改**，
  并用混合版本 E2E 验证（见第 7 节）。
- 跨平台字段名是 **camelCase JSON**（`groupId`/`hsMode`/`fileId`…），`NetworkPacket.to_dict()`
  与 Kotlin 序列化属性名必须一致；新增字段要在两端同时加。
- 固定参数不要单方面改动：PBKDF2-HMAC-SHA1 210k 迭代、HKDF info
  `localchat-session-v1` / `lc-direct-v1`、端口 9999、换行分帧 + 紧凑 JSON。

## 2. 协议演进（2026-09-21 起：未发布、无历史包袱）

**项目尚未发布：协议改动不需要为旧版本保留兼容路径**——字段可以直接改语义、
校验可以直接强制，不再遵循「可选字段默认旧行为 / 先观察后强制」的旧约定；
两端仍必须**同步修改、同步升级**（同一套协议的两份实现）。本节余下内容是
历史教训与仍生效的实现细节：

- README「版本兼容说明」已改为「未发布、无兼容兜底，两端同步升级」。
- Android 解码器 `ignoreUnknownKeys`、Python `from_dict` 忽略未知键仍保留
  （对未知字段优雅忽略是健壮性，不是旧版本兼容）。
- `seq`（防重放序号）的现状：发送端 `send_packet` / `sendPacket` 对每个加密
  包打 `seq`（1,2,3,…），不要移除；接收端对 `seq` 缺失的包按重放拒绝；
  每连接 nonce 缓存继续保护旧流。
- 新增「内容类」字段/类型的正确姿势参考 `audio`（语音消息）与
  `mentions`/`edited`：发送端省略默认值（普通消息字节不变），接收端未知
  kind normalize 后退化为 `file` 卡片照常下载/外部播放。
- 编辑收敛与签名（LESSONS 2026-09-21）：edit_message 的 `senderSig` 覆盖
  **编辑后完整消息**的 message transcript（`message_fields_parts`/
  `messageParts`，按接收副本时间戳+新内容重建后验证），验证通过后存为
  mesh 历史副本签名，历史推送天然通过 `verify_message`；relay 收到的编辑
  要镜像进 mesh 副本；**无签名/验签不过的编辑一律拒绝**（编辑改写历史，
  不做任何无签名容忍）。群编辑离线暂存/重放：Windows `pending_ops`，
  Android `pending_ops`（DB v7，`ChatViewModel.replayPendingOps`），两端
  语义保持一致。改动 `history_reply` 合并规则必须两端同步并跑互通 E2E。
  - 历史事故：`a37413e` 把 `seq` 设为强制字段，旧手机 APK 的每个加密包都被新版 PC 拒绝
    （`direct_hello` 都进不来），表现为「手机电脑完全不互通」——当时两端
    版本不一致；现在既然未发布、两端同步升级，校验可以直接强制。
- 校验失败**永远拒绝**：密码错误、TOFU 身份不符、签名不合法/缺失
  （编辑）等不因任何考虑放宽（见第 5 节）。

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
- **lambda 默认参表里不要放引用同级默认参的表达式**（`on=e not in mine` 在
  `e=emoji` 绑定前求值，构建菜单即 `NameError`）；派生值先在循环体里算成局部变量。
  回归测试：`windows/tests/test_functional.py::ViewModelFlowTest::test_direct_reaction_menu_builds_and_dispatches_toggle`，
  详见 `docs/LESSONS.md` 2026-09-21。
- 列表页用 `setItemWidget`：刷新要重建条目并 `item.setSizeHint(widget.sizeHint())`；
  数据变化一律通过 ViewModel 的 `pyqtSignal` 触发 `refresh()`。
- 后台线程不碰 UI，全部经信号回到主线程。
- **连到 Qt 信号的 lambda 不要带默认参数**（PyQt 槽 arity 探测脆弱，曾造成全量测试中漂移
  出现的 `TypeError: () missing 1 required positional argument: 'key'` 幽灵）；持有 QTimer
  的对象必须在 shutdown/teardown 中停止定时器（回归：
  `windows/tests/test_functional.py::ViewModelFlowTest::test_shutdown_stops_every_owned_timer`，
  详见 `docs/LESSONS.md` 2026-09-19 续报）。
- **页面持有原生资源必须在 hide/teardown 停止**：录音（`VoiceRecorder.cancel()`）、GIF
  动画（`delegate.clear_movies()`）、归属该页的 QTimer；切会话还要显式 abort 录音，
  否则「60 秒自动发送」会把语音发进当前打开的会话。`MainWindow.closeEvent` 统一调
  页面 `teardown()`。详见 `docs/LESSONS.md` 2026-09-19 续报 4。
- 离屏 GUI 测试要沿真实 UI 路径操作（点击行、`page.open_chat()`）。
  直接调 `vm.open_direct_chat()` 不会设置 `page._peer_id`，页面不刷新，会造成假失败。
- **输入框不要和按钮同排放 `AlignBottom`**：`QTextEdit` 默认 sizeHint 约 120px，
  会把行撑高、按钮只贴底边（表现为「大方块 + 左下角一排按钮」）。聊天底部固定为
  「输入框独占一行（`DroppableTextEdit.enable_auto_grow()`，40→120px）+ 下方
  action row」，样式见 `theme.py` 的 `composer`/`composerAction`/`composerText`；
  改按钮结构同步更新 `windows/tests/_real_gui_win.py`、`_real_exe_gui.py` 的坐标点击。
  详见 `docs/LESSONS.md` 2026-09-19 续报 6。
- **QApplication 级样式要成套给全**：`Fusion` 必须配显式浅色 palette
  （`theme.py::light_palette`，`main.py` 启动时设置）——QSS 只覆盖它点名的控件，
  其余回落系统调色板，深色系统下会整片变黑。`setFixedSize` 按钮要核对
  「文字宽 + 左右内边距 <= 固定宽」（PeerRow 行按钮用 `compact` 属性规则降内边距）；
  内容可能高过窗口的页面（设置/表单/多卡片）必须包 `QScrollArea`
  （包 `QStackedWidget` 时切页要重置滚动条——minimumSizeHint 取各页最大值）；
  行内贴边元素 alignment 要同时给水平 + 垂直分量；列表预览用
  `QFontMetrics.elidedText`，不要裸 `setMaximumWidth` 硬裁。
  回归测试：`windows/tests/test_functional.py::RenderWalkthroughFixTest`，
  详见 `docs/LESSONS.md` 2026-09-21 三则。

## 5. 安全校验不要为兼容放宽

- 首次接触的地址一致性检查（`network.py:3120` 起）：loopback 来源豁免、身份已通过 TOFU
  证明的对端豁免。**不要**给未知身份的地址不一致开口子，那正是中间人入口。
- TOFU（`DeviceIdentity`）、移除标记、消息发送者校验、通话参与者校验保持原样；
  「兼容」永远只施加于新字段缺失，而不是校验失败。
- 网络层的作者校验必须用于门控其监听器/持久化回调（`_edit_message(...)` 返回 True
  才通知），存储层写接口对「发送者声明」（如编辑的 senderId）加 SQL 条件做纵深防御；
  曾出现「内存拒绝但监听器照样落库」的伪造编辑。详见 `docs/LESSONS.md` 2026-09-19 续报 3。
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
- **按码点处理跨端字符逻辑**：Python 字符串按码点迭代，Kotlin String 按 UTF-16
  码元——`all { isEmojiChar(it) }` 这类直接遍历 Char 的判定会把代理对（emoji）
  判错，必须 `codePointAt` + `Character.charCount`（教训：`isBigEmoji`，
  `docs/LESSONS.md` 2026-09-19）。
- **嵌套泛型三层以上用 typealias**：`Map<String, List<Pair<String, String>>>`
  结尾的 `>>>` 会被 K2 词法器当无符号右移运算符，报一堆与类型无关的错误
  （`Interface Map does not have constructors` 等）；起个 `typealias` 绕开。
- **Room 写 SQL 先对 minSdk 的 SQLite 版本**：`ON CONFLICT ... DO UPDATE`（UPSERT）
  要 SQLite 3.24 = API 30，minSdk 24 上 prepare 即失败；父行改写用
  `INSERT OR IGNORE ... SELECT` + 相关子查询 `UPDATE`。DB 写路径别用裸
  `runCatching` 吞错（曾表现为「迁移后历史消失」）。详见 `docs/LESSONS.md`
  2026-09-19 续报 2。
- **`FileInputStream.getChannel()` 只读**：回填二进制头必须用
  `RandomAccessFile(file, "rw")`（或 `FileOutputStream(fd).getChannel()`）。
  曾导致录音 WAV 长度字段恒为 0，Windows 端 `wave` 读 0 帧、时长 0:00、播放无声。
  详见 `docs/LESSONS.md` 2026-09-19 续报 1。
- **聊天底部输入行只有 `ChatInputBar`**（`ui/screen/ChatInputBar.kt`，群聊与直聊共用）：
  行内只放「+ / 输入框 / 发送」，媒体、表情、语音都收在「+」面板里——
  6 个动作按钮摊在同一行会把输入框挤没。改按钮结构/按钮 desc 时同步更新
  `android/tools/emulator_e2e.py`（它按 `content-desc` 找节点，折叠后要先点
  `更多发送选项`）。详见 `docs/LESSONS.md` 2026-09-19 续报 5。

## 7. 测试与验证

```powershell
# Windows 全量单测（约 3 分钟，200+ 项）
cd windows; python -m pytest tests -q

# Android 单测 + 构建 APK
cd android; .\gradlew.bat testDebugUnitTest
.\gradlew.bat assembleDebug   # 产物: app/build/outputs/apk/debug/app-debug.apk

# 服务器（信令 + Web 聊天二合一；Web 不在局域网协议互通范围，细节见 web/README.md）
python -m pytest server\tests -q                 # 全部：加密/存储单元 + 账号 + HTTP+WS 端到端
python -m pytest server\tests\test_server.py -q  # 真实 ChatServer HTTP+WS 端到端
# 运行：双击 web\start-server.bat（等价 python server\localchat_server.py --http-host 0.0.0.0 --open）；
# 普通用户只开浏览器访问，不装任何东西、不敲命令。Web 服务端依赖 `cryptography`（AES-GCM）。
```

互通 E2E（需先启动模拟器并安装 APK）：

| 脚本 | 覆盖 |
| --- | --- |
| `windows/tests/win_emu_e2e.py` | 引擎层：按 IP 加联系人、双向消息、离线补发 |
| `windows/tests/win_emu_gui_e2e.py` | Windows 真实 UI：添加成员对话框 → 聊天页双向消息 |
| `windows/tests/win_emu_accept_e2e.py` | 手机先拨 → PC 请求卡片点「接受」→ 会话建立 + 双向聊天 |
| `android/tools/emulator_e2e.py` | 双 Android 模拟器全流程 |

**改协议两端必须同步改**（§2：未发布、无旧版本兼容兜底，不再做混合版本
验证；两端跑各自全量单测 + 互通 E2E 即可）。

**改 `server/webchat/`（Web 服务端）必须实际运行**：Python 与旧 Node 实现存在
运行时差异（如 Node `Buffer`/UTF-16 与 Python 码点、`os.startfile` 与 Node
`spawn` 等），没跑过的复刻代码可能整条加密/WS 路径都不可用；改完先
`python -m py_compile` 改动文件，再跑 `python -m pytest server\tests -q`（含真实
`ChatServer` HTTP+WS 端到端）。`server/tests/test_unit.py` 里有旧 Node 实现生成的
scrypt / AES-GCM 固定向量，改加密或存储格式时必须保持这些向量通过（老
`web/data` 数据兼容）。详见 `docs/LESSONS.md` 2026-09-25、2026-09-26。

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
