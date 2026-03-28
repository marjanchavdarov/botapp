/**
 * katalog.ai — Barcode Scanner
 * Adds a floating scan button to the PWA.
 * On scan → calls backend → looks up product on cijene.dev via MCP → shows price comparison.
 *
 * Uses ZXing (via CDN) for barcode detection in the browser camera stream.
 */

class BarcodeScanner {
    constructor(app) {
        this.app = app;
        this.isOpen = false;
        this.codeReader = null;
        this.stream = null;
        this.lastScanned = null;
        this.scanCooldown = false;

        this.injectStyles();
        this.injectHTML();
        this.setupEventListeners();
        this.loadZXing();
    }

    // ── Load ZXing from CDN ───────────────────────────────────────────────
    loadZXing() {
        if (window.ZXing) return;
        const script = document.createElement('script');
        script.src = 'https://cdn.jsdelivr.net/npm/@zxing/library@0.19.1/umd/index.min.js';
        script.onload = () => console.log('ZXing loaded');
        script.onerror = () => console.error('Failed to load ZXing');
        document.head.appendChild(script);
    }

    // ── Inject CSS ────────────────────────────────────────────────────────
    injectStyles() {
        const style = document.createElement('style');
        style.textContent = `
        /* Floating scan button */
        .scan-bubble {
            position: fixed;
            bottom: 80px;
            right: 16px;
            width: 52px;
            height: 52px;
            background: #00ff88;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 22px;
            cursor: pointer;
            z-index: 900;
            box-shadow: 0 4px 16px rgba(0,255,136,0.4);
            transition: transform 0.15s, box-shadow 0.15s;
            border: none;
            user-select: none;
        }
        .scan-bubble:active {
            transform: scale(0.92);
            box-shadow: 0 2px 8px rgba(0,255,136,0.3);
        }

        /* Scanner modal overlay */
        .scanner-overlay {
            position: fixed;
            inset: 0;
            background: #000;
            z-index: 1000;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: flex-start;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.2s;
        }
        .scanner-overlay.open {
            opacity: 1;
            pointer-events: all;
        }

        /* Header */
        .scanner-header {
            width: 100%;
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 16px 20px;
            background: #111;
        }
        .scanner-title {
            color: #00ff88;
            font-weight: bold;
            font-size: 16px;
            font-family: monospace;
        }
        .scanner-close {
            background: none;
            border: none;
            color: #fff;
            font-size: 22px;
            cursor: pointer;
            padding: 4px 8px;
        }

        /* Video container */
        .scanner-viewport {
            position: relative;
            width: 100%;
            flex: 1;
            overflow: hidden;
            background: #000;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .scanner-viewport video {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }

        /* Scanning frame overlay */
        .scan-frame {
            position: absolute;
            width: 260px;
            height: 160px;
            border: 2px solid #00ff88;
            border-radius: 12px;
            box-shadow: 0 0 0 2000px rgba(0,0,0,0.5);
            pointer-events: none;
        }
        .scan-frame::before,
        .scan-frame::after {
            content: '';
            position: absolute;
            width: 24px;
            height: 24px;
            border-color: #00ff88;
            border-style: solid;
        }
        .scan-frame::before {
            top: -2px; left: -2px;
            border-width: 3px 0 0 3px;
            border-radius: 10px 0 0 0;
        }
        .scan-frame::after {
            bottom: -2px; right: -2px;
            border-width: 0 3px 3px 0;
            border-radius: 0 0 10px 0;
        }

        /* Animated scan line */
        .scan-line {
            position: absolute;
            width: 240px;
            height: 2px;
            background: linear-gradient(90deg, transparent, #00ff88, transparent);
            animation: scanMove 2s ease-in-out infinite;
            top: 50%;
        }
        @keyframes scanMove {
            0%   { transform: translateY(-70px); opacity: 0; }
            10%  { opacity: 1; }
            90%  { opacity: 1; }
            100% { transform: translateY(70px); opacity: 0; }
        }

        /* Status bar */
        .scanner-status {
            color: #aaa;
            font-size: 13px;
            font-family: monospace;
            padding: 12px 20px;
            text-align: center;
            background: #111;
            width: 100%;
        }
        .scanner-status.detected {
            color: #00ff88;
        }
        .scanner-status.error {
            color: #ff5555;
        }

        /* Results panel */
        .scanner-results {
            width: 100%;
            background: #111;
            border-top: 1px solid #222;
            padding: 16px 20px;
            max-height: 50vh;
            overflow-y: auto;
            display: none;
        }
        .scanner-results.visible {
            display: block;
        }
        .result-barcode {
            font-size: 11px;
            color: #555;
            font-family: monospace;
            margin-bottom: 8px;
        }
        .result-product-name {
            color: #fff;
            font-size: 17px;
            font-weight: bold;
            margin-bottom: 4px;
        }
        .result-brand {
            color: #aaa;
            font-size: 13px;
            margin-bottom: 16px;
        }

        /* Price comparison table */
        .price-table {
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 16px;
        }
        .price-table th {
            text-align: left;
            color: #555;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 1px;
            padding: 6px 8px;
            border-bottom: 1px solid #222;
            font-family: monospace;
        }
        .price-table td {
            padding: 10px 8px;
            border-bottom: 1px solid #1a1a1a;
            font-size: 14px;
            font-family: monospace;
        }
        .price-table tr.cheapest td {
            background: #0a2a1a;
        }
        .price-table tr.cheapest .store-name {
            color: #00ff88;
            font-weight: bold;
        }
        .price-cheapest-badge {
            background: #00ff88;
            color: #000;
            font-size: 10px;
            font-weight: bold;
            padding: 2px 6px;
            border-radius: 10px;
            margin-left: 6px;
        }
        .price-value {
            font-weight: bold;
            color: #fff;
        }
        .price-value.best {
            color: #00ff88;
            font-size: 16px;
        }
        .price-discount {
            color: #ff9900;
            font-size: 11px;
        }
        .price-old {
            color: #555;
            text-decoration: line-through;
            font-size: 12px;
        }

        /* No results */
        .no-result {
            text-align: center;
            color: #555;
            padding: 20px;
            font-size: 14px;
            font-family: monospace;
        }

        /* Scan again button */
        .scan-again-btn {
            width: 100%;
            padding: 14px;
            background: #00ff88;
            color: #000;
            border: none;
            border-radius: 8px;
            font-weight: bold;
            font-size: 15px;
            cursor: pointer;
            font-family: monospace;
            margin-top: 8px;
        }
        .scan-again-btn:active { opacity: 0.8; }

        /* Loading spinner */
        .scan-spinner {
            display: inline-block;
            width: 18px;
            height: 18px;
            border: 2px solid #333;
            border-top-color: #00ff88;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            vertical-align: middle;
            margin-right: 8px;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        `;
        document.head.appendChild(style);
    }

