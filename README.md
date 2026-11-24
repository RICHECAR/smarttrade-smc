# SmartTrade — SMC V3 Multi-Broker Auto-Trader

Plateforme de trading automatique **Smart Money Concept (SMC V3)** multi-broker :
- **Kraken Futures LIVE** (contrats linéaires `PF_XBTUSD`, `PF_ETHUSD`, `PF_SOLUSD`) avec garde-fous institutionnels Phase 1.9.10-R1 (carnet WS v1 L2 avec reconnexion auto, VWAP vérifié, SL/TP `reduce_only` confirmés, réconciliation `ReconciliationV2`, plafond de risque dur `KRAKEN_MAX_TRADE_RISK_USD`).
- **MetaTrader 5 (MT5)** (crypto miroir SMC V3 + boucle dédiée Forex/Métaux `4h/15m/5m` sur `EURUSD`, `GBPUSD`, `USDJPY`, `XAUUSD`, garde-fou horaire rollover/week-end, plafond micro-lot `MT5_MAX_LOT_SIZE`).
- **Dashboard React/Vite + Accès Mobile Tailscale** (proxy relatif `/api` et `/ws`, `0` donnée mockée, `0` erreur TypeScript).

> Voir le bilan technique complet dans [docs/AUDIT_ETAT_PROJET_2026_11_24.md](docs/AUDIT_ETAT_PROJET_2026_11_24.md).

## ⚠️ Avertissement

**Ce bot trade avec de l'argent réel.** Testez toujours en mode `paper` / démo avant de passer en `live`. Ne committez jamais `.env` ni `data/trades.db`.

## 🚀 Démarrage Rapide (Windows)

### 1. Prérequis
- Python 3.10+ (testé Python 3.14)
- Node.js 18+
- Clés API **Kraken Futures** (sans droit de retrait) et/ou terminal **MetaTrader 5** (`terminal64.exe`)

### 2. Installation

```powershell
# Créer un environnement virtuel Python
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Installer les dépendances Python
pip install -r smc_futures/requirements.txt
pip install MetaTrader5

# Installer les dépendances Node.js
npm install
```

### 3. Configuration

```powershell
# Copier le template de configuration (ne jamais committer .env)
Copy-Item .env.example .env
notepad .env
```

### 4. Démarrage

```powershell
# Terminal 1: Backend SMC V3 + Kraken + MT5 (port 8000)
.\.venv\Scripts\python.exe run.py

# Terminal 2: Frontend Dashboard (accessible PC + Tailscale sur port 3000)
npm run dev -- --host 0.0.0.0 --port 3000
```

Ouvrez `http://localhost:3000` sur PC ou `http://<votre-ip-tailscale>:3000` sur mobile.

### 5. Tests de sécurité

```powershell
.\.venv\Scripts\python.exe -m pytest --noconftest tests/test_phase1_9_10_r1_fixes.py tests/test_phase1_9_10_post_fill_protections.py -q
npx tsc --noEmit
```

## 📊 Modes de Trading

| Mode | TRADING_MODE | AUTO_EXECUTE | Description |
|------|--------------|--------------|-------------|
| Monitor | paper | false | Signaux uniquement, pas d'ordres |
| Shadow | live | shadow | Simule les ordres sans les envoyer |
| Live | live | true | **RÉEL** - Ordres envoyés à Bitget |

### Passer en mode Live (DANGER)

```env
TRADING_MODE=live
AUTO_EXECUTE=true
BITGET_REAL_ONLY=true  # Refuse de démarrer si pas en live+true
```

## 🛡️ Sécurités

1. **Kill Switch** - `/api/bot/kill-switch` bloque tout
2. **Max Daily Loss** - `MAX_DAILY_LOSS_PCT=3.0` → kill-switch auto
3. **Max Trades/Day** - `MAX_TRADES_PER_DAY=10`
4. **Leverage Clamp** - Maximum 10x forcé dans le code
5. **Cooldown** - `TRADE_COOLDOWN_SEC=300`
6. **Session Filter** - Rejette les trades hors sessions institutionnelles

## 🕐 Session Filter

Le bot filtre les trades selon les sessions de marché à fort volume institutionnel.  
**Toutes les heures sont en UTC.**

### Sessions Actives

