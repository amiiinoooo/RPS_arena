
import json
import os
import secrets
import socket
import threading
import time

from flask import Flask, jsonify, render_template, request, session

app = Flask(__name__)
app.secret_key = secrets.token_bytes(32)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,
    MAX_CONTENT_LENGTH=8192,
)

GAME_HOST = os.environ.get("RPS_SERVER_HOST", "127.0.0.1")
GAME_PORT = 5000

connections = {}
connections_lock = threading.RLock()

SESSION_IDLE_SECONDS = 1800


class GameConnection:
    def __init__(self):
        self.socket = socket.create_connection(
            (GAME_HOST, GAME_PORT),
            timeout=8
        )
        self.socket.settimeout(8)
        self.reader = self.socket.makefile(
            "r", encoding="utf-8"
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

            self.socket.sendall(message.encode("utf-8"))

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
            self.socket.shutdown(socket.SHUT_RDWR)
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


def success(message, **data):
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


def get_connection():
    sid = session.get("sid")

    if not sid:
        return None

    with connections_lock:
        return connections.get(sid)


def remove_connection(sid):
    if not sid:
        return

    with connections_lock:
        connection = connections.pop(sid, None)

    if connection:
        connection.close()


def check_origin():
    origin = request.headers.get("Origin")

    if not origin:
        return True

    from urllib.parse import urlsplit

    parsed = urlsplit(origin)

    return (
        parsed.scheme in ("http", "https")
        and parsed.netloc == request.host
    )


@app.before_request
def protect_requests():
    if request.method == "POST" and not check_origin():
        return failure("Invalid request origin.", 403)


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/status")
def status():
    connection = get_connection()

    if connection is None:
        return success(
            "Not logged in.",
            logged_in=False
        )

    try:
        result = connection.send("status")

        username = result.get("username")

        if result.get("success") and username:
            return success(
                "Connected.",
                logged_in=True,
                username=username
            )

        remove_connection(session.get("sid"))
        session.clear()

    except (OSError, ValueError, ConnectionError):
        remove_connection(session.get("sid"))
        session.clear()

        return failure(
            "Connection to game server lost.",
            503
        )

    return success(
        "Not logged in.",
        logged_in=False
    )


@app.post("/api/register")
def register():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return failure("Invalid request.")

    username = data.get("username")
    password = data.get("password")

    if not isinstance(username, str):
        return failure("Invalid username.")

    if not isinstance(password, str):
        return failure("Invalid password.")

    connection = None

    try:
        connection = GameConnection()

        result = connection.send(
            "register",
            username=username,
            password=password
        )

        return jsonify(result)

    except (OSError, ValueError, ConnectionError):
        return failure(
            "Unable to reach the game server.",
            503
        )

    finally:
        if connection:
            connection.close()


@app.post("/api/login")
def login():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return failure("Invalid request.")

    username = data.get("username")
    password = data.get("password")

    if not isinstance(username, str):
        return failure("Invalid username.")

    if not isinstance(password, str):
        return failure("Invalid password.")

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

        with connections_lock:
            connections[sid] = connection

        session.clear()
        session["sid"] = sid

        return success(
            result.get("message", "Welcome!"),
            username=username
        )

    except (OSError, ValueError, ConnectionError):
        if connection:
            connection.close()

        return failure(
            "Unable to reach the game server.",
            503
        )


@app.post("/api/logout")
def logout():
    sid = session.get("sid")
    connection = get_connection()

    if connection:
        try:
            connection.send("logout")
        except (OSError, ValueError, ConnectionError):
            pass

    remove_connection(sid)
    session.clear()

    return success("Logged out successfully.")


def cleanup_sessions():
    while True:
        time.sleep(60)
        now = time.monotonic()

        with connections_lock:
            expired = [
                sid
                for sid, connection in connections.items()
                if now - connection.last_used > SESSION_IDLE_SECONDS
            ]

        for sid in expired:
            remove_connection(sid)


if __name__ == "__main__":
    threading.Thread(
        target=cleanup_sessions,
        daemon=True
    ).start()

    print("===================================")
    print("       RPS ARENA WEB")
    print("===================================")
    print(f"Game server: {GAME_HOST}:{GAME_PORT}")
    print("Website: http://127.0.0.1:8000")
    print("Local testing only")
    print("===================================")

    app.run(
        host="127.0.0.1",
        port=8000,
        debug=False,
        threaded=True
    )
