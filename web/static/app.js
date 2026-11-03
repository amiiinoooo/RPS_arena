
/*
==========================================
RPS ARENA WEB CLIENT
Login, Lobby, Gameplay, Browser Presence
==========================================
*/

const authScreen = document.getElementById(
    "auth-screen"
);
const dashboardScreen = document.getElementById(
    "dashboard-screen"
);
const form = document.getElementById("auth-form");
const formTitle = document.getElementById(
    "form-title"
);
const formDescription = document.getElementById(
    "form-description"
);
const usernameInput = document.getElementById(
    "username"
);
const passwordInput = document.getElementById(
    "password"
);
const submitButton = document.getElementById(
    "submit-button"
);
const switchButton = document.getElementById(
    "switch-button"
);
const switchPrompt = document.getElementById(
    "switch-prompt"
);
const messageBox = document.getElementById(
    "message"
);
const connectionIndicator = document.getElementById(
    "connection-indicator"
);


// ==========================================
// CLIENT STATE
// ==========================================

let formMode = "login";
let username = null;
let busy = false;
let stateBusy = false;
let currentState = null;
let lastSnapshot = null;
let lastNotice = null;

const TAB_STORAGE_KEY = "rps_arena_tab_id";

let tabId = sessionStorage.getItem(TAB_STORAGE_KEY);

if (!tabId) {
    tabId = crypto.randomUUID();
    sessionStorage.setItem(TAB_STORAGE_KEY, tabId);
}


// ==========================================
// NETWORK
// ==========================================

async function apiRequest(path, data = null) {
    const options = {
        method: data === null ? "GET" : "POST",
        credentials: "same-origin",
        headers: {
            "Accept": "application/json"
        }
    };

    if (data !== null) {
        options.headers["Content-Type"] =
            "application/json";

        options.body = JSON.stringify(data);
    }

    const response = await fetch(path, options);
    const result = await response.json();

    return result;
}


// ==========================================
// BROWSER PRESENCE
// ==========================================

async function sendPresence() {
    if (!username) {
        return;
    }

    try {
        const result = await apiRequest(
            "/api/presence",
            {tab_id: tabId}
        );

        if (!result.success) {
            await checkStatus();
        }
    } catch (error) {
        // Temporary network problems are handled
        // by the server's inactivity timeout.
    }
}


function notifyBrowserClosing() {
    if (!username) {
        return;
    }

    const data = new Blob(
        [JSON.stringify({tab_id: tabId})],
        {type: "application/json"}
    );

    navigator.sendBeacon(
        "/api/browser-leave",
        data
    );
}


// A refresh also triggers pagehide.
// The server waits 4 seconds so the
// refreshed page can renew its session.
window.addEventListener(
    "pagehide",
    notifyBrowserClosing
);

window.addEventListener(
    "pageshow",
    () => {
        if (username) {
            sendPresence();
        }
    }
);

window.addEventListener(
    "focus",
    () => {
        if (username) {
            sendPresence();
        }
    }
);


// ==========================================
// MESSAGES
// ==========================================

function showMessage(text, isError = false) {
    messageBox.textContent = text || "";

    messageBox.className = isError
        ? "message error"
        : "message success";

    if (!text) {
        messageBox.classList.add("hidden");
    }
}


// ==========================================
// ELEMENT HELPERS
// ==========================================

function element(tag, text = "") {
    const node = document.createElement(tag);
    node.textContent = String(text);
    return node;
}


function addInfo(parent, text, color = "#AEC0D7") {
    const node = element("p", text);
    node.style.color = color;
    node.style.lineHeight = "1.6";
    parent.appendChild(node);
    return node;
}


function addTitle(parent, text) {
    const node = element("h3", text);
    node.style.marginTop = "24px";
    parent.appendChild(node);
    return node;
}


function addPanel(parent) {
    const node = document.createElement("div");

    node.style.background = "#101827";
    node.style.border = "1px solid #2a3955";
    node.style.borderRadius = "12px";
    node.style.padding = "18px";
    node.style.marginTop = "14px";

    parent.appendChild(node);
    return node;
}


