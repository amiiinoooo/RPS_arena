
import json
import os
import secrets
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from waitress import serve


# ==========================================
# RPS ARENA WEB GATEWAY
# Waitress + Flask + Existing TCP Server
# ==========================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

GAME_HOST = os.environ.get(
    "RPS_SERVER_HOST",
    "127.0.0.1"
)

GAME_PORT = 5000

WEB_HOST = "127.0.0.1"
WEB_PORT = 8000

SECRET_KEY = os.environ.get(
    "RPS_FLASK_SECRET",
    ""
)

if len(SECRET_KEY) < 64:
    raise RuntimeError(
        "RPS_FLASK_SECRET is missing or too short. "
        "Check your private .env file."
    )

COOKIE_SECURE = (
    os.environ.get(
        "RPS_COOKIE_SECURE",
        "0"
    ) == "1"
)

CLOSE_GRACE_SECONDS = 4
BROWSER_TIMEOUT_SECONDS = 20

ALLOWED_ACTIONS = {
    "create_room",
    "join_room",
    "leave_room",
    "ready",
    "unready",
    "start_game",
    "reset_match",
    "choose",
}

VALID_MOVES = {
    "rock",
    "paper",
    "scissors",
}


# ==========================================
# FLASK CONFIGURATION
# ==========================================

app = Flask(__name__)

app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    MAX_CONTENT_LENGTH=8192,
    PERMANENT_SESSION_LIFETIME=1800,
)

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri="memory://",
    default_limits=[],
)

connections = {}
browser_tabs = {}

connections_lock = threading.RLock()


# ==========================================
# TCP CONNECTION TO GAME SERVER
# ==========================================

class GameConnection:

    def __init__(self):
        self.socket = socket.create_connection(
            (GAME_HOST, GAME_PORT),
            timeout=8
        )

        self.socket.settimeout(8)

        self.reader = self.socket.makefile(
            "r",
            encoding="utf-8"
        )

        self.lock = threading.Lock()
        self.last_used = time.monotonic()

    def send(self, action, **data):

        with self.lock:
            self.last_used = time.monotonic()

            message = json.dumps({
                "action": action,
                **data
            }) + "\n"

            self.socket.sendall(
                message.encode("utf-8")
            )

            line = self.reader.readline(8193)

            if not line:
                raise ConnectionError(
                    "Game server disconnected."
                )

            if len(line) > 8192:
                raise ConnectionError(
                    "Game server response too large."
                )

            result = json.loads(line)

            if not isinstance(result, dict):
                raise ValueError(
                    "Invalid game server response."
                )

            return result

    def close(self):

        try:
            self.socket.shutdown(
                socket.SHUT_RDWR
            )
        except OSError:
            pass

        try:
            self.socket.close()
        except OSError:
            pass

        try:
            self.reader.close()
        except OSError:
            pass


# ==========================================
# RESPONSE HELPERS
# ==========================================

def success(message="", **data):

    return jsonify({
        "success": True,
        "message": message,
        **data
    })


def failure(message, status=400):

    return jsonify({
        "success": False,
        "message": message
    }), status


def current_sid():
    return session.get("sid")


def get_connection():

    sid = current_sid()

    if not sid:
        return None

    with connections_lock:
        return connections.get(sid)


def remove_connection(sid):

    if not sid:
        return

    with connections_lock:
        connection = connections.pop(
            sid,
            None
        )

        browser_tabs.pop(
            sid,
            None
        )

    if connection:
        connection.close()


def disconnect_browser():

    sid = current_sid()

    remove_connection(sid)
    session.clear()


def server_unavailable():

    disconnect_browser()

    return failure(
        "Game server connection lost. Please log in again.",
        503
    )


# ==========================================
# HTTP SECURITY
# ==========================================

@app.after_request
def add_security_headers(response):

    response.headers["X-Content-Type-Options"] = (
        "nosniff"
    )

    response.headers["X-Frame-Options"] = (
        "DENY"
    )

    response.headers["Referrer-Policy"] = (
        "strict-origin-when-cross-origin"
    )

    response.headers["Cache-Control"] = (
        "no-store"
        if request.path.startswith("/api/")
        else "no-cache"
    )

    return response


@app.before_request
def protect_requests():

    if request.method != "POST":
        return None

    origin = request.headers.get("Origin")

    if not origin:
        return failure(
            "Missing request origin.",
            403
        )

    parsed = urlsplit(origin)

    if (
        parsed.scheme not in ("http", "https")
        or parsed.netloc != request.host
    ):
        return failure(
            "Invalid request origin.",
            403
        )

    if not request.is_json:
        return failure(
            "Expected JSON data.",
            415
        )

    return None


@app.errorhandler(429)
def rate_limit_exceeded(error):

    return failure(
        "Too many requests. Please try again later.",
        429
    )


