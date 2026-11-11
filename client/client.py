import socket
import threading
import queue
import json
import os
import time
import tkinter as tk
from collections import deque
from tkinter import simpledialog

SERVER_HOST = os.environ.get("RPS_SERVER_HOST", "127.0.0.1")
TCP_PORT = 5000
UDP_PORT = 5001
COUNTDOWN_PORT = 5003
CHAT_PORT = 5002

BG = "#101827"
PANEL = "#1B2940"
LIGHT = "#253650"
TEXT = "#F2F6FC"
MUTED = "#AEC0D7"
BLUE = "#4F96FF"
GREEN = "#40C98D"
RED = "#FF7777"

MOVES = ("rock", "paper", "scissors")


# ==========================================
# TCP NETWORK WORKER
# ==========================================

class NetworkWorker:
    def __init__(self, inbox):
        self.inbox = inbox
        self.jobs = queue.Queue()
        self.alive = True

        threading.Thread(
            target=self.run,
            daemon=True
        ).start()

    def request(self, action, payload, callback):
        self.jobs.put((action, payload, callback))

    def close(self):
        self.alive = False
        self.jobs.put(None)

    def run(self):
        try:
            sock = socket.create_connection(
                (SERVER_HOST, TCP_PORT),
                timeout=6
            )
            sock.settimeout(None)

            with sock:
                with sock.makefile("r", encoding="utf-8") as reader:
                    self.inbox.put(("connected", None))

                    while self.alive:
                        job = self.jobs.get()

                        if job is None:
                            return

                        action, payload, callback = job
                        request = {
                            "action": action,
                            **payload
                        }

                        message = json.dumps(request) + "\n"
                        sock.sendall(message.encode("utf-8"))

                        line = reader.readline()

                        if not line:
                            raise ConnectionError("Server disconnected.")

                        response = json.loads(line)
                        self.inbox.put(("response", (callback, response)))

        except (ConnectionError, OSError, ValueError) as exc:
            self.inbox.put(("disconnected", str(exc)))


# ==========================================
# UDP HEARTBEAT + NETWORK MONITOR
# ==========================================

class HeartbeatWorker:
    """
    Uses the existing UDP heartbeat protocol to
    measure RTT, packet loss, and jitter.
    """

    def __init__(self, inbox, token, port):
        self.inbox = inbox
        self.token = token
        self.port = port
        self.stop_event = threading.Event()

        self.samples = deque(maxlen=30)

        self.thread = threading.Thread(
            target=self.run,
            daemon=True
        )
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def run(self):
        sequence = 0

        try:
            destination_ip = socket.gethostbyname(SERVER_HOST)

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                while not self.stop_event.is_set():
                    sequence += 1

                    message = {
                        "type": "heartbeat",
                        "token": self.token,
                        "seq": sequence
                    }

                    sent_at = time.perf_counter()

                    udp.sendto(
                        json.dumps(message).encode("utf-8"),
                        (SERVER_HOST, self.port)
                    )

                    deadline = sent_at + 1.0
                    rtt_ms = None

                    while not self.stop_event.is_set():
                        remaining = deadline - time.perf_counter()

                        if remaining <= 0:
                            break

                        udp.settimeout(remaining)

                        try:
                            data, address = udp.recvfrom(2048)
                            reply = json.loads(data.decode("utf-8"))

                            valid = (
                                address[0] == destination_ip
                                and address[1] == self.port
                                and isinstance(reply, dict)
                                and reply.get("type") == "heartbeat_ack"
                                and reply.get("seq") == sequence
                            )

                            if valid:
                                rtt_ms = (time.perf_counter() - sent_at) * 1000
                                break

                        except (socket.timeout, ValueError, UnicodeError):
                            if time.perf_counter() >= deadline:
                                break

                        except OSError:
                            if self.stop_event.is_set():
                                return
                            break

                    if self.stop_event.is_set():
                        break

                    self.samples.append(rtt_ms)

                    successful = [
                        value
                        for value in self.samples
                        if value is not None
                    ]

                    sent_count = len(self.samples)
                    lost_count = sent_count - len(successful)

                    loss_pct = 100 * lost_count / sent_count if sent_count else 0.0

                    variations = [
                        abs(successful[i] - successful[i - 1])
                        for i in range(1, len(successful))
                    ]

                    jitter_ms = (
                        sum(variations) / len(variations)
                        if variations else None
                    )

                    self.inbox.put((
                        "heartbeat",
                        {
                            "worker": self,
                            "ok": rtt_ms is not None,
                            "ping_ms": rtt_ms,
                            "loss_pct": loss_pct,
                            "jitter_ms": jitter_ms,
                            "samples": sent_count
                        }
                    ))

                    self.stop_event.wait(2)

        except OSError as exc:
            self.inbox.put((
                "heartbeat_error",
                {
                    "worker": self,
                    "message": str(exc)
                }
            ))



