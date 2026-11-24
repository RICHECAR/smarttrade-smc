# 📄 Documentation des Variables d'Environnement

Ce document décrit toutes les variables de configuration utilisées dans le fichier `.env` pour le bot **SMC Auto-Trader**.

---

## 🏗️ 1. Déploiement & Identifiants
| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `NODE_ENV` | Mode d'environnement (`development`, `production`). | `production` |
| `BACKEND_PORT` | Port du backend FastAPI. | `5000` |
| `BACKEND_API_KEY` | Clé sécurisée pour la signature des requêtes internes. | **Requis** |
| `BITGET_API_KEY` | Clé API Bitget V2 Futures. | **Requis** |
| `BITGET_API_SECRET` | Secret API Bitget V2 Futures. | **Requis** |
| `BITGET_PASSPHRASE` | Passphrase API Bitget V2. | **Requis** |
| `BITGET_REAL_ONLY` | Si `true`, empêche le lancement du bot sauf en mode `live`. | `false` |

---

## 🦑 1b. Identifiants Kraken Futures
| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `KRAKEN_ENABLED` | Activer le trading Kraken Futures. | `false` |
| `KRAKEN_API_KEY` | Clé API Kraken Futures (clé publique). | _Optionnel_ |
| `KRAKEN_API_SECRET` | Secret API Kraken Futures (encodé base64). | _Optionnel_ |
| `KRAKEN_SYMBOLS` | Symboles Kraken à trader (ex: `BTC/USD,ETH/USD`). | `BTC/USD,ETH/USD` |
| `KRAKEN_PRODUCT_TYPE` | Type de produit : `perpetual` (linéaire) ou `inverse`. | `perpetual` |
| `KRAKEN_LEVERAGE` | Multiplicateur de levier Kraken (max 50x). | `3` |
| `KRAKEN_MARGIN_MODE` | Mode de marge : `cross` ou `isolated`. | `cross` |

**Exemple :**
```env
# Activer Kraken Futures
KRAKEN_ENABLED=true
KRAKEN_API_KEY=votre_cle_api_kraken
KRAKEN_API_SECRET=votre_secret_encode_base64
KRAKEN_SYMBOLS=BTC/USD,ETH/USD
KRAKEN_LEVERAGE=5
```

### Correspondance des symboles (Bitget ↔ Kraken)
| Bitget | Kraken Futures | Note |
| :--- | :--- | :--- |
| BTCUSDT | PF_XBTUSD | Kraken utilise XBT pour Bitcoin |
| ETHUSDT | PF_ETHUSD | |
| SOLUSDT | PF_SOLUSD | |
| XRPUSDT | PF_XRPUSD | |

**Note :** Les symboles sont traduits automatiquement par le bot. Vous pouvez utiliser l'un ou l'autre format.

---

## 🔀 1c. Paramètres Multi-Broker
| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `DEFAULT_BROKER` | Broker par défaut pour les ordres : `bitget` ou `kraken`. | `bitget` |
| `BALANCE_CHECK_ENABLED` | Vérifier le solde avant de passer les ordres. | `true` |

---

## 🤖 2. Mode principal du Bot
| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `TRADING_MODE` | Mode d'exécution : `paper` (simulé) ou `live` (fonds réels). | `paper` |
| `BOT_STRATEGY_MODE` | Moteur de stratégie : `smc` (Smart Money) ou `envelope` (Mean Reversion). | `smc` |
| `BOT_SYMBOLS` | Liste de symboles séparés par des virgules (ex: `BTCUSDT,ETHUSDT`). | `BTCUSDT` |
| `BOT_TIMEFRAME` | Timeframe principal pour l'analyse (ex: `15m`, `1h`). | `15m` |

---

