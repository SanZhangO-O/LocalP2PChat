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

## 2026-09-19 消息体验功能（编辑/回应/提及/置顶/群已读/语音/贴纸）三则

### 1. Kotlin 嵌套泛型结尾 `>>>` 被 K2 词法器拆成 `>>` + `>`

- 现象: `mutableStateOf<Map<String, List<Pair<String, String>>>>(emptyMap())`
  报「Interface Map does not have constructors」「Unresolved compareTo」等
  连串不知所云的错误；同文件 `List<...>>`（两级）完全正常。
- 根因: Kotlin 2.x 引入无符号右移运算符 `>>>` 后，连续三个 `>` 的泛型收尾
  在部分上下文按运算符 token 化，类型实参解析被打断。
- 修复: 文件级 `private typealias ReactionMap = Map<String, List<Pair<String, String>>>`
  绕开三连 `>`（MainActivity.kt）。
- 验证: `.\gradlew.bat compileDebugKotlin` 通过。
- 防再犯: 嵌套泛型两层以上时优先起 typealias；再遇泛型收尾 `>>>` 的怪错，
  先怀疑词法而非类型系统。

### 2. Python `is_big_emoji` 与 Kotlin 迭代粒度不同：UTF-16 代理对

- 现象: Windows 端 `is_big_emoji("👍")` 为 True，Android 首版 `isBigEmoji`
  对同一输入返回 False，贴纸检测单测失败。
- 根因: Python 字符串按**码点**迭代，Kotlin/Java String 按 **UTF-16 码元**
  迭代——高位代理对（0xD83D）不落进任何 emoji 区间，`all {}` 直接失败。
- 修复: Kotlin 侧用 `codePointAt` + `Character.charCount` 手动按码点扫描
  （`data/Models.kt` 的 `isBigEmoji`），上限也改为「16 个码点」而非 16 个
  char，与 Windows `models.is_big_emoji` 逐条对齐。
- 验证: `EmojiSupportTest`（贴纸页全部 emoji 必须被判为大 emoji）+
  `MessageExtrasProtocolTest.isBigEmoji` 用例。
- 防再犯: 任何按字符分类/截断/计数的跨端逻辑（emoji、长度上限、切面），
  Python 端按码点、Kotlin 端必须显式按码点处理，不要直接迭代 Char。

### 3. Python 读循环 listener 属性名不一致，异常被外层 except 吞掉

- 现象: 直聊新增的 reaction/edit/pin 处理器里用了 `self.listener`（P2PManager
  的属性名），而 `DirectChatManager` 的属性是 `self._listener`——AttributeError
  被读循环外层 `except` 整体吞掉，表现为「包已编码并到达对端、处理器就是不执行、
  会话悄然断开」，无任何日志。
- 根因: 两套 manager 的回调属性命名不一致 + 读循环用粗粒度 try/except 包住
  整个循环，处理器内的 AttributeError 终结循环而非报错。
- 修复: 统一改用 `self._listener` 并对每个回调单独 try/except +
  `logger.exception`（network.py 直聊读循环三处）。
- 验证: `windows/tests/test_message_extras.py::DirectExtrasTest`（事件回调
  断言此前全空、修复后命中）。
- 防再犯: 给读循环加处理器时，回调访问必须带空值保护并单独捕获记录；
  新增「包已到达但处理器未触发」类问题时，第一反应是在 wire 层打印已解码
  包类型定位断点，而不是怀疑编码。

### 4. `INSERT OR REPLACE` × 新外键级联 = 静默删数据；移动会话必须先重键子表

- 现象: 消息体验上线后，代码审查复现出三处数据丢失：①Windows `insert_messages` /
  `move_messages` 的 `INSERT OR REPLACE` 在 (groupId,id) 冲突时删除旧行，新增的
  `message_reactions`/`pinned_messages`/`group_reads` 随外键级联一起被清空；
  ②Android 直聊占位键迁移 `UPDATE OR REPLACE saved_messages SET groupId=...`
  在存在子行时直接 `FOREIGN KEY constraint failed`，被 `runCatching` 吞掉后
  表现为「会话历史消失」；③两者都让「已回应/已置顶的消息」在重插或迁移后丢失状态。
