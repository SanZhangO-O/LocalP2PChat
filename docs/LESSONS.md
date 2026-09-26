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

## 2026-09-19 续报 5：Android 输入行 6 个动作按钮与输入框同排，输入框被挤没

- 现象: 群聊/直聊底部把 文件/图片/视频/文件夹/表情/语音 6 个 48dp IconButton
  与 OutlinedTextField 放进同一个 Row，360dp 宽屏上输入框被压到几乎没有宽度。
- 修复: 新增共用组件 `ui/screen/ChatInputBar.kt`（群聊与直聊都换用它）：行内只留
  「+ 切换 / 输入框 / 发送」，6 个动作折叠进「+」面板（带文字标签 + 原
  content-desc）；录音中「+」变成红色停止按钮并显示秒数，面板收起也能停止发送。
- 验证: 同步改 `android/tools/emulator_e2e.py` 文件发送分支——先点
  `desc="更多发送选项"` 再点 `desc="发送文件"`（折叠后按钮不在初始树上）；
  建议 `cd android; .\gradlew.bat compileDebugKotlin`，再跑 `emulator_e2e.py`。
- 防再犯: Android 自动化按 `content-desc` 找节点，挪动/折叠按钮必须同步更新
  `android/tools/emulator_e2e.py`（以及 `windows/tests` 里任何 adb 驱动脚本）里的
  desc；底部输入行只放高频三件套，其余动作一律进「+」面板。

## 2026-09-19 续报 6：Windows 输入框不约束高度，按钮贴着底边漂在左下角

- 现象: 群聊/直聊底部把 通话/语音/📎/😀/🎤 与 `QTextEdit` 放进同一个
  `QHBoxLayout`，按钮全部 `AlignmentFlag.AlignBottom`。QTextEdit 默认 sizeHint
  约 120px，输入框变成一个半空的大方块，按钮只贴到它的底边，视觉上像浮在
  输入框左下方，和发送按钮基线也对不齐。
- 根因: `AlignBottom` 只决定按钮在行内的位置，不会让行高收敛到按钮高度；
  QTextEdit 不设约束就按 sizeHint（含滚动区）占高。
- 修复: 输入框独占一整行并 `DroppableTextEdit.enable_auto_grow()`
  （`widgets.py`：起步 40px、随文档长到 120px，`resizeEvent` 重算以处理换行），
  动作按钮移到下方独立 action row，发送按钮固定在行尾；底部容器改为
  `QFrame#composer`、头部 `QFrame#chatHeader`（白底 + 1px 分隔线），新增
  `composerAction` / `composerText` 按钮样式（`theme.py`）。
- 验证: 离屏渲染 `DirectChatPage` / `ChatPage`（920x680）确认空输入 42px、6 行
  草稿长到 122px、清空回落 42px，按钮与发送同一基线；
  `windows/tests/_real_gui_win.py`、`_real_exe_gui.py` 的坐标点击已同步为
  输入行 `b-72`、发送 `b-28`。
- 防再犯: 输入框不要和按钮同排放 `AlignBottom` 对齐；底部动作统一走 composer
  的独立 action row，输入框高度由 `enable_auto_grow` 管理。

## 2026-09-21 离屏渲染走查：深色系统下 Fusion 调色板未固定，未样式化控件全变黑

- 现象: 程序只 `app.setStyle("Fusion")` 没有固定 palette。Windows 深色模式下
  `app.palette().window()` 实测 `#1e1e1e`，APP_QSS 没显式命名的控件
  （搜索页 QComboBox、表情面板 QToolButton、群公告 QPlainTextEdit、
  QScrollArea 视口）全部黑底白字，和浅色主题撕裂。
- 根因: QSS 是「按名字覆盖」，没有命中的控件回落到 application palette；
  Fusion 风格不重置 palette，直接继承操作系统的深色调色板。
- 修复: `theme.py` 新增 `light_palette()`（与主题常量同源的完整浅色
  QPalette，含 Disabled 组），`main.py` 在 `setStyle("Fusion")` 之后
  `app.setPalette(light_palette())`。
- 验证: `windows/tests/test_functional.py::RenderWalkthroughFixTest::
  test_app_pins_light_fusion_palette` 断言调色板颜色、Disabled 组与
  main() 接线。
- 防再犯: 改 QApplication 级样式（style/QSS/palette/font）时必须成套给全：
  Fusion 一定要配显式 palette；新增依赖系统调色板的控件类型时，
  优先在 QSS 里命名或在 palette 里核对。

## 2026-09-21 离屏渲染走查：固定尺寸按钮吃全局 QSS 内边距，中文字被裁

- 现象: 群大厅成员行的「安全码」按钮 `setFixedSize(64, 40)`，全局
  `QPushButton { padding: 10px 20px; }` 让可用内容宽只剩 24px，三个汉字
  约 45px，只显示中间一个字；两字按钮同样被裁掉两侧。
