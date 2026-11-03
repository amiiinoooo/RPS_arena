
const authScreen = document.getElementById("auth-screen");
const dashboardScreen = document.getElementById(
    "dashboard-screen"
);

const form = document.getElementById("auth-form");
const formTitle = document.getElementById("form-title");
const formDescription = document.getElementById(
    "form-description"
);

const usernameInput = document.getElementById("username");
const passwordInput = document.getElementById("password");
const submitButton = document.getElementById(
    "submit-button"
);

const switchButton = document.getElementById(
    "switch-button"
);
const switchPrompt = document.getElementById(
    "switch-prompt"
);

const logoutButton = document.getElementById(
    "logout-button"
);

const messageBox = document.getElementById("message");
const displayUsername = document.getElementById(
    "display-username"
);

const connectionIndicator = document.getElementById(
    "connection-indicator"
);

let formMode = "login";
let busy = false;


function showMessage(text, isError = false) {
    messageBox.textContent = text;
    messageBox.className = isError
        ? "message error"
        : "message success";

    if (!text) {
        messageBox.classList.add("hidden");
    }
}


async function apiRequest(path, data = null) {
    const options = {
        method: data === null ? "GET" : "POST",
        credentials: "same-origin",
        headers: {
            "Accept": "application/json"
        }
    };

    if (data !== null) {
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(data);
    }

    const response = await fetch(path, options);

    const result = await response.json();

    if (!response.ok && result.success) {
        throw new Error("Unexpected server response.");
    }

    return result;
}


function showLoginScreen() {
    authScreen.classList.remove("hidden");
    dashboardScreen.classList.add("hidden");

    connectionIndicator.textContent =
        "RPS Arena Web | Local test";
}


function showDashboard(username) {
    authScreen.classList.add("hidden");
    dashboardScreen.classList.remove("hidden");

    displayUsername.textContent = username;

    connectionIndicator.textContent =
        "TCP game server: Connected";
}


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


form.addEventListener("submit", async (event) => {
    event.preventDefault();

    if (busy) {
        return;
    }

    const username = usernameInput.value.trim();
    const password = passwordInput.value;

    busy = true;
    submitButton.disabled = true;

    showMessage("");

    try {
        const result = await apiRequest(
            "/api/" + formMode,
            {
                username: username,
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
                showDashboard(result.username || username);
            }
        }

    } catch (error) {
        showMessage(
            "Unable to connect to the web server.",
            true
        );

    } finally {
        busy = false;
        submitButton.disabled = false;
    }
});


logoutButton.addEventListener("click", async () => {
    if (busy) {
        return;
    }

    busy = true;
    logoutButton.disabled = true;

    try {
        await apiRequest("/api/logout", {});
        showLoginScreen();
        showMessage("Logged out successfully.");

    } catch (error) {
        showMessage("Unable to log out.", true);

    } finally {
        busy = false;
        logoutButton.disabled = false;
    }
});


async function checkStatus() {
    try {
        const result = await apiRequest("/api/status");

        if (!result.success) {
            showLoginScreen();

            if (result.message) {
                showMessage(result.message, true);
            }

            return;
        }

        if (result.logged_in) {
            showDashboard(result.username);
        } else {
            showLoginScreen();
        }

    } catch (error) {
        connectionIndicator.textContent =
            "Web server connection unavailable";
    }
}


updateForm();
checkStatus();

setInterval(checkStatus, 5000);
