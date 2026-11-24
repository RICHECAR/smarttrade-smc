# 📊 Trading Daily Report — {{DATE}}

**Bot :** bitget-smc-auto-trader
**Environnement :** {{paper | live}}
**Timezone :** Europe/Paris
**Période analysée :** {{start_ts}} → {{end_ts}}

---

### 🧠 Résumé exécutif

* **Trades exécutés :** {{n_trades}}
* **PnL net :** {{pnl_abs}} ({{pnl_pct}}%)
* **Win Rate :** {{winrate}}%
* **Max Drawdown :** {{max_dd}}%
* **Statut des seuils Asia :** {{ACTIVE | UNDER_REVIEW | ROLLBACK_RECOMMENDED}}

---

### 🕒 Activité & Sessions

| Session | Trades | Winrate | PnL  |
| ------- | ------ | ------- | ---- |
| Asia    | {{}}   | {{}}    | {{}} |
| London  | {{}}   | {{}}    | {{}} |
| NY      | {{}}   | {{}}    | {{}} |

---

### 🚫 Rejets & Filtres

| Raison          | Occurrences |
| --------------- | ----------- |
| RANGE_TOO_LARGE | {{range_count}} |
| STALE_REJECT    | {{stale_count}} |
| NO_VALID_SETUP  | {{setup_count}} |
| RISK_BLOCKED    | {{risk_count}} |

---

### 📈 Observabilité Balance

* **Equity start :** {{equity_start}}
* **Equity end :** {{equity_end}}
* **Δ 24h :** {{delta_abs}} ({{delta_pct}}%)
* **7j change :** {{7d_pct}}%
* **30j change :** {{30d_pct}}%

---

```yaml
ASIA_MAX_RANGE_RATIO:
  BTC: 6.4
  ETH: 5.2
  ALT: 5.7
ASIA_RISK_FACTOR: 0.5
ADAPTIVE_MODE: {{mode}}
```

---

## 🧠 Adaptive Strategy & A/B Testing (Phase 8)

### 🔁 Mode & Exposition
- **Adaptive Mode :** {{adaptive_mode}}
- **Adaptive Live Weight :** {{adaptive_weight}}%
- **Last Weight Change :** {{last_weight_change_ts}} ({{last_weight_decision}})

---

### 🧪 A/B Comparison (Session Asia)

| Metric | Static (A) | Adaptive (B) | Δ |
|------|------------|--------------|---|
| Decisions | {{A_decisions}} | {{B_decisions}} | — |
| Accepts | {{static_accepts}} | {{adaptive_accepts}} | {{delta_accepts}} |
| Conflicts | — | — | {{conflicts}} ({{conflict_rate}}%) |

---

### 🧮 Decision Engine Output
- **Decision :** {{weight_decision}}
- **Confidence Level :** {{confidence_level}}
- **Reason :** {{decision_reason}}

---

### 🔐 Safety Status
- **Auto-Rollback Armed :** YES
- **Thresholds Status :** {{threshold_status}}
- **Last Rollback :** {{last_rollback_date}}

---

### 📌 Recommendation
{{free_text_recommendation}}

---

## 🛡️ Kill-Switch Status (Phase 8.2)

| Paramètre | Valeur |
|-----------|--------|
| **Daily Loss %** | {{ks_daily_loss_pct}}% / {{ks_max_daily_loss_pct}}% |
| **Daily Loss Abs** | {{ks_daily_pnl}} / -{{ks_max_daily_loss_abs}} |
| **Session ASIA Loss %** | {{ks_session_loss_pct_asia}}% / {{ks_max_session_loss_pct_asia}}% |
| **Consecutive Losses ASIA** | {{ks_consec_losses}} / {{ks_max_consec_losses}} |
| **Block Events Today** | {{ks_block_count}} |
| **Status** | {{ks_status}} |

---

## 📐 Capital Scaling (Phase 8.3)

| Paramètre | Valeur |
|-----------|--------|
| **Risk per Trade** | {{sc_current_risk_pct}}% |
| **Effective Max** | {{sc_eff_max_risk}}% |
| **Decision** | {{sc_decision}} |
| **Reason** | {{sc_reason}} |
| **30d Profit Factor** | {{sc_pf_30d}} |
| **30d Max Drawdown** | {{sc_dd_30d}}% |
| **30d Trades** | {{sc_trades_30d}} |

---

## ⚠️ Signaux Acceptés mais Bloqués à l'Exécution

| Raison de Blocage | Occurrences |
|-------------------|-------------|
| SPREAD_TOO_TIGHT (Maker→Taker fallback) | {{eb_spread_tight}} |
| GLOBAL_RISK | {{eb_global_risk}} |
| INSUFFICIENT_BALANCE | {{eb_insufficient_balance}} |
| STALE_DATA | {{eb_stale_data}} |
| OTHER | {{eb_other}} |

**Total signaux bloqués :** {{eb_total}}

---

## ⚖️ Execution Quality — Maker vs Taker

| Metric | Maker | Taker (Fallback) |
|--------|-------|------------------|
| Trades | {{eq_maker_trades}} | {{eq_taker_trades}} |
| Win Rate | {{eq_maker_wr}}% | {{eq_taker_wr}}% |
| Profit Factor | {{eq_maker_pf}} | {{eq_taker_pf}} |
| Avg R | {{eq_maker_avg_r}} | {{eq_taker_avg_r}} |
| Avg Fees | {{eq_maker_fees}} | {{eq_taker_fees}} |
| Net PnL | {{eq_maker_pnl}} | {{eq_taker_pnl}} |
| Avg Slippage (bps) | {{eq_maker_slip}} | {{eq_taker_slip}} |

- **Fallback Rate :** {{eq_fallback_rate}}%
- **TAKER_DEGRADING_EDGE :** {{eq_taker_flag}}
- **Warnings :** {{eq_warnings}}

---

## 🧭 Regime Awareness (Phase 9)

| Paramètre | Valeur |
|-----------|--------|
| **Market Regime** | {{rg_regime}} |
| **Regime Reason** | {{rg_reason}} |
| **Effective Regime** | {{rg_effective}} |
| **Volatility State** | {{rg_vol_state}} |
| **Vol Pause Active** | {{rg_vol_pause}} |
| **Recovery Active** | {{rg_recovery}} |
| **Recovery Step** | {{rg_recovery_step}} / {{rg_recovery_total}} |
| **Adaptive Weight Cap** | {{rg_adaptive_cap}}% |
| **Scaling Cap Factor** | {{rg_scale_cap}} |
| **Risk Reduction Factor** | {{rg_risk_factor}} |
| **Freeze Adaptive** | {{rg_freeze}} |
| **Block Scale-Up** | {{rg_block_scale}} |
| **Warnings** | {{rg_warnings}} |

---

### ⚙️ Diagnostic automatique

* {{OK | WARNING | CRITICAL}} — {{reason}}

---

*Rapport généré automatiquement — {{commit_hash}}*