| Session | Heures (UTC) | Description |
|---------|--------------|-------------|
| `LONDON` | 08:00 – 13:00 | Open London, pré-overlap |
| `LONDON_NY_OVERLAP` | 13:00 – 16:00 | **Plus haut volume** – Les deux sessions actives |
| `NEW_YORK` | 16:00 – 21:00 | Après-midi NY |
| `ASIAN/OFF` | 21:00 – 08:00 | Faible volume – **Rejeté par défaut** |

**Note:** Le weekend (Sam/Dim) est toujours bloqué.

### Configuration

```env
# Sessions actives pour le bot auto (défaut: london,ny)
SMC_SESSIONS=london,ny

# Bypass pour exécution manuelle (bouton "Execute Live Order")
# 1 = bypass (défaut) - ordres manuels autorisés à tout moment
# 0 = respecter le filtre - bloque aussi les ordres manuels hors session
BYPASS_SESSION_FILTER_FOR_MANUAL=1
```

### Comportement

| Type d'ordre | Session Filter | Bypass possible |
|--------------|----------------|-----------------|
| **Auto (bot)** | ✅ Toujours actif | Non |
| **Manuel (UI)** | Configurable | `BYPASS_SESSION_FILTER_FOR_MANUAL=1` |

### Réponse Preflight

L'endpoint `/api/manual/preflight` retourne maintenant les infos de session:

```json
{
  "can_execute": true,
  "session_allowed": false,
  "session_name": "ASIAN/OFF",
  "timezone_used": "UTC",
  "session_reason": "OUTSIDE_ALLOWED_SESSIONS"
}
```

Si `BYPASS_SESSION_FILTER_FOR_MANUAL=0` et hors session → `can_execute: false`.

## 🌏 Stratégie Asia/Weekend (Multi-Strategy Router)

Le bot utilise un **Strategy Router** qui sélectionne automatiquement la stratégie appropriée selon l'heure locale Europe/Paris.

### Principe

| Période | Heure Paris | Stratégie | Approche |
|---------|-------------|-----------|----------|
| **Asia** | 01:00 – 09:00 | AsiaWeekendStrategy | Range Mean Reversion |
| **London** | 09:00 – 14:00 | DefaultSMCStrategy | Trend Following SMC |
| **Overlap** | 14:00 – 17:30 | DefaultSMCStrategy | Highest Volume |
| **NY** | 17:30 – 22:00 | DefaultSMCStrategy | Trend Following SMC |
| **Off** | 22:00 – 01:00 | AsiaWeekendStrategy | Range Mean Reversion |
| **Weekend** | Sam/Dim | AsiaWeekendStrategy | Range Mean Reversion |

### AsiaWeekendStrategy - Mean Reversion

La stratégie Asia/Weekend est optimisée pour les marchés peu liquides:

1. **Détection de Range** — Identifie les zones de consolidation sur MTF
2. **Attente de Sweep** — Attend un sweep du haut/bas du range (liquidity grab)
3. **Confirmation LTF** — CHoCH/BOS contre le sweep
4. **Entry sur OB/FVG** — Entre sur Order Block ou FVG dans la direction du reversal
5. **TP Conservatif** — Cible mid-range (TP1) ou bord opposé (TP2)

### Filtres de sécurité

- **Range trop petit** → Skip (pas de place pour le trade)
- **Range trop grand** → Skip (marché en tendance)
- **Tendance HTF trop forte** → Skip (utilise default SMC)
- **Pas de confirmation de sweep** → Skip (rejection non confirmée)

### Configuration

```env
# Timezone pour la détection de session
TZ=Europe/Paris

# Fenêtre session Asie (heure Paris)
ASIA_START=01:00
ASIA_END=09:00

# Activer/désactiver les stratégies
ENABLE_ASIA_STRATEGY=1
ENABLE_WEEKEND_STRATEGY=1

# R:R Asia/Weekend (plus conservatif)
ASIA_WEEKEND_TP_RR_MIN=1.2
ASIA_WEEKEND_TP_RR_MAX=2.0

# Cible mid-range au lieu du bord opposé
ASIA_TARGET_MID_RANGE=1

# Limites de range (en ATR)
ASIA_MIN_RANGE_ATR=0.5
ASIA_MAX_RANGE_ATR=3.0

# Seuil de tendance HTF (au-dessus = skip mean reversion)
ASIA_DISABLE_IF_HTF_TREND_STRENGTH_GT=0.7

# Facteur de risque réduit (75% du risque normal)
ASIA_RISK_FACTOR=0.75
```

