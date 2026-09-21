"""QR invite payload codec + UI entry tests (Windows side).

The payload format must stay in sync with Android's network/QrInvite.kt:
    localchat://contact?v=1&n=<name>&f=<security code>&ip=<addr>&p=<port>
    localchat://group?v=1&g=<8-digit id>&n=<group name>&ip=<addr>&p=<port>&r=<relay>

The group password is NEVER part of a payload; decoding ignores any
password-looking parameter. Chinese literals are written as \\uXXXX escapes
so this file stays pure-ASCII on disk.

Test ports are per-run ephemeral (LESSONS: never reuse fixed ports across
full-suite rounds).
"""

import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton  # noqa: F401  (offscreen app)

import localchat.models as models_module
import localchat.network as network_module
import localchat.qrshare as qrshare
from localchat.models import Peer
from localchat.qrshare import (
    ContactInvite,
    GroupInvite,
    QrPayloadError,
    encode_contact_invite,
    encode_group_invite,
    parse_invite,
)
from localchat.securewire import DeviceIdentity
from tests.fake_peer import install_identity
from tests.test_functional import _fresh_db, make_vm

_APP = QApplication.instance() or QApplication([])


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class QrContactCodecTest(unittest.TestCase):
    def test_roundtrip_full(self):
        text = encode_contact_invite(
            name="\u5f20\u4e09", fingerprint="AB12CD34EF567890",
            ip="192.168.1.5", port=10001,
        )
        self.assertTrue(text.startswith("localchat://contact?"))
        invite = parse_invite(text)
        self.assertIsInstance(invite, ContactInvite)
        self.assertEqual(invite.name, "\u5f20\u4e09")
        self.assertEqual(invite.fingerprint, "AB12CD34EF567890")
        self.assertEqual(invite.ip, "192.168.1.5")
        self.assertEqual(invite.port, 10001)

    def test_defaults_omitted(self):
        text = encode_contact_invite(name="", fingerprint="", ip="192.168.0.9")
        self.assertNotRegex(text, r"[?&]p=")
        self.assertNotRegex(text, r"[?&]n=")
        self.assertNotRegex(text, r"[?&]f=")
        invite = parse_invite(text)
        # compare against the codec's own default (models.TCP_PORT), never the
        # mutable network_module.TCP_PORT: other tests override that global to
        # pick ephemeral ports and do not restore it.
        self.assertEqual(invite.port, models_module.TCP_PORT)
        self.assertEqual(invite.name, "")
        self.assertEqual(invite.fingerprint, "")

    def test_android_style_plus_space_decodes(self):
        # URLEncoder on Android encodes a space as "+": decode must map it
        # back the same way
        text = "localchat://contact?v=1&n=%E5%B0%8F+A&f=AB12CD34EF567890&ip=192.168.0.9&p=9999"
        invite = parse_invite(text)
        self.assertEqual(invite.name, "\u5c0f A")

    def test_chinese_name_percent_roundtrip(self):
        name = "\u5c0f\u660eAb1"
        invite = parse_invite(
            encode_contact_invite(name=name, fingerprint="", ip="10.0.0.1")
        )
        self.assertEqual(invite.name, name)

    def test_missing_ip_rejected(self):
        with self.assertRaises(QrPayloadError):
            parse_invite("localchat://contact?v=1&n=a")

    def test_bad_fingerprint_rejected(self):
        text = encode_contact_invite(
            name="x", fingerprint="NO ZBAD!!", ip="192.168.0.9"
        )
        with self.assertRaises(QrPayloadError):
            parse_invite(text)

    def test_wrong_scheme_and_version_rejected(self):
        with self.assertRaises(QrPayloadError):
            parse_invite("https://example.com/contact?v=1")
        with self.assertRaises(QrPayloadError):
            parse_invite(
                encode_contact_invite(
                    name="a", fingerprint="", ip="192.168.0.9"
                ).replace("v=1", "v=99")
            )
        with self.assertRaises(QrPayloadError):
            parse_invite("just some text")


