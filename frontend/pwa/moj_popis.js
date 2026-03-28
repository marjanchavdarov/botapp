/**
 * katalog.ai — Moj Popis (My Shopping List)
 * 
 * Features:
 * - Add items by typing or quantity
 * - Saved to localStorage (ready to migrate to Supabase)
 * - Basket optimizer: shows cheapest 1-store, 2-store, 3-store, and absolute cheapest split
 */

class MojPopis {
    constructor(app) {
        this.app = app;
        this.items = this.load();
        this.searching = false;
    }

    // ── Persistence ───────────────────────────────────────────────────────
    load() {
        try {
            return JSON.parse(localStorage.getItem('katalog_popis') || '[]');
        } catch { return []; }
    }

    save() {
        localStorage.setItem('katalog_popis', JSON.stringify(this.items));
    }

    addItem(name, qty = 1) {
        name = name.trim();
        if (!name) return false;
        // If already exists, increase qty
        const existing = this.items.find(i => i.name.toLowerCase() === name.toLowerCase());
        if (existing) {
            existing.qty += qty;
        } else {
            this.items.push({ id: Date.now(), name, qty });
        }
        this.save();
        return true;
    }

    removeItem(id) {
        this.items = this.items.filter(i => i.id !== id);
        this.save();
    }

    updateQty(id, delta) {
        const item = this.items.find(i => i.id === id);
        if (!item) return;
        item.qty = Math.max(1, item.qty + delta);
        this.save();
    }

    clear() {
        this.items = [];
        this.save();
    }

    // ── Render the page ───────────────────────────────────────────────────
    render() {
        return `
        <div class="popis-wrap">

            <!-- Add item -->
            <div class="popis-add-row">
                <input type="text" id="popisInput" class="popis-input"
                    placeholder="Dodaj proizvod (npr. mlijeko, kruh...)"
                    onkeydown="if(event.key==='Enter') window.mojPopis.submitAdd()">
                <div class="popis-qty-row">
                    <button class="popis-qty-btn" onclick="window.mojPopis.changeAddQty(-1)">−</button>
                    <span id="addQtyDisplay" class="popis-qty-val">1</span>
                    <button class="popis-qty-btn" onclick="window.mojPopis.changeAddQty(1)">+</button>
                    <button class="popis-add-btn" onclick="window.mojPopis.submitAdd()">➕ Dodaj</button>
                </div>
            </div>

            <!-- List -->
            <div class="popis-list" id="popisList">
                ${this.renderList()}
            </div>

            <!-- Actions -->
            ${this.items.length > 0 ? `
            <div class="popis-actions">
                <button class="popis-search-btn" onclick="window.mojPopis.searchBasket()" 
                    ${this.searching ? 'disabled' : ''}>
                    ${this.searching 
                        ? '<span class="popis-spinner"></span> Tražim...' 
                        : '🔍 Nađi najjeftiniju kombinaciju'}
                </button>
                <button class="popis-clear-btn" onclick="window.mojPopis.confirmClear()">🗑 Očisti</button>
            </div>
            ` : ''}

            <!-- Results -->
            <div id="popisResults"></div>
        </div>`;
    }

    renderList() {
        if (!this.items.length) {
            return `<div class="popis-empty">
                <div style="font-size:48px;margin-bottom:12px">🛒</div>
                <div style="color:#888;font-size:15px">Lista je prazna</div>
                <div style="color:#555;font-size:13px;margin-top:6px">Dodaj proizvode koje trebaš kupiti</div>
            </div>`;
        }
        return this.items.map(item => `
            <div class="popis-item" id="popis-item-${item.id}">
                <span class="popis-item-name">${this.escHtml(item.name)}</span>
                <div class="popis-item-controls">
                    <button class="popis-qty-btn sm" onclick="window.mojPopis.changeItemQty(${item.id}, -1)">−</button>
                    <span class="popis-qty-val">${item.qty}x</span>
                    <button class="popis-qty-btn sm" onclick="window.mojPopis.changeItemQty(${item.id}, 1)">+</button>
                    <button class="popis-del-btn" onclick="window.mojPopis.deleteItem(${item.id})">✕</button>
                </div>
            </div>
        `).join('');
    }