- 根因: REPLACE 是 delete+insert（SQLite/Room 同理），而子表 FK 是
  ON DELETE CASCADE / ON UPDATE NO ACTION；新增子表时没人回头改这些既有写路径。
- 修复: 父行改写一律 UPSERT（`ON CONFLICT(groupId,id) DO UPDATE SET ...`），
  绝不 REPLACE；移动会话按「复制/刷新父行 → 重键子表 → 删除源父行」顺序执行，
  Android 用 `@Transaction` 默认方法把三步绑成一个事务（ChatDao.moveMessages）。
- 验证: `windows/tests/test_message_extras.py` 的
  `test_reinsert_keeps_extras` / `test_move_into_existing_target_keeps_target_extras`；
  修复前两者都会失败（子行丢失），修复后 27 项全绿；Android 单测 + 编译通过。
- 防再犯: 给任何表加 `ON DELETE CASCADE` 子表时，先搜出所有
  `INSERT OR REPLACE`/`UPDATE OR REPLACE` 写路径并逐一评估级联影响；
  跨表移动键值必须显式重键全部子表并满足 FK 顺序。

### 5. 绘制回调里查数据库；群已读回执全表解密读

- 现象: Windows 反应气泡的 `reactions_provider` 在 delegate 的 `sizeHint`/`paint`
  里逐条查 SQLite（每次绘制两次查询），滚动/布局/GIF 每帧都触发；群已读回执
  处理整表读取并逐条解密全部消息体；Android `readLabelFor` 对每个组合项做全表扫描。
- 根因: 把「数据读取」放进了纯渲染路径；读取接口又恰好是「整会话 + 解密」级别。
- 修复: 反应表每次重建/刷新时一次性读入页面缓存（委托只做字典查询）；回执改为
  两条定向 SQL（`timestamp` 单行查询 + `isFromMe=1 AND timestamp<=?` 的 id 列表）；
  Android 已读标签用 `remember(messages, groupReaders, memberCount)` 单趟排序计算。
- 验证: 两端全量单测 + 手工核对调用点（paint/sizeHint 不再触碰 store）。
- 防再犯: delegate 的 paint/sizeHint、Compose 的组合体只允许访问不可变缓存；
  新增「每个对端每消息都会触发」的处理函数时，先问「这条 SQL 会不会读整表」。

## 2026-09-19 续报：审查发现的四个跨端/兼容根因

### 1. `FileInputStream.getChannel()` 只读：WAV 尺寸回填静默失败，Android 语音在 Windows 端 0:00

- 现象: Android 录的语音在 Windows 上时长显示 0:00、点击播放无声（Android 本端
  正常，因为它按时长=文件长度推算、MediaPlayer 宽容解析 chunk size）。
- 根因: 录音收尾用 `FileInputStream(out.fd).getChannel()` 回填 RIFF/data 长度，
  而该 channel 是**只读**的（`FileInputStream.getChannel()` 固定 readable=true,
  writable=false），`write` 抛 `NonWritableChannelException`；异常被
  `runCatching` 吞掉，header 里两个长度字段保持 0。Python `wave.open` 按 data
  chunk size 计算 `nframes` → 0 帧、无声。
- 修复: 新 `VoiceNotes.patchWavSizes` 用 `RandomAccessFile(file, "rw")`；header
  构造/回填抽成纯函数（`wavHeader`/`patchWavSizes`）便于 JVM 单测。
- 验证: `android/app/src/test/java/com/zqr/localchat/VoiceNotesTest.kt`
  （断言 bytes 4-7 = 36+dataBytes、40-43 = dataBytes、采样率、0.5s 半上进位）；
  JDK 实验确认 `FileInputStream(fd)` channel 写入必抛 NonWritableChannelException。
- 防再犯: 任何「先写占位头、结束回填」的二进制格式，回填必须用可写句柄
  （`RandomAccessFile` / `FileOutputStream(fd).getChannel()`）；回填函数禁止
  静默吞异常——那种「写入被拒绝」的错误必须让测试失败而不是留一个坏文件。

### 2. Room/SQLite UPSERT（`ON CONFLICT ... DO UPDATE`）需要 SQLite 3.24 = API 30，minSdk 24 上静默失败

- 现象: 占位会话（`direct:ip:port`）迁移到真实设备 id 后历史消失——迁移语句在
  Android 7-10 上 prepare 阶段即报语法错，被调用方 `runCatching` 吞掉，源行未删、
  目标行未刷新。
