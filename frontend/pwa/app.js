class KatalogApp {
    constructor() {
        this.country = this.detectCountry();
        this.lang = this.getLanguage();
        this.translations = window.TRANSLATIONS[this.country];
        this.init();
    }
    
    detectCountry() {
        // Check subdomain
        const host = window.location.host;
        if (host.startsWith('hr.')) return 'croatia';
        if (host.startsWith('si.')) return 'slovenia';
        
        // Check URL parameter
        const urlParams = new URLSearchParams(window.location.search);
        if (urlParams.get('country')) return urlParams.get('country');
        
        // Default
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
        // Replace placeholders in HTML
        document.body.innerHTML = document.body.innerHTML.replace(
            /{{(.*?)}}/g,
            (match, key) => this.translations[key.trim()] || match
        );
        
        // Set page title
        document.title = this.translations.app_name;
        
        // Update manifest for this country
        this.updateManifest();
    }
    
    updateManifest() {
        const manifest = document.getElementById('manifest');
        manifest.href = `/manifest-${this.country}.json`;
    }
    
    async loadProducts() {
        try {
            const response = await fetch(`/api/products?country=${this.country}`);
            const products = await response.json();
            this.renderProducts(products);
        } catch (error) {
            console.error('Error loading products:', error);
            this.showError(this.translations.error_general);
        }
    }
    
    renderProducts(products) {
        const container = document.getElementById('mainContent');
        
        if (!products || products.length === 0) {
            container.innerHTML = `<p class="no-products">${this.translations.no_products}</p>`;
            return;
        }
        
        // Group by store
        const byStore = this.groupByStore(products);
        
        let html = `<h2 class="section-title">${this.translations.today_deals}</h2>`;
        
        for (const [store, items] of Object.entries(byStore)) {
            html += `
                <div class="store-section">
                    <h3 class="store-name">${this.translations.stores[store] || store}</h3>
                    <div class="products-grid">
            `;
            
            items.forEach(product => {
                html += `
                    <div class="product-card" data-id="${product.id}">
                        ${product.discount_percent ? 
                            `<span class="discount-badge">-${product.discount_percent}</span>` : 
                            ''}
                        <h4 class="product-name">${product.product}</h4>
                        ${product.quantity ? 
                            `<span class="product-quantity">${product.quantity}</span>` : 
                            ''}
                        <div class="product-price">
                            ${product.original_price ? 
                                `<span class="old-price">${product.original_price} €</span>` : 
                                ''}
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
    
    initChat() {
        this.chatMessages = [];
        this.setupChatWebSocket();
        
        // Show greeting
        setTimeout(() => {
            this.addChatMessage(this.translations.chat_greeting, 'bot');
            this.addChatMessage(this.translations.chat_suggestions, 'bot');
            this.translations.chat_suggestion_items.forEach(item => {
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
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${sender}`;
        
        // Convert page numbers to clickable
        const linkedText = text.replace(
            new RegExp(`${this.translations.page} (\\d+)`, 'gi'),
            (match, page) => {
                return `<button onclick="app.viewPage(${page})" class="page-link">${match} 📖</button>`;
            }
        );
        
        if (sender === 'bot') {
            messageDiv.innerHTML = `
                <div class="avatar">🤖</div>
                <div class="bubble">${linkedText}</div>
            `;
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
        
        this.ws.send(JSON.stringify({
            message: text,
            country: this.country,
            language: this.lang
        }));
    }
    
    async viewPage(pageNumber, store = null) {
        const viewer = document.getElementById('pageViewer');
        const image = document.getElementById('pageImage');
        
        // Show loading
        image.style.backgroundImage = 'none';
        image.innerHTML = '<div class="loading">📷</div>';
        viewer.classList.add('open');
        
        try {
            const url = `/api/page-image/${pageNumber}?country=${this.country}${store ? `&store=${store}` : ''}`;
            const response = await fetch(url);
            const data = await response.json();
            
            if (data.image_url) {
                image.style.backgroundImage = `url('${data.image_url}')`;
                document.getElementById('pageNumber').textContent = 
                    `${this.translations.page} ${pageNumber}`;
            } else {
                image.innerHTML = `<p class="error">${this.translations.no_image}</p>`;
            }
        } catch (error) {
            image.innerHTML = `<p class="error">${this.translations.error_general}</p>`;
        }
    }
    
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
            day: '2-digit',
            month: '2-digit',
            year: 'numeric'
        });
    }
    
    toggleInList(productId) {
        // Add to user's list
        fetch('/api/favorites/toggle', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                product_id: productId,
                country: this.country
            })
        });
        
        // Update UI
        const button = document.querySelector(`[data-id="${productId}"] .add-to-list`);
        if (button) {
            const isAdded = button.classList.contains('added');
            button.classList.toggle('added');
            button.textContent = isAdded ? this.translations.add : this.translations.added;
        }
    }
}

// Initialize app
const app = new KatalogApp();