@app.errorhandler(413)
def request_too_large(error):

    return failure(
        "Request is too large.",
        413
    )


# ==========================================
# WEBSITE
# ==========================================

@app.get("/")
def home():

    return render_template(
        "index.html"
    )


# ==========================================
# REGISTRATION
# ==========================================

@app.post("/api/register")
@limiter.limit("5 per minute")
def register():

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return failure(
            "Invalid request."
        )

    username = data.get("username")
    password = data.get("password")

    if (
        not isinstance(username, str)
        or not isinstance(password, str)
    ):
        return failure(
            "Invalid registration details."
        )

    connection = None

    try:
        connection = GameConnection()

        result = connection.send(
            "register",
            username=username,
            password=password
        )

        return jsonify(result)

    except (
        OSError,
        ValueError,
        ConnectionError
    ):
        return failure(
            "Unable to reach the game server.",
            503
        )

    finally:
        if connection:
            connection.close()


# ==========================================
# LOGIN
# ==========================================

@app.post("/api/login")
@limiter.limit("10 per minute")
def login():

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return failure(
            "Invalid request."
        )

    username = data.get("username")
    password = data.get("password")

    if (
        not isinstance(username, str)
        or not isinstance(password, str)
    ):
        return failure(
            "Invalid login details."
        )

    if get_connection() is not None:
        return failure(
            "Already logged in. Log out first."
        )

    connection = None

    try:
        connection = GameConnection()

        result = connection.send(
            "login",
            username=username,
            password=password
        )

        if not result.get("success"):
            connection.close()

            return jsonify(result)

        sid = secrets.token_urlsafe(32)

        now = time.monotonic()

        with connections_lock:

            connections[sid] = connection

            browser_tabs[sid] = {
                "_created": now,
                "_tabs": {}
            }

        session.clear()
        session["sid"] = sid

        return success(
            result.get(
                "message",
                "Welcome!"
            ),
            username=username
        )

    except (
        OSError,
        ValueError,
        ConnectionError
    ):
        if connection:
            connection.close()

        return failure(
            "Unable to reach the game server.",
            503
        )


# ==========================================
# BROWSER PRESENCE
# ==========================================

def read_tab_id():

    data = request.get_json(
        silent=True
    )

    if not isinstance(data, dict):
        return None

    tab_id = data.get("tab_id")

    if (
        not isinstance(tab_id, str)
        or not 8 <= len(tab_id) <= 100
    ):
        return None

    return tab_id


@app.post("/api/presence")
def presence():

    sid = current_sid()
    tab_id = read_tab_id()

    if not sid or not tab_id:
        return failure(
            "Invalid browser session.",
            401
        )

    now = time.monotonic()

    with connections_lock:

        if sid not in connections:
            return failure(
                "Session expired.",
                401
            )

        record = browser_tabs.get(sid)

        if record is None:
            return failure(
                "Session expired.",
                401
            )

        record["_tabs"][tab_id] = {
            "last_seen": now,
            "closing_at": None
        }

    return success(
        "Browser active."
    )


@app.post("/api/browser-leave")
def browser_leave():

    sid = current_sid()
    tab_id = read_tab_id()

    if not sid or not tab_id:
        return success()

    with connections_lock:

        record = browser_tabs.get(sid)

        if record:
            tab = record["_tabs"].get(
                tab_id
            )

            if tab:
                tab["closing_at"] = (
                    time.monotonic()
                )

    return success(
        "Closing tab registered."
    )


# ==========================================
# SESSION STATUS
# ==========================================

@app.get("/api/status")
def status():

    connection = get_connection()

    if connection is None:
        return success(
            "Not logged in.",
            logged_in=False
        )

    try:
        result = connection.send(
            "status"
        )

        username = result.get(
            "username"
        )

        if (
            result.get("success")
            and username
        ):
            return success(
                "Connected.",
                logged_in=True,
                username=username
            )

        disconnect_browser()

        return success(
            "Session ended.",
            logged_in=False
        )

    except (
        OSError,
        ValueError,
        ConnectionError
    ):
        return server_unavailable()


# ==========================================
# LOBBY AND GAME STATE
# ==========================================

