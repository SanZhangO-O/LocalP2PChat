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