# ==========================================
# SEPARATE UDP COUNTDOWN RECEIVER - PORT 5003
# ==========================================

class CountdownWorker:
    def __init__(self, inbox, token, port):
        self.inbox = inbox
        self.token = token
        self.port = port
        self.stop_event = threading.Event()

        self.thread = threading.Thread(
            target=self.run,
            daemon=True
        )
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def run(self):
        try:
            server_ip = socket.gethostbyname(SERVER_HOST)

            with socket.socket(
                socket.AF_INET, socket.SOCK_DGRAM
            ) as udp:
                udp.settimeout(0.5)

                next_registration = 0.0
                last_sequence = -1

                while not self.stop_event.is_set():
                    now = time.monotonic()

                    if now >= next_registration:
                        registration = json.dumps({
                            "type": "countdown_register",
                            "token": self.token
                        }).encode("utf-8")

                        udp.sendto(
                            registration,
                            (server_ip, self.port)
                        )

                        next_registration = now + 3.0

                    try:
                        data, sender = udp.recvfrom(2048)

                        if sender != (server_ip, self.port):
                            continue

                        packet = json.loads(
                            data.decode("utf-8")
                        )

                        if not isinstance(packet, dict):
                            continue

                        if packet.get("type") != "countdown":
                            continue

                        seq = packet.get("seq")
                        room = packet.get("room_id")
                        round_number = packet.get("round")
                        remaining = packet.get("remaining")
                        match_id = packet.get("match_id")

                        if not (
                            type(seq) is int
                            and seq > last_sequence
                            and type(room) is int
                            and room > 0
                            and type(round_number) is int
                            and round_number > 0
                            and type(remaining) is int
                            and 0 <= remaining <= 60
                            and isinstance(match_id, str)
                            and len(match_id) == 24
                        ):
                            continue

                        last_sequence = seq

                        packet["worker"] = self
                        packet["received_at"] = time.monotonic()

                        self.inbox.put(
                            ("countdown", packet)
                        )

                    except socket.timeout:
                        continue

                    except (ValueError, UnicodeError):
                        continue

        except OSError as exc:
            if not self.stop_event.is_set():
                self.inbox.put((
                    "countdown_error",
                    {
                        "worker": self,
                        "message": str(exc)
                    }
                ))



# ==========================================
# UDP ROOM CHAT CLIENT (PORT 5002)
# ==========================================

class ChatWorker:
    def __init__(self, inbox, token, port):
        self.inbox = inbox
        self.token = token
        self.port = port

        self.stop_event = threading.Event()
        self.outgoing = queue.Queue()

        self.thread = threading.Thread(
            target=self.run,
            daemon=True
        )
        self.thread.start()

    def send(self, text):
        self.outgoing.put(text)

    def stop(self):
        self.stop_event.set()

    def run(self):
        try:
            server_ip = socket.gethostbyname(SERVER_HOST)

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.settimeout(0.2)
                next_registration = 0.0

                while not self.stop_event.is_set():
                    if time.monotonic() >= next_registration:
                        packet = {
                            "type": "chat_register",
                            "token": self.token
                        }

                        udp.sendto(
                            json.dumps(packet).encode("utf-8"),
                            (server_ip, self.port)
                        )

                        next_registration = time.monotonic() + 3

                    try:
                        while True:
                            text = self.outgoing.get_nowait()

                            packet = {
                                "type": "room_chat",
                                "token": self.token,
                                "text": text
                            }

                            udp.sendto(
                                json.dumps(packet).encode("utf-8"),
                                (server_ip, self.port)
                            )

                    except queue.Empty:
                        pass

                    try:
                        data, address = udp.recvfrom(2048)

                        if address != (server_ip, self.port):
                            continue

                        reply = json.loads(data.decode("utf-8"))

                        if (
                            not isinstance(reply, dict)
                            or reply.get("type") != "room_chat"
                            or type(reply.get("room_id")) is not int
                            or not isinstance(reply.get("from"), str)
                            or not isinstance(reply.get("text"), str)
                        ):
                            continue

                        self.inbox.put((
                            "chat_message",
                            {
                                "worker": self,
                                "room_id": reply["room_id"],
                                "from": reply["from"],
                                "text": reply["text"]
                            }
                        ))

                    except socket.timeout:
                        pass

                    except (ValueError, UnicodeError):
                        pass

        except OSError as exc:
            if not self.stop_event.is_set():
                self.inbox.put((
                    "chat_error",
                    {
                        "worker": self,
                        "message": str(exc)
                    }
                ))