    // ── Inject HTML ───────────────────────────────────────────────────────
    injectHTML() {
        // Floating button
        const btn = document.createElement('button');
        btn.className = 'scan-bubble';
        btn.id = 'scanBubble';
        btn.innerHTML = '📷';
        btn.title = 'Skeniraj barkod';
        document.getElementById('app').appendChild(btn);

        // Scanner overlay
        const overlay = document.createElement('div');
        overlay.className = 'scanner-overlay';
        overlay.id = 'scannerOverlay';
        overlay.innerHTML = `
            <div class="scanner-header">
                <span class="scanner-title">📷 Skeniraj barkod</span>
                <button class="scanner-close" id="scannerClose">✕</button>
            </div>
            <div class="scanner-viewport" id="scannerViewport">
                <video id="scannerVideo" playsinline muted></video>
                <div class="scan-frame">
                    <div class="scan-line"></div>
                </div>
            </div>
            <div class="scanner-status" id="scannerStatus">Usmjeri kameru prema barkodu</div>
            <div class="scanner-results" id="scannerResults"></div>
        `;
        document.getElementById('app').appendChild(overlay);
    }

    // ── Event Listeners ───────────────────────────────────────────────────
    setupEventListeners() {
        document.addEventListener('click', (e) => {
            if (e.target.closest('#scanBubble')) this.open();
            if (e.target.closest('#scannerClose')) this.close();
        });
    }