class QrGroupCodecTest(unittest.TestCase):
    def test_roundtrip_full(self):
        text = encode_group_invite(
            group_id="48291357", name="\u6d4b\u8bd5\u7fa4",
            ip="192.168.1.7", port=10002, relay="relay.example.com:25000",
        )
        self.assertTrue(text.startswith("localchat://group?"))
        invite = parse_invite(text)
        self.assertIsInstance(invite, GroupInvite)
        self.assertEqual(invite.group_id, "48291357")
        self.assertEqual(invite.name, "\u6d4b\u8bd5\u7fa4")
        self.assertEqual(invite.ip, "192.168.1.7")
        self.assertEqual(invite.port, 10002)
        self.assertEqual(invite.relay, "relay.example.com:25000")

    def test_relay_only_invite(self):
        text = encode_group_invite(group_id="12345678", relay="r.example.org:25000")
        invite = parse_invite(text)
        self.assertEqual(invite.ip, "")
        self.assertEqual(invite.relay, "r.example.org:25000")

    def test_group_id_digits_only(self):
        invite = parse_invite(
            encode_group_invite(group_id="12 34-5678", ip="192.168.0.1")
        )
        self.assertEqual(invite.group_id, "12345678")

    def test_group_id_length_enforced(self):
        with self.assertRaises(QrPayloadError):
            parse_invite(encode_group_invite(group_id="123", ip="192.168.0.1"))

    def test_password_parameter_never_decoded(self):
        # a crafted QR that smuggles a password must simply ignore it: the
        # joiner always types the password, the payload never carries one
        text = (
            "localchat://group?v=1&g=48291357&pw=SECRET&password=SECRET2"
            "&ip=192.168.0.1"
        )
        invite = parse_invite(text)
        self.assertIsInstance(invite, GroupInvite)
        self.assertNotIn("SECRET", invite.group_id + invite.name + invite.ip + invite.relay)

    def test_bad_relay_rejected(self):
        text = encode_group_invite(group_id="48291357", ip="192.168.0.1")
        text += "&r=not a relay"
        with self.assertRaises(QrPayloadError):
            parse_invite(text)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(QrPayloadError):
            parse_invite("localchat://other?v=1&x=1")


@unittest.skipUnless(qrshare.qr_matrix("ping") is not None, "qrcode not installed")
class QrMatrixTest(unittest.TestCase):
    PAYLOAD = "localchat://group?v=1&g=48291357&ip=192.168.0.1"

    def test_matrix_square_with_finders(self):
        matrix = qrshare.qr_matrix(self.PAYLOAD)
        self.assertTrue(matrix)
        side = len(matrix)
        self.assertTrue(all(len(row) == side for row in matrix))
        # the matrix includes a 2-module quiet border, then the three finder
        # patterns' dark anchors at the corners
        b = 2
        for r, c in ((b, b), (b, side - 7 - b), (side - 7 - b, b)):
            self.assertTrue(matrix[r][c])
            self.assertTrue(matrix[r + 6][c])

    @unittest.skipUnless(qrshare.scanner_available(), "opencv not installed")
    def test_decode_roundtrip_via_opencv(self):
        import numpy

        matrix = qrshare.qr_matrix(self.PAYLOAD)
        scale = 8
        rows = len(matrix)
        pixels = numpy.full((rows * scale, rows * scale), 255, dtype=numpy.uint8)
        for r, row in enumerate(matrix):
            for c, dark in enumerate(row):
                if dark:
                    pixels[r * scale:(r + 1) * scale, c * scale:(c + 1) * scale] = 0
        image = numpy.stack([pixels] * 3, axis=-1)
        text = qrshare.decode_qr_array(image)
        self.assertEqual(text, self.PAYLOAD)


