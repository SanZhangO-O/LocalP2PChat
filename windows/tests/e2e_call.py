"""Multi-process end-to-end video call harness (loopback, one machine).

Runs two real processes to prove the full call pipeline works over TCP:

  process A (host + callee): P2PManager host on PORT, a P2PManager callee that
      joins it, and a CallManager that auto-accepts incoming calls.
  process B (caller): a P2PManager caller that joins the same group and a
      CallManager that dials the callee.

Verifies, end to end:
  - group join (query/join handshake, members exchanged)
  - call signaling over the host relay: offer -> incoming -> accept -> ACTIVE
  - the direct media TCP connection, with JPEG video frames flowing BOTH ways
    (synthetic frames; this machine has no camera, which exercises the
    no-camera fallback path)
  - the audio transport: an injected PCM frame is parsed and delivered to the
    remote_audio signal on the other side
  - hangup returns both sides to idle

Usage:  python tests/e2e_call.py
"""

import os
import subprocess
import sys
import textwrap
import time

PORT = 22000
GROUP = "e2e测试群"
PASSWORD = "123456"
CALLER_NAME = "呼叫者"
CALLEE_NAME = "被叫者"
GLOBAL_TIMEOUT = 50

HOST_CODE = r"""
import os, sys, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.getcwd())
from PyQt6.QtWidgets import QApplication
app = QApplication([])
from localchat.call import CallManager, STATE_ACTIVE, STATE_IDLE, STATE_INCOMING
from localchat.network import P2PListener, P2PManager

class L(P2PListener):
    pass

PORT = int(os.environ["E2E_PORT"])
GROUP = os.environ["E2E_GROUP"]
PASSWORD = os.environ["E2E_PASSWORD"]
CALLEE_NAME = os.environ["E2E_CALLEE"]

host = P2PManager(L(), port=PORT, password=PASSWORD)
host.initialize_as_host("主机", GROUP)
host.start_as_host()

callee = P2PManager(L(), port=21031)
callee.initialize_as_client(CALLEE_NAME, GROUP, password=PASSWORD)
callee.confirm_join("127.0.0.1", PORT)

deadline = time.time() + 15
while time.time() < deadline:
    if callee.connection_result is not None and callee.connection_result[0]:
        break
    app.processEvents()
    time.sleep(0.02)
assert callee.connection_result is not None and callee.connection_result[0], "callee join failed"

cm = CallManager()
callee.call_listener = cm._on_signal
remote_frames = []
remote_audio = []
cm.remote_frame.connect(lambda q: remote_frames.append(q))
cm.remote_audio.connect(lambda b: remote_audio.append(b))
cm.state_changed.connect(lambda s, n, d: print("HOST_STATE", s, n, d, flush=True))
cm.incoming_call.connect(lambda cid, name: print("HOST_INCOMING", name, flush=True))
cm.call_ended.connect(lambda r: print("HOST_ENDED", r, flush=True))
cm.call_error.connect(lambda m: print("HOST_ERROR", m, flush=True))
print("HOST_READY", flush=True)

deadline = time.time() + 30
saw_active = False
while time.time() < deadline:
    app.processEvents()
    if cm.state == STATE_INCOMING:
        cm.accept_call()
    if cm.state == STATE_ACTIVE:
        saw_active = True
        if len(remote_frames) >= 3 and len(remote_audio) >= 1:
            break
    time.sleep(0.01)
assert saw_active, "callee never reached ACTIVE"
assert len(remote_frames) >= 3, "callee received too few video frames: %d" % len(remote_frames)
assert len(remote_audio) >= 1, "callee received no audio frames"
print("HOST_MEDIA_OK frames=%d audio=%d" % (len(remote_frames), len(remote_audio)), flush=True)

deadline = time.time() + 20
while time.time() < deadline:
    app.processEvents()
    if cm.state == STATE_IDLE:
        break
    time.sleep(0.01)
assert cm.state == STATE_IDLE, "callee did not return to idle after caller hangup"
print("HOST_OK", flush=True)
callee.stop()
host.stop()
"""

