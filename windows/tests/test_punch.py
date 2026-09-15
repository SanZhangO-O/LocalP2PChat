"""跨网段加入端到端测试（信令服务器 + TCP 打洞 + 中继兜底）。

拓扑：SignalingServer（真实的 server/signaling_server.py）跑在本机随机端口，
主机端 HostGroupServer + SignalingHostBridge 注册群组，成员用
P2PManager.confirm_join_via_server 凭数字 ID 加入：

  test_join_via_server_direct_punch  打洞路径（环回口 simultaneous open）
  test_join_via_server_relay_fallback  punch_timeout 压到 0.01 强制打洞失败，
                                     双方退回服务器加密中继完成加入

两个测试都断言：加入成功、双向成员可见、群名经 keep-open query 学到、
聊天双向送达 —— 即打洞和中继产出的 socket 对上层协议完全等价。

Chinese literals are written as unicode escapes so the file stays pure-ASCII
on disk. Run with:  python -m pytest tests/test_punch.py -q
"""

import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WINDOWS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO_ROOT, WINDOWS_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from server.signaling_server import SignalingServer

from localchat.network import HostGroupServer, P2PListener, P2PManager

PASSWORD = "pass123"
HOST_NAME = "\u4e3b\u673aA"  # 主机A
GROUP_NAME = "\u7fa4X"  # 群X


class Rec(P2PListener):
    def __init__(self):
        self.join_results = []

    def join_state_changed(self, p2p):
        if p2p.connection_result is not None:
            self.join_results.append(p2p.connection_result)


def wait_until(cond, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class PunchJoinTest(unittest.TestCase):
    def setUp(self):
        self.sig = SignalingServer(0)
        self.sig.start()
        self.cleanup = []

    def tearDown(self):
        for fn in reversed(self.cleanup):
            try:
                fn()
            except Exception:
                pass
        self.sig.stop()

    def _make_host(self, port):
        server = HostGroupServer(port)
        server.ensure_running()
        server.password_lookup = lambda mode, gid: PASSWORD
        host = P2PManager(Rec(), port=port, password=PASSWORD, host_server=server)
        host.initialize_as_host(HOST_NAME, GROUP_NAME, password=PASSWORD)
        host.set_join_id(host.numeric_group_id)
        host.start_as_host()
        server.enable_signaling("127.0.0.1", self.sig.port)
        self.cleanup.append(server.shutdown)
        self.cleanup.append(host.stop)
        return server, host

    def _join(self, member_port, punch_timeout=None):
        server, host = self._make_host(member_port - 10)
        member = P2PManager(Rec(), port=member_port)
        self.cleanup.append(member.stop)
        member.initialize_as_client("\u6210\u5458B", "", PASSWORD)  # 成员B
        member.set_join_id(host.numeric_group_id)
        if punch_timeout is None:
            member.confirm_join_via_server("127.0.0.1", self.sig.port)
        else:
            member.confirm_join_via_server(
                "127.0.0.1", self.sig.port, punch_timeout=punch_timeout
            )
        self.assertTrue(
            wait_until(lambda: member.connection_result is not None),
            "join must finish (punch or relay)",
        )
        ok, message = member.connection_result
        self.assertTrue(ok, message)
        # the group display name was learned over the keep-open query
        self.assertEqual(member.group_name, GROUP_NAME)
        self.assertTrue(wait_until(lambda: host.my_id in member.peers), "host in member peers")
        self.assertTrue(wait_until(lambda: member.my_id in host.peers), "member in host peers")
        return host, member

    def test_join_via_server_direct_punch(self):
        host, member = self._join(19622)
        self.assertTrue(wait_until(lambda: host.server_error is None))
        # chat must flow BOTH ways over the established channel
        self.assertIsNotNone(member.send_message("hello from member"))
        self.assertTrue(
            wait_until(
                lambda: any(m.content == "hello from member" for m in host.messages)
            ),
            "host received member chat",
        )
        self.assertIsNotNone(host.send_message("hello from host"))
        self.assertTrue(
            wait_until(
                lambda: any(m.content == "hello from host" for m in member.messages)
            ),
            "member received host chat",
        )

    def test_join_via_server_relay_fallback(self):
        # punch_timeout=0.01 for BOTH sides (the host bridge reads the module
        # global): every connect attempt fails on the spot, both sides report
        # failure and the session degrades to the server relay
        import localchat.punch as punch_module

        old_timeout = punch_module.PUNCH_TIMEOUT
        punch_module.PUNCH_TIMEOUT = 0.01
        try:
            host, member = self._join(19632, punch_timeout=0.01)
        finally:
            punch_module.PUNCH_TIMEOUT = old_timeout
        self.assertIsNotNone(member.send_message("relay hello"))
        self.assertTrue(
            wait_until(
                lambda: any(m.content == "relay hello" for m in host.messages)
            ),
            "host received chat over the relay",
        )
        self.assertIsNotNone(host.send_message("relay reply"))
        self.assertTrue(
            wait_until(
                lambda: any(m.content == "relay reply" for m in member.messages)
            ),
            "member received chat over the relay",
        )

    def test_join_via_server_wrong_password_rejected(self):
        server, host = self._make_host(19642)
        member = P2PManager(Rec(), port=19643)
        self.cleanup.append(member.stop)
        member.initialize_as_client("\u8def\u4eba", "", "wrongpassword")  # 路人
        member.set_join_id(host.numeric_group_id)
        member.confirm_join_via_server("127.0.0.1", self.sig.port, punch_timeout=0.01)
        self.assertTrue(
            wait_until(lambda: member.connection_result is not None),
            "join attempt must finish",
        )
        ok, message = member.connection_result
        self.assertFalse(ok, "wrong password must not join")
        self.assertIn("\u5bc6\u7801", message)  # 密码

    def test_join_via_server_unknown_group(self):
        # a member id that no host registered: pairing times out with a
        # clear error instead of hanging (MATCH_WAIT patched down so the
        # test stays fast)
        import localchat.punch as punch_module

        member = P2PManager(Rec(), port=19653)
        self.cleanup.append(member.stop)
        member.initialize_as_client("\u8def\u4eba", "", PASSWORD)
        member.set_join_id("00000000")
        old_wait = punch_module.MATCH_WAIT
        punch_module.MATCH_WAIT = 3.0
        try:
            member.confirm_join_via_server("127.0.0.1", self.sig.port, punch_timeout=0.01)
            self.assertTrue(
                wait_until(lambda: member.connection_result is not None, timeout=30.0),
                "unmatched join must fail, not hang",
            )
        finally:
            punch_module.MATCH_WAIT = old_wait
        ok, message = member.connection_result
        self.assertFalse(ok, message)


if __name__ == "__main__":
    unittest.main()