- 根因: QSS 内边距按规则参与内容区计算，固定尺寸不会让文字缩排或换行，
  超宽即静默裁剪（无告警）。
- 修复: `theme.py` 增加 `QPushButton[compact="true"] { padding: 10px 6px; }`，
  `group_lobby_page.py` 的 PeerRow 四个按钮统一 `setProperty("compact", True)`
  （宽度保持 64 不动，避免移动布局）。
- 验证: `RenderWalkthroughFixTest::test_lobby_peer_row_buttons_fit_fixed_width`
  断言每个按钮 `fontMetrics().horizontalAdvance(文本) <= width - 12`。
- 防再犯: `setFixedSize` 按钮必须核对「文字宽 + 左右内边距 <= 固定宽」；
  要改内边距用属性选择器（objectName 已被 ghost/danger 占用）。

## 2026-09-21 离屏渲染走查：高页面缺 QScrollArea 被压缩 + 列表行对齐/省略三则

- 现象: ① SettingsPage minimumSizeHint 高 913px、SetupPage 700px，主窗口
  920x680 可用仅 638px，布局被强压（按钮 12px 细条、卡片重叠、说明文字截断），
  1280 宽也不够；② 成员行时间戳 `addWidget(label, alignment=AlignTop)` 只有
  垂直对齐，水平方向被拉满，宽窗口下时间停在行中间；③ 列表预览用
  `setMaximumWidth(300/280)` 硬裁，无省略号，断句突兀。
- 根因: ① QWidget 页面把所有卡片的 minimumSizeHint 逐级上交，窗口一旦小于
  页面最小值 Qt 只能压缩布局而不滚动；② 布局 alignment 缺省水平分量时
  stretch 拉满整行；③ QLabel 不会自己截断加 …，只会按 maximumWidth 裁剪。
- 修复: ① SettingsPage 正文、SetupPage 的 QStackedWidget 分别包进
  `QScrollArea(widgetResizable, NoFrame)`（`settings_page.py`/`setup_page.py`，
  `theme.py` 补 QScrollArea 规则）；注意 `QStackedWidget` 的 minimumSizeHint 取
  各页最大值，Qt 也不会在 `setCurrentIndex` 时重置滚动条，所以 `show_mode()`
  里要 `scroll.verticalScrollBar().setValue(0)`，否则高页滚到底再切回矮页会停在
  偏移位置；② 时间戳改 `AlignRight | AlignTop`；
  ③ 预览统一 `QFontMetrics.elidedText`（`member_list_page.py` ContactRow
  300px、`group_list_page.py` GroupCard 280px）。
- 验证: `RenderWalkthroughFixTest` 五个用例（settings/setup 可滚动、切页滚动
  归零、按钮宽度、ContactRow 右对齐 + 省略、GroupCard 省略）；视觉复验可重跑
  `%LOCALAPPDATA%\Temp\kilo\lc_shot\shoot.py` 对照 out\ 截图。
- 防再犯: 页面高度可能超过窗口（设置/表单/多卡片页）一律包 QScrollArea，
  新增卡片字段前先看页面 minimumSizeHint；`QScrollArea` 包 `QStackedWidget`
  时切页要重置滚动条；行内「贴边」元素必须同时给
  水平 + 垂直 alignment；列表预览文本用 elidedText，不要裸 maximumWidth。

## 2026-09-21 回应菜单一打开就 NameError：lambda 默认参表里引用了前面的默认参

- 现象: 右键消息 → 「回应」子菜单弹出时崩
  `NameError: name 'e' is not defined`，指向
  `direct_chat_page.py` 的
  `lambda checked=False, m=msg, e=emoji, on=e not in mine: ...`；
  群聊页 `chat_page.py` 同款代码潜伏同一雷。
- 根因: lambda 默认参表达式在**创建时**于外层作用域从左到右求值，
  求值 `on=e not in mine` 时形参 `e` 还没绑定（形参只对函数体可见，
  对同级默认参不可见）——构建菜单即抛 NameError，菜单永远打不开。
- 修复: 把开关状态提前为普通变量
  `on = emoji not in mine` 再进默认参表
  （`direct_chat_page.py::_fill_reactions`、`chat_page.py` 回应子菜单）。
- 验证: 回归测试
  `windows/tests/test_functional.py::ViewModelFlowTest::
  test_direct_reaction_menu_builds_and_dispatches_toggle`
  （构建菜单不炸 + 已回应项 dispatch active=False、未回应项 active=True，
  走真实 triggered() 信号路径）；另用 AST 全库扫描
  「lambda 默认参引用同级更早默认参」确认无同类残留。
