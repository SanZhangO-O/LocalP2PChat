# LocalChatWin — LocalChat 的 Windows 桌面版（PyQt6）

与 Android 版 LocalChat 完全兼容的局域网群组聊天软件 Windows 版，使用 PyQt6 编写。

## 功能

- 同一局域网内通过 TCP 直连聊天，无需服务器
- **整个程序共用一个端口**（默认 9999，可在主界面右上角“设置”中修改）：所有群组都通过这个端口加入，多群组共存互不干扰
- 创建群组：本机作为创建者（Host），分享 `IP:端口` 地址给其他人加入
- 加入群组：输入创建者 IP 和 8 位群组数字 ID（创建者大厅展示），先查询确认群组信息再加入
- 多群组支持：群组列表、未读消息数、最近消息预览、消息转发到其他群组
- 消息持久化（SQLite）：重启后群组和聊天记录保留，成员群组自动重连，创建者群组自动恢复服务
- 成员在线检测：双向心跳（ping/pong）及时发现掉线成员和断开的连接（TCP 半开连接约 45 秒内被探测到，成员列表与群组连接状态自动更新）
- 消息删除同步：删除后群内所有成员的消息记录同步移除
- Windows 系统托盘新消息通知：连续消息自动合并为一条气泡；点击气泡可恢复窗口并跳转到对应群组；关闭窗口会最小化到系统托盘继续接收消息（托盘菜单“退出”可真正退出程序）
- **视频通话**：群组成员之间一对一视频通话（含音频），信令通过群组通道定向投递、媒体数据走双方直连 TCP（JPEG 视频 + PCM 音频），Android 与 Windows 端互通；支持接听/拒绝、静音、关闭摄像头、挂断，无摄像头时自动使用测试画面兜底
  - 版本兼容：通话建立时双方需完成媒体通道校验（`call_media_hello`），**通话双方必须都升级到本版本或更新版本**；与旧版本的一端通话会因校验缺失而失败（表现为"对方未接听/媒体通道校验失败"），聊天与文件传输不受影响

## 环境要求

- Windows 10 / 11
- Python 3.10+
- PyQt6（`pip install PyQt6`）
- opencv-python（`pip install opencv-python`，用于摄像头采集；无摄像头时自动降级为测试画面，不影响使用）
- imageio-ffmpeg（`pip install imageio-ffmpeg`，推荐）：当 OpenCV 的 Windows 摄像头后端无法枚举设备时（例如存在 VTubeStudioCam 等虚拟摄像头驱动，或使用 Microsoft Store 版 Python），程序自动改用 ffmpeg DirectShow 采集摄像头；未安装时回退到系统 PATH 上的 ffmpeg，都没有则使用测试画面
- sounddevice（`pip install sounddevice`，推荐）：音频采集/播放优先使用它（PortAudio，自带运行库）；未安装时回退到 QtMultimedia。如果通话中听不到对方声音或对方听不到你，请检查 Windows 声音设置里的默认录音/播放设备，并在应用内重新发起通话

## 运行

```bat
pip install -r requirements.txt
run.bat        （或 python main.py）
```

聊天数据保存在 `data/localchat.db`（项目目录下），删除该文件即可清空全部数据。

在控制台窗口按 **Ctrl+C** 可干净退出程序（会正常保存数据、释放端口）。

## 使用说明

1. 启动后点击右下角 **+** 添加群组
2. **创建群组**：输入昵称和群组名称，把界面上的“本机地址”（如 `192.168.1.100:9999`）分享给对方。默认端口 9999，可在主界面右上角“设置”中修改（整个程序共用一个端口）
3. **加入群组**：输入昵称、8 位群组数字 ID 和创建者的 IP（可含端口，如 `192.168.1.100:9999`），点击“查找群组”，确认群组信息后加入
4. 进入群组大厅后点击“进入聊天”开始聊天
5. 聊天中：右键消息可复制 / 转发 / 删除（只能删自己发的）
6. **视频通话**：在群组大厅成员列表中，点击成员右侧的“通话”按钮发起视频通话；对方会看到呼入提示，接听后即进入通话窗口（可静音、关摄像头、挂断）

## 注意事项（Windows 防火墙）

**群组连接**使用固定端口（默认 9999，可在“设置”中修改）；**视频通话的媒体连接使用随机端口**直连。两者都需要放行本程序的**所有入站连接**，而不是只放行 9999。

首次运行/首次通话时如被防火墙拦截，请选择“允许访问”。若之前已放行但仅限 9999（表现为：聊天正常、视频通话对方提示“无法连接媒体通道”），请补充放行整个程序：

1. 图形界面：控制面板 → Windows Defender 防火墙 → 允许应用或功能通过防火墙 → 更改设置 → 允许其他应用… → 选择 `python.exe`（或打包版 `LocalChat.exe`），勾选“专用”，确定。
2. 或命令行（管理员 PowerShell，路径换成你的实际 python.exe / LocalChat.exe）：
   ```powershell
   netsh advfirewall firewall add rule name="LocalChat" dir=in action=allow program="C:\Users\你的用户名\AppData\Local\Microsoft\WindowsApps\python.exe" enable=yes
   ```
   打包版：
   ```powershell
   netsh advfirewall firewall add rule name="LocalChat" dir=in action=allow program="C:\path\to\LocalChat.exe" enable=yes
   ```

视频通话失败的提示现在会包含具体的 `IP:端口`：若提示中的 IP 不是你的电脑在局域网中的地址（例如是虚拟网卡地址），请检查网络连接并重启应用。

## 与 Android 版互通

网络协议（JSON 行协议、端口 9999、包类型）与 Android 版完全一致，Android 设备与 Windows 设备可以在同一群组中互相聊天。

## 运行测试

```bat
python -m unittest tests.test_protocol -v
```

协议一致性测试使用与 Android 端 kotlinx.serialization 完全相同的字节格式（紧凑 JSON、省略 null 字段、不含 `isFromMe`、UTF-8、换行分帧）双向验证网络层，确保跨平台互通。

## 视频通话联调

单机即可验证完整通话链路（两个独立进程，真实 TCP）：

```bat
python tests/e2e_call.py
```

该脚本启动「主机+被叫」与「主叫」两个进程，跑通 入群 → 信令（呼叫/接听）→ 媒体直连 → 双向视频帧 → 音频通道 → 挂断 的完整流程；无摄像头时自动使用合成测试画面，麦克风不可用时仅音频采集降级、传输通道仍可验证。

调试音频设备时可设置环境变量强制指定 sounddevice 设备号（`python -c "import sounddevice as sd; print(sd.query_devices())"` 查看编号）：
`LOCALCHAT_AUDIO_IN=28`（录音设备号）、`LOCALCHAT_AUDIO_OUT=5`（播放设备号）。

## Ctrl+C 退出验证

Windows 上 Python 的 SIGINT 无法在 Qt 事件循环阻塞期间执行，导致 Ctrl+C 无效。程序已内置 Windows 控制台处理器修复；可用真实控制台信号验证：

```bat
python tests/sigint_verify.py
```

该脚本向真实应用的独立控制台投递 Ctrl+C（与手动按键一致），确认程序干净退出（rc=0）。