    // ── Add item UI ───────────────────────────────────────────────────────
    _addQty = 1;

    changeAddQty(delta) {
        this._addQty = Math.max(1, this._addQty + delta);
        const el = document.getElementById('addQtyDisplay');
        if (el) el.textContent = this._addQty;
    }

    submitAdd() {
        const input = document.getElementById('popisInput');
        if (!input) return;
        const name = input.value.trim();
        if (!name) return;
        if (this.addItem(name, this._addQty)) {
            input.value = '';
            this._addQty = 1;
            const qd = document.getElementById('addQtyDisplay');
            if (qd) qd.textContent = '1';
            this.refreshList();
            this.refreshActions();
        }
    }

    changeItemQty(id, delta) {
        this.updateQty(id, delta);
        this.refreshList();
    }

    deleteItem(id) {
        this.removeItem(id);
        this.refreshList();
        this.refreshActions();
    }

    confirmClear() {
        if (confirm('Obriši cijelu listu?')) {
            this.clear();
            this.refreshList();
            this.refreshActions();
            document.getElementById('popisResults').innerHTML = '';
        }
    }

    refreshList() {
        const el = document.getElementById('popisList');
        if (el) el.innerHTML = this.renderList();
    }

    refreshActions() {
        // Re-render the whole page section to show/hide action buttons
        const container = document.getElementById('mainContent');
        if (container && container.querySelector('.popis-wrap')) {
            container.innerHTML = this.render();
            // Re-focus input
            const input = document.getElementById('popisInput');
            if (input) input.focus();
        }
    }

    // ── Basket Search ─────────────────────────────────────────────────────
    async searchBasket() {
        if (this.searching || !this.items.length) return;
        this.searching = true;

        const resultsEl = document.getElementById('popisResults');
        resultsEl.innerHTML = `
            <div class="basket-loading">
                <span class="popis-spinner large"></span>
                <div>Tražim cijene za ${this.items.length} proizvoda...</div>
            </div>`;

        // Refresh button state
        const btn = document.querySelector('.popis-search-btn');
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<span class="popis-spinner"></span> Tražim...';
        }

        try {
            const country = this.app ? this.app.country : 'croatia';
            const response = await fetch('/api/basket', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    items: this.items.map(i => ({ name: i.name, qty: i.qty })),
                    country
                })
            });

            if (!response.ok) throw new Error(`Server error: ${response.status}`);
            const data = await response.json();
            this.renderBasketResults(data);

        } catch (err) {
            resultsEl.innerHTML = `
                <div class="basket-error">
                    ❌ Greška: ${err.message}<br>
                    <small>Provjeri internetsku vezu i pokušaj ponovo.</small>
                </div>`;
        }

        this.searching = false;
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '🔍 Nađi najjeftiniju kombinaciju';
        }
    }

    // ── Render basket results ─────────────────────────────────────────────
    renderBasketResults(data) {
        const el = document.getElementById('popisResults');
        if (!data || !data.combinations || !data.combinations.length) {
            el.innerHTML = `
                <div class="basket-error">
                    😕 Nismo pronašli dovoljno cijena za tvoju listu.<br>
                    <small>Pokušaj s općenitijim nazivima proizvoda.</small>
                </div>`;
            return;
        }

        const { combinations, item_results, not_found } = data;

        let html = `<div class="basket-results">`;

        // Not found warning
        if (not_found && not_found.length) {
            html += `
                <div class="basket-not-found">
                    ⚠️ Nismo pronašli cijene za: 
                    <strong>${not_found.map(n => this.escHtml(n)).join(', ')}</strong>
                </div>`;
        }

        html += `<h3 class="basket-results-title">🏆 Rezultati po broju trgovina</h3>`;

        // Render each combination (1-store, 2-store, etc.)
        combinations.forEach((combo, idx) => {
            const isFirst = idx === 0;
            const isCheapest = combo.is_absolute_cheapest;
            const storeCount = combo.stores.length;

            let headerLabel = '';
            if (isCheapest && storeCount > 1) {
                headerLabel = `🥇 Apsolutno najjeftinije (${storeCount} trgovine)`;
            } else if (storeCount === 1) {
                headerLabel = `🏪 1 trgovina`;
            } else {
                headerLabel = `🏪🏪 ${storeCount} trgovine`;
            }

            html += `
            <div class="basket-combo ${isFirst ? 'best' : ''} ${isCheapest ? 'absolute-best' : ''}">
                <div class="basket-combo-header">
                    <span class="basket-combo-label">${headerLabel}</span>
                    <span class="basket-combo-total">${combo.total.toFixed(2)} €</span>
                </div>`;

            // Savings vs most expensive single store
            if (data.most_expensive_single && combo.total < data.most_expensive_single) {
                const saving = (data.most_expensive_single - combo.total).toFixed(2);
                html += `<div class="basket-saving">💰 Uštedis ${saving} € vs najskupceg</div>`;
            }

            // Per-store breakdown
            combo.stores.forEach(store => {
                html += `
                <div class="basket-store-row">
                    <span class="basket-store-name">${this.escHtml(store.store)}</span>
                    <span class="basket-store-subtotal">${store.subtotal.toFixed(2)} €</span>
                </div>`;

                // Products from this store
                html += `<div class="basket-store-items">`;
                store.items.forEach(item => {
                    html += `
                    <div class="basket-product-row">
                        <span class="basket-product-name">${this.escHtml(item.name)} ${item.qty > 1 ? `×${item.qty}` : ''}</span>
                        <span class="basket-product-price">
                            ${item.matched 
                                ? `${(parseFloat(item.price) * item.qty).toFixed(2)} €`
                                : '<span style="color:#ff5555">nije nađeno</span>'}
                        </span>
                    </div>`;
                });
                html += `</div>`;
            });

            html += `</div>`;
        });

        html += `</div>`;
        el.innerHTML = html;

        // Scroll to results
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    escHtml(str) {
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }
}