### Logs

Le router log ses décisions:

```
[STRATEGY_ROUTER] BTCUSDT | selected=AsiaWeekendStrategy | reason=asia_session | session=asia | local_time=2026-02-20 03:45:00 | tz=Europe/Paris
[ASIA_STRAT] BTCUSDT | SIGNAL_CREATED | dir=long | rr=1.5 | entry=95000 | sl=94500 | tp=95750 | range=[94400-95800]
```

Rejets explicites:
```
[ASIA_STRAT] BTCUSDT | REJECTED | reason=HTF_TREND_TOO_STRONG | strength=0.82 | threshold=0.7
[ASIA_STRAT] BTCUSDT | REJECTED | reason=RANGE_TOO_SMALL | ratio=0.3 | min=0.5
```

## 🔧 Configuration SMC

### Fallback Modes (pour réduire le "bot muet")

```env
# Autoriser entry sur BOS+FVG même sans sweep
SWEEP_FALLBACK_ENABLED=true
SWEEP_FALLBACK_MIN_RR=2.0

# Autoriser BOS+OB si pas de FVG
FALLBACK_ALLOW_NO_FVG=true

# Créer zone synthétique si pas d'OB/FVG
ENTRY_ZONE_FALLBACK_MODE=true
```

### Counter-Trend

```env
SMC_ALLOW_COUNTERTREND=true
SMC_COUNTERTREND_MIN_RR=2.5
```

## � Small Account Mode (Petit Capital)

Mode optimisé pour faire croître un petit capital (~$20-100) sans dépôt additionnel.

### Activation

```env
# Activer le mode petit compte
SMALL_ACCOUNT_MODE=true

# Risque progressif: démarre à 1.5%, monte jusqu'à 3% max
SMALL_ACC_RISK_START=1.5
SMALL_ACC_RISK_MAX=3.0
SMALL_ACC_RISK_STEP=0.25
```

### Fonctionnement

1. **Auto-filtrage des symboles** — Le bot désactive automatiquement les symboles inaccessibles (ex: BTC/ETH avec $23)
2. **Risk progressif** — Si un symbole éligible a des rejets minQty, le risque augmente par paliers de 0.25%
3. **Cooldown dynamique** — Plus long sur petit capital et après des pertes

### Seuils d'équité estimés

| Symbole | Équité min ~$ | Premier accessible |
|---------|--------------|-------------------|
| XRPUSDT | 5$ | ✅ |
| DOGEUSDT | 8$ | ✅ |
| SOLUSDT | 15$ | |
| ETHUSDT | 32$ | |
| BTCUSDT | 95$ | |

### Monitoring

```bash
# Voir le statut détaillé avec symboles éligibles
curl http://localhost:8000/api/v1/status
```

Réponse :
```json
{
  "equity": 23.0,
  "small_account": {
    "eligible_symbols": ["XRPUSDT", "SOLUSDT"],
    "ineligible_symbols": ["BTCUSDT"],
    "next_equity_thresholds": {"BTCUSDT": 95.0}
  }
}
```

## �🐛 Troubleshooting
### Trop de rejets NO_LIQUIDITY_SWEEP (Soft Bypass)

**Problème:** Le bot rejette des setups valides (BOS + OB frais) juste parce qu'il n'y a pas de sweep.

**Solution: Activer le Soft Bypass**

```env
# Permet l'entrée sur BOS+OB frais même sans sweep
ALLOW_NO_SWEEP_WITH_OB=true

# RR minimum (défaut 1.5 = pas plus risqué qu'un trade normal)
SOFT_BYPASS_MIN_RR=1.5

# Age max de l'OB (bars)
SOFT_BYPASS_MAX_OB_AGE=15
```

**Ce qui est maintenu:**
- RR >= 1.5 vérifié
- HTF bias respecté (pas de counter-trend)
- Structure SMC valide (BOS ou CHoCH)
- OB frais et aligné avec le bias

**Ce qui est assoupli:**
- Pas besoin de liquidity sweep si BOS+OB présent

