/*
Handles merchant login/signup forms. Mirrors customer_auth.js but posts to the
merchant endpoints and redirects to the (upcoming) merchant dashboard on success.
*/

function showMerchantError(message) {
    const banner = document.getElementById("error-banner");
    if (banner) {
        banner.textContent = message;
        banner.style.display = "block";
    }
}

async function handleMerchantAuthSubmit(event, endpoint) {
    event.preventDefault();
    const form = event.target;
    const formData = new FormData(form);
    const payload = Object.fromEntries(formData.entries());

    try {
        const response = await fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });

        if (!response.ok) {
            const errorBody = await response.json().catch(() => ({}));
            showMerchantError(errorBody.detail || "Something went wrong. Please try again.");
            return;
        }

        window.location.href = "/merchant";

    } catch (err) {
        console.error("Merchant auth request failed:", err);
        showMerchantError("Couldn't reach the server. Please check your connection and try again.");
    }
}

document.addEventListener("DOMContentLoaded", () => {
    const loginForm = document.getElementById("merchant-login-form");
    if (loginForm) {
        loginForm.addEventListener("submit", (e) => handleMerchantAuthSubmit(e, "/api/merchant/login"));
    }

    const signupForm = document.getElementById("merchant-signup-form");
    if (signupForm) {
        signupForm.addEventListener("submit", (e) => handleMerchantAuthSubmit(e, "/api/merchant/signup"));
    }
});