# ==========================================
# TKINTER APPLICATION
# ==========================================

class RPSApp:
    def __init__(self, root):
        self.root = root
        self.root.title("RPS Arena Online")
        self.root.geometry("1080x740")
        self.root.minsize(820, 570)
        self.root.configure(bg=BG)

        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.inbox = queue.Queue()
        self.network = NetworkWorker(self.inbox)

        self.heartbeat = None
        self.countdown_worker = None
        self.countdown_packet = None
        self.countdown_label = None
        self.chat_worker = None
        self.chat_window = None
        self.chat_log = None
        self.chat_entry = None
        self.chat_history = {}
        self.chat_room_id = None
        self.udp_ok = False
        self.udp_last_ack = None
        self.udp_metrics = None

        self.connected = False
        self.logged_in = False
        self.username = None

        self.room = None
        self.match = None
        self.rooms = []

        self.poll_busy = False
        self.submitting = False

        self.form_mode = "login"
        self.message = "Connecting..."
        self.error = False

        self.last_snapshot = None

        self.monitor_canvas = None

        self.header = tk.Frame(root, bg=BG)
        self.header.pack(fill="x", padx=28, pady=(20, 10))

        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=28, pady=8)

        self.footer = tk.Label(
            root,
            text="Connecting...",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
            justify="center"
        )
        self.footer.pack(fill="x", padx=28, pady=(0, 12))

        self.render()

        self.root.after(100, self.process_inbox)
        self.root.after(1000, self.poll)

    # ======================================
    # CONNECTION MANAGEMENT
    # ======================================

    def close(self):
        self.stop_chat()
        self.stop_countdown()
        self.stop_heartbeat()
        self.network.close()
        self.root.destroy()

    def request(self, action, callback=None, **payload):
        if not self.connected:
            self.notice("Not connected to server.", True)
            return

        self.network.request(action, payload, callback)

    def start_heartbeat(self, token, port):
        self.stop_heartbeat()

        self.udp_ok = False
        self.udp_last_ack = None
        self.udp_metrics = None

        self.heartbeat = HeartbeatWorker(
            self.inbox,
            token,
            port
        )

    def start_chat(self, token, port):
        self.stop_chat()
        self.chat_worker = ChatWorker(self.inbox, token, port)

    def stop_chat(self):
        if self.chat_worker:
            self.chat_worker.stop()

        self.chat_worker = None
        self.close_chat_window()
        self.chat_history.clear()
        self.chat_room_id = None

    def close_chat_window(self):
        if self.chat_window is not None:
            try:
                self.chat_window.destroy()
            except tk.TclError:
                pass

        self.chat_window = None
        self.chat_log = None
        self.chat_entry = None

    def open_chat_window(self):
        if not self.room or not self.logged_in:
            return

        room_id = self.room["id"]

        if self.chat_window and self.chat_window.winfo_exists():
            if self.chat_room_id == room_id:
                self.chat_window.lift()
                self.chat_entry.focus_set()
                return

            self.close_chat_window()

        self.chat_room_id = room_id

        window = tk.Toplevel(self.root)
        window.title(f"UDP Room Chat | Room #{room_id}")
        window.geometry("440x340")
        window.configure(bg=BG)

        window.protocol(
            "WM_DELETE_WINDOW",
            self.close_chat_window
        )

        self.chat_window = window

        tk.Label(
            window,
            text=f"ROOM CHAT  |  UDP {CHAT_PORT}",
            bg=BG,
            fg=BLUE,
            font=("Segoe UI", 14, "bold")
        ).pack(pady=(15, 8))

        self.chat_log = tk.Text(
            window,
            height=10,
            bg=PANEL,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Segoe UI", 11),
            wrap="word"
        )

        self.chat_log.pack(
            fill="both",
            expand=True,
            padx=12
        )

        self.chat_log.config(state="disabled")

        row = tk.Frame(window, bg=BG)
        row.pack(fill="x", padx=12, pady=12)

        self.chat_entry = tk.Entry(
            row,
            bg=LIGHT,
            fg=TEXT,
            insertbackground=TEXT,
            font=("Segoe UI", 11),
            relief="flat"
        )

        self.chat_entry.pack(
            side="left",
            fill="x",
            expand=True,
            ipady=7
        )

        self.chat_entry.bind(
            "<Return>",
            lambda event: self.send_chat()
        )

        self.button(
            row,
            "Send",
            self.send_chat,
            side="right",
            padx=(8, 0)
        )

        self.refresh_chat_log()
        self.chat_entry.focus_set()

    def refresh_chat_log(self):
        if not self.chat_log or not self.chat_room_id:
            return

        lines = self.chat_history.get(
            self.chat_room_id,
            []
        )

        self.chat_log.config(state="normal")
        self.chat_log.delete("1.0", "end")
        self.chat_log.insert("end", "\n".join(lines))
        self.chat_log.see("end")
        self.chat_log.config(state="disabled")

    def send_chat(self):
        if not self.room or not self.chat_entry:
            return

        text = self.chat_entry.get().strip()

        if not text:
            return

        if len(text) > 200:
            self.notice(
                "Chat messages must be at most 200 characters.",
                True
            )
            return

        if not self.chat_worker:
            self.notice("UDP chat is not connected.", True)
            return

        self.chat_worker.send(text)
        self.chat_entry.delete(0, "end")

    def receive_chat(self, packet):
        if not self.room or packet["room_id"] != self.room["id"]:
            return

        room_id = packet["room_id"]
        lines = self.chat_history.setdefault(room_id, [])

        lines.append(
            f"{packet['from']}: {packet['text']}"
        )

        if len(lines) > 80:
            del lines[:-80]

        if self.chat_room_id == room_id:
            self.refresh_chat_log()

    def start_countdown(self, token, port):
        self.stop_countdown()

        self.countdown_worker = CountdownWorker(
            self.inbox, token, port
        )

    def stop_countdown(self):
        if self.countdown_worker:
            self.countdown_worker.stop()

        self.countdown_worker = None
        self.countdown_packet = None
        self.countdown_label = None

    def stop_heartbeat(self):
        if self.heartbeat:
            self.heartbeat.stop()
            self.heartbeat = None

        self.udp_ok = False
        self.udp_last_ack = None
        self.udp_metrics = None

    def process_inbox(self):
        try:
            while True:
                kind, data = self.inbox.get_nowait()

                if kind == "connected":
                    self.connected = True
                    self.notice("Connected to server.")
                    self.render()

                elif kind == "disconnected":
                    self.connected = False
                    self.logged_in = False
                    self.username = None
                    self.room = None
                    self.match = None
                    self.poll_busy = False

                    self.stop_chat()
                    self.stop_countdown()
                    self.stop_heartbeat()

                    self.notice("TCP disconnected: " + data, True)
                    self.render()

                elif kind == "response":
                    callback, response = data
                    if callback:
                        callback(response)

                elif kind == "heartbeat":
                    if self.heartbeat is data["worker"]:
                        self.udp_ok = data["ok"]
                        self.udp_metrics = data

                        if data["ok"]:
                            self.udp_last_ack = time.monotonic()

                        self.update_footer()
                        self.update_network_monitor_ui()

                elif kind == "countdown":
                    if self.countdown_worker is data["worker"]:
                        self.countdown_packet = data
                        self.update_countdown_label()

                elif kind == "chat_message":
                    if self.chat_worker is data["worker"]:
                        self.receive_chat(data)

                elif kind == "chat_error":
                    if self.chat_worker is data["worker"]:
                        self.notice(
                            "UDP chat error: " + data["message"], True
                        )

                elif kind == "countdown_error":
                    if self.countdown_worker is data["worker"]:
                        self.countdown_packet = None
                        self.update_countdown_label()

                elif kind == "heartbeat_error":
                    if self.heartbeat is data["worker"]:
                        self.udp_ok = False
                        self.update_footer()
                        self.update_network_monitor_ui()

        except queue.Empty:
            pass

        self.root.after(100, self.process_inbox)

    # ======================================
    # FOOTER + MONITOR
    # ======================================

    def notice(self, message, error=False):
        self.message = message
        self.error = error
        self.update_footer()

    def update_footer(self):
        if self.logged_in:
            tcp_text = "TCP connected" if self.connected else "TCP disconnected"
            udp_text = "UDP connected" if self.is_udp_recent() else "UDP waiting"
            info = f"Server: {SERVER_HOST}  |  {tcp_text}  |  {udp_text}  |  {self.message}"
        else:
            info = f"Server: {SERVER_HOST}  |  {self.message}"

        self.footer.config(
            text=info,
            fg=RED if self.error else MUTED
        )

    def is_udp_recent(self):
        return (
            self.udp_last_ack is not None
            and time.monotonic() - self.udp_last_ack < 8
        )

    def rounded_rectangle(self, canvas, x1, y1, x2, y2, radius=20, **kwargs):
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1
        ]
        return canvas.create_polygon(points, smooth=True, splinesteps=36, **kwargs)

    def update_network_monitor_ui(self):
        if not self.monitor_canvas:
            return

        canvas = self.monitor_canvas
        canvas.delete("all")

        width = int(canvas.cget("width"))
        height = int(canvas.cget("height"))

        self.rounded_rectangle(
            canvas,
            2,
            2,
            width - 2,
            height - 2,
            radius=22,
            fill=PANEL,
            outline=LIGHT,
            width=2
        )

        title_color = BLUE if self.is_udp_recent() else MUTED

        canvas.create_text(
            20,
            18,
            anchor="nw",
            text="UDP NETWORK MONITOR:",
            fill=title_color,
            font=("Segoe UI", 12, "bold")
        )

        ping = "--"
        loss = "--"
        jitter = "--"
        probes = "--"

        if self.udp_metrics is not None:
            ping = (
                f"{self.udp_metrics['ping_ms']:.1f} ms"
                if self.udp_metrics["ping_ms"] is not None
                else "timeout"
            )

            loss = f"{self.udp_metrics['loss_pct']:.1f}%"

            jitter = (
                f"{self.udp_metrics['jitter_ms']:.1f} ms"
                if self.udp_metrics["jitter_ms"] is not None
                else "collecting..."
            )

            probes = str(self.udp_metrics["samples"])

        line = (
            f"Ping: {ping}   |   "
            f"Loss: {loss}   |   "
            f"Jitter: {jitter}   |   "
            f"Probes: {probes}"
        )

        canvas.create_text(
            20,
            50,
            anchor="nw",
            text=line,
            fill=TEXT,
            font=("Segoe UI", 11, "bold")
        )

        status_color = GREEN if (self.connected and self.is_udp_recent()) else MUTED
        status_line = (
            f"TCP: {'Connected' if self.connected else 'Disconnected'}   |   "
            f"UDP: {'Connected' if self.is_udp_recent() else 'Waiting'}"
        )

        canvas.create_text(
            20,
            78,
            anchor="nw",
            text=status_line,
            fill=status_color,
            font=("Segoe UI", 10)
        )

    # ======================================
    # GRAPHICAL HELPERS
    # ======================================

    def clear(self):
        self.countdown_label = None
        for child in self.header.winfo_children():
            child.destroy()

        for child in self.body.winfo_children():
            child.destroy()

        self.monitor_canvas = None
        self.update_footer()

    def label(self, parent, text, size=12, color=TEXT, bold=False, **pack):
        widget = tk.Label(
            parent,
            text=text,
            bg=parent.cget("bg"),
            fg=color,
            font=("Segoe UI", size, "bold" if bold else "normal"),
            anchor="w",
            justify="left",
            wraplength=780
        )
        widget.pack(**pack)
        return widget

    def button(self, parent, text, command, color=BLUE, **pack):
        widget = tk.Button(
            parent,
            text=text,
            command=command,
            bg=color,
            fg="white",
            activebackground=LIGHT,
            activeforeground="white",
            font=("Segoe UI", 11, "bold"),
            relief="flat",
            bd=0,
            padx=15,
            pady=9,
            cursor="hand2"
        )
        widget.pack(**pack)
        return widget

    def panel(self):
        frame = tk.Frame(
            self.body,
            bg=PANEL,
            padx=16,
            pady=12
        )
        frame.pack(fill="x", pady=5)
        return frame

    def build_header(self):
        left = tk.Frame(self.header, bg=BG)
        left.pack(side="left", fill="x", expand=True, anchor="nw")

        self.label(left, "RPS ARENA", 25, TEXT, True)
        self.label(
            left,
            f"Player: {self.username}" if self.logged_in else "Online Multiplayer Rock Paper Scissors",
            11,
            MUTED,
            pady=(3, 0)
        )

        if self.logged_in:
            right = tk.Frame(self.header, bg=BG)
            right.pack(side="right", anchor="ne")

            self.monitor_canvas = tk.Canvas(
                right,
                width=470,
                height=112,
                bg=BG,
                highlightthickness=0,
                bd=0
            )
            self.monitor_canvas.pack(anchor="e")
            self.update_network_monitor_ui()

    # ======================================
    # SCREEN ROUTER
    # ======================================

    def render(self):
        self.clear()
        self.build_header()

        if not self.logged_in:
            self.render_auth()
        elif self.room is None:
            self.render_lobby()
        elif self.room["status"] == "playing":
            self.render_game()
        elif self.room["status"] == "finished":
            self.render_finished()
        else:
            self.render_room()

    # ======================================
    # AUTHENTICATION
    # ======================================

    def render_auth(self):
        frame = self.panel()

        self.label(
            frame,
            "Create account" if self.form_mode == "register" else "Login",
            19,
            TEXT,
            True,
            pady=(0, 12)
        )

        self.label(frame, "Username", 11, MUTED)

        username_entry = tk.Entry(
            frame,
            bg=LIGHT,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Segoe UI", 13)
        )
        username_entry.pack(fill="x", ipady=8, pady=(5, 12))

        self.label(frame, "Password", 11, MUTED)

        password_entry = tk.Entry(
            frame,
            show="*",
            bg=LIGHT,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            font=("Segoe UI", 13)
        )
        password_entry.pack(fill="x", ipady=8, pady=(5, 12))

        action = self.form_mode

        self.button(
            frame,
            "Register" if action == "register" else "Login",
            lambda: self.authenticate(
                action,
                username_entry.get().strip(),
                password_entry.get()
            ),
            fill="x",
            pady=5
        )

        self.button(
            frame,
            "Switch to Login" if action == "register" else "Create an account",
            self.switch_auth,
            LIGHT,
            fill="x",
            pady=5
        )

        if not self.connected:
            self.label(frame, "Server not connected.", 11, RED, pady=10)

    def switch_auth(self):
        self.form_mode = "register" if self.form_mode == "login" else "login"
        self.render()

    def authenticate(self, action, username, password):
        if not username or not password:
            self.notice("Enter username and password.", True)
            return

        self.request(
            action,
            lambda result: self.auth_result(action, username, result),
            username=username,
            password=password
        )

    def auth_result(self, action, username, response):
        self.notice(response.get("message", ""), not response.get("success"))

        if response.get("success"):
            if action == "login":
                self.logged_in = True
                self.username = username
                self.room = None
                self.match = None
                self.rooms = []

                self.start_heartbeat(
                    response["heartbeat_token"],
                    response.get("udp_port", UDP_PORT)
                )
                self.start_countdown(
                    response["heartbeat_token"],
                    response.get("countdown_port", COUNTDOWN_PORT)
                )
                self.start_chat(
                    response["heartbeat_token"],
                    response.get("chat_port", CHAT_PORT)
                )

                self.last_snapshot = None
                self.refresh_state()
            else:
                self.form_mode = "login"

        self.render()

    # ======================================
    # LOBBY
    # ======================================

    def render_lobby(self):
        bar = tk.Frame(self.body, bg=BG)
        bar.pack(fill="x", pady=(0, 10))

        self.button(
            bar,
            "Create Room",
            self.create_room,
            side="left",
            padx=(0, 8)
        )

        self.button(
            bar,
            "Refresh",
            self.refresh_rooms,
            LIGHT,
            side="left"
        )

        self.button(
            bar,
            "Logout",
            lambda: self.act("logout"),
            LIGHT,
            side="right"
        )

        self.label(self.body, "Available Rooms", 18, TEXT, True, pady=(6, 10))

        if not self.rooms:
            self.label(self.body, "No rooms available. Create one!", 12, MUTED)

        for room in self.rooms:
            frame = self.panel()

            self.label(
                frame,
                f"{room['name']} | Room #{room['id']}",
                15,
                TEXT,
                True
            )

            self.label(
                frame,
                f"Host: {room['host']}   Players: {room['player_count']}/4   Status: {room['status'].upper()}",
                11,
                MUTED,
                pady=(5, 8)
            )

            if room["status"] not in ("playing", "finished") and room["player_count"] < 4:
                self.button(
                    frame,
                    "Join Room",
                    lambda rid=room["id"]: self.act("join_room", room_id=rid),
                    fill="x"
                )

    def create_room(self):
        name = simpledialog.askstring(
            "Create Room",
            "Enter room name:",
            parent=self.root
        )

        if name is not None:
            self.act("create_room", room_name=name.strip())

    def refresh_rooms(self):
        if self.logged_in and self.room is None:
            self.request("list_rooms", self.rooms_result)

    def rooms_result(self, response):
        if not self.logged_in or self.room is not None:
            return

        if response.get("success"):
            rooms = response.get("rooms", [])
            if rooms != self.rooms:
                self.rooms = rooms
                self.render()

    # ======================================
    # WAITING ROOM
    # ======================================

    def render_room(self):
        room = self.room

        self.label(
            self.body,
            f"{room['name']} | Room #{room['id']}",
            19,
            TEXT,
            True
        )

        self.label(
            self.body,
            f"{room['player_count']}/4 players | Status: {room['status'].upper()}",
            12,
            MUTED,
            pady=(5, 12)
        )

        for player in room["players"]:
            frame = self.panel()

            is_host = player == room["host"]
            is_ready = player in room["ready_players"]

            self.label(
                frame,
                player + (" (HOST)" if is_host else ""),
                13,
                TEXT,
                True
            )

            self.label(
                frame,
                "READY" if is_ready else "NOT READY",
                11,
                GREEN if is_ready else MUTED
            )

        bar = tk.Frame(self.body, bg=BG)
        bar.pack(fill="x", pady=12)

        my_ready = self.username in room["ready_players"]

        self.button(
            bar,
            "Cancel Ready" if my_ready else "I'm Ready",
            lambda: self.act("unready" if my_ready else "ready"),
            LIGHT if my_ready else GREEN,
            side="left",
            padx=(0, 8)
        )

        if room["host"] == self.username:
            self.button(
                bar,
                "Start Game",
                lambda: self.act("start_game"),
                BLUE,
                side="left",
                padx=(0, 8)
            )

        self.button(
            bar,
            "Leave Room",
            lambda: self.act("leave_room"),
            LIGHT,
            side="right"
        )

        self.button(
            self.body, "Open UDP Room Chat",
            self.open_chat_window, LIGHT, pady=(4, 8)
        )

        if not room["can_start"]:
            self.label(
                self.body,
                "Start requires 2-4 players and everyone ready.",
                11,
                MUTED
            )

    # ======================================
    # SCOREBOARD
    # ======================================

    def score_panel(self, scores):
        frame = self.panel()

        self.label(frame, "SCOREBOARD", 14, TEXT, True, pady=(0, 7))

        ranking = sorted(
            scores.items(),
            key=lambda item: (-item[1], item[0].lower())
        )

        rank = 0
        previous_score = None

        for index, (player, score) in enumerate(ranking, 1):
            if score != previous_score:
                rank = index

            self.label(
                frame,
                f"#{rank} {player}: {score} points",
                12,
                TEXT,
                pady=2
            )

            previous_score = score

    def result_panel(self, result):
        if not result:
            return

        frame = self.panel()

        for player, move in result["choices"].items():
            self.label(
                frame,
                f"{player}: {move.upper()}    +{result['points'][player]} points",
                12,
                TEXT,
                pady=3
            )

    # ======================================
    # GAME SCREEN
    # ======================================

    def update_countdown_label(self):
        if (
            not self.countdown_label
            or not self.match
            or not self.room
        ):
            return

        if self.match.get("phase") != "choosing":
            return

        packet = self.countdown_packet

        valid = (
            packet is not None
            and time.monotonic() - packet["received_at"] < 2.5
            and packet["room_id"] == self.room.get("id")
            and packet["round"] == self.match.get("round")
            and packet["match_id"] == self.match.get("id")
        )

        if valid:
            seconds = packet["remaining"]
            transport = "UDP 5003"
        else:
            seconds = self.match["remaining"]
            transport = "TCP fallback"

        self.countdown_label.config(
            text=f"Time remaining: {seconds} seconds ({transport})"
        )

    def render_game(self):
        self.button(
            self.body, "Open UDP Room Chat",
            self.open_chat_window, LIGHT, pady=(0, 6)
        )
        if not self.match:
            self.label(self.body, "Preparing match...", 16, MUTED)
            return

        match = self.match
        title = f"ROUND {match['round']}"

        if match["round"] > match["total_rounds"]:
            title += " | TIEBREAKER"

        self.label(self.body, title, 22, TEXT, True)
        self.countdown_label = self.label(
            self.body,
            f"Time remaining: {match['remaining']} seconds (TCP)",
            14,
            GREEN,
            pady=(5, 9)
        )
        self.update_countdown_label()

        self.score_panel(match["scores"])

        if match["phase"] == "results":
            self.label(self.body, "ROUND RESULTS", 17, TEXT, True, pady=(10, 5))
            self.result_panel(match.get("last_result"))
            self.label(
                self.body,
                "Next round begins automatically.",
                11,
                MUTED,
                pady=8
            )
            return

        frame = self.panel()

        self.label(frame, "Choose your move", 17, TEXT, True, pady=(0, 10))

        if match["my_choice"]:
            self.label(
                frame,
                f"Submitted: {match['my_choice'].upper()}",
                15,
                GREEN,
                True
            )

            self.label(
                frame,
                "Waiting for other players...",
                11,
                MUTED,
                pady=8
            )

        else:
            bar = tk.Frame(frame, bg=PANEL)
            bar.pack(fill="x")

            for move in MOVES:
                self.button(
                    bar,
                    move.upper(),
                    lambda m=move, r=match["round"]: self.choose(m, r),
                    LIGHT if self.submitting else BLUE,
                    side="left",
                    expand=True,
                    fill="x",
                    padx=5
                )

        self.label(
            self.body,
            f"Submitted: {len(match['submitted'])}/{len(match['players'])} players",
            11,
            MUTED,
            pady=(8, 6)
        )

        for player in match["players"]:
            submitted = player in match["submitted"]

            self.label(
                self.body,
                f"{player}: {'Submitted' if submitted else 'Choosing...'}",
                11,
                GREEN if submitted else MUTED,
                pady=2
            )

        self.button(
            self.body,
            "Leave Match",
            lambda: self.act("leave_room"),
            LIGHT,
            pady=15
        )

    # ======================================
    # FINISHED MATCH
    # ======================================

    def render_finished(self):
        self.button(
            self.body, "Open UDP Room Chat",
            self.open_chat_window, LIGHT, pady=(0, 6)
        )
        if not self.match:
            self.label(self.body, "Loading results...", 16, MUTED)
            return

        match = self.match

        self.label(self.body, "MATCH FINISHED", 24, TEXT, True)

        if match["draw"]:
            title = "DRAW: " + ", ".join(match["winners"])
        else:
            title = "WINNER: " + match["winners"][0]

        self.label(self.body, title, 18, GREEN, True, pady=(10, 8))
        self.label(self.body, f"Rounds played: {match['round']}", 11, MUTED)

        self.score_panel(match["scores"])

        self.label(self.body, "Final round", 13, TEXT, True, pady=(8, 2))
        self.result_panel(match.get("last_result"))

        bar = tk.Frame(self.body, bg=BG)
        bar.pack(fill="x", pady=12)

        if self.room and self.room["host"] == self.username:
            self.button(
                bar,
                "Play Again",
                lambda: self.act("reset_match"),
                side="left",
                padx=(0, 8)
            )

        self.button(
            bar,
            "Leave Room",
            lambda: self.act("leave_room"),
            LIGHT,
            side="left"
        )

    # ======================================
    # GAME ACTIONS
    # ======================================

    def choose(self, move, round_number):
        if self.submitting:
            return

        self.submitting = True

        self.request(
            "choose",
            self.choice_result,
            move=move,
            round=round_number
        )

    def choice_result(self, response):
        self.submitting = False
        self.notice(response.get("message", ""), not response.get("success"))
        self.refresh_state()

    def act(self, action, **payload):
        self.request(
            action,
            lambda response: self.action_result(action, response),
            **payload
        )

    def action_result(self, action, response):
        self.notice(response.get("message", ""), not response.get("success"))

        if action == "logout" and response.get("success"):
            self.stop_chat()
            self.stop_countdown()
            self.stop_heartbeat()

            self.logged_in = False
            self.username = None
            self.room = None
            self.match = None
            self.rooms = []
            self.poll_busy = False
            self.last_snapshot = None
            self.form_mode = "login"

            self.render()
            return

        self.refresh_state()

    # ======================================
    # AUTOMATIC REFRESH
    # ======================================

    def refresh_state(self):
        if not self.logged_in or self.poll_busy or not self.connected:
            return

        self.poll_busy = True
        self.request("player_state", self.state_result)

    def state_result(self, response):
        if not self.logged_in:
            self.poll_busy = False
            return

        if not response.get("success"):
            self.poll_busy = False
            self.notice(response.get("message", ""), True)
            return

        self.room = response.get("room") if response.get("in_room") else None
        if self.chat_window and (
            not self.room or self.room["id"] != self.chat_room_id
        ):
            self.close_chat_window()

        if self.room and self.room["status"] in ("playing", "finished"):
            self.request("game_state", self.game_result)
        else:
            self.match = None
            self.poll_busy = False
            self.render_if_changed()

            if self.room is None:
                self.refresh_rooms()

    def game_result(self, response):
        self.poll_busy = False

        if not self.logged_in:
            return

        if response.get("success"):
            self.room = response.get("room")
            self.match = response.get("match")

            if response.get("notice"):
                self.notice(response["notice"], True)

            self.render_if_changed()
        else:
            self.notice(response.get("message", ""), True)

    def render_if_changed(self):
        snapshot = json.dumps(
            {
                "room": self.room,
                "match": self.match
            },
            sort_keys=True
        )

        if snapshot != self.last_snapshot:
            self.last_snapshot = snapshot
            self.render()

    def poll(self):
        if self.logged_in and self.connected:
            self.refresh_state()

        self.update_footer()
        self.update_network_monitor_ui()
        self.update_countdown_label()

        self.root.after(1000, self.poll)


# ==========================================
# START DESKTOP APPLICATION
# ==========================================

def main():
    root = tk.Tk()
    RPSApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()  