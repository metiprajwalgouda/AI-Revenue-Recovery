/*
Cart is stored in the browser's localStorage -- this is a real website running
in the customer's own browser (not a Claude artifact), so localStorage is the
right, standard tool here: it survives page navigation within the storefront
without needing a server round-trip for every cart change.

Cart shape: [{ product_id, name, price, quantity }, ...]
*/

const CART_KEY = "shopdemo_cart";

function getCart() {
    try {
        const raw = localStorage.getItem(CART_KEY);
        return raw ? JSON.parse(raw) : [];
    } catch (e) {
        console.error("Cart read failed, resetting cart:", e);
        return [];
    }
}

function saveCart(cart) {
    localStorage.setItem(CART_KEY, JSON.stringify(cart));
    updateCartBadge();
}

function addToCart(product) {
    const cart = getCart();
    const existing = cart.find(item => item.product_id === product.id);
    if (existing) {
        existing.quantity += 1;
    } else {
        cart.push({ product_id: product.id, name: product.name, price: product.price, quantity: 1 });
    }
    saveCart(cart);
}

function removeFromCart(productId) {
    const cart = getCart().filter(item => item.product_id !== productId);
    saveCart(cart);
}

function updateQuantity(productId, quantity) {
    const cart = getCart();
    const item = cart.find(i => i.product_id === productId);
    if (item) {
        item.quantity = Math.max(1, quantity);
        saveCart(cart);
    }
}

function clearCart() {
    localStorage.removeItem(CART_KEY);
    updateCartBadge();
}

function cartTotal() {
    return getCart().reduce((sum, item) => sum + item.price * item.quantity, 0);
}

function updateCartBadge() {
    const badge = document.getElementById("cart-count");
    if (badge) {
        const count = getCart().reduce((sum, item) => sum + item.quantity, 0);
        badge.textContent = count;
    }
}

document.addEventListener("DOMContentLoaded", updateCartBadge);