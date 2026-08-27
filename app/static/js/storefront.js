/*
Loads and renders the product grid on the storefront home page.
*/

async function loadProducts() {
    const grid = document.getElementById("product-grid");
    try {
        const response = await fetch("/api/products");
        if (!response.ok) throw new Error(`Server returned ${response.status}`);
        const products = await response.json();

        if (products.length === 0) {
            grid.innerHTML = "<p>No products available yet. Check back soon.</p>";
            return;
        }

        grid.innerHTML = products.map(renderProductCard).join("");

        // Wire up "Add to cart" buttons after rendering
        grid.querySelectorAll("[data-add-to-cart]").forEach(button => {
            button.addEventListener("click", () => {
                const product = JSON.parse(button.dataset.product);
                addToCart(product);
                button.textContent = "Added ✓";
                setTimeout(() => { button.textContent = "Add to cart"; }, 1200);
            });
        });

    } catch (err) {
        console.error("Failed to load products:", err);
        grid.innerHTML = `<div class="error-banner">Couldn't load products. Please refresh the page.</div>`;
    }
}

function renderProductCard(product) {
    const imgSrc = product.image_url || "https://placehold.co/300x200?text=" + encodeURIComponent(product.name);
    const lowStock = product.stock > 0 && product.stock <= 3;
    const outOfStock = product.stock === 0;

    return `
        <div class="product-card">
            <img src="${escapeHtml(imgSrc)}" alt="${escapeHtml(product.name)}">
            <h3>${escapeHtml(product.name)}</h3>
            <p class="desc">${escapeHtml(product.description || "")}</p>
            ${lowStock ? `<p class="stock-low">Only ${product.stock} left</p>` : ""}
            <p class="price">₹${product.price.toFixed(2)}</p>
            <button
                data-add-to-cart
                data-product='${JSON.stringify({ id: product.id, name: product.name, price: product.price })}'
                ${outOfStock ? "disabled" : ""}
            >${outOfStock ? "Out of stock" : "Add to cart"}</button>
        </div>
    `;
}

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

document.addEventListener("DOMContentLoaded", loadProducts);