## 🛡️ 3. Gestion du risque & Limites
| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `RISK_PCT` | Risque par trade en % du solde (0.01 = 1%). | `0.01` |
| `MIN_RR` | Ratio Risque/Récompense minimum requis pour prendre un trade. | `1.5` |
| `BOT_LEVERAGE` | Multiplicateur de levier (plafonné à 10x dans le code). | `10` |
| `MAX_TRADES_PER_DAY` | Maximum de trades autorisés par jour (sécurité globale). | `12` |
| `MAX_DAILY_LOSS_PCT` | Arrêt du trading si la perte journalière dépasse ce %. | `3.0` |
| `MAX_OPEN_TRADES_PER_SYMBOL` | Max de positions ouvertes simultanées par symbole (anti-pyramiding). | `1` |
| `TRADE_COOLDOWN_SEC` | Secondes minimum entre deux trades consécutifs sur le même symbole. | `300` |

---

## ⚙️ 4. Paramètres d'exécution
| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `AUTO_EXECUTE` | Interrupteur principal pour l'exécution automatique des ordres. | `false` |
| `FORCE_EXECUTE` | **DEBUG UNIQUEMENT**. Contourne les filtres souples (RR, session, cooldown). | `false` |
| `SMC_ORDER_TYPE` | Type d'ordre à l'entrée : `market` ou `limit`. | `market` |
| `ALLOW_MANUAL_TRADES` | Activer l'exécution manuelle de trades depuis le bouton UI. | `true` |

---

## 🕒 5. Filtre de session

**Les stratégies SMC dépendent du volume institutionnel, qui culmine pendant les sessions de Londres et New York.**  
Le bot rejette les trades en dehors de ces fenêtres à fort volume pour éviter les périodes de faible liquidité.

### Fenêtres de session (UTC)
| Session | Heures (UTC) | Description |
| :--- | :--- | :--- |
| `LONDON` | 08:00 – 13:00 | Ouverture London, pré-overlap |
| `LONDON_NY_OVERLAP` | 13:00 – 16:00 | **Volume le plus élevé** – Les deux sessions actives |
| `NEW_YORK` | 16:00 – 21:00 | Après-midi NY |
| `ASIAN/OFF` | 21:00 – 08:00 | Volume faible – **Rejeté par défaut** |

**Le trading le weekend (Sam/Dim) est toujours bloqué.**

### Configuration
| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `SMC_SESSIONS` | Sessions actives pour le bot auto. Séparées par virgules : `london`, `ny`, `asia`. | `london,ny,asia` |
| `BYPASS_SESSION_FILTER_FOR_MANUAL` | Autoriser les trades manuels hors sessions. `1`=oui, `0`=non. | `1` |

**Exemple :**
```env
# Trader uniquement pendant la session NY
SMC_SESSIONS=ny

# Bloquer aussi les trades manuels hors sessions
BYPASS_SESSION_FILTER_FOR_MANUAL=0
```

### Champs de réponse Preflight
L'endpoint `/api/manual/preflight` retourne les infos de session :
```json
{
  "session_allowed": true,
  "session_name": "LONDON_NY_OVERLAP",
  "timezone_used": "UTC",
  "session_reason": null
}
```

Si hors session :
```json
{
  "session_allowed": false,
  "session_name": "ASIAN/OFF",
  "timezone_used": "UTC",
  "session_reason": "OUTSIDE_ALLOWED_SESSIONS"
}
```

---

## 🌏 5b. Stratégie Asie/Weekend (Routeur Multi-Stratégie)

Le bot utilise un **routeur de stratégie** qui sélectionne automatiquement la stratégie appropriée selon l'heure locale (Europe/Paris).

### Routage des sessions

| Session | Heure Paris | Stratégie |
| :--- | :--- | :--- |
| Asie | 01:00 – 09:00 | AsiaWeekendStrategy |
| London | 09:00 – 14:00 | DefaultSMCStrategy |
| Overlap | 14:00 – 17:30 | DefaultSMCStrategy |
| NY | 17:30 – 22:00 | DefaultSMCStrategy |
| Off | 22:00 – 01:00 | AsiaWeekendStrategy |
| Weekend | Sam/Dim | AsiaWeekendStrategy |