class QrViewModelTest(unittest.TestCase):
    def setUp(self):
        install_identity()
        port = _free_port()
        self._orig_tcp_port = network_module.TCP_PORT
        network_module.TCP_PORT = port
        self.vm = make_vm(_fresh_db("lc_qr_vm.db"))
        self.vm.set_nickname("\u626b\u7801\u673a")

    def tearDown(self):
        self.vm.shutdown()
        network_module.TCP_PORT = self._orig_tcp_port

    def test_contact_payload_roundtrip(self):
        invite = self.vm.parse_qr_invite(self.vm.qr_contact_payload())
        self.assertIsInstance(invite, ContactInvite)
        self.assertEqual(invite.name, "\u626b\u7801\u673a")
        self.assertEqual(invite.fingerprint, self.vm.security_code)
        self.assertEqual(invite.port, network_module.TCP_PORT)
        self.assertTrue(invite.ip)

    def test_group_invite_payload_host_only(self):
        self.assertIsNone(self.vm.qr_group_invite_payload())
        self.vm.create_group("\u626b\u7801\u673a", "\u6d4b\u8bd5\u7fa4")
        payload = self.vm.qr_group_invite_payload()
        invite = self.vm.parse_qr_invite(payload)
        self.assertIsInstance(invite, GroupInvite)
        self.assertEqual(invite.group_id, self.vm.active_group_numeric_id())
        self.assertEqual(invite.name, "\u6d4b\u8bd5\u7fa4")
        # the password must never appear in the invite
        self.assertNotIn(self.vm.active_group_password, payload)

    def test_add_contact_pins_qr_fingerprint(self):
        self.assertFalse(
            self.vm.add_direct_contact("999.0.0.1", "x", expected_fingerprint="AB")
        )
        self.assertEqual(self.vm.direct._qr_expected_fps, {})
        self.assertTrue(
            self.vm.add_direct_contact(
                "192.168.0.44:%d" % _free_port(), "\u5c0f\u4e59",
                expected_fingerprint="ab12cd34ef567890",
            )
        )
        (endpoint, expected), = self.vm.direct._qr_expected_fps.items()
        self.assertEqual(expected, "AB12CD34EF567890")
        # the one-shot expectation is consumed by the next completed handshake
        self.assertEqual(
            self.vm.direct._take_qr_expected_fingerprint(*endpoint),
            "AB12CD34EF567890",
        )
        self.assertEqual(
            self.vm.direct._take_qr_expected_fingerprint(*endpoint), ""
        )


class QrIdentityBindingTest(unittest.TestCase):
    """The scanned QR pins the peer's 安全码: the first handshake must present
    exactly that identity key, or the session is refused (MITM between scan
    and dial)."""

    def setUp(self):
        install_identity()
        self.port = _free_port()
        self.server = network_module.HostGroupServer(self.port)
        self.server.ensure_running()
        self.b = network_module.DirectChatManager()
        self.b.configure("dev-B", "\u5c0fB", "127.0.0.1", self.port)
        self.server.direct_manager = self.b
        self.a = network_module.DirectChatManager()
        self.events = []
        self.a.on_event = self.events.append
        self.a.configure("dev-A", "\u5c0fA", "127.0.0.1", self.port)
        # B knows A (auto-accept): this test exercises the DIALER-side pin
        self.b.add_contact(self.a.my_peer())

    def tearDown(self):
        self.a.shutdown()
        self.b.shutdown()
        self.server.shutdown()

    def _b_peer(self):
        return Peer("dev-B", "\u5c0fB", "127.0.0.1", self.port)

    def test_wrong_qr_fingerprint_refuses_session(self):
        fp = DeviceIdentity.fingerprint()
        wrong = ("0" if fp[0] != "0" else "1") + fp[1:]
        self.a.set_qr_expected_fingerprint("127.0.0.1", self.port, wrong)
        result = self.a.start_chat(self._b_peer())
        self.assertIsNone(result, "a pinned 安全码 mismatch must refuse the session")
        self.assertFalse(self.a.is_chat_alive("dev-B"))
        self.assertTrue(
            any("安全码与二维码不一致" in e for e in self.events), self.events
        )
        # the one-shot expectation was consumed: a clean re-dial succeeds
        result = self.a.start_chat(self._b_peer())
        self.assertEqual(result, "dev-B")
        self.assertTrue(self.a.is_chat_alive("dev-B"))

    def test_matching_qr_fingerprint_accepts_session(self):
        fp = DeviceIdentity.fingerprint()
        self.a.set_qr_expected_fingerprint("127.0.0.1", self.port, fp.lower())
        result = self.a.start_chat(self._b_peer())
        self.assertEqual(result, "dev-B")
        self.assertTrue(self.a.is_chat_alive("dev-B"))


