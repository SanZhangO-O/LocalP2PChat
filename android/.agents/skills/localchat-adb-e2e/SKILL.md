---
name: localchat-adb-e2e
description: Use when debugging or testing the LocalChat Android app (this repo) with adb + Android emulators — driving the app via uiautomator/input, running or extending tools/emulator_e2e.py, or investigating two-emulator networking, connection drops, or UI automation pitfalls. Load before any adb/emulator test session.
---

# LocalChat adb 双模拟器调试注意事项

本仓库的 LocalChat（局域网 P2P 聊天 App）调试/测试用 adb + 两台 Android 模拟器完成。
正式 E2E 流程已固化为 `tools/emulator_e2e.py`（用法见其 docstring），本文是实测踩过的坑，改脚本或手工调试前先读。

## 0. 环境事实（写死的常量）

- 应用固定端口 `Constants.TCP_PORT = 9999`；`ServerSocket(port)` 绑定 0.0.0.0，所以 adb forward（连 localhost）可达。
- 现有 AVD：`Medium_Phone_2` 和 `Medium_Phone_API_36.1`（后者 ini 别名指向 `Medium_Phone.avd` 数据目录），均为 android-36.1 x86_64。
- SDK 在 `C:\Users\zhangqir\AppData\Local\Android\Sdk`，`adb`/`emulator` 不在 PATH，要用全路径。
- 日志 tag：`P2PManager`、`HostGroupServer`、`DirectChat`、`GroupMesh`。

## 1. 模拟器网络：两台 NAT 互相连不到（最重要）

每台模拟器 guest 的 `getLocalIpAddress()` 都返回 `10.0.2.15`（各自隔离 NAT 的内部地址）。
**A 看到的 B 公告 IP 和 B 自己看到的 IP 是同一个 10.0.2.15** —— 所以任何走"公告 IP"的直连都会连到自己或连不上。

打通方法：adb forward（guest 通过宿主机回环别名 `10.0.2.2` 到达宿主机端口）：

```
adb -s emulator-5554 forward tcp:9999 tcp:9999    # B 经 10.0.2.2:9999 到达 A
adb -s emulator-5556 forward tcp:10001 tcp:9999   # A 经 10.0.2.2:10001 到达 B（直连回复用）
```

由此得到的可用/不可用矩阵：

| 功能 | 双模拟器 | 说明 |
|---|---|---|
| 建群 / 加群（走宿主中继） | ✅ | B 用 `10.0.2.2:9999` 加入 |
| 群聊双向（宿主中继） | ✅ | 全程不重启应用即可 |
| 直连聊天双向 | ✅ | B 加 A 用 `10.0.2.2:9999`，A 加 B 用 `10.0.2.2:10001` |
| 群 mesh（宿主掉线聊天） | ❌ | 链路用公告 IP → 连到自己 |
| 文件字节下载 | ❌ | offer 带公告 IP → 下载优雅失败（属预期，可测失败路径） |
| 视频通话媒体流 | ❌ | 走公告 IP |

**陷阱：群成员同步进联系人列表时带的是公告 IP**，点它会"连自己"。直连测试必须用"添加成员"按 forward 地址加，且点开时要点 forward 地址那行（`contains="10.0.2.2"`），不能点群同步出来的同名成员。

## 2. 连接被杀 = "消息收不到"的头号原因

测试流程里 `am force-stop` / 重启应用会掐断宿主↔成员 TCP 连接（宿主侧 socket 变 TIME_WAIT）。
症状：A 发出消息自己能看到、B 永远收不到，但双方成员列表还都在（presence 是 join 时快照的）。
**流程内禁止 force-stop，导航一律 BACK + 点击**；只有流程开头的"从干净状态启动"才允许重启应用。

快速诊断：`cat /proc/net/tcp6` 找 `:270F`（9999 的 hex）——状态 `01`=ESTABLISHED，`06`=TIME_WAIT（连接已死）。B 若出现 `10.0.2.15:xxxx ↔ 10.0.2.15:270F` 就是连到了自己。

## 3. UI 驱动（uiautomator + input）

- Compose 控件靠 `uiautomator dump` 的 text / content-desc 定位，`input tap` 点中心点。
- **键盘弹出会改布局**：每次输入/点击前重新 dump；输入后 `input keyevent 4` 收起键盘再做下一步。
- **发消息别用 ENTER**：多行输入框上模拟器键盘的 ENTER 插入换行、不触发 IME Send，文字会一直躺在输入框里（`wait_text` 还会假阳性通过）。一律点发送按钮（content-desc=`发送`）。
- 系统权限弹窗会挡住一切：`pm clear` 后必须重新 `pm grant` 四个权限（ACCESS_LOCAL_NETWORK / POST_NOTIFICATIONS / CAMERA / RECORD_AUDIO）；兜底再检测并点掉 `Allow` / `While using the app`。
- 刚 `adb push` 的文件可能不在系统选择器里（媒体索引延迟）：测试早期就 push，或 `am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file:///sdcard/Download/xxx`。

## 4. Windows 宿主机陷阱

- **别用 `cmd 2>&1 | Select-Object -First N` 启动长驻进程**（模拟器）：输出超 N 行后管道被截断、进程被杀死。启动模拟器一律重定向到日志文件 `*> emu.log`，放后台跑。
- adb/python 输出中文：Python 子进程必须 `encoding="utf-8"` 解码 adb 输出；PowerShell 控制台 GBK 显示乱码只是显示层问题，不影响匹配。**不要用 PowerShell here-string 把中文传给 python**（会按 GBK 编码进管道），写成 UTF-8 文件再执行。
- 模拟器起不来：删 AVD 目录残留锁 `hardware-qemu.ini.lock` / `multiinstance.lock`，并确认没有旧 qemu/emulator 进程占端口（`taskkill /F /IM qemu-system-x86_64.exe`）。

## 5. 验证信号

- 大厅"群组成员 (N人)" + 双方能看到对方 = join/presence 通了（最快信号）。
- 创建者大厅卡片（仅 isHost 显示）：本机地址 / 群组数字ID / 群组密码，均带复制按钮；群列表里**不显示**群号，要进群大厅看。
- `run-as com.zqr.localchat` 可读 Room DB（debug 包），直接 sqlite 查库验证持久化。
- 全流程 ~8-10 分钟（模拟器上 uiautomator dump 每次 1-3 秒），文件传输步骤最脆，可 `--skip-file`。

## 6. 下次直接跑

```
python tools/emulator_e2e.py --all              # 开机+装包+全流程（~10 分钟）
python tools/emulator_e2e.py --test --skip-file # 只跑聊天流程
python tools/emulator_e2e.py --cleanup --uninstall
```
截图落 `.interop/shots/`、日志落 `.interop/logs/`（已 gitignore）。