### Configuration du routeur

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `TZ` / `STRATEGY_TIMEZONE` | Fuseau horaire pour la détection de session | `Europe/Paris` |
| `ASIA_START` | Heure de début session Asie (locale) | `01:00` |
| `ASIA_END` | Heure de fin session Asie (locale) | `09:00` |
| `ENABLE_ASIA_STRATEGY` | Activer la stratégie session Asie | `1` |
| `ENABLE_WEEKEND_STRATEGY` | Activer la stratégie weekend | `1` |

### Paramètres de la stratégie Asie/Weekend

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `ASIA_WEEKEND_TP_RR_MIN` | R:R minimum pour Asie/Weekend | `1.1` |
| `ASIA_WEEKEND_TP_RR_MAX` | R:R maximum (plafond TP) | `2.0` |
| `ASIA_TARGET_MID_RANGE` | Cibler le mid-range au lieu du bord opposé | `1` |
| `ASIA_RANGE_LOOKBACK` | Nombre de bougies pour la détection de range | `20` |
| `ASIA_MIN_RANGE_ATR` | Taille min du range en ATR | `0.5` |
| `ASIA_MAX_RANGE_ATR` | Taille max du range en ATR (défaut) | `3.0` |
| `ASIA_DISABLE_IF_HTF_TREND_STRENGTH_GT` | Ignorer le mean reversion si tendance > seuil | `0.7` |
| `ASIA_RISK_FACTOR` | Facteur de réduction du risque (0.75 = 75%) | `0.75` |

### Seuils de range par actif (NOUVEAU)

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `ASIA_MAX_RANGE_RATIO_BTC` | Ratio ATR max du range pour BTC (plus élevé = plus tolérant) | `6.0` |
| `ASIA_MAX_RANGE_RATIO_ETH` | Ratio ATR max du range pour ETH | `5.0` |
| `ASIA_MAX_RANGE_RATIO_ALT` | Ratio ATR max du range pour les altcoins | `4.0` |
| `ASIA_WEEKEND_RELAX_RANGE` | Multiplicateur weekend sur les seuils (1.3 = 30% de relâchement) | `1.3` |

### Mode Range en Tendance (NOUVEAU)

Quand le range dépasse le seuil mais que la structure est propre, autorise l'entrée avec un risque réduit :

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `ASIA_TRENDING_RANGE_ALLOWED` | Activer le mode range en tendance | `1` |
| `ASIA_TRENDING_RANGE_MIN_RR` | R:R minimum pour les entrées en range tendance | `1.0` |
| `ASIA_TRENDING_RANGE_MAX_RR` | R:R maximum pour les entrées en range tendance | `1.5` |
| `ASIA_TRENDING_RANGE_RISK_MULT` | Multiplicateur de risque (0.75 = 75% du normal) | `0.75` |

**Exemple de log (amélioré) :**
```
[ASIA_STRAT] BTCUSDT | RANGE_OK | mode=NORMAL_RANGE | ratio=5.2 | threshold=6.0 | allowed_by=BTC_PROFILE
[ASIA_STRAT] ETHUSDT | REJECTED | reason=RANGE_TOO_LARGE (ratio=6.5 > 5.0, source=ETH_PROFILE) | allowed_by=ETH_PROFILE
[ASIA_STRAT] XRPUSDT | RANGE_OK | mode=TRENDING_RANGE | ratio=5.8 | threshold=4.0 | allowed_by=ALT_WEEKEND
```

---