class QrDialogEntryTest(unittest.TestCase):
    """The 「二维码」 entries exist in the dialogs and dispatch the scanned
    payload (AGENTS.md: clicked(bool) injection trap respected)."""

    def setUp(self):
        install_identity()
        self._orig_tcp_port = network_module.TCP_PORT
        network_module.TCP_PORT = _free_port()
        self.vm = make_vm(_fresh_db("lc_qr_ui.db"))

    def tearDown(self):
        self.vm.shutdown()
        network_module.TCP_PORT = self._orig_tcp_port

    def _buttons(self, widget):
        return {b.text(): b for b in widget.findChildren(QPushButton)}

    def test_add_contact_dialog_has_qr_entries(self):
        import localchat.ui.member_list_page as mlp
        from localchat.ui.member_list_page import MemberListPage

        page = MemberListPage(self.vm, lambda: None, lambda: None, lambda c: None)
        dialog = page._build_add_dialog()
        buttons = self._buttons(dialog)
        self.assertIn("\u626b\u4e8c\u7ef4\u7801", buttons)
        self.assertIn("\u6211\u7684\u4e8c\u7ef4\u7801", buttons)

        # scanning a contact invite: confirm + pinned identity + add
        added = []

        def fake_confirm(parent, vm, invite):
            added.append(invite)
            vm.add_direct_contact(
                f"{invite.ip}:{invite.port}", invite.name,
                expected_fingerprint=invite.fingerprint,
            )
            return True

        payload = self.vm.qr_contact_payload()
        orig_confirm = mlp.confirm_contact_invite
        orig_import = mlp.open_import_dialog
        mlp.confirm_contact_invite = fake_confirm
        mlp.open_import_dialog = lambda parent, title="": payload
        try:
            buttons["\u626b\u4e8c\u7ef4\u7801"].click()
        finally:
            mlp.confirm_contact_invite = orig_confirm
            mlp.open_import_dialog = orig_import
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].fingerprint, self.vm.security_code)

        # a group invite scanned here must NOT add a contact
        group_payload = encode_group_invite(group_id="48291357", ip="192.168.0.1")
        mlp.open_import_dialog = lambda parent, title="": group_payload
        try:
            buttons["\u626b\u4e8c\u7ef4\u7801"].click()
        finally:
            mlp.open_import_dialog = orig_import
        self.assertEqual(len(added), 1)
        dialog.deleteLater()
        page.deleteLater()

    def test_show_my_qr_entry_opens_dialog(self):
        import localchat.ui.member_list_page as mlp
        from localchat.ui.member_list_page import MemberListPage

        page = MemberListPage(self.vm, lambda: None, lambda: None, lambda c: None)
        dialog = page._build_add_dialog()
        opened = []

        class FakeQrDialog:
            def __init__(self, vm, parent=None):
                opened.append(vm)

            def exec(self):
                return 0

        orig = mlp.ContactQrDialog
        mlp.ContactQrDialog = FakeQrDialog
        try:
            self._buttons(dialog)["\u6211\u7684\u4e8c\u7ef4\u7801"].click()
        finally:
            mlp.ContactQrDialog = orig
        self.assertEqual(opened, [self.vm])
        dialog.deleteLater()
        page.deleteLater()

    def test_join_form_scan_prefills_fields(self):
        import localchat.ui.setup_page as sp
        from localchat.ui.setup_page import SetupPage

        page = SetupPage(self.vm, lambda: None, lambda: None)
        page.show_mode(2)  # MODE_JOIN
        payload = encode_group_invite(
            group_id="48291357", name="\u6d4b\u8bd5\u7fa4",
            ip="192.168.0.55", port=_free_port(), relay="r.example.org:25000",
        )
        orig = sp.open_import_dialog
        sp.open_import_dialog = lambda parent, title="": payload
        try:
            self._buttons(page)["\u626b\u4e8c\u7ef4\u7801"].click()
        finally:
            sp.open_import_dialog = orig
        digits = "".join(ch for ch in page.join_group_edit.text() if ch.isdigit())
        self.assertEqual(digits, "48291357")
        self.assertTrue(page.join_ip_edit.text().startswith("192.168.0.55:"))
        self.assertEqual(page.join_server_edit.currentText(), "r.example.org:25000")
        # the password is NEVER prefilled from a QR: it must be typed
        self.assertEqual(page.join_password_edit.text(), "")
        page.deleteLater()

    def test_group_lobby_has_invite_qr_entry(self):
        from localchat.ui.group_lobby_page import GroupLobbyPage

        self.vm.create_group("\u626b\u7801\u673a", "\u6d4b\u8bd5\u7fa4")
        page = GroupLobbyPage(
            self.vm, on_back=lambda: None, on_open_chat=lambda: None, on_leave=lambda: None
        )
        buttons = self._buttons(page)
        self.assertIn("\u7fa4\u9080\u8bf7\u4e8c\u7ef4\u7801", buttons)
        page.deleteLater()


if __name__ == "__main__":
    unittest.main()
