/*
Handles merchant login/signup forms.
*/

function showMerchantError(message) {
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

function fillDemoMerchant(email, password) {
    const emailInput = document.querySelector('input[name="email"]');
    const passInput = document.querySelector('input[name="password"]');
    if (emailInput) emailInput.value = email || "demo@example.com";
    if (passInput) passInput.value = password || "password123";
}

async function handleMerchantAuthSubmit(event, endpoint) {
    event.preventDefault();
    const form = event.target;
    const submitBtn = form.querySelector('button[type="submit"]');
    const origBtnText = submitBtn ? submitBtn.innerHTML : "Submit";

    const formData = new FormData(form);
    const payload = Object.fromEntries(formData.entries());

    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = `<span>⏳ Authenticating...</span>`;
    }

    try {
        const response = await fetch(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });

        if (!response.ok) {
            const errorBody = await response.json().catch(() => ({}));
            showMerchantError(errorBody.detail || "Something went wrong. Please check credentials and try again.");
            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.innerHTML = origBtnText;
            }
            return;
        }

        window.location.href = "/merchant";

    } catch (err) {
        console.error("Merchant auth request failed:", err);
        showMerchantError("Couldn't reach the server. Please check your connection and try again.");
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.innerHTML = origBtnText;
        }
    }
}

// Global exports for inline onclick
window.togglePasswordVisibility = togglePasswordVisibility;
window.fillDemoMerchant = fillDemoMerchant;

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