function addButton(
    parent,
    label,
    callback,
    primary = true,
    disabled = false
) {
    const button = element("button", label);

    button.type = "button";

    button.className = primary
        ? "primary-button"
        : "secondary-button";

    button.style.marginTop = "12px";
    button.disabled = disabled;

    button.addEventListener("click", callback);
    parent.appendChild(button);

    return button;
}


// ==========================================
// SCREEN MANAGEMENT
// ==========================================

function showLoginScreen() {
    username = null;
    currentState = null;
    lastSnapshot = null;
    lastNotice = null;

    authScreen.classList.remove("hidden");
    dashboardScreen.classList.add("hidden");

    connectionIndicator.textContent =
        "RPS Arena Web | Local test";
}


function showDashboard(playerUsername) {
    const changed = username !== playerUsername;

    username = playerUsername;

    authScreen.classList.add("hidden");
    dashboardScreen.classList.remove("hidden");

    dashboardScreen.style.width =
        "min(100%, 760px)";

    dashboardScreen.style.maxWidth = "760px";
    dashboardScreen.style.textAlign = "left";

    connectionIndicator.textContent =
        "TCP game server: Connected";

    if (changed) {
        lastSnapshot = null;
        renderDashboard();
        sendPresence();
    }
}


// ==========================================
// MAIN SCREEN ROUTER
// ==========================================

function renderDashboard() {
    if (!username) {
        return;
    }

    dashboardScreen.replaceChildren();

    const header = document.createElement("div");
    header.style.display = "flex";
    header.style.justifyContent = "space-between";
    header.style.alignItems = "center";
    header.style.flexWrap = "wrap";
    header.style.gap = "12px";

    const heading = element("h2", "RPS ARENA");
    heading.style.margin = "0";

    const playerName = element(
        "strong",
        "Player: " + username
    );
    playerName.style.color = "#58def2";

    header.appendChild(heading);
    header.appendChild(playerName);
    dashboardScreen.appendChild(header);

    const separator = document.createElement("hr");
    separator.style.border = "none";
    separator.style.borderTop = "1px solid #2a3955";
    separator.style.margin = "20px 0";
    dashboardScreen.appendChild(separator);

    if (!currentState) {
        addInfo(
            dashboardScreen,
            "Loading multiplayer lobby..."
        );
    } else if (
        currentState.in_room &&
        currentState.room
    ) {
        const room = currentState.room;
        const match = currentState.match;

        if (room.status === "playing") {
            renderGame(dashboardScreen, room, match);
        } else if (room.status === "finished") {
            renderFinished(
                dashboardScreen, room, match
            );
        } else {
            renderRoom(dashboardScreen, room);
        }
    } else {
        renderLobby(
            dashboardScreen,
            currentState.rooms || []
        );
    }

    addButton(
        dashboardScreen,
        "LOG OUT",
        logout,
        false
    );
}


// ==========================================
// AUTHENTICATION
// ==========================================

function updateForm() {
    const registering = formMode === "register";

    formTitle.textContent = registering
        ? "Join the arena"
        : "Welcome back";

    formDescription.textContent = registering
        ? "Create your player account."
        : "Enter your account to join the arena.";

    submitButton.textContent = registering
        ? "CREATE ACCOUNT"
        : "ENTER THE ARENA";

    switchPrompt.textContent = registering
        ? "Already have an account?"
        : "Don't have an account?";

    switchButton.textContent = registering
        ? "Sign in"
        : "Create account";

    passwordInput.autocomplete = registering
        ? "new-password"
        : "current-password";
}


switchButton.addEventListener("click", () => {
    formMode = formMode === "login"
        ? "register"
        : "login";

    passwordInput.value = "";
    showMessage("");
    updateForm();
});


