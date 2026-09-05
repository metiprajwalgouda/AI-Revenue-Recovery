/*
Shared nav behavior for the merchant dashboard layout (base_merchant.html) --
currently just the logout link.
*/

document.addEventListener("DOMContentLoaded", () => {
    const logoutLink = document.getElementById("merchant-logout-link");
    if (logoutLink) {
        logoutLink.addEventListener("click", async (e) => {
            e.preventDefault();
            await fetch("/api/merchant/logout", { method: "POST" });
            window.location.href = "/merchant/login";
        });
    }
});