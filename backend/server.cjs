/**
 * Secure Backend Signing Service
 * Handles API key signing on backend to prevent exposing secrets in frontend
 * 
 * Usage: Run with `node backend/server.js`
 * Environment: Set KRAKEN_API_KEY, KRAKEN_API_SECRET, KRAKEN_PASSPHRASE, BACKEND_API_KEY
 *              (Legacy BITGET_* env vars still accepted for backward compatibility)
 */

const express = require('express');
const crypto = require('crypto');
const cors = require('cors');
const rateLimit = require('express-rate-limit');
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

// Load .env from project root (parent of /backend)
const envPath = path.resolve(__dirname, '..', '.env');
if (fs.existsSync(envPath)) {
  const envContent = fs.readFileSync(envPath, 'utf-8');
  for (const line of envContent.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eqIdx = trimmed.indexOf('=');
    if (eqIdx === -1) continue;
    const key = trimmed.slice(0, eqIdx).trim();
    const val = trimmed.slice(eqIdx + 1).trim();
    if (!process.env[key]) process.env[key] = val;
  }
  console.log(`✅ Loaded .env from ${envPath}`);
  console.log(`   TRADING_MODE = ${process.env.TRADING_MODE || '(not set)'}`);
}

const app = express();
const PORT = process.env.BACKEND_PORT || 5000;
const BACKEND_API_KEY = process.env.BACKEND_API_KEY || 'dev-key-change-in-production';
const NODE_ENV = process.env.NODE_ENV || 'development';

// In-memory cache for proxied requests
const memoryCache = new Map();

/**
 * Get item from cache
 * @param {string} key 
 */
function getFromCache(key) {
  const item = memoryCache.get(key);
  if (!item) return null;
  if (Date.now() > item.expiresAt) {
    memoryCache.delete(key);
    return null;
  }
  return item.data;
}

/**
 * Set item in cache
 * @param {string} key 
 * @param {any} data 
 * @param {number} ttlMs 
 */
function setToCache(key, data, ttlMs) {
  memoryCache.set(key, {
    data,
    expiresAt: Date.now() + ttlMs
  });
}

/**
 * Middleware to check cache before proxying
 */
const checkCache = (req, res, next) => {
  // Only cache GET requests for specific endpoints
  const cacheablePaths = ['/api/smc/market/candles', '/api/v1/market/ticker', '/api/v1/positions'];
  const isCacheable = cacheablePaths.some(p => req.path.startsWith(p));

  if (req.method === 'GET' && isCacheable) {
    const cachedData = getFromCache(req.originalUrl);
    if (cachedData) {
      // console.log(`[CACHE_HIT] ${req.originalUrl}`);
      return res.json(cachedData);
    }
  }
  next();
};

// Middleware
app.use(cors({
  origin: process.env.CORS_ORIGIN || 'http://localhost:3000',
  credentials: true
}));

app.use(express.json({ limit: '1mb' }));

/**
 * Proxy to Python Trading Engine (Port 8000)
 */
const proxy = require('express-http-proxy');

const proxyErrorHandler = (err, res, next) => {
  console.error('[PROXY_ERROR]', err.message);
  res.status(502).json({
    error: 'Bad Gateway',
    message: 'The trading engine (Python) is currently unreachable. Ensure it is running on port 8000.',
    details: err.message
  });
};

// 1. TOP PRIORITY: Bitget API V2 Proxy (Delegates all logic to Python)
app.use('/api/v2', proxy('http://127.0.0.1:8000', {
  proxyReqPathResolver: function (req) {
    return '/api/v2' + req.url;
  },
  parseReqBody: true,
  proxyErrorHandler,
  userResDecorator: function (proxyRes, proxyResData, userReq, userRes) {
    if (proxyRes.statusCode === 500) {
      console.error(`[PROXY][V2] 500 from 8000 for ${userReq.url}`);
    }
    return proxyResData;
  }
}));

// Rate limiting configuration
const isDev = NODE_ENV === 'development';

// Standard limiter: 1000 req / 15m in dev, 100 in prod
const standardLimiter = rateLimit({
  windowMs: 15 * 60 * 1000, // 15 minutes
  max: isDev ? 2000 : 100,
  message: 'Too many requests, please try again later.'
});