    // ── Open Scanner ──────────────────────────────────────────────────────
    async open() {
        this.isOpen = true;
        const overlay = document.getElementById('scannerOverlay');
        overlay.classList.add('open');
        document.getElementById('scannerResults').classList.remove('visible');
        document.getElementById('scannerResults').innerHTML = '';
        this.setStatus('Usmjeri kameru prema barkodu', '');

        await this.startCamera();
    }

    // ── Close Scanner ─────────────────────────────────────────────────────
    close() {
        this.isOpen = false;
        document.getElementById('scannerOverlay').classList.remove('open');
        this.stopCamera();
    }

    // ── Start Camera + ZXing ─────────────────────────────────────────────
    async startCamera() {
        try {
            // Request camera
            this.stream = await navigator.mediaDevices.getUserMedia({
                video: {
                    facingMode: 'environment', // back camera on mobile
                    width: { ideal: 1280 },
                    height: { ideal: 720 }
                }
            });

            const video = document.getElementById('scannerVideo');
            video.srcObject = this.stream;
            await video.play();

            this.startDecoding(video);

        } catch (err) {
            if (err.name === 'NotAllowedError') {
                this.setStatus('Kamera nije dopuštena. Dopusti pristup kameri.', 'error');
            } else {
                this.setStatus('Kamera nije dostupna: ' + err.message, 'error');
            }
        }
    }

    // ── Stop Camera ───────────────────────────────────────────────────────
    stopCamera() {
        if (this.stream) {
            this.stream.getTracks().forEach(t => t.stop());
            this.stream = null;
        }
        if (this.codeReader) {
            try { this.codeReader.reset(); } catch(e) {}
            this.codeReader = null;
        }
    }