form.addEventListener("submit", async event => {
    event.preventDefault();

    if (busy) {
        return;
    }

    const enteredUsername =
        usernameInput.value.trim();

    const password = passwordInput.value;

    busy = true;
    submitButton.disabled = true;
    showMessage("");

    try {
        const result = await apiRequest(
            "/api/" + formMode,
            {
                username: enteredUsername,
                password: password
            }
        );

        showMessage(
            result.message || "",
            !result.success
        );

        if (result.success) {
            passwordInput.value = "";

            if (formMode === "register") {
                formMode = "login";
                updateForm();
            } else {
                showDashboard(
                    result.username || enteredUsername
                );

                await refreshState();
            }
        }
    } catch (error) {
        showMessage(
            "Unable to reach the web server.",
            true
        );
    } finally {
        busy = false;
        submitButton.disabled = false;
    }
});


// ==========================================
// LOBBY
// ==========================================

function renderLobby(parent, rooms) {
    addTitle(parent, "MULTIPLAYER LOBBY");

    addInfo(
        parent,
        "Create a room or join another player."
    );

    addButton(
        parent,
        "+ CREATE ROOM",
        createRoom
    );

    addButton(
        parent,
        "REFRESH ROOMS",
        refreshState,
        false
    );

    addTitle(parent, "Available Rooms");

    if (rooms.length === 0) {
        const panel = addPanel(parent);

        addInfo(
            panel,
            "No rooms available. Create the first one!"
        );
        return;
    }

    for (const room of rooms) {
        const panel = addPanel(parent);

        panel.appendChild(
            element(
                "h3",
                room.name + " | Room #" + room.id
            )
        );

        addInfo(panel, "Host: " + room.host);

        addInfo(
            panel,
            "Players: " +
            room.player_count +
            "/" + room.max_players
        );

        addInfo(
            panel,
            "Status: " + room.status.toUpperCase()
        );

        const canJoin = (
            room.status !== "playing" &&
            room.status !== "finished" &&
            room.player_count < room.max_players
        );

        if (canJoin) {
            addButton(
                panel,
                "JOIN ROOM",
                () => performAction(
                    "join_room",
                    {room_id: room.id}
                )
            );
        }
    }
}


// ==========================================
// WAITING ROOM
// ==========================================

function renderRoom(parent, room) {
    addTitle(
        parent,
        room.name + " | Room #" + room.id
    );

    addInfo(parent, "Host: " + room.host);

    addInfo(
        parent,
        "Players: " +
        room.player_count +
        "/" + room.max_players
    );

    addTitle(parent, "PLAYERS");

    for (const player of room.players) {
        const panel = addPanel(parent);

        const isHost = player === room.host;

        const ready = room.ready_players.includes(
            player
        );

        panel.appendChild(
            element(
                "strong",
                player + (isHost ? " (HOST)" : "")
            )
        );

        addInfo(
            panel,
            ready ? "READY ✓" : "NOT READY",
            ready ? "#40d7a0" : "#95a6c5"
        );
    }

    const myReady = room.ready_players.includes(
        username
    );

    addButton(
        parent,
        myReady ? "CANCEL READY" : "I'M READY",
        () => performAction(
            myReady ? "unready" : "ready"
        ),
        !myReady
    );

    if (room.host === username) {
        addButton(
            parent,
            "START GAME",
            () => performAction("start_game"),
            true,
            !room.can_start
        );

        if (!room.can_start) {
            addInfo(
                parent,
                "Start requires at least two players and everyone ready."
            );
        }
    }

    addButton(
        parent,
        "LEAVE ROOM",
        () => performAction("leave_room"),
        false
    );
}


// ==========================================
// SCOREBOARD
// ==========================================

function renderScoreboard(parent, scores) {
    addTitle(parent, "🏆 SCOREBOARD");

    const panel = addPanel(parent);

    const ranking = Object.entries(
        scores || {}
    ).sort((a, b) => {
        if (a[1] !== b[1]) {
            return b[1] - a[1];
        }

        return a[0].localeCompare(b[0]);
    });

    let rank = 0;
    let previousScore = null;

    ranking.forEach(([player, score], index) => {
        if (score !== previousScore) {
            rank = index + 1;
        }

        const line = element(
            "p",
            "#" + rank + "  " +
            player + "  |  " +
            score + " points"
        );

        line.style.padding = "10px";
        line.style.margin = "6px 0";
        line.style.borderRadius = "8px";

        line.style.background =
            player === username
                ? "#263f63"
                : "#1b2940";

        line.style.color =
            player === username
                ? "#58def2"
                : "#f3f6ff";

        panel.appendChild(line);
        previousScore = score;
    });
}


