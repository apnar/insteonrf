#!/usr/bin/env python3
"""Generate harmless, closed-loop Insteon test traffic for receiver A/Bs.

Sends Get Engine Version (``get_engine``) round-robin to the given devices
through insteon-mqtt, and sends the next request only once the previous one
has reported END (or after a timeout). So it never queues anything inside
insteon-mqtt, however short ``--pause`` is: ``--pause 0`` gives genuinely
back-to-back exchanges, ``--pause 8`` gives a burst out of silence every
eight seconds or so.

Do not replace this with a fixed-rate publisher. On 2026-10-09 one sent a
request every 0.3 s, faster than the PLM can carry them, and reused session
ids across runs: insteon-mqtt built a backlog of thousands of commands,
fanned every reply out to each reused session at ~5,000 log lines a second,
and real light commands waited behind it for 20 minutes until the insteon
sidecar was restarted.

Usage::

    python tools/test_traffic.py --seconds 300 --pause 0 29.4E.52 38.FA.56
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from typing import Any


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("addrs", nargs="+", help="device addresses, e.g. 29.4E.52")
    p.add_argument("--mqtt", default="192.168.88.5")
    p.add_argument("--user", default=os.environ.get("INSTEONRF_MQTT_USER"))
    p.add_argument("--password", default=os.environ.get("INSTEONRF_MQTT_PASS"))
    p.add_argument("--seconds", type=float, default=300.0)
    p.add_argument("--pause", type=float, default=0.0,
                   help="seconds of quiet to leave after each exchange completes")
    p.add_argument("--timeout", type=float, default=5.0,
                   help="give up waiting for an END after this long")
    a = p.parse_args(argv)

    import paho.mqtt.client as mqtt

    run = uuid.uuid4().hex[:6]
    done = threading.Event()

    def on_message(client: Any, userdata: Any, msg: Any) -> None:
        try:
            if json.loads(msg.payload).get("type") == "END":
                done.set()
        except (ValueError, AttributeError):
            pass

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"test-traffic-{run}")
    if a.user:
        c.username_pw_set(a.user, a.password)
    c.on_message = on_message
    c.connect(a.mqtt)
    c.loop_start()

    end = time.time() + a.seconds
    sent = timeouts = 0
    while time.time() < end:
        addr = a.addrs[sent % len(a.addrs)]
        sent += 1
        # Unique per request: insteon-mqtt answers every subscriber of a
        # session name, so a reused name multiplies its replies.
        session = f"{run}-{sent}"
        topic = f"insteon/command/{addr}/session/{session}"
        done.clear()
        c.subscribe(topic)
        time.sleep(0.05)
        c.publish(f"insteon/command/{addr}", json.dumps({"cmd": "get_engine", "session": session}))
        if not done.wait(a.timeout):
            timeouts += 1
        c.unsubscribe(topic)
        if a.pause:
            time.sleep(a.pause)
    c.loop_stop()
    c.disconnect()
    print(f"sent {sent}, timeouts {timeouts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