    // ── ZXing Decode Loop ─────────────────────────────────────────────────
    startDecoding(video) {
        if (!window.ZXing) {
            // Retry once ZXing loads
            setTimeout(() => this.startDecoding(video), 500);
            return;
        }

        try {
            const hints = new Map();
            // Support common barcode formats
            const formats = [
                ZXing.BarcodeFormat.EAN_13,
                ZXing.BarcodeFormat.EAN_8,
                ZXing.BarcodeFormat.UPC_A,
                ZXing.BarcodeFormat.UPC_E,
                ZXing.BarcodeFormat.CODE_128,
                ZXing.BarcodeFormat.CODE_39,
                ZXing.BarcodeFormat.QR_CODE,
            ];
            hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, formats);
            hints.set(ZXing.DecodeHintType.TRY_HARDER, true);

            this.codeReader = new ZXing.BrowserMultiFormatReader(hints);

            this.codeReader.decodeFromVideoElement(video, (result, err) => {
                if (!this.isOpen) return;

                if (result && !this.scanCooldown) {
                    this.onBarcodeDetected(result.getText());
                }
                // Ignore err — ZXing fires errors constantly when no barcode in frame
            });

        } catch (e) {
            this.setStatus('Greška skenera: ' + e.message, 'error');
        }
    }

    // ── Barcode Detected ─────────────────────────────────────────────────
    async onBarcodeDetected(barcode) {
        if (barcode === this.lastScanned) return;
        this.lastScanned = barcode;
        this.scanCooldown = true;

        // Haptic feedback
        if (navigator.vibrate) navigator.vibrate(50);

        this.setStatus(`<span class="scan-spinner"></span> Pronađeno: ${barcode} — tražim cijene...`, 'detected');

        const results = document.getElementById('scannerResults');
        results.innerHTML = `<div class="no-result"><span class="scan-spinner"></span> Tražim cijene...</div>`;
        results.classList.add('visible');

        try {
            const data = await this.lookupBarcode(barcode);
            this.renderResults(barcode, data);
        } catch (err) {
            results.innerHTML = `
                <div class="no-result">❌ Greška: ${err.message}</div>
                <button class="scan-again-btn" onclick="window.katalogScanner.scanAgain()">🔄 Skeniraj ponovo</button>
            `;
            this.setStatus('Greška pri dohvaćanju cijena', 'error');
        }

        // Allow re-scan after 8 seconds
        setTimeout(() => { this.scanCooldown = false; }, 8000);
    }

    // ── API Call to Backend ───────────────────────────────────────────────
    async lookupBarcode(barcode) {
        const country = this.app ? this.app.country : 'croatia';
        const response = await fetch(`/api/barcode/${barcode}?country=${country}`, {
            method: 'GET',
            headers: { 'Content-Type': 'application/json' }
        });

        if (!response.ok) {
            throw new Error(`Server error: ${response.status}`);
        }

        return await response.json();
    }

    // ── Render Price Comparison ───────────────────────────────────────────
    renderResults(barcode, data) {
        const results = document.getElementById('scannerResults');

        if (!data || (!data.prices?.length && !data.product_name)) {
            results.innerHTML = `
                <div class="result-barcode">Barkod: ${barcode}</div>
                <div class="no-result">😕 Proizvod nije pronađen na cijene.dev<br><small>Pokušaj drugi barkod</small></div>
                <button class="scan-again-btn" onclick="window.katalogScanner.scanAgain()">🔄 Skeniraj ponovo</button>
            `;
            this.setStatus('Proizvod nije pronađen', 'error');
            return;
        }

        const prices = data.prices || [];
        const cheapest = prices.length ? prices.reduce((a, b) =>
            parseFloat(a.price) < parseFloat(b.price) ? a : b
        ) : null;

        let html = `
            <div class="result-barcode">Barkod: ${barcode}</div>
            ${data.product_name ? `<div class="result-product-name">${data.product_name}</div>` : ''}
            ${data.brand ? `<div class="result-brand">${data.brand}</div>` : ''}
        `;

        if (prices.length) {
            html += `
                <table class="price-table">
                    <thead>
                        <tr>
                            <th>Trgovina</th>
                            <th>Cijena</th>
                            <th>Akcija</th>
                        </tr>
                    </thead>
                    <tbody>
            `;

            // Sort by price ascending
            const sorted = [...prices].sort((a, b) => parseFloat(a.price) - parseFloat(b.price));

            sorted.forEach((item, idx) => {
                const isCheapest = idx === 0;
                html += `
                    <tr class="${isCheapest ? 'cheapest' : ''}">
                        <td>
                            <span class="store-name">${item.store}</span>
                            ${isCheapest ? '<span class="price-cheapest-badge">NAJJEFTINIJE</span>' : ''}
                        </td>
                        <td>
                            <span class="price-value ${isCheapest ? 'best' : ''}">${item.price} €</span>
                            ${item.original_price ? `<br><span class="price-old">${item.original_price} €</span>` : ''}
                        </td>
                        <td>
                            ${item.discount_percent ? `<span class="price-discount">-${item.discount_percent}</span>` : '—'}
                        </td>
                    </tr>
                `;
            });

            html += `</tbody></table>`;

            if (cheapest) {
                this.setStatus(`✅ Najjeftinije: ${cheapest.store} — ${cheapest.price} €`, 'detected');
            }
        } else {
            html += `<div class="no-result">Nema dostupnih cijena</div>`;
            this.setStatus('Nema dostupnih cijena', '');
        }

        html += `<button class="scan-again-btn" onclick="window.katalogScanner.scanAgain()">🔄 Skeniraj ponovo</button>`;

        results.innerHTML = html;
        results.classList.add('visible');
    }

    // ── Scan Again ────────────────────────────────────────────────────────
    scanAgain() {
        this.lastScanned = null;
        this.scanCooldown = false;
        document.getElementById('scannerResults').classList.remove('visible');
        document.getElementById('scannerResults').innerHTML = '';
        this.setStatus('Usmjeri kameru prema barkodu', '');
    }

    // ── Status Helper ─────────────────────────────────────────────────────
    setStatus(msg, type) {
        const el = document.getElementById('scannerStatus');
        if (!el) return;
        el.innerHTML = msg;
        el.className = 'scanner-status' + (type ? ` ${type}` : '');
    }
}

// Auto-init when DOM is ready — attach to window for global access
document.addEventListener('DOMContentLoaded', () => {
    // Wait for app to be initialized
    const tryInit = () => {
        if (window.app) {
            window.katalogScanner = new BarcodeScanner(window.app);
        } else {
            setTimeout(tryInit, 200);
        }
    };
    tryInit();
});