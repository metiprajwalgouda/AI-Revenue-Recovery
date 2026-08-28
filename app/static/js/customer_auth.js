/*
Handles the customer login/signup forms. On success, the server sets an
httpOnly session cookie automatically (we never touch the token in JS --
httpOnly means JS can't read it even if we tried, which is the point).
*/

function showError(message) {
    const banner = document.getElementById("error-banner");
    if (banner) {
        banner.textContent = message;
        banner.style.display = "block";
    }
}

async function handleAuthFormSubmit(event, endpoint) {
    event.preventDefault();
    const form = event.target;
    const formData = new FormData(form);
    const payload = Object.fromEntries(formData.entries());

    // Don't send empty optional fields as empty strings -- let the server see them as absent.
    Object.keys(payload).forEach(key => {
        if (payload[key] === "") delete payload[key];
    });

    try {
        const response = await fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });

        if (!response.ok) {
            const errorBody = await response.json().catch(() => ({}));
            showError(errorBody.detail || "Something went wrong. Please try again.");
            return;
        }

        // Redirect to the page the customer was probably trying to reach.
        const redirectTo = new URLSearchParams(window.location.search).get("next") || "/";
        window.location.href = redirectTo;

    } catch (err) {
        console.error("Auth request failed:", err);
        showError("Couldn't reach the server. Please check your connection and try again.");
    }
}

document.addEventListener("DOMContentLoaded", () => {
    const loginForm = document.getElementById("login-form");
    if (loginForm) {
        loginForm.addEventListener("submit", (e) => handleAuthFormSubmit(e, "/api/customer/login"));
    }

    const signupForm = document.getElementById("signup-form");
    if (signupForm) {
        signupForm.addEventListener("submit", (e) => handleAuthFormSubmit(e, "/api/customer/signup"));
    }
});