// Market data & positions: very permissive in dev
const readLimiter = rateLimit({
  windowMs: 1 * 60 * 1000, // 1 minute
  max: isDev ? 500 : 60,
  message: 'Market data rate limit exceeded'
});

// Orders: strictly limited
const ordersLimiter = rateLimit({
  windowMs: 10 * 1000, // 10 seconds
  max: 5,
  message: 'Order rate limit reached. Slow down.'
});

// Apply global limiter but more permissive in dev
app.use(standardLimiter);

const validateAPIKey = (req, res, next) => {
  const apiKey = req.headers['x-api-key'];

  if (!apiKey || apiKey !== BACKEND_API_KEY) {
    return res.status(401).json({
      error: 'Unauthorized',
      message: 'Invalid or missing API key'
    });
  }

  next();
};

/**
 * Normalizes order payload for V2 API
 */
function normalizeOrderBody(b) {
  if (!b) return b;
  const symbol = String(b.symbol || "").split("_")[0].replace("/", "").replace(":", "").replace("-", "").toUpperCase();

  return {
    ...b,
    symbol,
    productType: (b.productType || "USDT-FUTURES").toUpperCase(),
    marginCoin: (b.marginCoin || "USDT").toUpperCase(),
    marginMode: (b.marginMode || "isolated").toLowerCase(),
    // Normalize numeric fields if present
    size: b.size ? Number(b.size) : undefined,
    price: b.price ? Number(b.price) : undefined,
    stopLoss: b.stopLoss ? Number(b.stopLoss) : undefined,
    takeProfit: b.takeProfit ? Number(b.takeProfit) : undefined,
  };
}

// 2. Health & Execution Health Proxy (Maps /api/health to Python health)
app.use('/api/health', proxy('http://127.0.0.1:8000', {
  proxyReqPathResolver: function (req) {
    return '/api/health' + req.url;
  },
  parseReqBody: false,
  proxyErrorHandler,
}));

/**
 * Health check endpoint
 */
app.get('/health', (req, res) => {
  res.json({
    status: 'ok',
    environment: NODE_ENV,
    timestamp: new Date().toISOString()
  });
});

/**
 * Robust Risk Management & Pre-Trade Validation
 */
const RiskManager = {
  async getAccountData(headers) {
    try {
      const response = await fetch('http://127.0.0.1:8000/api/v1/balances?marginCoin=USDT', {
        headers,
        signal: AbortSignal.timeout(5000)
      });
      const data = await response.json();

      if ((data.code === '00000' || data.status === 'success')) {
        const assets = data.data || [];
        if (Array.isArray(assets) && assets.length > 0) {
          return assets[0];
        }
        // Specific fallback for "data" being the object directly
        if (data.available || data.availableBalance) return data;
      }
      console.warn("[RiskManager] Balance fetch returned no data:", JSON.stringify(data));
      return null;
    } catch (e) {
      console.error("[RiskManager] Failed to fetch balance:", e.message);
      return null;
    }
  },

  async getContractSpecs(symbol, headers) {
    try {
      const response = await fetch(`http://127.0.0.1:8000/api/v1/market/contracts?symbol=${symbol}`, {
        headers,
        signal: AbortSignal.timeout(5000)
      });
      const data = await response.json();

      // Some endpoints return data wrapped in 'data'
      const specs = data.data || data;

      if (!specs || (!specs.qtyStep && !specs.minQty)) {
        console.warn("[RiskManager] Contract specs missing fields:", JSON.stringify(data));
        return null;
      }

      return {
        qtyStep: parseFloat(specs.qtyStep || 0.001),
        minQty: parseFloat(specs.minQty || 0.001),
        minNotional: parseFloat(specs.minNotional || 5.0),
        pricePlace: parseInt(specs.pricePlace || 2),
        tickSize: parseFloat(specs.priceStep || 0.01)
      };
    } catch (e) {
      console.error("[RiskManager] Failed to fetch contract specs:", e.message);
      return null;
    }
  },

  async getLeverage(symbol, headers) {
    try {
      const response = await fetch(`http://127.0.0.1:8000/api/v1/account/leverage?symbol=${symbol}`, { headers });
      const data = await response.json();
      return data.leverage || 10;
    } catch (e) {
      return 10;
    }
  },

  calculateSafeSize(balance, entryPrice, stopLoss, leverage, riskPct = 1.0, specs) {
    const availableBalance = parseFloat(balance.available || balance.availableBalance || 0);
    const riskAmount = availableBalance * (riskPct / 100.0);
    const stopDistance = Math.abs(entryPrice - stopLoss);

    if (stopDistance === 0) return { size: 0, error: "Invalid SL (Distance=0)" };

    // Math: riskAmount = size * stopDistance
    let rawSize = riskAmount / stopDistance;

    // Check leverage constraints (Margin Available)
    const maxNotionalByLeverage = availableBalance * leverage * 0.95; // 5% safety buffer
    const maxQtyByLeverage = maxNotionalByLeverage / entryPrice;

    if (rawSize > maxQtyByLeverage) {
      rawSize = maxQtyByLeverage;
    }

    // Round to contract step
    const step = specs.qtyStep || 0.001;
    let finalSize = Math.floor(rawSize / step) * step;

    // Final precision fix
    const precision = Math.max(0, -Math.floor(Math.log10(step)));
    finalSize = parseFloat(finalSize.toFixed(precision));

    const marginRequired = (finalSize * entryPrice) / leverage;
    const notional = finalSize * entryPrice;

    return {
      size: finalSize,
      notional,
      marginRequired,
      availableBalance,
      riskAmount,
      isPossible: finalSize >= specs.minQty && notional >= specs.minNotional && marginRequired <= availableBalance,
      specs
    };
  }
};