**Log:** `[SWEEP_BYPASS_SOFT] XRPUSDT | reason=BOS+OB | htf_bias=bullish`
### Erreur 40009 "sign signature error"
**Cause:** Signature HMAC incorrecte
**Solution:** 
- Vérifiez que le timestamp est en millisecondes
- Pour GET: prehash = `timestamp + "GET" + path + "?" + query`  
- Pour POST: prehash = `timestamp + "POST" + path + body`

### Erreur 400172 "productType cannot be empty"
**Cause:** `productType` manquant dans le body
**Solution:** Toujours inclure `"productType": "USDT-FUTURES"` dans les requêtes POST

### Erreur "checkBDScale"
**Cause:** Trop de décimales dans le prix TP/SL
**Solution:** Le bot arrondit automatiquement selon le symbole:
- BTC: 1 décimale
- ETH/SOL: 2 décimales
- XRP: 4 décimales

### Bot "muet" (pas d'ordres)

**Vérifiez les logs pour ces raisons:**
1. `HTF_INSUFFICIENT_DATA` - Pas assez de candles 4H (besoin >= 300)
2. `NO_LIQUIDITY_SWEEP` - Activez `ALLOW_NO_SWEEP_WITH_OB=true` (voir Soft Bypass ci-dessus)
3. `NO_ENTRY_ZONE` - Activez `ENTRY_ZONE_FALLBACK_MODE=true`
4. `COUNTERTREND_BLOCKED` - Activez `SMC_ALLOW_COUNTERTREND=true`
5. `LOW_RR` - Réduisez `MIN_RR` ou améliorez les zones

### API 404 sur /api/v2/mix/account/open-positions
**Solution:** L'endpoint correct est `/api/v2/mix/position/all-position`

## 🧪 Tests

```powershell
# Exécuter tous les tests
pytest tests/ -v

# Tests spécifiques
pytest tests/test_core_functionality.py -v
pytest tests/test_bitget_signature_fix.py -v
```

## 📁 Structure du projet

```
bitget-smc-auto-trader/
├── smc_futures/                 # Core Python engine
│   ├── orchestrator.py          # Main loop, 3-tier MTF
│   ├── api_smc_engine.py        # FastAPI endpoints
│   ├── smc_config.py            # Configuration centralisée
│   ├── app_context.py           # Dependency injection
│   └── engine/
│       ├── strategy/            # SMC logic
│       │   ├── signal_builder.py
│       │   ├── mtf_bias.py
│       │   ├── liquidity.py
│       │   └── ...
│       └── execution/           # Bitget API
│           ├── bitget_executor.py
│           ├── order_manager.py
│           └── bitget_v2_client.py
├── backend/                     # Node.js signing proxy
├── components/                  # React frontend
├── tests/                       # Pytest tests
├── docs/                        # Documentation
│   └── ARCHITECTURE.md
├── .env.example                 # Template configuration
└── README.md
```

## 📚 Documentation

- [Architecture](docs/ARCHITECTURE.md) - Vue technique détaillée
- [Configuration](.env.example) - Toutes les options

---

## 🎯 Strategy Presets (Nouveau v3.0)

Les presets permettent de basculer entre différentes configurations de stratégie optimisées pour des sessions de trading spécifiques.

### Presets Disponibles

| Preset | Session | Description |
|--------|---------|-------------|
| `Scalp_London_Killzone` | London | Scalping agressif London killzone (07:00-10:00 UTC) |
| `NY_Reversal_SMC` | NY | Reversals NY après le run London (13:00-16:00 UTC) |

### Configuration

```env
# Sélectionner un preset dans .env
PRESET_NAME=Scalp_London_Killzone
```

### Paramètres par Preset

**Scalp_London_Killzone** (défaut):
```
MIN_RR=1.6, SWEEP_LOOKBACK=20, TRADE_COOLDOWN=420s
SWEEP_FALLBACK_MAX_FVG_AGE=18, ALLOW_NO_SWEEP_WITH_OB=false
```

**NY_Reversal_SMC**:
```
MIN_RR=1.8, SWEEP_LOOKBACK=60, TRADE_COOLDOWN=600s  
SWEEP_FALLBACK_MAX_FVG_AGE=35, ALLOW_NO_SWEEP_WITH_OB=true
```

