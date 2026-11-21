"""Small, dependency-free reliability helpers for the browser monitor."""
import json
import os
import random
import time
from pathlib import Path


def atomic_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, target)


def poll_seconds(env):
    # Fast mode is an explicit deployment decision, not the default.
    value = int(env.get('TARZKARTI_POLL_SECONDS', '60'))
    if value < 60 and env.get('TARZKARTI_FAST_POLL_RISK_ACK') != '1':
        raise ValueError('Sub-minute polling needs TARZKARTI_FAST_POLL_RISK_ACK=1; IP/account blocking is possible')
    if value < 10:
        raise ValueError('Polling below 10 seconds is not supported')
    return value


def cycle_delay(started, interval, failures=0):
    # Start-to-start cadence, with positive jitter and bounded error backoff.
    target = min(900, interval * (2 ** min(failures, 6)))
    target += random.uniform(0, target * .1)
    return max(target if failures else 0, started + target - time.monotonic())


class DurableOutbox:
    """At-least-once delivery. A crash after send can produce one duplicate."""
    def __init__(self, path):
        self.path = Path(path)
        try:
            self.items = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.items, list):
                raise ValueError('Outbox must be a list')
        except FileNotFoundError:
            self.items = []
        # Do not silently discard an unreadable queue.

    def enqueue(self, text):
        self.items.append(str(text))
        atomic_json(self.path, self.items)

    def flush(self, send):
        while self.items:
            send(self.items[0])
            self.items.pop(0)
            atomic_json(self.path, self.items)