CALLER_CODE = r"""
import os, sys, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.getcwd())
from PyQt6.QtWidgets import QApplication
app = QApplication([])
from localchat.call import CallManager, CH_AUDIO, STATE_ACTIVE, STATE_IDLE
from localchat.network import P2PListener, P2PManager

class L(P2PListener):
    pass

PORT = int(os.environ["E2E_PORT"])
GROUP = os.environ["E2E_GROUP"]
PASSWORD = os.environ["E2E_PASSWORD"]
CALLER_NAME = os.environ["E2E_CALLER"]

caller = P2PManager(L(), port=21032)
caller.initialize_as_client(CALLER_NAME, GROUP, password=PASSWORD)
caller.confirm_join("127.0.0.1", PORT)
deadline = time.time() + 15
while time.time() < deadline:
    if caller.connection_result is not None and caller.connection_result[0]:
        break
    app.processEvents()
    time.sleep(0.02)
assert caller.connection_result is not None and caller.connection_result[0], "caller join failed"

cm = CallManager()
caller.call_listener = cm._on_signal
remote_frames = []
cm.remote_frame.connect(lambda q: remote_frames.append(q))
cm.state_changed.connect(lambda s, n, d: print("CALLER_STATE", s, n, d, flush=True))
cm.call_ended.connect(lambda r: print("CALLER_ENDED", r, flush=True))
cm.call_error.connect(lambda m: print("CALLER_ERROR", m, flush=True))

# wait for the callee member to appear, then dial (match by name; the first
# peer in the list is the host)
callee_id = None
callee_name = os.environ["E2E_CALLEE"]
deadline = time.time() + 15
while time.time() < deadline:
    app.processEvents()
    for pid, peer in caller.peers.items():
        if peer.name == callee_name:
            callee_id = pid
            break
    if callee_id:
        break
    time.sleep(0.02)
assert callee_id, "callee peer not visible"
cm.start_call(caller, callee_id)
print("CALLER_DIALING", flush=True)

deadline = time.time() + 30
while time.time() < deadline:
    app.processEvents()
    if cm.state == STATE_ACTIVE and len(remote_frames) >= 3:
        break
    time.sleep(0.01)
assert cm.state == STATE_ACTIVE, "caller never reached ACTIVE"
assert len(remote_frames) >= 3, "caller received too few video frames: %d" % len(remote_frames)

# inject one synthetic PCM frame so the callee can verify the audio path
cm._send_media(CH_AUDIO, b"\x00\x01" * 320)
time.sleep(1.0)
for _ in range(100):
    app.processEvents()
    time.sleep(0.01)
print("CALLER_MEDIA_OK frames=%d" % len(remote_frames), flush=True)

cm.hangup()
deadline = time.time() + 10
while time.time() < deadline:
    app.processEvents()
    if cm.state == STATE_IDLE:
        break
    time.sleep(0.01)
assert cm.state == STATE_IDLE, "caller did not return to idle after hangup"
print("CALLER_OK", flush=True)
caller.stop()
"""


def _base_env() -> dict:
    env = dict(os.environ)
    env.update(
        {
            "E2E_PORT": str(PORT),
            "E2E_GROUP": GROUP,
            "E2E_PASSWORD": PASSWORD,
            "E2E_CALLER": CALLER_NAME,
            "E2E_CALLEE": CALLEE_NAME,
        }
    )
    return env


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Windows may keep the previous run's port in TIME_WAIT for a moment and
    # intermittently refuse a fresh bind; retry the whole scenario once.
    for attempt in (1, 2):
        ok = _run_once(root)
        if ok:
            print("=" * 60)
            print("E2E 视频通话联调通过：信令、媒体直连、双向视频帧、音频通道、挂断全部 OK")
            print("=" * 60)
            return 0
        print("[harness] attempt %d failed; retrying..." % attempt, flush=True)
        time.sleep(1.5)
    print("=" * 60)
    print("E2E 联调失败，请查看上面的输出")
    print("=" * 60)
    return 1


def _run_once(root: str) -> bool:
    ok = True

    host_proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(HOST_CODE)],
        cwd=root,
        env=_base_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        # wait for the host+callee side to be ready
        ready = False
        start = time.time()
        while time.time() - start < 30:
            line = host_proc.stdout.readline()
            if not line:
                break
            sys.stdout.write("[host] " + line)
            sys.stdout.flush()
            if "HOST_READY" in line:
                ready = True
                break
        if not ready:
            print("[harness] host side never became ready")
            ok = False
        else:
            caller_proc = subprocess.Popen(
                [sys.executable, "-c", textwrap.dedent(CALLER_CODE)],
                cwd=root,
                env=_base_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                for line in caller_proc.stdout:
                    sys.stdout.write("[caller] " + line)
                    sys.stdout.flush()
                rc = caller_proc.wait(timeout=GLOBAL_TIMEOUT)
                if rc != 0:
                    ok = False
                    print("[harness] caller process exited with rc=%d" % rc)
            finally:
                if caller_proc.poll() is None:
                    caller_proc.kill()
            # drain the rest of the host output
            while True:
                line = host_proc.stdout.readline()
                if not line:
                    break
                sys.stdout.write("[host] " + line)
                sys.stdout.flush()
            hrc = host_proc.wait(timeout=GLOBAL_TIMEOUT)
            if hrc != 0:
                ok = False
                print("[harness] host process exited with rc=%d" % hrc)
    finally:
        if host_proc.poll() is None:
            host_proc.kill()

    print("=" * 60)
    return ok


if __name__ == "__main__":
    sys.exit(main())
