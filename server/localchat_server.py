#!/usr/bin/env python3
"""LocalChat 一体化服务器：信令/打洞/中继 + Web 多用户聊天，一个进程。

信令面（默认 :25000）：不同 NAT 网段的 Windows/Android 客户端凭群组数字 ID
互相打洞或经本进程中继端到端加密流量（见 signaling_server.py）。
Web 面（默认 :8090）：浏览器多用户聊天（HTTP+WS，账号/历史/文件均在本机，
见 webchat/ 包）。两个协议面互相独立，互不影响。

用法：
  python localchat_server.py [--http 8090] [--http-host 0.0.0.0]
                             [--signaling-port 25000] [--secret <访问密钥>]
                             [--data <目录>] [--public <目录>] [--open]
                             [--no-signaling] [--no-web]

Web 依赖第三方包 cryptography（AES-256-GCM 静态加密）；信令面仅标准库。
"""

import logging
import os
import sys
import time

from signaling_server import SignalingServer
from webchat.server import ChatServer, parse_args as parse_web_args

DEFAULT_SIGNALING_PORT = 25000

log = logging.getLogger("localchat")


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    web = parse_web_args(argv)
    run_web = True
    run_signaling = True
    signaling_port = DEFAULT_SIGNALING_PORT
    secret = os.environ.get("LOCALCHAT_SIGNALING_SECRET") or None
    items = list(argv)
    i = 0
    while i < len(items):
        a = items[i]
        if a == "--no-web":
            run_web = False
            i += 1
        elif a == "--no-signaling":
            run_signaling = False
            i += 1
        elif a == "--signaling-port" and i + 1 < len(items):
            try:
                signaling_port = int(items[i + 1])
            except ValueError:
                print("invalid --signaling-port: %s" % items[i + 1], file=sys.stderr)
                return 2
            i += 2
        elif a == "--secret" and i + 1 < len(items):
            secret = items[i + 1]
            i += 2
        else:
            i += 1

    signaling = None
    chat = None
    if run_signaling:
        signaling = SignalingServer(signaling_port, secret=secret)
        try:
            signaling.start()
        except OSError as e:
            log.error("cannot bind signaling port %d: %s", signaling_port, e)
            return 1
    if run_web:
        chat = ChatServer(web)
        chat.start()
    if signaling is None and chat is None:
        log.warning("nothing to run: both --no-web and --no-signaling given")
        return 2
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        if chat is not None:
            chat.stop()
        if signaling is not None:
            signaling.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
