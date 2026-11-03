
import socket
import threading
import sqlite3
import json
import hashlib
import secrets
import hmac
import re
import time
import os
from pathlib import Path

# ==========================================
# RPS ARENA - TCP + UDP SERVER
# ==========================================

HOST = os.environ.get("RPS_BIND_HOST", "127.0.0.1")
TCP_PORT = 5000
UDP_PORT = 5001

MIN_PLAYERS = 2
MAX_PLAYERS = 4

TOTAL_ROUNDS = 5
ROUND_SECONDS = 15
RESULT_SECONDS = 4
MAX_EXTRA_ROUNDS = 3

HEARTBEAT_TIMEOUT = 25
MAX_MESSAGE = 8192

MOVES = ("rock", "paper", "scissors")

DB_PATH = Path(__file__).with_name("users.db")
ITERATIONS = 250000

rooms = {}
sessions = {}
state_lock = threading.RLock()

next_room_id = 1


# ==========================================
# DATABASE
# ==========================================

def initialize_database():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                salt TEXT NOT NULL,
                password_hash TEXT NOT NULL
            )
        """)

    print("[DATABASE] Ready")


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        ITERATIONS
    ).hex()


def register_user(username, password):
    if not isinstance(username, str):
        return error("Invalid username.")

    if not isinstance(password, str):
        return error("Invalid password.")

    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
        return error(
            "Username must be 3-20 letters, numbers or underscores."
        )

    if not 8 <= len(password) <= 128:
        return error(
            "Password must contain 8-128 characters."
        )

    salt = secrets.token_bytes(16)
    password_hash = hash_password(password, salt)

    try:
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            db.execute(
                """INSERT INTO users
                   (username, salt, password_hash)
                   VALUES (?, ?, ?)""",
                (username, salt.hex(), password_hash)
            )

        print(f"[REGISTER] {username}")

        return ok("Account created successfully!")

    except sqlite3.IntegrityError:
        return error("Username already exists.")


def verify_login(username, password):
    if not isinstance(username, str):
        return False

    if not isinstance(password, str):
        return False

    if len(password) > 128:
        return False

    with sqlite3.connect(DB_PATH) as db:
        user = db.execute(
            """SELECT salt, password_hash
               FROM users WHERE username = ?""",
            (username,)
        ).fetchone()

    if user is None:
        return False

    salt = bytes.fromhex(user[0])
    actual = hash_password(password, salt)

    return hmac.compare_digest(actual, user[1])


# ==========================================
# RESPONSE HELPERS
# ==========================================

def ok(message="", **data):
    return {
        "success": True,
        "message": message,
        **data
    }


def error(message):
    return {
        "success": False,
        "message": message
    }


# ==========================================
# ROOMS
# ==========================================

def find_room(username):
    # Caller holds state_lock.
    for room_id, room in rooms.items():
        if username in room["players"]:
            return room_id
    return None


def room_status(room):
    if room["status"] in ("playing", "finished"):
        return room["status"]

    count = len(room["players"])

    if count < MIN_PLAYERS:
        return "waiting"

    if count == MAX_PLAYERS:
        return "full"

    return "ready"


def serialize_room(room_id, room):
    count = len(room["players"])

    all_ready = (
        count >= MIN_PLAYERS
        and all(
            player in room["ready"]
            for player in room["players"]
        )
    )

    return {
        "id": room_id,
        "name": room["name"],
        "host": room["host"],
        "players": list(room["players"]),
        "ready_players": sorted(room["ready"]),
        "player_count": count,
        "min_players": MIN_PLAYERS,
        "max_players": MAX_PLAYERS,
        "status": room_status(room),
        "can_start": (
            room["status"] == "waiting"
            and all_ready
        )
    }


def leave_room(username):
    # Caller holds state_lock.
    room_id = find_room(username)

    if room_id is None:
        return None

    room = rooms[room_id]

    room["players"].remove(username)
    room["ready"].discard(username)

    print(f"[ROOM] {username} left room {room_id}")

    if not room["players"]:
        del rooms[room_id]
        print(f"[ROOM] Deleted room {room_id}")
        return room_id

    if room["host"] == username:
        room["host"] = room["players"][0]
        print(f"[ROOM] New host: {room['host']}")

    if room["status"] == "playing":
        room["status"] = "waiting"
        room["match"] = None
        room["ready"].clear()
        room["notice"] = (
            f"Match interrupted: {username} disconnected or left."
        )
        print(f"[MATCH] Room {room_id} interrupted")

    return room_id


# ==========================================
# GAME ENGINE
# ==========================================

def beats(a, b):
    return (
        (a == "rock" and b == "scissors")
        or (a == "paper" and b == "rock")
        or (a == "scissors" and b == "paper")
    )


def create_match(players):
    return {
        "players": list(players),
        "round": 1,
        "phase": "choosing",
        "deadline": time.monotonic() + ROUND_SECONDS,
        "choices": {},
        "scores": {p: 0 for p in players},
        "last_result": None,
        "history": [],
        "winners": [],
        "ranking": [],
        "draw": False
    }


def calculate_round(room):
    match = room["match"]

    if (
        room["status"] != "playing"
        or match["phase"] != "choosing"
    ):
        return

    players = match["players"]
    choices = dict(match["choices"])

    points = {p: 0 for p in players}

    for i in range(len(players)):
        for j in range(i + 1, len(players)):
            a = players[i]
            b = players[j]

            ma = choices.get(a)
            mb = choices.get(b)

            if ma is None and mb is not None:
                points[b] += 1

            elif mb is None and ma is not None:
                points[a] += 1

            elif ma is not None and mb is not None:
                if beats(ma, mb):
                    points[a] += 1

                elif beats(mb, ma):
                    points[b] += 1

    for player, points_earned in points.items():
        match["scores"][player] += points_earned

    result = {
        "round": match["round"],
        "choices": {
            p: choices.get(p, "timeout")
            for p in players
        },
        "points": points,
        "total_scores": dict(match["scores"])
    }

    match["last_result"] = result
    match["history"].append(result)

    match["phase"] = "results"
    match["deadline"] = (
        time.monotonic() + RESULT_SECONDS
    )

    print(f"[ROUND {match['round']}] {result['choices']}")
    print(f"[SCORES] {match['scores']}")


def finish_match(room):
    match = room["match"]

    ranking = sorted(
        match["players"],
        key=lambda p: (
            -match["scores"][p],
            p.lower()
        )
    )

    top = match["scores"][ranking[0]]

    winners = [
        p for p in ranking
        if match["scores"][p] == top
    ]

    match["ranking"] = ranking
    match["winners"] = winners
    match["draw"] = len(winners) > 1
    match["phase"] = "finished"
    match["deadline"] = None

    room["status"] = "finished"

    print(f"[MATCH FINISHED] Winners: {winners}")


def advance_match(room):
    match = room["match"]

    if match["phase"] != "results":
        return

    round_number = match["round"]

    if round_number >= TOTAL_ROUNDS:
        top = max(match["scores"].values())

        leaders = [
            p for p, score in match["scores"].items()
            if score == top
        ]

        if (
            len(leaders) == 1
            or round_number >= (
                TOTAL_ROUNDS + MAX_EXTRA_ROUNDS
            )
        ):
            finish_match(room)
            return

    match["round"] += 1
    match["phase"] = "choosing"
    match["choices"] = {}
    match["deadline"] = (
        time.monotonic() + ROUND_SECONDS
    )

    print(f"[ROUND {match['round']}] Started")


def timer_worker():
    while True:
        time.sleep(0.2)

        with state_lock:
            now = time.monotonic()

            for room in list(rooms.values()):
                if room["status"] != "playing":
                    continue

                match = room["match"]

                if match is None:
                    continue

                if now < match["deadline"]:
                    continue

                if match["phase"] == "choosing":
                    calculate_round(room)

                elif match["phase"] == "results":
                    advance_match(room)


def game_state(username):
    room_id = find_room(username)

    if room_id is None:
        return error("You are not in a room.")

    room = rooms[room_id]
    match = room["match"]

    data = {
        "room": serialize_room(room_id, room),
        "notice": room.get("notice", ""),
        "match": None
    }

    if match is None:
        return ok(**data)

    remaining = 0

    if match["deadline"] is not None:
        remaining = max(
            0,
            int(
                match["deadline"]
                - time.monotonic()
                + 0.999
            )
        )

    data["match"] = {
        "round": match["round"],
        "total_rounds": TOTAL_ROUNDS,
        "phase": match["phase"],
        "remaining": remaining,
        "players": list(match["players"]),
        "submitted": list(match["choices"].keys()),
        "my_choice": match["choices"].get(username),
        "scores": dict(match["scores"]),
        "last_result": match["last_result"],
        "winners": list(match["winners"]),
        "ranking": list(match["ranking"]),
        "draw": match["draw"]
    }

    return ok(**data)


def choose(username, request):
    room_id = find_room(username)

    if room_id is None:
        return error("Join a room first.")

    room = rooms[room_id]

    if room["status"] != "playing":
        return error("No active match.")

    match = room["match"]

    if match["phase"] != "choosing":
        return error("Round is not accepting choices.")

    round_number = request.get("round")

    if (
        type(round_number) is not int
        or round_number != match["round"]
    ):
        return error("Wrong round number.")

    if time.monotonic() >= match["deadline"]:
        calculate_round(room)
        return error("Round timer expired.")

    if username in match["choices"]:
        return error("Choice already submitted.")

    move = request.get("move")

    if move not in MOVES:
        return error("Invalid move.")

    match["choices"][username] = move

    print(
        f"[CHOICE] {username} submitted "
        f"round {match['round']}"
    )

    if len(match["choices"]) == len(match["players"]):
        calculate_round(room)

    return ok("Choice submitted!")


# ==========================================
# LOBBY ACTIONS
# ==========================================

def lobby_action(action, username, request):
    global next_room_id

    with state_lock:

        if action == "player_state":
            room_id = find_room(username)

            if room_id is None:
                return ok(
                    in_room=False,
                    is_host=False,
                    room=None
                )

            room = rooms[room_id]

            return ok(
                in_room=True,
                is_host=room["host"] == username,
                room=serialize_room(room_id, room)
            )

        if action == "list_rooms":
            return ok(rooms=[
                serialize_room(rid, room)
                for rid, room in rooms.items()
            ])

        if action == "create_room":
            if find_room(username) is not None:
                return error("Already in a room.")

            name = request.get("room_name", "")

            if not isinstance(name, str):
                return error("Invalid room name.")

            name = name.strip()

            if not 1 <= len(name) <= 30:
                return error("Room name must be 1-30 characters.")

            rid = next_room_id
            next_room_id += 1

            rooms[rid] = {
                "name": name,
                "host": username,
                "players": [username],
                "ready": set(),
                "status": "waiting",
                "match": None,
                "notice": ""
            }

            print(f"[ROOM] {username} created room {rid}")

            return ok(
                f"Room {rid} created!",
                room=serialize_room(rid, rooms[rid])
            )

        if action == "join_room":
            if find_room(username) is not None:
                return error("Leave your current room first.")

            rid = request.get("room_id")

            if type(rid) is not int or rid not in rooms:
                return error("Room not found.")

            room = rooms[rid]

            if room["status"] != "waiting":
                return error("Room is not accepting players.")

            if len(room["players"]) >= MAX_PLAYERS:
                return error("Room is full.")

            room["players"].append(username)
            room["notice"] = ""

            print(f"[ROOM] {username} joined {rid}")

            return ok(
                "Joined room!",
                room=serialize_room(rid, room)
            )

        if action == "my_room":
            rid = find_room(username)

            if rid is None:
                return error("Not in a room.")

            return ok(room=serialize_room(rid, rooms[rid]))

        if action == "leave_room":
            rid = leave_room(username)

            if rid is None:
                return error("Not in a room.")

            return ok(f"Left room {rid}.")

        if action in ("ready", "unready"):
            rid = find_room(username)

            if rid is None:
                return error("Join a room first.")

            room = rooms[rid]

            if room["status"] != "waiting":
                return error("Cannot change readiness now.")

            if action == "ready":
                room["ready"].add(username)
            else:
                room["ready"].discard(username)

            return ok(
                "Ready updated!",
                room=serialize_room(rid, room)
            )

        if action == "start_game":
            rid = find_room(username)

            if rid is None:
                return error("Join a room first.")

            room = rooms[rid]

            if room["host"] != username:
                return error("Only the host can start.")

            if room["status"] != "waiting":
                return error("Room is not waiting.")

            if len(room["players"]) < MIN_PLAYERS:
                return error("At least 2 players needed.")

            if not all(
                p in room["ready"]
                for p in room["players"]
            ):
                return error("Not all players are ready.")

            room["match"] = create_match(room["players"])
            room["status"] = "playing"
            room["notice"] = ""

            print(
                f"[MATCH] Started room {rid}: "
                f"{room['players']}"
            )

            return ok(
                "GAME STARTED!",
                room=serialize_room(rid, room)
            )

        if action == "reset_match":
            rid = find_room(username)

            if rid is None:
                return error("Join a room first.")

            room = rooms[rid]

            if room["host"] != username:
                return error("Only the host can reset.")

            if room["status"] != "finished":
                return error("Match must finish first.")

            room["status"] = "waiting"
            room["match"] = None
            room["ready"].clear()
            room["notice"] = ""

            return ok(
                "Match reset!",
                room=serialize_room(rid, room)
            )

        if action == "game_state":
            return game_state(username)

        if action == "choose":
            return choose(username, request)

        return error("Unknown action.")


# ==========================================
# SESSION MANAGEMENT
# ==========================================

def end_session(username, connection):
    if not username:
        return

    with state_lock:
        session = sessions.get(username)

        if (
            session is None
            or session["connection"] is not connection
        ):
            return

        del sessions[username]
        leave_room(username)

    print(f"[SESSION] {username} disconnected")


def timeout_worker():
    while True:
        time.sleep(2)

        expired = []
        now = time.monotonic()

        with state_lock:
            for username, session in list(sessions.items()):
                last_activity = max(
                    session["last_tcp"],
                    session["last_udp"]
                )

                if now - last_activity > HEARTBEAT_TIMEOUT:
                    expired.append(
                        (username, session["connection"])
                    )

        for username, connection in expired:
            print(f"[TIMEOUT] {username} inactive")

            end_session(username, connection)

            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

            try:
                connection.close()
            except OSError:
                pass


# ==========================================
# UDP HEARTBEAT SERVER
# ==========================================

def udp_worker():
    with socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM
    ) as udp:

        udp.bind((HOST, UDP_PORT))

        print(f"[UDP] Listening on {HOST}:{UDP_PORT}")

        while True:
            try:
                data, address = udp.recvfrom(2048)

                message = json.loads(
                    data.decode("utf-8")
                )

                if not isinstance(message, dict):
                    continue

                if message.get("type") != "heartbeat":
                    continue

                token = message.get("token")
                sequence = message.get("seq")

                if (
                    not isinstance(token, str)
                    or type(sequence) is not int
                    or sequence < 0
                ):
                    continue

                username_found = None

                with state_lock:
                    for username, session in sessions.items():
                        if hmac.compare_digest(
                            session["token"], token
                        ):
                            session["last_udp"] = (
                                time.monotonic()
                            )
                            session["udp_count"] += 1

                            username_found = username
                            break

                if username_found is None:
                    continue

                response = {
                    "type": "heartbeat_ack",
                    "seq": sequence
                }

                udp.sendto(
                    json.dumps(response).encode("utf-8"),
                    address
                )

                if sequence % 10 == 0:
                    print(
                        f"[UDP HEARTBEAT] "
                        f"{username_found} seq={sequence}"
                    )

            except (
                ValueError,
                UnicodeError,
                OSError
            ) as exc:
                print(f"[UDP ERROR] {exc}")


# ==========================================
# TCP SERVER
# ==========================================

def send_message(connection, response):
    message = json.dumps(response) + "\n"

    connection.sendall(
        message.encode("utf-8")
    )


def handle_client(connection, address):
    print(f"[TCP CONNECTED] {address}")

    username = None

    try:
        with connection:
            with connection.makefile(
                "r", encoding="utf-8"
            ) as reader:

                while True:
                    line = reader.readline(MAX_MESSAGE + 1)

                    if not line:
                        break

                    if (
                        len(line) > MAX_MESSAGE
                        or not line.endswith("\n")
                    ):
                        send_message(
                            connection,
                            error("Invalid message length.")
                        )
                        break

                    try:
                        request = json.loads(line)
                    except json.JSONDecodeError:
                        send_message(
                            connection,
                            error("Invalid JSON.")
                        )
                        continue

                    if not isinstance(request, dict):
                        send_message(
                            connection,
                            error("Invalid request.")
                        )
                        continue

                    action = request.get("action")

                    if username:
                        with state_lock:
                            session = sessions.get(username)

                            if (
                                session is None
                                or session["connection"] is not connection
                            ):
                                break

                            session["last_tcp"] = (
                                time.monotonic()
                            )

                    if action == "register":
                        response = register_user(
                            request.get("username"),
                            request.get("password")
                        )

                    elif action == "login":
                        wanted = request.get("username")
                        password = request.get("password")

                        if username:
                            response = error("Already logged in.")

                        elif not verify_login(wanted, password):
                            response = error(
                                "Incorrect username or password."
                            )

                        else:
                            with state_lock:
                                if wanted in sessions:
                                    response = error(
                                        "Account already online."
                                    )
                                else:
                                    token = secrets.token_hex(32)
                                    now = time.monotonic()

                                    sessions[wanted] = {
                                        "connection": connection,
                                        "token": token,
                                        "last_tcp": now,
                                        "last_udp": now,
                                        "udp_count": 0
                                    }

                                    username = wanted

                                    response = ok(
                                        f"Welcome, {wanted}!",
                                        heartbeat_token=token,
                                        udp_port=UDP_PORT
                                    )

                            if response["success"]:
                                print(f"[LOGIN] {wanted}")

                    elif action == "logout":
                        end_session(username, connection)
                        username = None
                        response = ok("Logged out.")

                    elif action == "status":
                        response = ok(
                            username=username
                        )

                    elif action == "heartbeat_status":
                        if username is None:
                            response = error("Please log in.")
                        else:
                            with state_lock:
                                session = sessions.get(username)

                                if session is None:
                                    response = error(
                                        "Session expired."
                                    )
                                else:
                                    age = (
                                        time.monotonic()
                                        - session["last_udp"]
                                    )

                                    response = ok(
                                        udp_received=(
                                            session["udp_count"] > 0
                                        ),
                                        udp_age=round(age, 1),
                                        udp_count=(
                                            session["udp_count"]
                                        )
                                    )

                    else:
                        if username is None:
                            response = error(
                                "Please log in first."
                            )
                        else:
                            response = lobby_action(
                                action,
                                username,
                                request
                            )

                    send_message(connection, response)

    except (
        ConnectionError,
        OSError,
        UnicodeError,
        ValueError
    ) as exc:
        print(f"[TCP ERROR] {address}: {exc}")

    finally:
        end_session(username, connection)
        print(f"[TCP DISCONNECTED] {address}")


# ==========================================
# MAIN
# ==========================================

def start_server():
    initialize_database()

    for worker in (
        timer_worker,
        timeout_worker,
        udp_worker
    ):
        threading.Thread(
            target=worker,
            daemon=True
        ).start()

    with socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM
    ) as server:

        server.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1
        )

        server.bind((HOST, TCP_PORT))
        server.listen()

        print("\n===================================")
        print("        RPS ARENA SERVER")
        print("===================================")
        print(f"Server address: {HOST}")
        print(f"TCP game port: {TCP_PORT}")
        print(f"UDP heartbeat port: {UDP_PORT}")
        print(f"Room capacity: {MIN_PLAYERS}-{MAX_PLAYERS}")
        print(f"Round timer: {ROUND_SECONDS} seconds")
        print("[STATUS] Server online")
        print("===================================\n")

        try:
            while True:
                connection, address = server.accept()

                threading.Thread(
                    target=handle_client,
                    args=(connection, address),
                    daemon=True
                ).start()

        except KeyboardInterrupt:
            print("\n[STOPPING] Server stopped.")


if __name__ == "__main__":
    start_server()
