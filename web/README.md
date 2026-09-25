# LocalChat Web 服务（多用户）

同一套局域网协议的第三个独立实现：**Node.js 多用户聊天服务器 + 浏览器界面**。
一台服务器（家里主机 / NAS / VPS）承载多个账号，每个用户从自己的浏览器登录；
每个账号是一个**独立的 LocalChat 设备**（独立身份密钥、联系人、群组、独立协议
端口），与 Windows / Android / 其他 Web 服务在同一路域网内直接互通，无需任何
第三方服务器。

```
浏览器A(账号甲) ─┐                                  ┌─ Windows 端
浏览器B(账号乙) ─┼─ Web服务(Node, HTTP+WS) ─ 各账号 ─┼─ Android 端
浏览器C(账号丙) ─┘   每账号一个TCP协议端口           └─ 局域网其他设备
```

## 运行

依赖：Node.js ≥ 18（无需 npm install，全部使用标准库）。

```bat
node web\server\main.js --http-host 0.0.0.0
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--http` | 8090 | Web 界面端口（所有用户共用） |
| `--http-host` | 127.0.0.1 | 界面绑定地址；部署给他人访问用 `0.0.0.0` |
| `--port-base` | 9999 | 协议端口分配起点（第 1 个账号 9999，之后 10000、10001…） |
| `--data` | `web/data` | 数据目录（账号、身份密钥、聊天记录） |

首次打开界面会要求**创建第一个账号**；之后已登录用户可在「账号与设置」里
新增账号。登录基于用户名 + 密码（scrypt 哈希存储，HttpOnly 会话 Cookie，
登录失败限速）。服务器重启后会话失效，需要重新登录。

每个账号对局域网呈现为独立设备：其他端「添加成员」时填**服务器 IP + 该账号
的协议端口**（账号创建后界面里可查看，例如 `192.168.1.10:10000`）。第一个
账号默认占用 9999，与两端默认端口一致。

## 功能范围

与两端互通的协议能力（每个账号独立一份）：

- **直聊**：身份签名握手（`lc-direct-v1`）、首次接触请求卡片（接受/忽略 +
  安全码指纹）、离线暂存自动补发、已读回执、正在输入、撤回、编辑、
  表情回应、置顶。
- **群组**：建群（密码握手）、8 位数字入群号码、成员赞助入群、群主中继 +
  成员间网状（主机离线也能聊）、网状历史回补 + 删除墓碑收敛、群公告/改名、
  移出成员、群已读回执、群消息设备签名（TOFU，`lc-group-v1`）。
- **文件**：per-file 密钥 AES-256-GCM、`file_download`（token + offset）、
  断点续传、图片内联预览。

尚未实现：通话/群语音会议、回复引用/转发/@提及的发送入口、群文件共享区、
二维码、跨网段中继打洞。

## 安全说明

- **界面有登录但传输仍是明文 HTTP**：仅在可信局域网内开放 `0.0.0.0`；跨公网
  请自行套 HTTPS 反向代理（WS 走 `wss` 即可，无需改代码）。
- 每账号身份密钥（EC P-256）在 `data/accounts/<id>/identity.json`
  （明文 PKCS#8），聊天正文以 `enc1:`（AES-256-GCM）加密落盘，内容密钥在
  各账号目录 `secret_key.json`；请保护好服务器数据目录的文件权限。
- TOFU 与两端一致：对端身份密钥首次接触即绑定，变更一律拒绝（可能中间人）。
- 账号密码仅存 scrypt 哈希；登录接口按来源 IP 限速。

## 测试

```powershell
node --test web\test\unit.test.js    # 密码学向量、包序列化、transcript、TOFU 存储
node --test web\test\auth.test.js    # 账号注册/口令哈希/端口分配/会话
node --test web\test\interop.test.js # 与 windows/ 真实引擎的跨语言互通（需 pip install -r windows/requirements.txt）
```

互通测试覆盖：密码握手（双向 + 错误密码拒绝）、直聊身份握手 + hello/ack +
已读回执、直聊 TOFU 换密钥拒绝、Node 加入 Python 群（双向中继）、Python 成员
加入 Node 群、网状链接（mesh_chat / history_reply 双向）、文件传输（两端互为
发送方）。Python 侧驱动为 `web/test/py_driver.py`（复用 `windows/localchat`
真实引擎与 `fake_peer.py`）。