- 防再犯: lambda 默认参表只能放**外部已求值**的名字；
  需要派生值就在循环体里先算成局部变量再绑定。
  与 AGENTS.md §4 的「clicked bool 注入」是同一批陷阱家族：
  默认参表只用于吸收信号实参与捕获循环变量，不放表达式。

## 2026-09-21 回溯补录：语音播放回调裸赋值 int16 字节，广播形状不符每块崩一次

- 现象: 播放语音时 sounddevice 回调持续抛
  `ValueError: could not broadcast input array from shape (1600,) into
  shape (1600,1)`，无声且刷屏。
- 根因: `outdata` 是形状 `(frames, channels)` 的 numpy int16 缓冲，
  `f.readframes()` 返回的是裸 PCM 字节；直接（或按样本数切片）把
  一维 frombuffer 结果赋给二维切片会形状不符。wave/字节缓冲的坑与
  LESSONS 2026-09-19 的 `FileInputStream.getChannel()` 只读同族：
  原始字节 ↔ numpy/结构化视图之间必须显式解码。
- 修复: `audio_note.py::VoicePlayer` 回调里
  `frombuffer(data, int16, count=done*CHANNELS).reshape(-1, CHANNELS)`
  后赋 `outdata[:done]`（与 `call.py` 同法）；commits `8a72cb0` + `a345e41`。
- 验证: 真机播放语音正常（用户控制台旧堆栈来自修复前进程）。
- 防再犯: 给 sounddevice/Qt 多媒体喂数据时，先核对目标缓冲的
  dtype 与形状，再决定 frombuffer + reshape 路径；
  「裸字节直接赋二维切片」一律是形状错误。

## 2026-09-21 编辑消息四面修复：mesh 历史签名不一致 / 直聊离线半套用 / 副本别名 / Android 缺暂存

- 现象(审查发现，静态确认):
  1. mesh 编辑收敛失效：`update_mesh_message`/`updateMeshMessage` 把 mesh 副本
     的 `senderSig` 换成 edit 包签名（edit transcript），而 `history_reply`
     接收端先按 message transcript 过滤——已编辑条目验签必败、整条被静默
     丢弃；且经 host relay 收到编辑的成员不更新 mesh 副本，推历史时把
     *旧文本*（旧签名有效）推给重连成员，编辑被静默回退。离线成员收不到
     新文本，甚至整条消息丢失。注释宣称的语义与实际相反。
  2. Windows 直聊离线编辑半套用：`DirectChatManager.edit_message` 对端离线
     仍先改内存副本并刷新 UI，`edit_direct_message` 只在发送成功时落库——
     UI 显示新文本、重启回退、对端永远不知道；docstring 还谎称有 staged
     replay（从未实现）。Android 一直是干净的 online-only。
  3. Windows `mesh.broadcast` 把 relay 视图同一 ChatMessage 对象塞进 mesh
     状态：`broadcast_edit` 的原地改签会覆盖 `p2p.edit_message` 刚做的
     messageParts 重签。
  4. Android 群编辑无任何离线暂存（Windows 有 pending_ops），双路径断时
     编辑永不重放；Windows storage docstring 引用的「ChatDao.stagePendingOp
     Android parity」并不存在。
- 修复(两端同步；项目未发布，按「无历史包袱」直接改协议语义，不留旧版容忍):
  - edit_message 的 `senderSig` 改为覆盖「编辑后完整消息」的 message
    transcript（`message_fields_parts`/`messageParts`：gid+作者+消息 id+
    副本时间戳+新内容摘要；旧的 edit transcript 与 `verify_edit`/
    `editParts` 已删除）。接收端按本地副本时间戳+新内容严格验签，通过后
    把该签名存为 mesh 历史副本的签名 → 历史推送天然通过 verify_message；
    relay 收到的编辑也镜像进 mesh 副本；作者本机未初始化身份（无法签名）
    时直接拒绝编辑，绝不发无签名编辑。
  - 历史合并恢复单一过滤（verify_message），无任何 fallback；验签不过的
    条目即使来自作者本人链路也丢弃。
  - 直聊编辑改 online-only（先 send 成功再改本地，本地与持久层永不分歧）。
  - `mesh.broadcast` 存 deepcopy，relay 视图与 mesh 状态彻底分家。
  - Android 新增 `pending_ops` 表（DB v7，FK 级联）+ `replayPendingOps`
    （connectionLost 恢复 / mesh 链路建立时触发），edit 暂存重放对齐 Windows。