## 🧠 6. Stratégie SMC : Biais & Sweeps
| Variable | Description | Défaut | Recommandé (Scalp) |
| :--- | :--- | :--- | :--- |
| `SWEEP_LOOKBACK` | Bougies à remonter pour la détection des Hauts/Bas. Plus bas = réaction plus rapide. | `50` | `20` |
| `SWEEP_MIN_WICK_ATR` | Taille min de mèche (multiple ATR) pour un sweep valide. | `0.3` | `0.1` |
| `SWEEP_ALLOW_EQUAL_HL` | Autoriser les Equal Highs/Lows comme pools de liquidité ? | `true` | `true` |
| `SMC_NEUTRAL_PERMISSIVE` | Autoriser le trading en biais HTF neutre si le setup local est fort ? | `true` | `true` |

---

### 5b. Contournement souple du Sweep (Nouveau)
Autoriser l'entrée **sans** Liquidity Sweep si des conditions spécifiques sont remplies (BOS + Order Block).

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `ALLOW_NO_SWEEP_WITH_OB` | Activer le mode "Contournement souple" (BOS + OB frais). | `false` |
| `NO_SWEEP_MIN_RR` | RR minimum requis pour ce mode de contournement. | `1.5` |
| `NO_SWEEP_REQUIRE_FRESH_OB` | L'Order Block doit-il être "frais" (non mitigé/récent) ? | `true` |
| `NO_SWEEP_BLOCK_STRONG_HTF` | Bloquer le contournement si le biais HTF est fort (>0.7) et opposé ? | `true` |

---

## 🎯 6. Stratégie SMC : Zones d'entrée & Replis
| Variable | Description | Défaut | Config active |
| :--- | :--- | :--- | :--- |
| `FORCE_ENTRY_ZONE` | **DEBUG UNIQUEMENT**. Contourne complètement le check `NO_ENTRY_ZONE`. | `false` | `false` |
| `SWEEP_FALLBACK_ENABLED` | Autoriser l'entrée sans Sweep si la Structure (BOS) est forte ? | `true` | `true` |
| `SWEEP_FALLBACK_MAX_FVG_AGE` | Âge max (bougies) des zones FVG en mode fallback. | `10` | `30` |
| `FALLBACK_ALLOW_NO_FVG` | Autoriser l'entrée sur simple OB (sans FVG) en mode fallback ? | `false` | `true` |
| `ENTRY_ZONE_ACCEPT_NONFRESH_FVG` | Autoriser la ré-entrée sur des zones mitigées (touchées). | `false` | `true` |
| `ENTRY_ZONE_PADDING_ATR` | Zone tampon autour des niveaux POI (multiple ATR). | `0.0` | `0.1` |
| `ENTRY_ZONE_FALLBACK_MODE` | Activer une zone synthétique basée sur l'ATR si aucun POI trouvé. | `false` | `true` |

---

## 🖥️ 7. Frontend & Système
| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `VITE_API_URL` | URL de l'API pour le frontend React. | `http://localhost:8000` |
| `VITE_WS_URL` | URL WebSocket pour le frontend React. | `ws://localhost:8000` |

---

## 🌡️ 8. Filtre de Volatilité Globale (NOUVEAU)

Évite le trading sur les marchés "morts" (ATR trop faible). Filtre les faux sweeps et micro-ranges.

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `VOLATILITY_FILTER_ENABLED` | Activer le filtre de volatilité (skip si ATR trop bas) | `true` |
| `VOLATILITY_FILTER_MIN_ATR_RATIO` | Ratio min ATR vs moyenne (0.4 = ATR >= 40% de la moyenne) | `0.4` |

**Comportement :** Si `ATR_actuel / ATR_moyen < 0.4` → signal rejeté avec raison `DEAD_MARKET_LOW_VOLATILITY`.

---

## 📐 9. Position Sizing Pondéré par la Confiance (NOUVEAU)

Module la taille de position en fonction du score de confiance du signal. Meilleur signal = plus gros size.

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `CONFIDENCE_SIZING_ENABLED` | Activer le sizing pondéré par la confiance | `true` |
| `CONFIDENCE_SIZING_MIN_MULT` | Multiplicateur plancher (confiance basse) | `0.5` |
| `CONFIDENCE_SIZING_MAX_MULT` | Multiplicateur plafond (confiance haute) | `1.5` |