// DEPRECATED proxy block removed (Moved to top-level handler)

/**
 * Sign Kraken Request
 * POST /api/sign/kraken (also accepts /api/sign/bitget for backward compat)
 * 
 * Body:
 * {
 *   "method": "GET",
 *   "endpoint": "/api/v2/spot/account/assets",
 *   "body": "" | JSON string
 * }
 */
app.post(['/api/sign/kraken', '/api/sign/bitget'], validateAPIKey, (req, res) => {
  try {
    const { method, endpoint, body } = req.body;

    if (!method || !endpoint) {
      return res.status(400).json({
        error: 'Bad Request',
        message: 'Missing method or endpoint'
      });
    }

    const apiKey = process.env.KRAKEN_API_KEY || process.env.BITGET_API_KEY;
    const apiSecret = process.env.KRAKEN_API_SECRET || process.env.BITGET_API_SECRET;
    const passphrase = process.env.KRAKEN_PASSPHRASE || process.env.BITGET_PASSPHRASE;

    if (!apiKey || !apiSecret || !passphrase) {
      return res.status(500).json({
        error: 'Server Error',
        message: 'API credentials not configured'
      });
    }

    const timestamp = Date.now().toString();
    const bodyString = body ? JSON.stringify(body) : '';

    // Signature: HMAC-SHA256(timestamp + method + endpoint + body)
    const preHash = timestamp + method.toUpperCase() + endpoint + bodyString;
    const signature = crypto
      .createHmac('sha256', apiSecret)
      .update(preHash)
      .digest('base64');

    res.json({
      headers: {
        'ACCESS-KEY': apiKey,
        'ACCESS-SIGN': signature,
        'ACCESS-TIMESTAMP': timestamp,
        'ACCESS-PASSPHRASE': passphrase,
        'Content-Type': 'application/json',
        'locale': 'en-US'
      },
      timestamp,
      signature
    });
  } catch (error) {
    console.error('Signing error:', error);
    res.status(500).json({
      error: 'Internal Server Error',
      message: error.message
    });
  }
});

/**
 * Sign Binance Request
 * POST /api/sign/binance
 * 
 * Body:
 * {
 *   "params": { "symbol": "BTCUSDT", ... }
 * }
 */
app.post('/api/sign/binance', validateAPIKey, (req, res) => {
  try {
    const { params } = req.body;

    if (!params) {
      return res.status(400).json({
        error: 'Bad Request',
        message: 'Missing params'
      });
    }

    const apiKey = process.env.BINANCE_API_KEY;
    const apiSecret = process.env.BINANCE_API_SECRET;

    if (!apiKey || !apiSecret) {
      return res.status(500).json({
        error: 'Server Error',
        message: 'Binance credentials not configured'
      });
    }

    const timestamp = Date.now();
    const queryParams = { ...params, timestamp };
    const queryString = new URLSearchParams(queryParams).toString();

    // Binance signature: HMAC-SHA256(queryString)
    const signature = crypto
      .createHmac('sha256', apiSecret)
      .update(queryString)
      .digest('hex');

    res.json({
      headers: {
        'X-MBX-APIKEY': apiKey
      },
      queryParams: queryString,
      signature,
      timestamp
    });
  } catch (error) {
    console.error('Signing error:', error);
    res.status(500).json({
      error: 'Internal Server Error',
      message: error.message
    });
  }
});