@app.get("/api/state")
def player_state():

    connection = get_connection()

    if connection is None:
        return failure(
            "Please log in first.",
            401
        )

    try:
        state = connection.send(
            "player_state"
        )

        if not state.get("success"):
            return jsonify(state)

        if state.get("in_room"):

            room = state.get("room")
            match = None
            notice = ""

            if (
                room
                and room.get("status") in (
                    "playing",
                    "finished"
                )
            ):
                game = connection.send(
                    "game_state"
                )

                if game.get("success"):
                    match = game.get(
                        "match"
                    )

                    room = game.get(
                        "room",
                        room
                    )

                    notice = game.get(
                        "notice",
                        ""
                    )

            return success(
                "Player state updated.",
                in_room=True,
                room=room,
                rooms=[],
                match=match,
                notice=notice
            )

        lobby = connection.send(
            "list_rooms"
        )

        if not lobby.get("success"):
            return jsonify(lobby)

        return success(
            "Lobby updated.",
            in_room=False,
            room=None,
            rooms=lobby.get(
                "rooms",
                []
            ),
            match=None,
            notice=""
        )

    except (
        OSError,
        ValueError,
        ConnectionError
    ):
        return server_unavailable()


# ==========================================
# GAME ACTIONS
# ==========================================

@app.post("/api/action")
def game_action():

    connection = get_connection()

    if connection is None:
        return failure(
            "Please log in first.",
            401
        )

    data = request.get_json(
        silent=True
    )

    if not isinstance(data, dict):
        return failure(
            "Invalid request."
        )

    action = data.get("action")

    if action not in ALLOWED_ACTIONS:
        return failure(
            "Action not available."
        )

    payload = {}

    if action == "create_room":

        name = data.get(
            "room_name"
        )

        if not isinstance(name, str):
            return failure(
                "Invalid room name."
            )

        name = name.strip()

        if not 1 <= len(name) <= 30:
            return failure(
                "Room name must be 1-30 characters."
            )

        payload["room_name"] = name

    elif action == "join_room":

        room_id = data.get(
            "room_id"
        )

        if type(room_id) is not int:
            return failure(
                "Invalid room ID."
            )

        payload["room_id"] = room_id

    elif action == "choose":

        move = data.get(
            "move"
        )

        round_number = data.get(
            "round"
        )

        if move not in VALID_MOVES:
            return failure(
                "Invalid move."
            )

        if (
            type(round_number) is not int
            or round_number < 1
        ):
            return failure(
                "Invalid round number."
            )

        payload["move"] = move
        payload["round"] = round_number

    try:
        result = connection.send(
            action,
            **payload
        )

        return jsonify(result)

    except (
        OSError,
        ValueError,
        ConnectionError
    ):
        return server_unavailable()


# ==========================================
# LOGOUT
# ==========================================

@app.post("/api/logout")
def logout():

    connection = get_connection()

    if connection:
        try:
            connection.send(
                "logout"
            )
        except (
            OSError,
            ValueError,
            ConnectionError
        ):
            pass

    disconnect_browser()

    return success(
        "Logged out successfully."
    )


# ==========================================
# BROWSER SESSION CLEANUP
# ==========================================

def cleanup_sessions():

    while True:
        time.sleep(1)

        now = time.monotonic()
        expired = []

        with connections_lock:

            for sid, record in list(
                browser_tabs.items()
            ):
                tabs = record["_tabs"]

                for tab_id, tab in list(
                    tabs.items()
                ):

                    closing_at = tab[
                        "closing_at"
                    ]

                    if closing_at is not None:

                        if (
                            now - closing_at
                            >= CLOSE_GRACE_SECONDS
                        ):
                            del tabs[tab_id]

                    elif (
                        now - tab["last_seen"]
                        >= BROWSER_TIMEOUT_SECONDS
                    ):
                        del tabs[tab_id]

                if (
                    not tabs
                    and now - record["_created"]
                    >= CLOSE_GRACE_SECONDS
                ):
                    expired.append(sid)

        for sid in expired:

            with connections_lock:

                record = browser_tabs.get(
                    sid
                )

                if (
                    record is None
                    or record["_tabs"]
                ):
                    continue

                connection = connections.pop(
                    sid,
                    None
                )

                browser_tabs.pop(
                    sid,
                    None
                )

            if connection:
                print(
                    "[WEB] Browser session expired."
                )

                connection.close()


# ==========================================
# START WAITRESS
# ==========================================

def main():

    threading.Thread(
        target=cleanup_sessions,
        daemon=True,
        name="browser-cleanup"
    ).start()

    print("===================================")
    print("       RPS ARENA WEB SERVER")
    print("===================================")
    print(
        f"Game server: {GAME_HOST}:{GAME_PORT}"
    )
    print(
        f"Website: http://{WEB_HOST}:{WEB_PORT}"
    )
    print("Web server: Waitress")
    print("Login rate limiting: Enabled")
    print("Registration rate limiting: Enabled")
    print("Browser session cleanup: Enabled")
    print(
        "Secure cookies: "
        + (
            "Enabled"
            if COOKIE_SECURE
            else "Disabled (local HTTP mode)"
        )
    )
    print("===================================")

    serve(
        app,
        host=WEB_HOST,
        port=WEB_PORT,
        threads=8,
        connection_limit=100
    )


if __name__ == "__main__":
    main()