- 验证: `windows/tests/test_message_extras.py::EditConvergenceTest`
  （签名编辑经真实 mesh 历史回填收敛到空历史新成员、update_mesh_message
  对无签名/错签名/异作者严格拒绝、历史批次逐条过滤：签名改写收敛 +
  enforced 作者的裸条目丢弃 + 合法新条目照收）、
  `DirectExtrasTest::test_offline_direct_edit_changes_nothing`、
  `test_group_auth.py::test_delete_edit_update_and_kick_roundtrip`
  （verify_message_fields：内容/时间戳绑定 + 无签名拒绝）、
  Android `GroupAuthTranscriptTest`（fields 版 messageParts 与对象版
  字节一致）、`MessageExtrasProtocolTest`（edit 包 wire 形态不变）。
- 防再犯:
  - 「副本签名一致性」断言必须写死在测试里：凡是改写消息内容的路径，
    改完立刻 `verify_message`/`verifyMessage` 自检（EditConvergenceTest
    的写法），不要只测「内容变没变」。
  - 签名 transcript 与消费场景必须同名同源：一个签名只能被按同一
    transcript 的校验消费；edit 专用 transcript 与 message transcript
    并存时，跨通道使用必炸（历史推送/存储副本按 message transcript 验）。
    未发布项目直接删掉多余 transcript，保持「一种操作一种签名」。
  - 在验证函数里复用通用 `_check`/`check` 时警惕 legacy 放行分支：
    「pub 缺失 → legacy accept」对改写历史的操作是验证旁路，入口必须
    显式要求签名存在（verify_message_fields 的严格前置判断）。
  - 同一可变对象不能同时挂在两个互不同步的状态视图里（relay 视图 vs
    mesh 状态），入第二视图时 deepcopy。
  - 文档/docstring 里引用「对端已有实现」（如 Android parity）前先 grep
    确认存在；不存在的「parity」注释会误导后续审查放行缺口。

## 2026-09-22 多对端审查修复：Wire 并发发送、mesh 乱序与成员路径 running 不变量

- 现象: 「一个客户端连多个服务器」专项审查发现三类跨端问题：
  1) Android 端同一 Wire 上多写者（直聊 ping/pong 直写、mesh 每包一线程、
     心跳在 IO 池）并发发送时，接收端偶发「packet sequence violation」断连
     且在途消息静默丢失；Windows 端无此问题。
  2) 两端 mesh 链路上「发消息后立刻删除/编辑」，对端（host 离线路径）
     可能先收 delete（目标不存在被丢弃）后收 chat，已删消息复活且无法
     被历史推送纠正（发送方本地已移除）。
  3) Android 群语音成员侧媒体路径瞬时自毁（读循环首轮退出 → leaveLocal）。
- 根因:
  1) Android `Wire.sendPacket` 的 seq 打戳在锁内、JSON/加密/`println` 在
     锁外，seq 顺序 ≠ 上线顺序；类文档声称「PrintWriter 序列化整行写入」
     是错的——PrintWriter 只同步单次 write，多次 write 之间可交错（行帧
     与 seq 双重损坏）。Windows `Wire.send_packet` 整体持锁，两端不一致。
  2) mesh 广播对每条链路的每包各起一个线程（Windows `_spawn(_link_write)`
     / Android 曾有 `thread{}` 与绕过 `sendExecutor` 的路径），包间无顺序
     保证；接收端对未知 id 的 delete 静默丢弃，与「发送方立刻删历史」
     组合成永久分歧。relay 路径早有单发送者（Windows send worker /
     Android sendScope）且注释写明了该乱序危害，但 mesh 路径漏修。
  3) `GroupCallManager.running` 只在 host 的 `startMeeting` 置位，成员侧
     `goActive` 从不置位，而成员读循环 live 检查、上行发送循环、引擎守卫
     全部 gate 在 running 上——成员媒体路径整体依赖一个永假的标志。
- 修复:
  1) Android sendPacket 整体纳入 `sendLock`（Windows parity）；mesh 改为
     每链路一条 FIFO 写线程（`Link.sendQueue` + `linkSender` / Windows
     `_link_sender` + `send_queue`），广播一律入队；直聊/心跳/relay 的
     并发写者由 Wire 锁保证行原子与 seq 顺序。
  2) 同上——每链路 FIFO 使 chat→delete/edit 的提交序 = 上线序；满员拒绝
     移到 mesh_ack/join_ack 之前，避免拨号方重试预算被重置后永久重拨。
  3) `goActive` 在锁内置 `running = true`（1:1 CallManager 两条路径都置位
     的 parity 是真实的）；`startEngines` 全程持锁 + `!running` 守卫，
     `leaveLocal` 引擎拆除移入锁内，闭合 leave→join 双建引擎泄漏麦克风。
- 验证: 两端 `py_compile` / `compileDebugKotlin` 通过；全量单测与互通 E2E
  按需执行（见 AGENTS.md §7）。