- 根因: 3.24（2018）才引入 UPSERT 语法；Android 11（API 30）之前内置
  SQLite 3.9-3.22，而项目 minSdk=24 且未配置 bundled SQLite 驱动。
- 修复: 改成可移植两步：`INSERT OR IGNORE ... SELECT` 补缺行 +
  相关子查询 `UPDATE`（`ChatDao.refreshMovedMessages`）刷新既有行，仍由
  `@Transaction moveMessages` 保证「父行 → 子表重键 → 删源行」顺序。
- 验证: `.\gradlew.bat testDebugUnitTest` + `assembleDebug` 通过（Room 编译期
  校验两条 SQL）。
- 防再犯: 写 Room `@Query` 前先对照 **minSdk 对应 SQLite 版本**（API 24=3.9、
  28=3.22、30=3.28）；涉及删除重建语义的父行改写一律「IGNORE + UPDATE」，
  不要 UPSERT/REPLACE；`runCatching` 里的 DB 写路径必须能暴露失败（日志或返回
  值），否则兼容问题会伪装成「没有变化」。

### 3. edit_message 监听器不看 apply 结果：伪造编辑被写进数据库（Windows 直聊）

- 现象: 对端发 `edit_message` 篡改我自己的消息：内存列表正确拒绝（气泡不变），
  但 ViewModel 的持久化监听器照常收到通知，`update_message_content` 无作者条件，
  重启后 DB 里的消息正文变成伪造文本。
- 根因: 读循环里 `_edit_message(...)` 的返回值被忽略，监听器无条件回调；
  且存储层 UPDATE 只按 (groupId,id) 定位，没有作者条件（Android 用返回值门控，
  两端行为分叉）。
- 修复: ①读循环 `if packet.sender_id == peer_id and self._edit_message(...)` 才
  回调；②`ChatStore.update_message_content(..., sender_id=None)` 支持 `AND
  senderId = ?` 作者条件（Windows），Android 加
  `ChatDao.updateMessageContentFrom` 并在 `persistEditedContent` 传入期望作者；
  群编辑持久化同样带上 senderId。
- 验证: `test_message_extras.py::DirectExtrasTest::test_forged_edit_of_peer_message_is_never_persisted`
  （正向对照：合法编辑必须到达监听器；负向：伪造包不增加回调数），
  `ExtrasStoreTest::test_update_message_content_author_gate`；负向对照跑过
  （去掉门控即失败 2 != 1）。
- 防再犯: 「网络层拒绝 + 监听器照样通知」是伪造落库的经典组合——凡是
  listener/持久化回调，先确认上游校验函数的返回值被用于门控；存储层写接口
  对「发送者声明」类字段一律支持并优先使用作者条件。

### 4. 页面隐藏/切会话不停止资源：录音继续并向新会话发送、GIF 动画不停

- 现象: Windows 群聊/直聊录音中切会话或返回，麦克风继续采、60 秒上限触发时
  `vm.send_file` 把语音发进「当前打开的会话」；GIF 动画在页面隐藏后仍每帧重绘，
  QMovie 回调可能触碰已销毁的 viewport。
- 根因: 页面只清了状态字典，没有生命周期钩子；栈切页隐藏不会通知页面。
- 修复: 两端各自加「离开即清理」——Windows `hideEvent`/`teardown`（中止录音
  cancel、停 extras/reveal/voice 定时器、`delegate.clear_movies()`），
  `open_chat`/`_on_back`/`_on_group_changed` 显式 abort，`MainWindow.closeEvent`
  统一调页面 `teardown()`；Android `VoiceRecorder.cancel()` 改为非阻塞（主线程
  onDispose 只置标志，采集线程自行删文件），播放器 `onFinished` 移入
  `DisposableEffect`；录制目录启动时 prune。
- 验证: 全量单测（Windows 318 项、Android 单测+assembleDebug）通过；
  `VoiceHelpersTest::test_cancel_is_safe_when_idle`。
- 防再犯: 任何持有原生资源（麦克风、播放器、动画、定时器）的页面，必须在
  hide/teardown 路径显式停止；「60 秒后自动发送」这类延迟动作要绑定发起时的
  目标，页面切换时中止而不是让它落到当前会话。