**Formule :** `risk_effectif = risk_pct × (min_mult + (max_mult - min_mult) × confidence_score)`

**Exemples :**
| Confidence | Multiplicateur | Risk base 1% → Risk effectif |
| :--- | :--- | :--- |
| 0.3 | 0.80 | 0.80% |
| 0.5 | 1.00 | 1.00% |
| 0.7 | 1.20 | 1.20% |
| 1.0 | 1.50 | 1.50% |

---

## 🔄 10. Lookback Adaptatif du Range (NOUVEAU)

Adapte dynamiquement la fenêtre de détection de range selon la volatilité. Haute volatilité → lookback court (réaction rapide). Basse volatilité → lookback long (range stable).

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `ADAPTIVE_RANGE_LOOKBACK_ENABLED` | Activer le lookback adaptatif | `true` |
| `ADAPTIVE_RANGE_LOOKBACK_MIN` | Minimum de bougies pour le lookback | `8` |
| `ADAPTIVE_RANGE_LOOKBACK_MAX` | Maximum de bougies pour le lookback | `30` |

**Comportement :**
- ATR actuel = 2× moyenne → lookback = 8 bougies (détection rapide)
- ATR actuel = 1× moyenne → lookback = 15 bougies (normal)
- ATR actuel = 0.5× moyenne → lookback = 30 bougies (stable)

---

## 🔗 11. Filtre de Corrélation (NOUVEAU)

Réduit progressivement le risque quand plusieurs actifs corrélés ont des positions ouvertes. Bloque les entrées au-delà de `MAX_POSITIONS` dans le même groupe.

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `CORRELATION_FILTER_ENABLED` | Activer le filtre de corrélation | `true` |
| `CORRELATION_RISK_MULT` | Multiplicateur de risque quand 1 corrélé déjà ouvert (0.4 = −60%) | `0.4` |
| `CORRELATION_MAX_POSITIONS` | Nombre max de positions corrélées simultanées | `2` |
| `CORRELATION_GROUPS` | Groupes d'actifs corrélés. Format: `name:SYM1,SYM2\|name2:SYM3,SYM4` | `majors:BTCUSDT,ETHUSDT\|alts:SOLUSDT,XRPUSDT,ADAUSDT` |

**Comportement :**
- 0 corrélé ouvert → risk × 1.0 (normal)
- 1 corrélé ouvert → risk × 0.4 (−60%)
- 2+ corrélés ouverts → signal **bloqué** (CORRELATION_BLOCKED)
- Groupes séparés : `majors` (BTC, ETH) et `alts` (SOL, XRP, ADA) sont évalués indépendamment

---

## 📏 12. Protection Slippage (MISE À JOUR)

Mesure le slippage entre le prix d'entrée prévu et le prix de fill réel. **Kill switch** : ferme immédiatement la position si le slippage dépasse le seuil critique.

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `MAX_ENTRY_SLIPPAGE_ATR` | Seuil max de slippage en multiple d'ATR (warning) | `0.3` |
| `MAX_ENTRY_SLIPPAGE_BPS` | Seuil max de slippage en basis points (warning) | `30` |
| `SLIPPAGE_KILL_THRESHOLD_BPS` | Seuil kill switch : fermeture d'urgence si slippage > N bps (0 = désactivé) | `60` |
| `SLIPPAGE_KILL_COOLDOWN_SEC` | Cooldown après kill switch (secondes) — empêche boucle re-trade | `300` |

**Comportement :**
- Si `|fill - entry| > 0.3 × ATR` ou `> 30 bps` → log WARNING `[SLIPPAGE]`
- Si `slippage > 60 bps` → **EMERGENCY CLOSE** immédiat de la position `[SLIPPAGE_KILL]`
- Après un kill switch : **trading pausé 5 minutes** (anti-boucle)
- Le champ `slippage_bps` et `slippage_exceeded` sont ajoutés au résultat d'exécution

