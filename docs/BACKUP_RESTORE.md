# BACKUP & RESTORE — Procédures Opérationnelles
## Phase 11

---

## Architecture

```
data/backups/
├── manifest.json            # Registre de tous les backups
├── trades_backup_*.db       # Backups SQLite (hot copy)
├── event_wal_*.jsonl        # Backups du Write-Ahead Log
└── pre_restore_*.db         # Safety copies avant restore
```

**Source:** `data/trades.db` (SQLite, WAL mode, PRAGMA synchronous=FULL)

---

## Créer un Backup

```bash
python scripts/go_live_check.py --backup
```

Ou en Python:
```python
from core.backup_manager import get_backup_manager
mgr = get_backup_manager()
record = mgr.create_backup()
print(f"Backup: {record.filename} ({record.size_bytes} bytes, integrity={record.integrity_ok})")
```

**Ce qui est sauvegardé:**
- `data/trades.db` → copie hot via sqlite3 backup API
- `logs/event_wal.jsonl` → copie WAL (si existe)
- Métadonnées: SHA-256, timestamp, integrity check

---

## Vérifier un Backup

```python
from core.backup_manager import get_backup_manager
mgr = get_backup_manager()
result = mgr.verify_backup("trades_backup_20250101_120000.db")
print(result)
# {"exists": True, "integrity_ok": True, "hash_match": True, "size_bytes": 72704}
```

**Vérifications:**
1. Le fichier existe
2. PRAGMA integrity_check = "ok"
3. SHA-256 correspond au manifest

---

## Restaurer un Backup

```python
from core.backup_manager import get_backup_manager
mgr = get_backup_manager()

# Lister les backups disponibles
for b in mgr.list_backups():
    print(f"{b['filename']} | {b['timestamp']} | integrity={b['integrity_ok']}")

# Restaurer (crée automatiquement une safety copy du DB actuel)
result = mgr.restore_backup("trades_backup_20250101_120000.db")
print(result)
```

**Sécurités:**
- Vérifie l'intégrité du backup AVANT de restaurer
- Crée une copie `pre_restore_*.db` du DB actuel
- Refuse les backups corrompus

---

## Rotation Automatique

- Par défaut: **10 backups maximum** (`BACKUP_MAX_COUNT=10`)
- Les plus anciens sont supprimés automatiquement
- Le manifest est mis à jour à chaque opération

---

## Configuration (.env)

```env
BACKUP_DIR=data/backups        # Répertoire des backups
BACKUP_MAX_COUNT=10            # Nombre max de backups conservés
BACKUP_MIN_FREE_MB=500         # Espace disque minimum requis
```

---

## Bonnes Pratiques

1. **Backup avant chaque mise à jour** du code ou de la config
2. **Backup avant chaque changement de tier** de capital
3. **Vérifier l'intégrité** après chaque backup automatique
4. **Tester le restore** au moins une fois par semaine
5. **Copier les backups** sur un stockage externe périodiquement
6. **Ne jamais supprimer** `manifest.json` manuellement

---

## Fréquence Recommandée

| Tier | Fréquence Backup | Rétention |
|------|-------------------|-----------|
| PAPER | Hebdomadaire | 3 backups |
| TIER_1 | Quotidien | 7 backups |
| TIER_2 | 2x/jour | 10 backups |
| TIER_3+ | 4x/jour + avant chaque trade batch | 20 backups |
