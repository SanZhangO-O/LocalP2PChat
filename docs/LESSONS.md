# 踩坑记录（LESSONS）

记录「花了时间才定位」的问题，格式：现象 / 根因 / 修复 / 验证 / 防再犯。
`AGENTS.md` 只放改代码时的规则，长记录放这里。

## 2026-09-18 加密静态存储让 SQL LIKE 搜索“静默无结果”

- 现象: 给消息历史加关键词搜索时，按 `content LIKE '%kw%'` 查询恒为空——库里明明有这条消息，
  界面和测试都搜不到，也没有任何报错（LIKE 对密文就是不匹配）。
- 根因: 聊天正文在静态存储时被加密成 `enc1:<base64>`（Windows `secretbox.protect`、
  Android `StoreCipher.protect`），`saved_messages.content` 列不是明文；
  `saved_groups.lastMessage` 同理。
- 修复: 两段式匹配。静态明文列走转义后的 SQL LIKE 预筛
  （`windows/localchat/storage.py` 的 `search_messages` / `escape_like`，
  Android `data/ChatDao.kt` 的 `searchByNameColumns`），正文再由
  `_dec()` / `StoreCipher.unprotect()` 解密后逐条 `in` / `contains` 匹配
  （Android `data/MessageSearch.kt` 的 `matches`）。`%`、`_`、`\` 均转义为字面量。
- 验证: `cd windows; python -m pytest tests/test_functional.py -q -k MessageSearchStoreTest`
  （`test_like_wildcards_are_literal` 断言通配符只做字面匹配，
  `test_content_search_across_groups_and_direct` 断言密文正文仍可搜到）；
  Android `.\gradlew.bat testDebugUnitTest --tests "*MessageSearchTest"`。
- 防再犯: 任何对「消息正文 / 最后一条预览」过滤、排序、统计的新代码，都不能直接对 `content`
  列做 SQL 比较；要么解密后匹配，要么只查静态明文列。

## 2026-09-18 全量 Windows 测试在机器忙时出现“假失败”

- 现象: 紧接着 Android Gradle 构建跑 `cd windows; python -m pytest tests -q`，出现 1~4 个网络
  用例失败（如 `P2PNetworkTest::test_chat_and_delete_sync`、
  `test_protocol.py::AndroidClientToPythonHost`），且每次失败的用例不同；单独重跑全部通过。
- 根因: 这些用例依赖 5~10s 的 `wait_until` 超时窗口，机器被 Gradle/Kotlin 构建占满时握手与重连
  超时，属于负载型时序假失败，与代码改动无关。
- 修复: 无代码修复；跑全量套件前先让构建等重负载进程结束。
- 验证: 同一提交下静置后 `python -m pytest tests -q` → `218 passed`；单独重跑失败用例 → 21 passed。
- 防再犯: 全量套件成片失败时，先单独重跑失败用例再判断是否回归；不要把一个运行里成片的网络超时
  当成功能破坏。

### 续报（同日，五分支合并验证）：负载源不止 Gradle 构建

- 补充现象: 机器上同时有 Android 模拟器（qemu-system-x86_64，约 2.3GB 常驻、CPU 6000s+）和
  两个 Gradle 守护进程（各约 2GB）时，同一提交连续三次全量跑分别失败
  `test_functional::test_folder_offer_grouping_and_download`、
  `test_functional::test_group_page_shows_typing_names`、
  `test_group_admin::test_owner_kick_removes_member_everywhere`
  与 `test_punch::test_join_via_server_wrong_password_rejected`（失败集合每次不同）；
  期间还偶发一次 `CALL ERROR: Exceptions caught in Qt event loop: TypeError: () missing 1
  required positional argument: 'key'`，无论装 `sys.excepthook` / `threading.excepthook` /
  `qInstallMessageHandler` 都抓不到堆栈，且目标用例随运行漂移，未定位到任何代码路径。
- 结论: 与上一条同因（负载型时序假失败 + 偶发 Qt 层噪音），非功能回归；单独重跑（含 kick/punch）
  全部通过，`testDebugUnitTest` 与 `assembleDebug` 均成功。
- 防再犯: 全量验证前确认没有模拟器/Gradle 守护进程在跑（`Get-Process` 看
  qemu/java），必要时先停掉再跑；出现 `CALL ERROR ... TypeError` 时先隔离重跑，
  不要据此改代码。

### 续报（2026-09-19）："key" TypeError 幽灵已定位并修复

- 新证据: 无人工负载、连续多轮全量（约半数轮次）复现 `TypeError: () missing 1 required
  positional argument: 'key'`。pytest-qt `_except_hook` 包装探针 + `faulthandler` 实锤：
  `tb=None`，主线程正在 `_process_events` 泵 Qt 事件；归因用例随运行漂移
  （ViewModelFlowTest 两个、`test_group_admin::test_owner_kick_removes_member_everywhere`、
  `test_direct_chat` typing）。全库仅 `view_model.py` 的
  `lambda key=scope_key: self._stop_typing(key)` 以 'key' 为首参。
- 根因: ①连到 QTimer.timeout 的「带默认参 lambda」槽对 PyQt 的槽 arity 探测脆弱；
  ②`ChatViewModel.shutdown()` 早先不停止任何 QTimer，且 shutdown 之后仍可能有排队信号
  （typing / raw_tray）在后续用例的事件泵里重新武装定时器——被泄漏 ViewModel 的定时器
  于是在别人的测试里派发。
- 修复: `view_model.py` —— 该 lambda 改为零参闭包（不存在 arity 歧义）；
  `shutdown()` 停掉全部自有定时器、清空 typing 状态并置 `_shutdown_done` 守卫
  （`_typing_activity` / `_ensure_typing_timer` / `_on_raw_tray` 幂等化，
  排队信号不能再复活定时器）。
- 验证: 回归测试
  `windows/tests/test_functional.py::ViewModelFlowTest::test_shutdown_stops_every_owned_timer`
  （断言 shutdown 后 `findChildren(QTimer)` 无 active）；修复后连续多轮全量
  `286 passed`，探针零命中。
- 防再犯: 连到 Qt 信号的 lambda 一律不带默认参（用闭包工厂，或显式第一参吸收信号实参，
  见 AGENTS.md §4）；持有 QTimer 的对象必须在 shutdown/teardown 停止它们；
  再遇同类"幽灵"，先上 pytest-qt hook 探针拿实锤，再动代码。

### 续报（2026-09-19 晚）：'key' 幽灵已修复；同类原生 AV 噪音归档

- 新证据: 修复当天晚些时候，全量套件在另一台时段出现 Windows 原生崩溃
  （WER：`0xc0000374` 堆损坏 / `0xc0000005`，ntdll/python313.dll，异常线程
  `<invalid frame>`，崩溃点随运行漂移：kick 用例、test_protocol 开头、前几个用例等），
  一度连续 5 轮复现。关键排除证据：**把 network.py 整体 stash 回原版后同样崩溃**，
  且崩溃轮与通过轮交替（287 passed ×2 → AV → 287 passed ×2）。
- 结论: 原生层（C 扩展/Qt）偶发损坏，与本次 Python 改动无关，属机器状态敏感的
  环境噪音（同 2026-09-18 条），无法在本仓库内修复。
- 顺带修复（同日）: `AcceptRequestImmediateDialTest` 与 HeartbeatTest 的间歇
  bind/查询超时根因是**固定测试端口跨全量轮次复用**——Windows 下
  `SO_EXCLUSIVEADDRUSE` 不豁免 TIME_WAIT，上一轮遗留的 TIME_WAIT 让监听绑定
  重试数秒。改为每轮 `_free_port()` 临时端口
  （test_security_fixes.py / test_protocol.py），回归验证多轮全量绿。
- 防再犯: 测试监听端口一律用临时端口或轮次内随机，不要跨轮复用固定值；
  遇原生 AV 先看 WER（`Get-WinEvent` Application 日志搜 python），再用
  stash 单文件法排除改动归因，不要凭直觉回改代码。