// ==========================================
// ROUND RESULTS
// ==========================================

function renderRoundResults(parent, result) {
    if (!result) {
        return;
    }

    addTitle(
        parent,
        "ROUND " + result.round + " RESULTS"
    );

    const panel = addPanel(parent);

    for (
        const [player, move]
        of Object.entries(result.choices || {})
    ) {
        const points = result.points?.[player] ?? 0;

        const line = element(
            "p",
            player + ": " +
            move.toUpperCase() +
            "  |  +" + points + " points"
        );

        line.style.padding = "9px";
        line.style.margin = "5px 0";

        line.style.color =
            player === username
                ? "#58def2"
                : "#f3f6ff";

        panel.appendChild(line);
    }
}


// ==========================================
// ACTIVE MATCH
// ==========================================

function renderGame(parent, room, match) {
    addTitle(parent, "⚔️ " + room.name);

    if (!match) {
        addInfo(parent, "Preparing match...");
        return;
    }

    const tiebreaker =
        match.round > match.total_rounds;

    addTitle(
        parent,
        "ROUND " + match.round +
        (tiebreaker ? " | TIEBREAKER" : "")
    );

    const timer = element(
        "h2",
        "⏱ " + match.remaining + " seconds"
    );

    timer.style.fontSize = "32px";
    timer.style.color = match.remaining <= 5
        ? "#ff727e"
        : "#40d7a0";

    parent.appendChild(timer);

    renderScoreboard(parent, match.scores);

    if (match.phase === "results") {
        addTitle(parent, "ROUND COMPLETE");

        renderRoundResults(
            parent,
            match.last_result
        );

        addInfo(
            parent,
            "Next round starts automatically."
        );

    } else if (match.phase === "choosing") {
        const panel = addPanel(parent);

        panel.appendChild(
            element("h3", "CHOOSE YOUR MOVE")
        );

        if (match.my_choice) {
            addInfo(
                panel,
                "✓ You selected: " +
                match.my_choice.toUpperCase(),
                "#40d7a0"
            );

            addInfo(
                panel,
                "Waiting for other players..."
            );

        } else {
            const moves = [
                ["rock", "🪨 ROCK"],
                ["paper", "📄 PAPER"],
                ["scissors", "✂️ SCISSORS"]
            ];

            for (const [move, label] of moves) {
                addButton(
                    panel,
                    label,
                    () => performAction(
                        "choose",
                        {
                            move: move,
                            round: match.round
                        }
                    ),
                    true,
                    busy || match.remaining <= 0
                );
            }
        }

        addInfo(
            parent,
            "Submitted: " +
            match.submitted.length +
            "/" + match.players.length +
            " players"
        );

        for (const player of match.players) {
            const submitted =
                match.submitted.includes(player);

            addInfo(
                parent,
                player + ": " +
                (submitted ? "Submitted ✓" : "Choosing..."),
                submitted ? "#40d7a0" : "#AEC0D7"
            );
        }
    }

    addButton(
        parent,
        "LEAVE MATCH",
        () => performAction("leave_room"),
        false
    );
}


// ==========================================
// MATCH FINISHED
// ==========================================

function renderFinished(parent, room, match) {
    addTitle(parent, "🏆 MATCH FINISHED");

    if (!match) {
        addInfo(parent, "Loading results...");
        return;
    }

    const winners = match.winners || [];

    const resultText = match.draw
        ? "DRAW: " + winners.join(", ")
        : "WINNER: " + winners.join(", ");

    const winnerTitle = element(
        "h2",
        resultText
    );

    winnerTitle.style.color = "#40d7a0";
    winnerTitle.style.marginTop = "24px";

    parent.appendChild(winnerTitle);

    addInfo(
        parent,
        "Rounds played: " + match.round
    );

    renderScoreboard(parent, match.scores);

    renderRoundResults(
        parent,
        match.last_result
    );

    if (room.host === username) {
        addButton(
            parent,
            "🔄 PLAY AGAIN",
            () => performAction("reset_match")
        );
    } else {
        addInfo(
            parent,
            "Waiting for the host to start a new match."
        );
    }

    addButton(
        parent,
        "LEAVE ROOM",
        () => performAction("leave_room"),
        false
    );
}