/**
 * Validate credentials (test auth)
 * POST /api/validate/kraken (also accepts /api/validate/bitget for backward compat)
 */
app.post(['/api/validate/kraken', '/api/validate/bitget'], validateAPIKey, (req, res) => {
  try {
    const apiKey = process.env.KRAKEN_API_KEY || process.env.BITGET_API_KEY;
    const apiSecret = process.env.KRAKEN_API_SECRET || process.env.BITGET_API_SECRET;
    const passphrase = process.env.KRAKEN_PASSPHRASE || process.env.BITGET_PASSPHRASE;

    const isValid = !!(apiKey && apiSecret && passphrase);

    res.json({
      exchange: 'kraken',
      configured: isValid,
      keyLength: apiKey ? apiKey.length : 0,
      secretLength: apiSecret ? apiSecret.length : 0,
      passphraseLength: passphrase ? passphrase.length : 0
    });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

/**
 * Validate credentials (test auth)
 * POST /api/validate/binance
 */
app.post('/api/validate/binance', validateAPIKey, (req, res) => {
  try {
    const apiKey = process.env.BINANCE_API_KEY;
    const apiSecret = process.env.BINANCE_API_SECRET;

    const isValid = !!(apiKey && apiSecret);

    res.json({
      exchange: 'binance',
      configured: isValid,
      keyLength: apiKey ? apiKey.length : 0,
      secretLength: apiSecret ? apiSecret.length : 0
    });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

/**
 * AI Chat endpoint (Ollama local integration)
 * POST /api/ai/chat
 * Body: { message: string }
 */
app.post('/api/ai/chat', validateAPIKey, (req, res) => {
  try {
    const { message } = req.body;
    if (!message || typeof message !== 'string') {
      return res.status(400).json({ error: 'Bad Request', message: 'Missing message in body' });
    }

    // Execute Ollama locally using the DeepSeek R1 model
    // Note: Ollama must be installed and the model available locally
    // Use positional prompt argument (CLI varies by version)
    const args = ['run', 'deepseek-r1', message];
    const output = execFileSync('ollama', args, { encoding: 'utf8', maxBuffer: 10 * 1024 * 1024 });

    return res.json({ response: output });
  } catch (err) {
    console.error('Ollama execution error:', err);
    return res.status(500).json({ error: 'AI_CALL_FAILED', message: err.message || String(err) });
  }
});

/**
 * Proxy to Python Trading Engine (Port 8000)
 * Forwards all /api/smc/* and /api/v1/* requests
 */
// Proxy library already required above

app.get("/api/smc/state", async (req, res) => {
  try {
    const symbol = String(req.query.symbol || "").toUpperCase();
    if (!symbol) return res.status(400).json({ error: "symbol is required" });

    // Proxy logic handled by separate middleware, but if we had local compute:
    // const state = await computeSmcState(symbol);
    // return res.json(state);

    // For now, let the proxy handle it, but we can catch general errors here if needed
    // However, the proxy is applied to /api/smc below.
    res.status(501).json({ error: "Use proxied endpoint" });
  } catch (err) {
    console.error("[/api/smc/state] ERROR:", err?.stack || err);
    res.status(500).json({
      error: "internal_error",
      message: String(err?.message || err),
    });
  }
});

app.use(checkCache);

app.use('/api/smc', readLimiter);
app.get('/api/v1/market', readLimiter);
app.get('/api/v1/positions', readLimiter);
app.use('/api/v1/order', ordersLimiter);

// Enhanced Order placement with Pre-Trade Risk Check
app.post('/api/v1/order', async (req, res) => {
  console.log(`\n[ORDER_START] Incoming request to /api/v1/order`);
  console.log(`[ORDER_BODY_RAW]`, JSON.stringify(req.body));

  try {
    const headers = {
      'X-API-KEY': req.headers['x-api-key'] || req.headers['x-bitget-key'],
      'X-API-SECRET': req.headers['x-api-secret'] || req.headers['x-bitget-secret'],
      'X-API-PASSPHRASE': req.headers['x-api-passphrase'] || req.headers['x-bitget-passphrase']
    };

    const body = normalizeOrderBody(req.body);
    const { symbol, side, price, stopLoss, leverage: reqLeverage, productType } = body;
    console.log(`[ORDER_NORMALIZED] Symbol: ${symbol}, Side: ${side}, PT: ${productType}`);

    // Only Futures need the advanced risk check for balance IF size isn't already provided
    if (productType === 'USDT-FUTURES' && (!body.size || Number(body.size) <= 0)) {
      const [balance, specs, leverage] = await Promise.all([
        RiskManager.getAccountData(headers),
        RiskManager.getContractSpecs(symbol, headers),
        reqLeverage || RiskManager.getLeverage(symbol, headers)
      ]);

      if (balance && specs) {
        const entryPrice = parseFloat(price || body.entryPrice || 0);
        const sl = parseFloat(stopLoss || body.stopLoss || 0);

        if (entryPrice > 0 && sl > 0) {
          const riskPct = parseFloat(process.env.RISK_PER_TRADE || 1.0);
          const riskResult = RiskManager.calculateSafeSize(balance, entryPrice, sl, leverage, riskPct, specs);

          if (!riskResult.isPossible) {
            console.warn(`[OrderBlocked] ${symbol} ${side}: Size=${riskResult.size} MarginReq=${riskResult.marginRequired} Balance=${riskResult.availableBalance}`);
            return res.status(400).json({
              status: "error",
              error: "RISK_VALIDATION_FAILED",
              message: "The order amount exceeds the balance or is below minimum requirements.",
              details: riskResult
            });
          }

          body.size = Number(riskResult.size);
          console.info(`[OrderValidated] ${symbol} ${side} Size: ${body.size} (Margin: ${riskResult.marginRequired.toFixed(2)} USDT)`);
        } else {
          return res.status(400).json({
            status: "error",
            error: "MISSING_RISK_PARAMS",
            message: "entryPrice and stopLoss are required for automatic position sizing."
          });
        }
      } else {
        console.error(`[RiskError] Could not fetch balance (${!!balance}) or specs (${!!specs}) for ${symbol}`);
        return res.status(502).json({
          status: "error",
          error: "RISK_DATA_UNAVAILABLE",
          message: "Could not fetch account balance or symbol specs from trading engine to calculate safe size."
        });
      }
    }

    if (!body.size || parseFloat(body.size) <= 0) {
      console.warn(`[ORDER_FINAL_ERR] Size missing or <= 0: ${body.size}`);
      return res.status(400).json({
        status: "error",
        error: "INVALID_SIZE",
        message: "Order size is missing or invalid. Check calculation parameters."
      });
    }

    if (process.env.TRADING_MODE !== 'live') {
      return res.status(403).json({ error: "FORBIDDEN", message: "Live trading is disabled. Set TRADING_MODE=live in .env to enable." });
    }

    console.log(`[ORDER_FORWARDING] Sending to Python:`, JSON.stringify(body));

    const response = await fetch('http://127.0.0.1:8000/api/v1/order', {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });

    const data = await response.json();
    console.log(`[ORDER_PYTHON_RES] Status: ${response.status}`, JSON.stringify(data));
    res.status(response.status).json(data);

  } catch (err) {
    console.error("[OrderProxy] Error:", err.message);
    res.status(500).json({ error: "Internal Proxy Error", message: err.message });
  }
});

app.use('/api/smc', proxy('http://127.0.0.1:8000', {
  proxyReqPathResolver: function (req) {
    return '/api/smc' + req.url;
  },
  parseReqBody: false,
  proxyErrorHandler,
  userResDecorator: function (proxyRes, proxyResData, userReq, userRes) {
    if (proxyRes.statusCode === 500) {
      console.error(`[PROXY][SMC] 500 from 8000 for ${userReq.url}`);
    }

    // Cache the response if successful
    if (proxyRes.statusCode === 200 && userReq.method === 'GET') {
      try {
        const data = JSON.parse(proxyResData.toString('utf8'));
        let ttl = 10000; // 10s default for SMC
        if (userReq.url.includes('candles')) {
          // 15m candles: 15s cache, 4H: 60s cache
          ttl = userReq.url.includes('15m') ? 15000 : 60000;
        }
        setToCache(userReq.originalUrl, data, ttl);
      } catch (e) {
        // Silent fail on parse
      }
    }

    return proxyResData;
  }
}));

app.use('/api/trade', proxy('http://127.0.0.1:8000', {
  proxyReqPathResolver: function (req) {
    return '/api/v1' + req.url; // Map /api/trade to /api/v1 on Python engine
  },
  parseReqBody: false,
  proxyErrorHandler,
  userResDecorator: function (proxyRes, proxyResData, userReq, userRes) {
    if (proxyRes.statusCode === 500) {
      console.error(`[PROXY][TRADE] 500 from 8000 for ${userReq.url}`);
    }
    return proxyResData;
  }
}));

// DEPRECATED proxy block removed (Moved to top-level handler)

app.use('/api/v1', proxy('http://127.0.0.1:8000', {
  proxyReqPathResolver: function (req) {
    return '/api/v1' + req.url;
  },
  proxyReqBodyDecorator: function (bodyContent, srcReq) {
    if (srcReq.method === 'POST' && srcReq.url.includes('/order')) {
      try {
        const body = JSON.parse(bodyContent.toString());
        return JSON.stringify(normalizeOrderBody(body));
      } catch (e) {
        return bodyContent;
      }
    }
    return bodyContent;
  },
  parseReqBody: true, // Needed for body decorator
  proxyErrorHandler,
  userResDecorator: function (proxyRes, proxyResData, userReq, userRes) {
    if (proxyRes.statusCode === 500) {
      console.error(`[PROXY][V1] 500 from 8000 for ${userReq.url}`);
    }

    // Cache the response if successful
    if (proxyRes.statusCode === 200 && userReq.method === 'GET') {
      try {
        const data = JSON.parse(proxyResData.toString('utf8'));
        let ttl = 2000; // 2s default for V1 (ticker)
        if (userReq.url.includes('positions')) ttl = 5000;
        setToCache(userReq.originalUrl, data, ttl);
      } catch (e) { }
    }

    // Security check: If TRADING_MODE is not live, block order placement
    if (userReq.method === 'POST' && userReq.url.includes('/order') && process.env.TRADING_MODE !== 'live') {
      userRes.status(403);
      return JSON.stringify({ error: "FORBIDDEN", message: "Live trading is disabled. Set TRADING_MODE=live in .env to enable." });
    }
    return proxyResData;
  }
}));

/**
 * Static Files & SPA Fallback
 */
const distPath = path.resolve(__dirname, '..', 'dist');
if (fs.existsSync(distPath)) {
  console.log(`📂 Serving static files from ${distPath}`);
  app.use(express.static(distPath));
  // SPA Fallback: serve index.html for any unknown route NOT starting with /api
  app.get('*', (req, res, next) => {
    if (req.path.startsWith('/api') || req.path.startsWith('/health')) {
      return next();
    }
    res.sendFile(path.join(distPath, 'index.html'));
  });
}

/**
 * 404 Handler for API
 */
app.use('/api', (req, res) => {
  res.status(404).json({
    error: 'Not Found',
    message: `API Endpoint ${req.path} not found`
  });
});

/**
 * Error Handler
 */
app.use((err, req, res, next) => {
  console.error('Unhandled error:', err);
  res.status(500).json({
    error: 'Internal Server Error',
    message: NODE_ENV === 'development' ? err.message : 'An unexpected error occurred'
  });
});

// Start server
app.listen(PORT, () => {
  console.log(`\n🚀 SmartTrade AI Backend running on http://localhost:${PORT}`);
  console.log(`📝 Environment: ${NODE_ENV}`);
  console.log(`🔐 API Key configured: ${!!BACKEND_API_KEY && BACKEND_API_KEY !== 'dev-key-change-in-production' ? 'Yes ✓' : 'No ✗ - Using default key!'}`);
  console.log(`\nAvailable endpoints:`);
  console.log(`  GET  /health - Health check`);
  console.log(`  POST /api/sign/kraken - Sign Kraken request`);
  console.log(`  POST /api/sign/binance - Sign Binance request`);
  console.log(`  POST /api/validate/kraken - Validate Kraken credentials`);
  console.log(`  POST /api/validate/binance - Validate Binance credentials\n`);
});

module.exports = app;