### Logging

Chaque trade loggé en DB contient automatiquement:
- `preset_name`: Nom du preset actif
- `session`: Sessio de trading (London/NY)

---

## 📊 Trading Dashboard (Nouveau v3.0)

Dashboard React intégré pour visualiser les performances en temps réel.

### Fonctionnalités

- **KPIs**: PnL, Win Rate, Avg R:R, Trades, Max DD, Profit Factor
- **Courbe R:R**: Évolution des R:R dans le temps (Recharts)
- **Heatmap**: Performance par jour/heure
- **Table Trades**: Derniers trades avec preset/session
- **Live Updates**: WebSocket pour refresh automatique

### Accès

1. Cliquez sur l'icône 📊 dans la barre d'en-tête
2. Sélectionnez la plage de dates
3. Cliquez "Refresh" pour charger les données

### Configuration Frontend

```env
# Dans .env
VITE_API_URL=http://localhost:8000
VITE_WS_URL=ws://localhost:8000
```

---

## 🔌 API Stats & Trades (Nouveau v3.0)

Nouveaux endpoints pour les statistiques de trading.

### Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/trades?from=YYYY-MM-DD&to=YYYY-MM-DD` | Liste des trades |
| `GET /api/trades/open` | Trades actuellement ouverts |
| `GET /api/stats/summary?from=...&to=...` | Stats agrégées |
| `GET /api/stats/rr-series?from=...&to=...` | Série R:R pour graphique |
| `GET /api/stats/heatmap?from=...&to=...` | Données heatmap jour/heure |
| `GET /api/stats/sessions?from=...&to=...` | Stats par session |
| `GET /api/stats/presets?from=...&to=...` | Stats par preset |
| `WS /ws/trades` | WebSocket trades en temps réel |

### Exemples

```bash
# Résumé des 30 derniers jours
curl "http://localhost:8000/api/stats/summary?from=2026-01-20&to=2026-02-19"

# Réponse
{
  "pnl": 150.25,
  "win_rate": 0.65,
  "avg_rr": 1.82,
  "trades": 42,
  "max_dd": 45.50,
  "profit_factor": 2.15,
  "wins": 27,
  "losses": 15
}
```

```bash
# Liste des trades
curl "http://localhost:8000/api/trades?from=2026-02-01&to=2026-02-19"

# Réponse
[
  {
    "id": "abc123",
    "ts": "2026-02-15T09:30:00Z",
    "symbol": "BTCUSDT",
    "side": "long",
    "entry": 95000,
    "stop": 94500,
    "tp": 96000,
    "rr": 2.0,
    "pnl": 25.50,
    "session": "London",
    "preset": "Scalp_London_Killzone",
    "result": "win"
  }
]
```

### WebSocket Events

```javascript
// Connexion
const ws = new WebSocket("ws://localhost:8000/ws/trades");

ws.onmessage = (evt) => {
  const msg = JSON.parse(evt.data);
  
  if (msg.type === "trade_opened") {
    console.log("Nouveau trade:", msg.trade);
  }
  
  if (msg.type === "trade_closed") {
    console.log("Trade fermé:", msg.trade, "PnL:", msg.trade.pnl);
  }
};
```

---

## 📝 Changelog

### v3.0.0 (Février 2026)
- ✨ **Strategy Presets** - Configurations prédéfinies London/NY switchables via `PRESET_NAME`
- ✨ **Trading Dashboard** - Visualisation KPIs, courbe R:R, heatmap, table trades
- ✨ **API Stats** - Endpoints `/api/stats/*` et `/api/trades`
- ✨ **WebSocket Trades** - Push temps réel via `/ws/trades`
- ✨ **Position Sizing** - Module EUR/USDT avec support FX et contract specs
- 🔧 Position sizing basé sur stop distance (risque réel)
- 🔧 Logging preset_name + session dans chaque trade

### v2.1.0 (Février 2026)
- ✅ Fix comparaison RR avec epsilon (évite 1.999 < 2.0)
- ✅ Fix pagination HTF candles (get_candles_paginated)
- ✅ Ajout .env.example propre
- ✅ Ajout tests pytest complets
- ✅ Documentation ARCHITECTURE.md

## 📄 License

MIT