- 防再犯:
  - 跨端同名组件（Wire）的并发契约必须逐字节对照：一端「整体持锁」另一端
    「打戳在锁内、写在锁外」就是协议级分叉；类文档宣称的线程安全要用
    锁的覆盖范围验证，PrintWriter/BufferedWriter 的内部同步不等于调用序列原子。
  - 「每包一线程」的发送路径天然无序：只要存在 delete/edit/管理包这类
    依赖前序包的报文，发送侧必须有每链路（或每会话）FIFO 串行点；
    修 relay 时要顺手检查 mesh/直聊路径是否同病。
  - 状态标志（如 running）的置位点必须覆盖所有会消费它的角色路径；
    「读循环/引擎/发送线程都检查 X」时先 grep X 的所有赋值点。
  - 名册/连接表上限是防伪造膨胀的最后防线：check-then-act 要么与插入同锁，
    要么用原子声明（putIfAbsent / CAS update）；拒绝要发生在对端「认为
    建立成功」的信令（ack）之前，否则对方的有界重试预算会被重置成永久重拨。

## 2026-09-22 续报：复审发现同一轮修复遗留的四则缺口

- 现象: 对上面修复轮的复审发现其自身仍有四处缺口：
  1) Windows mesh 九个广播函数仍 `self._spawn(self._link_write, ...)`——
     `_link_write` 已改成入队后线程只剩一次 put，但线程「启动顺序」无保证，
     紧随 chat 的 delete 线程可能先入队，FIFO 保证根本不成立（Android 是
     调用线程内联 `enqueue`，两端再次分叉）。
  2) Android `handleJoin` 用 `_peers.update`（CAS 可重跑 lambda）设外部
     `admitted` 标志，但 else 分支不重置：失败迭代的 `admitted=true` 泄漏到
     最终拒绝路径，join_ack 发出而成员根本不在名册里。
  3) 两端 mesh 的 `mesh_ack` 都在「预检之后、原子 claim 之前」发送：满员边界
     上并发 join 仍能造成 ack-then-refuse，拨号方重试预算照样被重置——预检
     只是收窄了窗口，没有消除 TOCTOU。
  4) Windows 已把首触自动接受收紧为「仅已知设备 id」，Android DirectChat
     仍按 endpoint（ip:port）匹配自动接受，DHCP 地址复用窗口两端不一致；
     另外两端 mesh 发送队列无界，半死对端（停读但持续 ping）可撑爆内存。
- 根因:
  1) 「改成非阻塞入队」不等于修好顺序——凡是还经 `_spawn`/`thread{}`
     搬运的调用点，入队动作本身就不在同一调用线程上，顺序仍靠调度运气。
  2) CAS 循环的 lambda 会带着上一次迭代的副作用重跑；外部标志只在成功
     分支赋值就是泄漏点。
  3) 「拒绝在 ack 之前」必须是「原子 admission（claim+插入同锁/同 CAS）
     之后才 ack」，任何先预检、后发 ack、再安装的三段式都留 TOCTOU。
  4) 安全策略的跨端收紧必须 grep 对端同语义代码同步修改；有界队列是
     「不信任对端配合」的内存防线。
- 修复:
  1) Windows 九处改为内联 `self._link_write(link, packet)`（入队非阻塞，
     调用循环即串行点）；两端发送队列加界（`MESH_SEND_QUEUE_CAP = 4096`），
     溢出按死链处理（alive=false + 关 socket，读循环收尸）。
  2) else 分支补 `admitted = false`。
  3) `mesh_ack` 移入 `_register_link` / `registerLink`，在原子 claim+install
     持有之后发送；ack 发送失败有对应的安装回退（读循环尚未启动，就地清理）。
     满员拒绝与「已有活链」拒绝现在都发生在任何 ack 之前。
  4) Android `DirectChat.handleDirectHello` 改为仅 `containsKey(peer.id)`
     自动接受（Windows `_maybe_incoming_request` parity）。
- 验证: 两端 `py_compile` / `compileDebugKotlin` 通过；全量单测与互通 E2E
  按需执行（见 AGENTS.md §7）。
- 防再犯:
  - 修「每包一线程」时 grep 所有 `_spawn`/`thread` 残留：入队点必须留在
    调用线程上，否则 FIFO 是假的；两端同名机制要逐行对照调用形态。
  - `Atomic*`/`update{}` 的 lambda 里写外部标志，每个分支都要完整赋值，
    把「本次迭代的完整结论」写全，而不是只写成功路径。
  - 协议信令（ack）的发送点要紧贴原子状态变更之后：审查时画「预检 → ack →
    安装」的三段图，凡是 ack 不在原子段之后的都是 TOCTOU。
  - 跨端行为收紧（安全策略、校验、上限）同步另一端时，按语义 grep 对端
    （「首触」「auto-accept」「endpoint」），不要只对照同名函数。
  - 面向对端流量积累的队列一律有界，满队=对端不配合=按断链处理，
    而不是无界缓存或阻塞广播调用方。