// ==========================================
// CREATE ROOM
// ==========================================

async function createRoom() {
    const roomName = window.prompt(
        "Enter a room name (1-30 characters):"
    );

    if (roomName === null) {
        return;
    }

    const cleaned = roomName.trim();

    if (
        cleaned.length < 1 ||
        cleaned.length > 30
    ) {
        showMessage(
            "Room name must be 1-30 characters.",
            true
        );
        return;
    }

    await performAction(
        "create_room",
        {room_name: cleaned}
    );
}


// ==========================================
// GAME ACTIONS
// ==========================================

async function performAction(action, payload = {}) {
    if (busy || !username) {
        return;
    }

    busy = true;

    try {
        const result = await apiRequest(
            "/api/action",
            {
                action,
                ...payload
            }
        );

        showMessage(
            result.message || "",
            !result.success
        );

        if (result.success) {
            lastSnapshot = null;
            await refreshState();
        }

    } catch (error) {
        showMessage(
            "Unable to perform this action.",
            true
        );

    } finally {
        busy = false;
    }
}


// ==========================================
// LIVE STATE UPDATES
// ==========================================

async function refreshState() {
    if (!username || stateBusy) {
        return;
    }

    stateBusy = true;

    try {
        const result = await apiRequest(
            "/api/state"
        );

        if (!result.success) {
            showMessage(
                result.message ||
                "Unable to load game state.",
                true
            );

            if (
                result.message &&
                (
                    result.message.includes("log in") ||
                    result.message.includes("connection lost")
                )
            ) {
                showLoginScreen();
            }

            return;
        }

        const snapshot = JSON.stringify({
            in_room: result.in_room,
            room: result.room,
            rooms: result.rooms,
            match: result.match
        });

        currentState = result;

        if (
            result.notice &&
            result.notice !== lastNotice
        ) {
            lastNotice = result.notice;
            showMessage(result.notice, true);
        }

        if (snapshot !== lastSnapshot) {
            lastSnapshot = snapshot;
            renderDashboard();
        }

    } catch (error) {
        showMessage(
            "Game connection unavailable.",
            true
        );

    } finally {
        stateBusy = false;
    }
}


// ==========================================
// LOGOUT
// ==========================================

async function logout() {
    if (busy) {
        return;
    }

    busy = true;

    try {
        const result = await apiRequest(
            "/api/logout",
            {}
        );

        if (result.success) {
            showLoginScreen();
            showMessage(
                "Logged out successfully."
            );
        } else {
            showMessage(
                result.message || "Logout failed.",
                true
            );
        }

    } catch (error) {
        showMessage(
            "Unable to log out.",
            true
        );

    } finally {
        busy = false;
    }
}


// ==========================================
// SESSION CHECK
// ==========================================

async function checkStatus() {
    try {
        const result = await apiRequest(
            "/api/status"
        );

        if (!result.success) {
            showLoginScreen();

            showMessage(
                result.message ||
                "Game server unavailable.",
                true
            );

            return;
        }

        if (result.logged_in) {
            showDashboard(result.username);

            if (!currentState) {
                await refreshState();
            }

        } else if (username !== null) {
            showLoginScreen();
        }

    } catch (error) {
        connectionIndicator.textContent =
            "Web server unavailable";
    }
}


// ==========================================
// INITIALIZE
// ==========================================

updateForm();
checkStatus();

// Keep the browser session active.
setInterval(sendPresence, 2000);

// Check authentication status.
setInterval(checkStatus, 5000);

// Update game and lobby.
setInterval(refreshState, 1000);