// ── CSS ───────────────────────────────────────────────────────────────────────
(function injectPopisStyles() {
    const style = document.createElement('style');
    style.textContent = `
    .popis-wrap { padding: 16px; max-width: 600px; margin: 0 auto; }

    /* Add row */
    .popis-add-row {
        background: #1a1a1a;
        border-radius: 12px;
        padding: 14px;
        margin-bottom: 16px;
        border: 1px solid #333;
    }
    .popis-input {
        width: 100%;
        padding: 12px 14px;
        background: #222;
        border: 1px solid #444;
        color: #eee;
        border-radius: 8px;
        font-size: 15px;
        margin-bottom: 10px;
        font-family: inherit;
    }
    .popis-input:focus { outline: none; border-color: #00ff88; }
    .popis-qty-row {
        display: flex;
        align-items: center;
        gap: 8px;
    }
    .popis-qty-btn {
        width: 34px; height: 34px;
        background: #333; border: 1px solid #555;
        color: #eee; border-radius: 6px;
        font-size: 18px; cursor: pointer;
        display: flex; align-items: center; justify-content: center;
        flex-shrink: 0;
    }
    .popis-qty-btn:active { background: #444; }
    .popis-qty-btn.sm { width: 28px; height: 28px; font-size: 15px; }
    .popis-qty-val {
        min-width: 28px; text-align: center;
        color: #fff; font-weight: bold; font-size: 15px;
    }
    .popis-add-btn {
        margin-left: auto;
        padding: 8px 16px;
        background: #00ff88; color: #000;
        border: none; border-radius: 8px;
        font-weight: bold; font-size: 14px;
        cursor: pointer;
    }
    .popis-add-btn:active { opacity: 0.8; }

    /* List */
    .popis-list { margin-bottom: 16px; }
    .popis-empty {
        text-align: center;
        padding: 40px 20px;
        color: #666;
    }
    .popis-item {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 12px 14px;
        background: #1a1a1a;
        border: 1px solid #2a2a2a;
        border-radius: 10px;
        margin-bottom: 8px;
        gap: 12px;
    }
    .popis-item-name {
        flex: 1;
        color: #eee;
        font-size: 15px;
        text-transform: capitalize;
    }
    .popis-item-controls {
        display: flex;
        align-items: center;
        gap: 6px;
        flex-shrink: 0;
    }
    .popis-del-btn {
        background: none; border: none;
        color: #ff5555; font-size: 16px;
        cursor: pointer; padding: 4px 6px;
        margin-left: 4px;
    }

    /* Actions */
    .popis-actions {
        display: flex;
        gap: 10px;
        margin-bottom: 20px;
    }
    .popis-search-btn {
        flex: 1;
        padding: 14px;
        background: #00ff88; color: #000;
        border: none; border-radius: 10px;
        font-weight: bold; font-size: 15px;
        cursor: pointer;
        display: flex; align-items: center; justify-content: center; gap: 8px;
    }
    .popis-search-btn:disabled { background: #1a3a2a; color: #555; cursor: not-allowed; }
    .popis-clear-btn {
        padding: 14px 18px;
        background: #2a1a1a; color: #ff5555;
        border: 1px solid #3a2a2a; border-radius: 10px;
        font-size: 14px; cursor: pointer;
    }

    /* Spinner */
    .popis-spinner {
        display: inline-block;
        width: 16px; height: 16px;
        border: 2px solid #1a3a2a;
        border-top-color: #00ff88;
        border-radius: 50%;
        animation: popis-spin 0.7s linear infinite;
        vertical-align: middle;
    }
    .popis-spinner.large { width: 32px; height: 32px; border-width: 3px; }
    @keyframes popis-spin { to { transform: rotate(360deg); } }

    /* Loading */
    .basket-loading {
        text-align: center;
        padding: 40px;
        color: #888;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 16px;
        font-size: 14px;
    }

    /* Error */
    .basket-error {
        background: #2a1a1a;
        border: 1px solid #3a2020;
        border-radius: 10px;
        padding: 16px;
        color: #ff8888;
        font-size: 14px;
        line-height: 1.6;
        margin-bottom: 16px;
    }

    /* Not found */
    .basket-not-found {
        background: #2a2000;
        border: 1px solid #443300;
        border-radius: 8px;
        padding: 12px 14px;
        color: #ffcc66;
        font-size: 13px;
        margin-bottom: 16px;
    }

    /* Results */
    .basket-results { margin-top: 8px; }
    .basket-results-title {
        color: #888;
        font-size: 13px;
        text-transform: uppercase;
        letter-spacing: 1px;
        margin-bottom: 12px;
        font-family: monospace;
    }

    /* Combo card */
    .basket-combo {
        background: #1a1a1a;
        border: 1px solid #2a2a2a;
        border-radius: 12px;
        padding: 16px;
        margin-bottom: 12px;
        transition: border-color 0.2s;
    }
    .basket-combo.best {
        border-color: #00ff88;
        background: #0a1f14;
    }
    .basket-combo.absolute-best {
        border-color: #ffcc00;
        background: #1a1500;
    }
    .basket-combo-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 8px;
    }
    .basket-combo-label {
        font-weight: bold;
        font-size: 14px;
        color: #eee;
    }
    .basket-combo-total {
        font-size: 22px;
        font-weight: bold;
        color: #00ff88;
        font-family: monospace;
    }
    .basket-combo.absolute-best .basket-combo-total { color: #ffcc00; }

    .basket-saving {
        color: #00cc66;
        font-size: 12px;
        margin-bottom: 10px;
    }

    /* Store row */
    .basket-store-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 8px 0 4px;
        border-top: 1px solid #2a2a2a;
        margin-top: 6px;
    }
    .basket-store-name {
        font-weight: bold;
        color: #fff;
        font-size: 14px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .basket-store-subtotal {
        color: #aaa;
        font-size: 14px;
        font-family: monospace;
    }

    /* Product rows inside store */
    .basket-store-items {
        padding: 4px 0 8px 12px;
        border-left: 2px solid #2a2a2a;
        margin-left: 4px;
    }
    .basket-product-row {
        display: flex;
        justify-content: space-between;
        padding: 3px 0;
        font-size: 13px;
        color: #888;
    }
    .basket-product-name { flex: 1; }
    .basket-product-price {
        color: #aaa;
        font-family: monospace;
        margin-left: 12px;
    }
    `;
    document.head.appendChild(style);
})();

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    const tryInit = () => {
        if (window.app) {
            window.mojPopis = new MojPopis(window.app);
        } else {
            setTimeout(tryInit, 200);
        }
    };
    tryInit();
});