## 2026-09-25 Web 端（Node 复刻）首轮可运行性审查：加密/身份/群主 id 三处硬伤 + 两处传输截断

- 现象: 对 `web/`（Node ≥ 18 多用户复刻）做挑剔审查并用最小脚本实际跑通时发现：
  1) 一切都加密不可用——`aes-gcm roundtrip` 单测本应失败，任何 `send_packet` /
  文件帧 / `enc1:` 落盘都会抛 `TypeError: cipher.getTag is not a function`
  （Node v24.14.0 实测；`crypto.Cipheriv` 只有 `getAuthTag()`）。
  2) `new DeviceIdentity(dir)`（首次运行/无身份文件）必崩：
  `TypeError: Cannot read properties of null (reading 'privateB64')`。
  3) 成员加入群后 `creatorId` 恒为空：任何 `group_update` / `kick_member`
  都被 `_handleGroupUpdateAsClient` / `_handleKickAsClient` 判为越权，
  直接把成员与 host 的 relay 断开（群公告/改名/移出成员全不可用）。
  4) 文件下载偶发「文件不完整」：发送端可能在最后一个数据帧或 4 字节 EOF
  落地前就 RST 掉连接；接收端 `.part` 保留、重试才收敛。
  5) UI/接口级：`createGroup` 不复述 `reqId`（前端 `request()` 20s 后
  超时、弹窗不关）；服务器重启后 `serveFile` 只查内存 `downloads` 表，
  已下载文件 404（快照却标 `done`）；上传超限只 `req.destroy()` 不响应，
  浏览器端一直等到超时。
  6) 服务器重启后**群主自己的群聊历史全部消失**（成员加入的群由于走
  `enterMesh` 会恢复；群主群只恢复群名/公告/密码，不读消息文件）。
- 根因:
  1) Node 与 WebCrypto 的 API 名字混淆：GCM 取 tag 是 `cipher.getAuthTag()`，
  `getTag()` 不存在；`createCipheriv` 的 tag 长度用 `authTagLength` 选项。
  代码从未真正跑过（单测存在但没人执行），所以协议栈三个实现里它一直是坏的。
  2) `_load()` 在 `this.pair = pair` 之前调 `this._save()`，而 `_save()` 读
  `this.pair.privateB64`；身份文件存在时走 else 分支不触发，掩盖了首次运行路径。
  3) Windows 的 creator 是在视图层（`view_model.py:4061-4078`）按
  「sponsor ack 的 host → query 的 creatorId → 拨号地址上的名册成员」三步
  落库的；web 复刻只在恢复存档时读 `creatorId`，加入路径完全没写。
  4) `conn.end()` 只保证「排空后发 FIN」，紧跟的 `conn.destroy()` 会直接关掉
  fd，内核里未发出的缓冲（尾帧 + EOF）被丢弃；Python 接收端以
  `received != expected` 判不完整，于是表现为可重试的假失败。
  6) `_restoreState` 的分支不对称：成员分支做了「读历史 + enterMesh」，
  群主分支只 `createHost` 后设置元数据；消息虽已加密落盘
  （`_saveGroupChat`），恢复路径没人读。
- 修复:
  - `web/server/crypto.js:45-55`：`getAuthTag()` + `authTagLength: GCM_TAG_LEN`。
  - `web/server/identity.js:51-57`：生成新密钥后先 `this.pair = pair` 再 `_save()`。
  - `web/server/group.js:539-620`：`joinGroup` 把 query 的 `creatorId` 传进
    `_joinExchange`；creator 依次取 `join_ack.host.id` / query creatorId /
    `members[0].id`，并在已存在 group 上补写。
  - `web/server/files.js:30-116`：`complete(eof)` 改为 `conn.end()` /
    `conn.end(EOF)`（排空后 FIN），10s 兜底 `destroy` + `close` 时清定时器；
    只有错误/中断路径才立即 `destroy`。
  - `web/server/account.js`：`createGroup` 回填 `reqId`；`serveFile` 改为扫
    `downloads/`（跳过 `.part`）并补 `Content-Length`；上传超限先回 413、
    再 `unpipe` + 删分片 + `req.resume()` 把剩余体读完（不 RST）。
  - `web/server/account.js:241-256`：群主分支同样 `_restoreGroupMessages`
    回填 `g.messages`（按时间戳排序），与成员分支对称。
