
import json
import socket
import threading
import time
from collections import deque


class BrowserUDPChat:
    """One UDP socket per browser login."""

    def __init__(self, host, port, token):
        self.host = socket.gethostbyname(host)
        self.port = port
        self.token = token

        self.alive = threading.Event()
        self.alive.set()

        self.messages = deque(maxlen=100)
        self.sequence = 0
        self.lock = threading.Lock()

        self.socket = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM
        )
        self.socket.settimeout(0.3)

        self.last_send = 0.0

        self.thread = threading.Thread(
            target=self._listen,
            daemon=True
        )
        self.thread.start()

    def _listen(self):
        next_register = 0.0

        while self.alive.is_set():
            now = time.monotonic()

            if now >= next_register:
                try:
                    self.socket.sendto(
                        json.dumps({
                            "type": "chat_register",
                            "token": self.token
                        }).encode("utf-8"),
                        (self.host, self.port)
                    )
                except OSError:
                    break

                next_register = now + 3.0

            try:
                packet, address = self.socket.recvfrom(2048)

                if address != (self.host, self.port):
                    continue

                item = json.loads(
                    packet.decode("utf-8")
                )

                if (
                    not isinstance(item, dict)
                    or item.get("type") != "room_chat"
                    or type(item.get("room_id")) is not int
                    or not isinstance(item.get("from"), str)
                    or not isinstance(item.get("text"), str)
                ):
                    continue

                with self.lock:
                    self.sequence += 1

                    self.messages.append({
                        "id": self.sequence,
                        "room_id": item["room_id"],
                        "from": item["from"],
                        "text": item["text"]
                    })

            except socket.timeout:
                pass

            except (OSError, ValueError, UnicodeError):
                if not self.alive.is_set():
                    break

    def send(self, text):
        now = time.monotonic()

        with self.lock:
            if now - self.last_send < 0.5:
                return False

            self.last_send = now

        packet = json.dumps({
            "type": "room_chat",
            "token": self.token,
            "text": text
        }).encode("utf-8")

        try:
            self.socket.sendto(
                packet,
                (self.host, self.port)
            )
            return True
        except OSError:
            return False

    def since(self, after):
        with self.lock:
            return [
                dict(message)
                for message in self.messages
                if message["id"] > after
            ]

    def close(self):
        self.alive.clear()
        self.socket.close()