---

## ⏱️ 12b. Execution Latency Guard (NOUVEAU)

Annule les trades dont le signal est trop ancien — le marché a déjà bougé.

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `MAX_SIGNAL_AGE_SEC` | Âge max du signal avant annulation (secondes, 0 = désactivé) | `5.0` |

**Comportement :**
- `signal_age = now - t_signal`
- Si `signal_age > 5s` → **HARD REJECT** (SIGNAL_TOO_OLD)
- Non bypassable par FORCE_EXECUTE

---

## ⚡ 13. Filtre Volatilité Extrême (MISE À JOUR)

Bloque les entrées quand la volatilité dépasse un seuil critique (ATR actuel >> moyenne). Complémentaire au filtre de marché mort (section 8) qui bloque quand ATR est trop *bas*.

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `EXTREME_VOLATILITY_FILTER_ENABLED` | Activer le filtre de volatilité extrême | `true` |
| `EXTREME_VOLATILITY_MAX_ATR_RATIO` | Ratio max ATR actuel / ATR moyen avant blocage | `2.5` |
| `ATR_MEAN_WINDOW` | Fenêtre glissante pour le calcul de l'ATR moyen (bougies) | `100` |

**Comportement :**
- ATR actuel / ATR moyen ≤ 2.5 → OK (trading normal)
- ATR actuel / ATR moyen > 2.5 → **HARD REJECT** (EXTREME_VOLATILITY)
- Non bypassable par FORCE_EXECUTE (sécurité hard)
- La fenêtre de 100 bougies lisse les variations et réduit les faux positifs

---

## 🎯 14. Regime Filter (NOUVEAU)

Détecte le régime de marché (TREND / RANGE / HIGH_VOL / LOW_VOL) et bloque ou ajuste la confiance des signaux en fonction de la compatibilité stratégie ↔ régime.

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `REGIME_FILTER_ENABLED` | Activer la détection de régime | `true` |
| `REGIME_TREND_THRESHOLD` | Nombre de HH/HL ou LH/LL consécutifs pour déclarer TREND | `4` |
| `REGIME_HIGH_VOL_ATR_RATIO` | ATR ratio above which → HIGH_VOL regime | `2.0` |
| `REGIME_LOW_VOL_ATR_RATIO` | ATR ratio below which → LOW_VOL regime | `0.5` |
| `REGIME_LOOKBACK` | Lookback pour le calcul de force directionnelle | `20` |
| `REGIME_COMPRESSION_RATIO` | Range compression : range/ATR < seuil → breakout imminent (aide TREND) | `1.0` |

**Détection de régime :**
1. **ATR ratio** (prioritaire) : HIGH_VOL ou LOW_VOL si ratio dépasse les seuils
2. **Force directionnelle** : HH/HL ou LH/LL consécutifs ≥ seuil → TREND
3. **Range compression** : si range des 5 dernières bougies / ATR < 1.0 ET dir_strength ≥ seuil-1 → TREND (breakout imminent)
4. Sinon → RANGE

**Matrice de gating :**
| Régime | DefaultSMC (trend) | AsiaWeekend (mean reversion) |
| :--- | :--- | :--- |
| TREND | ✅ confiance × 1.0 | ❌ **BLOQUÉ** — reversion en trend = pertes |
| RANGE | ⚠️ confiance × 0.8 | ✅ confiance × 1.0 |
| HIGH_VOL | ❌ **BLOQUÉ** — volatilité extrême | ❌ **BLOQUÉ** |
| LOW_VOL | ⚠️ confiance × 0.7 — R:R dégradé | ⚠️ confiance × 0.7 |

---

## 🪤 14b. Liquidity Trap Filter (NOUVEAU)

Bloque l'entrée si le prix est trop proche d'un cluster de liquidité à forte densité. Évite les stop hunts.

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `LIQUIDITY_TRAP_MIN_ATR_DIST` | Distance min en ATR entre entry et cluster (0 = désactivé) | `0.3` |