- 验证: 用临时脚本（不入库）实跑：单 Node 直接聊（消息/已读/输入中/编辑/
  回应/置顶/文件收发/撤回）+ 首触请求卡片/接受/离线暂存重拨补发 + 群 relay
  （加入、双向消息、编辑签名收敛、公告、群已读、踢人）+ 双 Node mesh
  （mesh_chat/history_reply）+ 真实 `ChatServer` HTTP/WS（注册/登录 401/
  会话 cookie/未认证 WS 拒绝/createGroup reqId/上传 413/logout）+ 同数据
  目录重启（身份、昵称、群与历史消息、文件元数据全部恢复）全部通过；
  文件服务器 319545 字节全量 + EOF 校验通过。
  语法 `node --check web/server/*.js web/public/app.js`。
  注：现有 `web/test/unit.test.js` 的 `aes-gcm roundtrip ...` 与
  `identity tofu store persists and enforces` 本可拦住 1) 2)，本轮未跑套件
  （项目约定不自动跑测试），首次全量跑 web 单测时这两条应作为基线。
- 防再犯:
  - 复刻实现必须**实际运行**再声明完成：`web/test/*.test.js` 与临时端到端
    脚本至少各跑一次；`getAuthTag` 这类 API 名字错误不会被静态审查放过，
    但会被任何一次真实调用暴露。改 `web/` 后先跑 `node --test web\test\*.test.js`。
  - 「先赋值、后使用」的状态初始化（`this.pair`）在错误分支里最容易漏：
    新增 `_save()`/序列化调用时检查它依赖的字段是否已就绪。
  - 对端有「视图层补齐协议字段」的逻辑（如 Windows 的 creatorId 三步回退）
    时，复刻端必须在同一位置补同等逻辑，而不是只复刻网络层字段解析。
  - `end()` 与 `destroy()` 不是同一语义：正常结束只 `end()`（必要时带尾数据），
    立即销毁仅用于错误路径；凡「写响应后马上 destroy」的代码都在丢数据。
  - 服务端拒绝请求（413/400）必须先写响应再处理剩余请求体（drain），
    直接 `req.destroy()` 会把响应一起掐掉，客户端只看到网络错误。

## 2026-09-26 Web 服务端 Node.js 重写为 Python，并与信令服务器二合一

- 现象/背景: `web/` 原为 Node.js 多用户聊天服务器（HTTP+WS），信令/打洞/中继是
  另一个 Python 程序（顶层 `server/signaling_server.py`），部署要装两套运行时。
  按需求把 Web 服务端整体重写为 Python（`web/server/webchat/` 包）并与信令服务器
  合并为一个入口 `web/server/localchat_server.py`（一个进程、两个独立监听面），
  顶层 `server/` 目录随之取消、整体并入 `web/server/`。
- 根因/要点: 运行时切换最大的风险不是功能复刻，而是**老数据不可读**——
  `accounts.json` 的 scrypt 口令哈希与 `enc1:`（AES-256-GCM）落盘聊天记录都是
  Node（WebCrypto/`crypto.scryptSync`）写出来的字节，Python 侧必须逐字节复现：
  scrypt 参数 N=16384/r=8/p=1/dklen=64（Node 默认值，Python 要显式传并给足
  `maxmem`），GCM 布局 `nonce(12B)||ct||tag(16B)`，JSON 字段名 camelCase
  （`passHash`/`groupId`/`senderName`…），`web/data` 目录布局不变。
- 修复:
  - 新增 `web/server/webchat/{util,crypto,store,accounts,engine,webapp,server}.py`，
    与旧 JS 模块一一对应；WS 用标准库手写 RFC6455（mask 校验、分片 4MiB 上限、
    协议错误 close 1002），AES-GCM 用 `cryptography`（Python 标准库无 AES，
    「信令零依赖」性质在 Web 面不再成立）。
  - 统一入口 `web/server/localchat_server.py`：`--no-signaling`/`--no-web` 可只跑
    一面；`web/server/signaling_server.py` 独立运行保持不变。
  - 测试移植为 `web/tests/`（pytest），`test_unit.py` 内嵌**用旧 Node 实现生成
    的 scrypt/AES-GCM 固定向量**，把「运行时切换不得破坏老数据」变成可回归断言。
  - 文档同步：`web/README.md`（并入原信令服务器部署/客户端配置说明）、
    根 `README.md`、`AGENTS.md` §7、`web/start-server.bat`（Python 启动器，
    检查 `cryptography`）。
- 验证: `python -m py_compile` 全部新文件通过；向量生成命令
  （node -e scryptSync/AES-GCM）与 Python 复现值逐字节一致。**pytest 套件
  （`python -m pytest web\tests -q`）按项目约定未自动运行，首次验证时
  必须实际跑通**。
