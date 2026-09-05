/*
Handles the customer login/signup forms. On success, the server sets an
httpOnly session cookie automatically.
*/

function showError(message) {
    const banner = document.getElementById("error-banner");
    if (banner) {
        banner.textContent = message;
        banner.style.display = "block";
        banner.style.background = "#fef2f2";
        banner.style.color = "#b91c1c";
        banner.style.border = "1px solid #fecaca";
        banner.style.fontWeight = "600";
    }
}

function togglePasswordVisibility(inputId, btn) {
    const input = document.getElementById(inputId);
    if (!input) return;
    if (input.type === "password") {
        input.type = "text";
        btn.textContent = "🙈";
        btn.title = "Hide password";
    } else {
        input.type = "password";
        btn.textContent = "👁️";
        btn.title = "Show password";
    }
}

function fillDemoCustomer(email, password) {
    const emailInput = document.querySelector('input[name="email"]');
    const passInput = document.querySelector('input[name="password"]');
    if (emailInput) emailInput.value = email || "priya.sharma@example.com";
    if (passInput) passInput.value = password || "password123";
}

async function handleAuthFormSubmit(event, endpoint) {
    event.preventDefault();
    const form = event.target;
    const submitBtn = form.querySelector('button[type="submit"]');
    const origBtnText = submitBtn ? submitBtn.innerHTML : "Submit";

    const formData = new FormData(form);
    const payload = Object.fromEntries(formData.entries());

    // Don't send empty optional fields as empty strings -- let the server see them as absent.
    Object.keys(payload).forEach(key => {
        if (payload[key] === "") delete payload[key];
    });

    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = `<span>⏳ Processing...</span>`;
    }

    try {
        const response = await fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });

        if (!response.ok) {
            const errorBody = await response.json().catch(() => ({}));
            showError(errorBody.detail || "Something went wrong. Please check your details and try again.");
            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.innerHTML = origBtnText;
            }
            return;
        }

        // Redirect to next or home
        const redirectTo = new URLSearchParams(window.location.search).get("next") || "/";
        window.location.href = redirectTo;

    } catch (err) {
        console.error("Auth request failed:", err);
        showError("Couldn't reach the server. Please check your connection and try again.");
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.innerHTML = origBtnText;
        }
    }
}

// Global exports for inline onclick
window.togglePasswordVisibility = togglePasswordVisibility;
window.fillDemoCustomer = fillDemoCustomer;

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