**Comportement :**
- Parcourt les niveaux de heatmap avec score ≥ 4.0
- Si `|entry - cluster_price| / ATR < 0.3` → **BLOCKED** `[LIQUIDITY_TRAP]`
- Protège contre : entry → stop hunt → SL touché → reversal sans nous

---

## 🧠 15. Liquidity Heatmap

Construit une carte de liquidité scorée en temps réel. Détecte où les stops sont accumulés (clusters de prix, breakout pools, niveaux HTF) et attribue un score composite à chaque niveau. La stratégie utilise la heatmap pour :
- **Booster la confiance** quand le trade pointe vers un major target (+15% si score ≥ 6)
- **Identifier les cibles probables** du marché (sweep → reversal)

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `LIQUIDITY_HEATMAP_ENABLED` | Activer la heatmap de liquidité | `true` |
| `LIQUIDITY_HEATMAP_CLUSTER_PCT` | Tolérance % pour grouper les prix en cluster | `0.001` (0.1%) |
| `LIQUIDITY_HEATMAP_MIN_CLUSTER_SIZE` | Nombre min de touches pour former un cluster | `3` |
| `LIQUIDITY_HEATMAP_BREAKOUT_SL_ATR` | Offset ATR pour estimer les SL des breakout traders | `0.5` |
| `LIQUIDITY_HEATMAP_MAX_LEVELS` | Nombre max de niveaux scorés conservés | `20` |
| `LIQUIDITY_WEIGHT_EQUAL_HL` | Poids des equal highs/lows dans le score | `2.0` |
| `LIQUIDITY_WEIGHT_CLUSTER` | Poids de la densité de cluster dans le score | `3.0` |
| `LIQUIDITY_WEIGHT_BREAKOUT` | Poids des breakout pools dans le score | `1.5` |
| `LIQUIDITY_WEIGHT_HTF` | Poids des niveaux HTF swing dans le score | `2.0` |

**Formule de scoring :**
```
score = equal_hl × 2.0 + cluster_density × 3.0 + breakout × 1.5 + htf_swing × 2.0
```

**Exemple :**
| Prix | Score | Composants |
| :--- | :--- | :--- |
| 69000 | 9.2 | cluster=3×3.0 + htf_swing=2.0 |
| 68800 | 7.5 | equal_hl=2×2.0 + cluster=2×3.0 - breakout |
| 68200 | 3.1 | cluster=1×3.0 |

**Comportement :**
- Score ≥ 6.0 → confidence boost +15%, tag `HEATMAP_TARGET`
- Score 3.0-5.9 → confidence boost +5%
- Score < 3.0 → pas de boost
- Niveaux swept (prix dépassé) → automatiquement retirés

---

## 🎛️ 16. Adaptive Risk Engine — Phase 7 (NOUVEAU)

Moteur de risque adaptatif institutionnel. Ajuste automatiquement le risque par trade, le levier effectif et le multiplicateur de taille selon 5 facteurs : volatilité, drawdown récent, performance récente (WR/PF), régime de marché, qualité d'exécution.

**Principe :** Le multiplicateur ne dépasse jamais 1.0 — l'ARE ne peut que *réduire* le risque, jamais l'augmenter.

### Modes de risque
| Mode | Multiplicateur | Description |
| :--- | :--- | :--- |
| `normal` | 0.70 – 1.00 | Conditions normales |
| `reduced` | 0.50 – 0.70 | Un facteur dégradé — réduction légère |
| `defensive` | 0.25 – 0.50 | Plusieurs facteurs dégradés — réduction forte, levier ÷2 |
| `blocked` | 0.00 | Conditions extrêmes — trade refusé |