- 防再犯:
  - 跨运行时重写存储/加密格式时，先做固定向量互验（老运行时生成 → 新运行时
    复现），再谈功能对齐；向量要作为测试常驻，防止日后参数被「顺手优化」。
  - JS 与 Python 的字符串语义差异点：JS `String.length`/`slice` 按 UTF-16 码元，
    Python 按码点（长度上限、表情截断处注意）；`bytearray(int)` 是零填充不是
    整数字节化，大整数 XOR 解 mask 后要 `to_bytes(len, "big")`。
  - 「二合一」只合并部署形态，不合并协议面：Web 聊天与局域网协议仍互不相通，
    两端客户端与信令协议字节不变。
  - 移动 Python 包目录时，包内「按 `__file__` 相对定位的默认路径」（如
    `webchat/server.py` 的 data/public 默认目录）基准会随之漂移：挪完后必须
    逐个重算 `parents[N]` 层数并核对目标（本次 `web/server/webchat/server.py`
    的 parents[2] 从仓库根变成 `web/`），conftest 的 `sys.path` 注入路径同理。

## 2026-09-26 Web/信令服务器审查：信令 host 不弹出导致第二个成员配到正在关闭的连接

- 现象: 审查 `web/server/signaling_server.py` 时发现：同一群组先后来两个成员时，
  第二个成员会立刻收到 `matched`，但其对 host 映射地址的 punch 永远打不通
  （host 侧正忙于上一个会话、控制连接已按设计关闭），最终超时/中继兜底失败。
- 根因: `_try_match` 用 `hosts[-1]` 取 host 但**不弹出**——循环内同一 host conn
  可以连续配对多个成员。而客户端约定（`windows/localchat/punch.py` 的
  `SignalingHostBridge`）是 host 收到第一个 `matched` 就关闭控制连接、复用其
  本地端口去 punch，然后重连再注册服务下一个成员；一份 host 注册只应服务
  一次配对。服务器多发的第二个 `matched` 落在正在关闭/已关闭的连接上。
- 修复: `web/server/signaling_server.py` `_try_match` 改为 `hosts.pop()`（每份
  host 注册只配对一次），并在匹配/清理后丢弃空列表，`_cleanup` 同步清空
  `_hosts`/`_waiting` 的空组条目。
- 验证: 静态审查 + 修改后走读 host/member 两条客户端流程（punch.py 与
  signaling_server.py 的状态机逐条对照）。pytest 套件按项目约定未自动运行。
- 防再犯: 信令服务器的匹配语义必须以客户端桥的生命周期为准——**host 注册
  一次 = 一次配对**，改匹配逻辑时对照 `punch.py` 注释（「on `matched` the
  session thread closes it … reconnects and re-registers」）逐条核对。

## 2026-09-26 Web 套件首次实跑：GCM 解密剥掉 tag 导致历史全部读不出 + WS 测试客户端不解析「随握手到达」的帧

- 现象: `python -m pytest web\tests -q` 首次实跑 5 失败：AES-GCM roundtrip、
  Node 固定向量、SecretBox/Store 落盘回读全部 `InvalidTag`/空串；
  `test_server.py` 的 WS 登录偶发「timeout waiting for ws message; docs: []」
  （且在测试间游走，隔离跑能过、全量跑挂）。
- 根因:
  1) `webchat/crypto.py` 的 `aes_gcm_decrypt` 把 blob 切成
     `blob[12:-16]` 再交给 `AESGCM.decrypt`——但 `cryptography` 的 API 约定是
     **decrypt 收到密文时 tag 必须拼在末尾**（`encrypt` 返回 `ct||tag`），
     自己剥 tag 等于把 tag 丢掉，必然 `InvalidTag`。落盘路径 `unprotect`
     吞掉异常返回 ""，表现为「重启后聊天历史全空」。上一节记录的
     「pytest 按约定未自动运行」正是它没被拦住的原因。
  2) `web/tests/helpers.py` 的 `WsClient` 只在 `_read_loop` 的 `recv` 里
     解析帧；当 101 握手响应与首个服务器帧挤进**同一个 TCP 段**时，帧字节
     存进 `initial` buffer 后永远没人解析（下一个 recv 无数据），
     `docs` 恒空——纯测试辅助端的分段竞态，服务器确实发出去了
     （用 sendlog + `len(ws.buffer)` 实测：504 字节躺在 buffer 里）。
- 修复: `crypto.py` decrypt 直接传 `blob[12:]`（含 tag）；`helpers.py`
  `WsClient.__init__` 在启动读线程前先 `self._feed(b"")` 解析预缓冲帧。
- 验证: 自制 25 连接复现脚本（故障率 3/25 → 0/25）；全量
  `python -m pytest web\tests -q` 连跑 4 次全绿（15 passed，约 5.3s，
  不再有 5s 超时拖尾）。
- 防再犯:
  - 加解密对称性必须靠「roundtrip + 外部固定向量」双向断言，且**改完运行时
    的第一件事就是实跑套件**（本项目约定「不自动跑测试」意味着人工节点必须跑，
    不是可以不跑）。
  - WS/HTTP 测试客户端若存在「initial buffer」路径，构造时就地解析一次，
    否则同段到达的帧会被静默吞掉（表现为随机超时，隔离跑能过）。

