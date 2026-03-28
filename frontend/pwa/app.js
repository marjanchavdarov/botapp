class KatalogApp {
    constructor() {
        this.country = this.detectCountry();
        this.lang = this.getLanguage();
        this.translations = window.TRANSLATIONS[this.country];
        this.currentPage = 'home';
        this.init();
    }
    
    detectCountry() {
        const host = window.location.host;
        if (host.startsWith('hr.')) return 'croatia';
        if (host.startsWith('si.')) return 'slovenia';
        const urlParams = new URLSearchParams(window.location.search);
        if (urlParams.get('country')) return urlParams.get('country');
        return 'croatia';
    }
    
    getLanguage() {
        const languages = {
            'croatia': 'hr',
            'slovenia': 'sl'
        };
        return languages[this.country] || 'hr';
    }
    
    async init() {
        this.loadTranslations();
        this.setupEventListeners();
        await this.loadProducts();
        this.initChat();
    }
    
    loadTranslations() {
        document.body.innerHTML = document.body.innerHTML.replace(
            /{{(.*?)}}/g,
            (match, key) => this.translations[key.trim()] || match
        );
        document.title = this.translations.app_name;
        this.updateManifest();
    }
    
    updateManifest() {
        const manifest = document.getElementById('manifest');
        manifest.href = `/manifest-${this.country}.json`;
    }

    // ── Navigation ────────────────────────────────────────────────────────
    setupEventListeners() {
        // Bottom nav
        document.querySelectorAll('.nav-item').forEach(btn => {
            btn.addEventListener('click', () => {
                const page = btn.dataset.page;
                document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.navigateTo(page);
            });
        });

        // Chat bubble
        const chatBubble = document.getElementById('chatBubble');
        if (chatBubble) {
            chatBubble.addEventListener('click', () => {
                document.getElementById('chatPanel').classList.add('open');
            });
        }

        // Close chat
        const closeChat = document.getElementById('closeChat');
        if (closeChat) {
            closeChat.addEventListener('click', () => {
                document.getElementById('chatPanel').classList.remove('open');
            });
        }

        // Send message
        const sendBtn = document.getElementById('sendMessage');
        if (sendBtn) sendBtn.addEventListener('click', () => this.sendMessage());

        const chatInput = document.getElementById('chatInput');
        if (chatInput) {
            chatInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') this.sendMessage();
            });
        }

        // Search input
        const searchInput = document.getElementById('searchInput');
        if (searchInput) {
            searchInput.addEventListener('input', (e) => {
                this.filterProducts(e.target.value);
            });
        }

        // Page viewer close
        const closeViewer = document.getElementById('closeViewer');
        if (closeViewer) {
            closeViewer.addEventListener('click', () => {
                document.getElementById('pageViewer').classList.remove('open');
            });
        }

        // Page viewer prev/next
        document.getElementById('prevPage')?.addEventListener('click', () => this.changePage(-1));
        document.getElementById('nextPage')?.addEventListener('click', () => this.changePage(1));
    }

    navigateTo(page) {
        this.currentPage = page;
        const container = document.getElementById('mainContent');

        // Hide search bar on non-home pages
        const searchBar = document.querySelector('.search-bar');
        if (searchBar) {
            searchBar.style.display = (page === 'home') ? '' : 'none';
        }

        switch (page) {
            case 'home':
                if (searchBar) searchBar.style.display = '';
                this.loadProducts();
                break;

            case 'favorites':
                this.renderFavorites();
                break;

            case 'moj-popis':
                if (window.mojPopis) {
                    container.innerHTML = window.mojPopis.render();
                    this.updatePopisBadge();
                } else {
                    container.innerHTML = `<p style="padding:20px;color:#888">Učitavanje...</p>`;
                }
                break;

            case 'catalogues':
                this.renderCatalogues();
                break;

            case 'chat':
                document.getElementById('chatPanel').classList.add('open');
                // Keep current page visible behind chat
                document.querySelectorAll('.nav-item').forEach(b => {
                    if (b.dataset.page === this.currentPage) b.classList.add('active');
                });
                break;

            case 'more':
                this.renderMore();
                break;
        }
    }

    updatePopisBadge() {
        const badge = document.getElementById('popisBadge');
        if (!badge || !window.mojPopis) return;
        const count = window.mojPopis.items.length;
        badge.textContent = count > 0 ? count : '';
        badge.style.display = count > 0 ? '' : 'none';
    }

    // ── Products ──────────────────────────────────────────────────────────
    async loadProducts() {
        try {
            const response = await fetch(`/api/products?country=${this.country}`);
            const products = await response.json();
            this._allProducts = products;
            this.renderProducts(products);
        } catch (error) {
            console.error('Error loading products:', error);
            const container = document.getElementById('mainContent');
            if (container) container.innerHTML = `<p class="no-products">${this.translations.error_general || 'Greška pri učitavanju.'}</p>`;
        }
    }

    filterProducts(query) {
        if (!this._allProducts) return;
        if (!query.trim()) {
            this.renderProducts(this._allProducts);
            return;
        }
        const q = query.toLowerCase();
        const filtered = this._allProducts.filter(p =>
            (p.product || '').toLowerCase().includes(q) ||
            (p.brand || '').toLowerCase().includes(q) ||
            (p.store || '').toLowerCase().includes(q) ||
            (p.category || '').toLowerCase().includes(q)
        );
        this.renderProducts(filtered);
    }
    
    renderProducts(products) {
        const container = document.getElementById('mainContent');
        
        if (!products || products.length === 0) {
            container.innerHTML = `<p class="no-products">${this.translations.no_products}</p>`;
            return;
        }
        
        const byStore = this.groupByStore(products);
        let html = `<h2 class="section-title">${this.translations.today_deals}</h2>`;
        
        for (const [store, items] of Object.entries(byStore)) {
            html += `
                <div class="store-section">
                    <h3 class="store-name">${this.translations.stores?.[store] || store}</h3>
                    <div class="products-grid">
            `;
            items.forEach(product => {
                html += `
                    <div class="product-card" data-id="${product.id}">
                        ${product.discount_percent ? 
                            `<span class="discount-badge">-${product.discount_percent}</span>` : ''}
                        <h4 class="product-name">${product.product}</h4>
                        ${product.quantity ? 
                            `<span class="product-quantity">${product.quantity}</span>` : ''}
                        <div class="product-price">
                            ${product.original_price ? 
                                `<span class="old-price">${product.original_price} €</span>` : ''}
                            <span class="sale-price">${product.sale_price} €</span>
                        </div>
                        <div class="product-footer">
                            <span class="valid-until">
                                ${this.translations.valid_until} ${this.formatDate(product.valid_until)}
                            </span>
                            <button class="add-to-list ${product.inList ? 'added' : ''}" 
                                    onclick="app.toggleInList('${product.id}')">
                                ${product.inList ? this.translations.added : this.translations.add}
                            </button>
                        </div>
                        <button class="view-page" onclick="app.viewPage(${product.page_number}, '${product.store}')">
                            ${this.translations.view_page} ${product.page_number} 📖
                        </button>
                    </div>
                `;
            });
            html += `</div></div>`;
        }
        
        container.innerHTML = html;
    }

    // ── Favourites page ───────────────────────────────────────────────────
    renderFavorites() {
        const container = document.getElementById('mainContent');
        container.innerHTML = `
            <div style="padding:20px;text-align:center;color:#888;margin-top:40px">
                <div style="font-size:48px;margin-bottom:12px">❤️</div>
                <div style="font-size:16px">Favoriti dolaze uskoro</div>
            </div>`;
    }

    // ── Catalogues page ───────────────────────────────────────────────────
    renderCatalogues() {
        const container = document.getElementById('mainContent');
        container.innerHTML = `
            <div style="padding:20px;text-align:center;color:#888;margin-top:40px">
                <div style="font-size:48px;margin-bottom:12px">🛒</div>
                <div style="font-size:16px">Katalozi dolaze uskoro</div>
            </div>`;
    }

    // ── More page ─────────────────────────────────────────────────────────
    renderMore() {
        const container = document.getElementById('mainContent');
        container.innerHTML = `
            <div style="padding:20px">
                <h2 style="color:#eee;margin-bottom:20px">Više</h2>
                <div style="display:flex;flex-direction:column;gap:12px">
                    <div class="more-card" onclick="app.navigateTo('moj-popis')">
                        <span style="font-size:24px">🛒</span>
                        <div>
                            <div style="font-weight:bold;color:#eee">Moj Popis</div>
                            <div style="font-size:13px;color:#888">Lista za kupovinu + usporedba cijena</div>
                        </div>
                    </div>
                </div>
            </div>
            <style>
                .more-card {
                    display:flex;align-items:center;gap:16px;
                    background:#1a1a1a;border:1px solid #333;border-radius:12px;
                    padding:16px;cursor:pointer;
                }
                .more-card:hover { border-color:#00ff88; }
            </style>`;
    }

    // ── Chat ──────────────────────────────────────────────────────────────
    initChat() {
        this.chatMessages = [];
        this.setupChatWebSocket();
        
        setTimeout(() => {
            this.addChatMessage(this.translations.chat_greeting, 'bot');
            this.addChatMessage(this.translations.chat_suggestions, 'bot');
            this.translations.chat_suggestion_items?.forEach(item => {
                this.addChatMessage(item, 'bot');
            });
        }, 500);
    }
    
    setupChatWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws?country=${this.country}&lang=${this.lang}`;
        this.ws = new WebSocket(wsUrl);
        this.ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            this.addChatMessage(data.message, 'bot');
            if (data.products && data.products.length > 0) {
                this.showProductSuggestions(data.products);
            }
        };
    }
    
    addChatMessage(text, sender) {
        const container = document.getElementById('chatMessages');
        if (!container) return;
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${sender}`;
        
        const linkedText = text.replace(
            new RegExp(`${this.translations.page} (\\d+)`, 'gi'),
            (match, page) => `<button onclick="app.viewPage(${page})" class="page-link">${match} 📖</button>`
        );
        
        if (sender === 'bot') {
            messageDiv.innerHTML = `
                <div class="avatar">🤖</div>
                <div class="bubble">${linkedText}</div>`;
        } else {
            messageDiv.innerHTML = `<div class="bubble">${text}</div>`;
        }
        
        container.appendChild(messageDiv);
        container.scrollTop = container.scrollHeight;
    }
    
    async sendMessage() {
        const input = document.getElementById('chatInput');
        const text = input.value.trim();
        if (!text) return;
        this.addChatMessage(text, 'user');
        input.value = '';
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({
                message: text,
                country: this.country,
                language: this.lang
            }));
        }
    }

    // ── Page viewer ───────────────────────────────────────────────────────
    async viewPage(pageNumber, store = null) {
        this._currentPageNumber = pageNumber;
        this._currentStore = store;
        const viewer = document.getElementById('pageViewer');
        const image  = document.getElementById('pageImage');
        
        image.style.backgroundImage = 'none';
        image.innerHTML = '<div class="loading">📷</div>';
        viewer.classList.add('open');
        
        try {
            const url = `/api/page-image/${pageNumber}?country=${this.country}${store ? `&store=${store}` : ''}`;
            const response = await fetch(url);
            const data = await response.json();
            
            if (data.image_url) {
                image.style.backgroundImage = `url('${data.image_url}')`;
                image.innerHTML = '';
                document.getElementById('pageNumber').textContent = 
                    `${this.translations.page} ${pageNumber}`;
            } else {
                image.innerHTML = `<p class="error">${this.translations.no_image}</p>`;
            }
        } catch (error) {
            image.innerHTML = `<p class="error">${this.translations.error_general}</p>`;
        }
    }

    changePage(delta) {
        if (this._currentPageNumber) {
            this.viewPage(this._currentPageNumber + delta, this._currentStore);
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────
    groupByStore(products) {
        return products.reduce((acc, product) => {
            const store = product.store;
            if (!acc[store]) acc[store] = [];
            acc[store].push(product);
            return acc;
        }, {});
    }
    
    formatDate(dateStr) {
        if (!dateStr) return '';
        const date = new Date(dateStr);
        return date.toLocaleDateString(this.lang, {
            day: '2-digit', month: '2-digit', year: 'numeric'
        });
    }
    
    toggleInList(productId) {
        fetch('/api/favorites/toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ product_id: productId, country: this.country })
        });
        const button = document.querySelector(`[data-id="${productId}"] .add-to-list`);
        if (button) {
            const isAdded = button.classList.contains('added');
            button.classList.toggle('added');
            button.textContent = isAdded ? this.translations.add : this.translations.added;
        }
    }
}

// Initialize
const app = new KatalogApp();