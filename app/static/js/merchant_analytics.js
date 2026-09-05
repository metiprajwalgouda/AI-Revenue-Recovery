/*
Loads and renders recovery analytics and real-time activity on the merchant dashboard.
Auto-refreshes every 5 seconds for live status synchronization.
*/

async function loadAnalytics() {
    const cardsWrap = document.getElementById("analytics-cards");
    const outcomesBody = document.getElementById("recent-outcomes-body");

    if (!cardsWrap || !outcomesBody) return;

    try {
        const response = await fetch("/api/merchant/analytics");
        if (response.status === 401) {
            window.location.href = "/merchant/login";
            return;
        }
        if (!response.ok) throw new Error(`Server returned ${response.status}`);

        const data = await response.json();
        cardsWrap.innerHTML = renderCards(data);

        if (!data.recent_outcomes || data.recent_outcomes.length === 0) {
            outcomesBody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: #9ca3af; padding: 24px;">No recovery attempts recorded yet.</td></tr>`;
        } else {
            outcomesBody.innerHTML = data.recent_outcomes.map(renderOutcomeRow).join("");
        }

    } catch (err) {
        console.error("Failed to load analytics:", err);
        if (cardsWrap.children.length === 0 || cardsWrap.innerHTML.includes("Loading")) {
            cardsWrap.innerHTML = `<div class="error-banner">Couldn't load recovery analytics.</div>`;
        }
    }
}

function renderCards(data) {
    const confirmedDiscounts = data.total_confirmed_discounts != null
        ? data.total_confirmed_discounts
        : (data.total_discount_cost != null ? data.total_discount_cost : (data.total_amount_offered || 0));
    const cards = [
        { label: "Active Carts", value: data.started_count || 0 },
        { label: "Completed Orders", value: data.completed_count || 0 },
        { label: "Abandoned Carts", value: data.abandoned_count || 0 },
        { label: "At Risk / Abandoned", value: `₹${(data.total_abandoned_value || 0).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}` },
        { label: "Confirmed Discounts", value: `₹${(confirmedDiscounts).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}` },
        { label: "Confirmed Recovered", value: `₹${(data.total_confirmed_recovered || 0).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`, highlight: true },
        { label: "Total Lost", value: `₹${(data.total_lost_value || 0).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}` },
        { label: "Recovery Rate", value: `${data.recovery_rate_pct || 0}%`, highlight: true },
    ];
    return cards.map(c => `
        <div class="analytics-card ${c.highlight ? "highlight" : ""}">
            <div class="analytics-value">${c.value}</div>
            <div class="analytics-label">${c.label}</div>
        </div>
    `).join("");
}

function renderOutcomeRow(o) {
    const confirmedDisplay = o.confirmed_recovered_amount != null
        ? `<strong style="color: #15803d;">₹${o.confirmed_recovered_amount.toFixed(2)} ✓</strong>`
        : `<span style="color: #9ca3af;">—</span>`;
    const offeredDisplay = o.amount_offered != null ? `₹${o.amount_offered.toFixed(2)}` : `<span style="color: #9ca3af;">—</span>`;
    const confPct = o.confidence ? Math.round(o.confidence * 100) : 0;
    const methodBadge = o.method === 'rule' 
        ? `<span style="background: #eff6ff; color: #1d4ed8; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 600;">Rule</span>`
        : `<span style="background: #faf5ff; color: #7c3aed; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 600;">AI / LLM</span>`;

    return `
        <tr>
            <td><code style="background: #f3f4f6; padding: 2px 6px; border-radius: 4px; font-weight: 700;">${o.event_id}</code></td>
            <td><strong>${(o.predicted_reason || 'unknown').replace(/_/g, ' ')}</strong> <span style="font-size: 12px; color: #6b7280;">(${confPct}%)</span></td>
            <td>${methodBadge}</td>
            <td><span style="font-weight: 500;">${(o.action_taken || 'none').replace(/_/g, ' ')}</span></td>
            <td>${offeredDisplay}</td>
            <td>${confirmedDisplay}</td>
        </tr>
    `;
}

async function loadLiveSessions() {
    try {
        const res = await fetch("/api/merchant/live-sessions");
        if (!res.ok) return;
        const sessions = await res.json();
        
        const countEl = document.getElementById("live-count");
        if (countEl) countEl.textContent = sessions.length;
        const list = document.getElementById("live-sessions-list");
        if (!list) return;
        
        if (sessions.length === 0) {
            list.innerHTML = "<li style='color: #9ca3af; padding: 12px 0;'>No active shoppers currently checking out.</li>";
            return;
        }
        
        list.innerHTML = sessions.map(s => {
            const timeAgo = Math.round((new Date() - new Date(s.started_at)) / 1000);
            const timeStr = timeAgo < 60 ? `${timeAgo}s ago` : `${Math.round(timeAgo/60)}m ago`;
            return `<li style="padding: 10px 0; border-bottom: 1px solid #f3f4f6; display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <strong style="color: #111827;">${s.customer_name || 'Shopper'}</strong> 
                    <span style="font-size: 12px; color: #6b7280;">(${s.customer_email || 'Guest'})</span>
                    <div style="font-size: 12px; color: #374151; margin-top: 2px;">Cart Value: <strong>₹${(s.cart_value || 0).toFixed(2)}</strong></div>
                </div>
                <span style="color: #6b7280; font-size: 11px; background: #f3f4f6; padding: 3px 8px; border-radius: 4px;">${timeStr}</span>
            </li>`;
        }).join("");
    } catch (e) {
        console.error("Failed to load live sessions", e);
    }
}

async function loadAutomatedActions() {
    try {
        const res = await fetch("/api/merchant/automated-actions");
        if (!res.ok) return;
        const actions = await res.json();
        
        const list = document.getElementById("automated-actions-list");
        if (!list) return;
        
        if (actions.length === 0) {
            list.innerHTML = "<li style='color: #9ca3af; padding: 12px 0;'>No automated recovery actions triggered recently.</li>";
            return;
        }
        
        list.innerHTML = actions.map(a => {
            const timeAgo = Math.round((new Date() - new Date(a.timestamp)) / 1000);
            const timeStr = timeAgo < 60 ? `${timeAgo}s ago` : `${Math.round(timeAgo/60)}m ago`;
            const amountStr = a.amount_offered != null ? `• Discount ₹${a.amount_offered.toFixed(2)}` : "";
            return `<li style="padding: 10px 0; border-bottom: 1px solid #f3f4f6; display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <strong style="color: #111827; font-size: 13px;">${(a.action_taken || '').replace(/_/g, ' ').toUpperCase()}</strong> 
                    <span style="font-size: 12px; color: #6b7280;">to ${a.customer_name || 'Customer'}</span>
                    <div style="font-size: 12px; color: #374151; margin-top: 2px;">
                        Cart: ₹${(a.cart_value || 0).toFixed(2)} <span style="color: #15803d; font-weight: 600;">${amountStr}</span>
                    </div>
                </div>
                <span style="color: #6b7280; font-size: 11px; background: #f3f4f6; padding: 3px 8px; border-radius: 4px;">${timeStr}</span>
            </li>`;
        }).join("");
    } catch (e) {
        console.error("Failed to load automated actions", e);
    }
}

document.addEventListener("DOMContentLoaded", () => {
    loadAnalytics();
    loadLiveSessions();
    loadAutomatedActions();

    // Periodic auto-update every 5 seconds
    setInterval(loadAnalytics, 5000);
    setInterval(loadLiveSessions, 5000);
    setInterval(loadAutomatedActions, 5000);
});