### Configuration générale

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `ADAPTIVE_RISK_ENABLED` | Activer le moteur de risque adaptatif | `true` |
| `ADAPTIVE_RISK_MIN_MULT` | Multiplicateur plancher (jamais en dessous) | `0.25` |
| `ADAPTIVE_RISK_MAX_MULT` | Multiplicateur plafond (jamais au-dessus, ≤ 1.0) | `1.00` |
| `ADAPTIVE_RISK_DEFENSIVE_MULT` | Seuil de passage en mode `defensive` | `0.50` |

### Facteur 1 : Volatilité (ATR ratio)

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `ADAPTIVE_RISK_ATR_REDUCED` | ATR ratio au-delà duquel → mode `reduced` | `1.5` |
| `ADAPTIVE_RISK_ATR_DEFENSIVE` | ATR ratio au-delà duquel → mode `defensive` | `2.0` |
| `ADAPTIVE_RISK_ATR_BLOCK` | ATR ratio au-delà duquel → mode `blocked` | `2.8` |
| `ADAPTIVE_RISK_BLOCK_ON_EXTREME_VOL` | Activer le blocage en volatilité extrême | `true` |

### Facteur 2 : Drawdown récent

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `ADAPTIVE_RISK_DD_REDUCED_PCT` | Drawdown % → mode `reduced` | `1.5` |
| `ADAPTIVE_RISK_DD_DEFENSIVE_PCT` | Drawdown % → mode `defensive` | `2.5` |
| `ADAPTIVE_RISK_DD_BLOCK_PCT` | Drawdown % → mode `blocked` | `3.5` |

### Facteur 3 : Performance récente (Win Rate / Profit Factor)

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `ADAPTIVE_RISK_WR_WINDOW` | Nombre de trades récents pour le calcul WR/PF | `20` |
| `ADAPTIVE_RISK_MIN_WR` | Win rate minimum (en dessous → réduction proportionnelle) | `0.45` |
| `ADAPTIVE_RISK_MIN_PF` | Profit factor minimum (en dessous → mode `reduced`) | `1.05` |

### Facteur 4 : Régime de marché

Active automatiquement des réductions de risque pour certaines combinaisons stratégie/régime.

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `ADAPTIVE_RISK_REDUCE_IN_LOW_VOL` | Réduire le risque en régime LOW_VOL | `true` |
| `ADAPTIVE_RISK_REDUCE_IN_RANGE_FOR_BREAKOUT` | Réduire si RANGE + stratégie breakout | `true` |
| `ADAPTIVE_RISK_REDUCE_IN_TREND_FOR_MEANREV` | Réduire si TREND + stratégie mean-reversion | `true` |

### Facteur 5 : Qualité d'exécution récente

| Variable | Description | Défaut |
| :--- | :--- | :--- |
| `ADAPTIVE_RISK_MAX_RECENT_KILLS` | Seuil de slippage kills récents → mode `blocked` | `2` |
| `ADAPTIVE_RISK_MAX_RECENT_SLIPPAGE_BPS` | Slippage max en bps (au-delà → réduction) | `35.0` |
| `ADAPTIVE_RISK_MAX_RECENT_FILL_DELAY_MS` | Délai de fill max en ms (au-delà → réduction) | `2000.0` |
| `ADAPTIVE_RISK_BLOCK_ON_EXECUTION_DEGRADE` | Activer le blocage si trop de kills | `true` |

### Intégration

L'ARE s'insère dans `OrderManager.execute()` entre `pre_flight_checks()` et `execute_signal()`. Le multiplicateur ARE est combiné multiplicativement avec le `risk_multiplier` de Phase 5 (capital allocator) :
```
effective_mult = risk_multiplier_P5 × adaptive_size_mult_P7
```

Le levier est réduit de moitié en mode `defensive` :
```
effective_leverage = leverage ÷ 2  (si mode == defensive)
```

### Endpoint API

`GET /api/analytics/adaptive-risk` — Retourne les décisions ARE agrégées (total, répartition par mode, raisons de blocage, statistiques multiplier).
