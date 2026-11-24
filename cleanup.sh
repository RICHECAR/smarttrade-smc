#!/usr/bin/env bash
###############################################################################
# cleanup.sh — Trading Bot Maintenance Script
# 
# Usage:
#   ./cleanup.sh              # Mode SAFE (par défaut)
#   ./cleanup.sh --aggressive # Mode AGRESSIF (confirmation requise)
#   ./cleanup.sh --dry-run    # Simulation (aucune suppression)
#   ./cleanup.sh --dry-run --aggressive  # Simulation mode agressif
#
# Exécution cron recommandée :
#   0 3 */30 * * /chemin/vers/cleanup.sh >> /chemin/vers/cleanup_cron.log 2>&1
###############################################################################

set -euo pipefail

# ─── Configuration ───────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLEANUP_LOG="${PROJECT_DIR}/cleanup.log"
LOG_RETENTION_DAYS=30
MAX_LOG_SIZE_MB=100
MIN_DISK_FREE_GB=10
DRY_RUN=false
AGGRESSIVE=false
TOTAL_FREED=0

# ─── Fichiers/dossiers PROTÉGÉS (ne jamais supprimer) ───────────────────────
PROTECTED_FILES=(
    ".env"
    ".env.local"
    ".env.example"
    ".env.backup.pre-safety"
    "safe-live.env"
    "run.py"
    "package.json"
    "package-lock.json"
    "tsconfig.json"
    "vite.config.ts"
    "docker-compose.prod.yml"
    "pytest.ini"
    "trades.db"
    "README.md"
    "CHANGELOG.md"
)

PROTECTED_DIRS=(
    "backend"
    "core"
    "services"
    "config"
    "hooks"
    "components"
    "pages"
    "notifications"
    "observability"
    "orchestrator"
    "portfolio"
    "smc_futures"
    "scripts"
    "tools"
    "systemd"
    "docs"
    ".git"
)

# ─── Parsing des arguments ───────────────────────────────────────────────────
for arg in "$@"; do
    case $arg in
        --dry-run)   DRY_RUN=true ;;
        --aggressive) AGGRESSIVE=true ;;
        --help|-h)
            head -15 "$0" | tail -12
            exit 0
            ;;
        *)
            echo "❌ Argument inconnu: $arg"
            echo "Usage: $0 [--dry-run] [--aggressive]"
            exit 1
            ;;
    esac
done

# ─── Fonctions utilitaires ───────────────────────────────────────────────────
timestamp() { date '+%Y-%m-%d %H:%M:%S'; }

log() {
    local msg="[$(timestamp)] $1"
    echo "$msg"
    echo "$msg" >> "$CLEANUP_LOG"
}

log_section() {
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "  $1"
    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

get_size_bytes() {
    if [ -f "$1" ]; then
        stat -f%z "$1" 2>/dev/null || stat -c%s "$1" 2>/dev/null || echo 0
    elif [ -d "$1" ]; then
        du -sb "$1" 2>/dev/null | cut -f1 || echo 0
    else
        echo 0
    fi
}

bytes_to_human() {
    local bytes=$1
    if [ "$bytes" -ge 1073741824 ]; then
        echo "$(echo "scale=2; $bytes/1073741824" | bc) GB"
    elif [ "$bytes" -ge 1048576 ]; then
        echo "$(echo "scale=2; $bytes/1048576" | bc) MB"
    elif [ "$bytes" -ge 1024 ]; then
        echo "$(echo "scale=2; $bytes/1024" | bc) KB"
    else
        echo "${bytes} B"
    fi
}

safe_delete_file() {
    local file="$1"
    local basename
    basename=$(basename "$file")

    # Vérifier si le fichier est protégé
    for pf in "${PROTECTED_FILES[@]}"; do
        if [ "$basename" = "$pf" ]; then
            log "  🛡️  PROTÉGÉ (ignoré): $file"
            return 0
        fi
    done

    local size
    size=$(get_size_bytes "$file")

    if [ "$DRY_RUN" = true ]; then
        log "  [DRY-RUN] Supprimerait: $file ($(bytes_to_human "$size"))"
    else
        rm -f "$file"
        log "  ✅ Supprimé: $file ($(bytes_to_human "$size"))"
    fi
    TOTAL_FREED=$((TOTAL_FREED + size))
}

safe_delete_dir() {
    local dir="$1"
    local dirname
    dirname=$(basename "$dir")

    # Vérifier si le dossier est protégé
    for pd in "${PROTECTED_DIRS[@]}"; do
        if [ "$dirname" = "$pd" ]; then
            log "  🛡️  PROTÉGÉ (ignoré): $dir"
            return 0
        fi
    done

    local size
    size=$(get_size_bytes "$dir")

    if [ "$DRY_RUN" = true ]; then
        log "  [DRY-RUN] Supprimerait dossier: $dir ($(bytes_to_human "$size"))"
    else
        rm -rf "$dir"
        log "  ✅ Supprimé dossier: $dir ($(bytes_to_human "$size"))"
    fi
    TOTAL_FREED=$((TOTAL_FREED + size))
}

# ─── Vérification espace disque ─────────────────────────────────────────────
check_disk_space() {
    log_section "VÉRIFICATION ESPACE DISQUE"
    local free_kb
    free_kb=$(df -k "$PROJECT_DIR" | tail -1 | awk '{print $4}')
    local free_gb=$((free_kb / 1048576))
    log "Espace libre actuel: ${free_gb} GB"
    if [ "$free_gb" -lt "$MIN_DISK_FREE_GB" ]; then
        log "⚠️  ATTENTION: Espace disque critique (<${MIN_DISK_FREE_GB} GB)"
    fi
}

# ─── Étape 1: Nettoyage caches Python ───────────────────────────────────────
clean_python_caches() {
    log_section "NETTOYAGE CACHES PYTHON"

    # __pycache__
    local count=0
    while IFS= read -r -d '' dir; do
        safe_delete_dir "$dir"
        count=$((count + 1))
    done < <(find "$PROJECT_DIR" -type d -name "__pycache__" -not -path "*/.venv/*" -not -path "*/node_modules/*" -print0 2>/dev/null)
    log "  Total __pycache__ traités: $count"

    # .pyc isolés
    find "$PROJECT_DIR" -type f -name "*.pyc" -not -path "*/.venv/*" -not -path "*/node_modules/*" -print0 2>/dev/null | \
    while IFS= read -r -d '' file; do
        safe_delete_file "$file"
    done

    # .pytest_cache
    while IFS= read -r -d '' dir; do
        safe_delete_dir "$dir"
    done < <(find "$PROJECT_DIR" -type d -name ".pytest_cache" -not -path "*/.venv/*" -print0 2>/dev/null)

    # mypy / ruff caches
    for cache_dir in ".mypy_cache" ".ruff_cache" ".cache"; do
        local target="${PROJECT_DIR}/${cache_dir}"
        if [ -d "$target" ]; then
            safe_delete_dir "$target"
        fi
    done

    log "  ✔ Caches Python nettoyés"
}

# ─── Étape 2: Nettoyage logs > 30 jours ─────────────────────────────────────
clean_old_logs() {
    log_section "NETTOYAGE LOGS > ${LOG_RETENTION_DAYS} JOURS"

    local log_dirs=("${PROJECT_DIR}/logs" "${PROJECT_DIR}/reports")
    for log_dir in "${log_dirs[@]}"; do
        if [ -d "$log_dir" ]; then
            log "  Scan: $log_dir"
            find "$log_dir" -type f \( -name "*.log" -o -name "*.jsonl" -o -name "*.txt" \) \
                -mtime +${LOG_RETENTION_DAYS} -print0 2>/dev/null | \
            while IFS= read -r -d '' file; do
                safe_delete_file "$file"
            done
        fi
    done

    # Logs à la racine du projet
    find "$PROJECT_DIR" -maxdepth 1 -type f \( -name "*.log" -o -name "orchestrator*.log" \) \
        -mtime +${LOG_RETENTION_DAYS} -print0 2>/dev/null | \
    while IFS= read -r -d '' file; do
        safe_delete_file "$file"
    done

    log "  ✔ Logs anciens nettoyés"
}

# ─── Étape 3: Rotation des gros logs ────────────────────────────────────────
rotate_large_logs() {
    log_section "ROTATION DES LOGS > ${MAX_LOG_SIZE_MB} MB"

    local max_bytes=$((MAX_LOG_SIZE_MB * 1048576))

    find "$PROJECT_DIR" -type f \( -name "*.log" -o -name "*.jsonl" \) \
        -not -path "*/.venv/*" -not -path "*/node_modules/*" 2>/dev/null | \
    while IFS= read -r file; do
        local size
        size=$(get_size_bytes "$file")
        if [ "$size" -gt "$max_bytes" ]; then
            if [ "$DRY_RUN" = true ]; then
                log "  [DRY-RUN] Tronquerait: $file ($(bytes_to_human "$size") → garder derniers 10MB)"
            else
                # Garder les derniers 10 MB du fichier
                local keep_bytes=$((10 * 1048576))
                tail -c "$keep_bytes" "$file" > "${file}.tmp" && mv "${file}.tmp" "$file"
                log "  🔄 Tronqué: $file ($(bytes_to_human "$size") → 10 MB)"
            fi
            TOTAL_FREED=$((TOTAL_FREED + size - 10485760))
        fi
    done

    log "  ✔ Rotation terminée"
}

# ─── Étape 4: Nettoyage fichiers temporaires ────────────────────────────────
clean_temp_files() {
    log_section "NETTOYAGE FICHIERS TEMPORAIRES"

    # Fichiers de sortie de tests à la racine
    find "$PROJECT_DIR" -maxdepth 1 -type f \( \
        -name "test_out*.txt" -o \
        -name "test_err*.txt" -o \
        -name "test_result*.txt" -o \
        -name "test_output*.txt" -o \
        -name "test_output*.log" -o \
        -name "test_*_output*.txt" -o \
        -name "tests_output*.txt" -o \
        -name "pytest_out*.txt" -o \
        -name "pytest_full_run*.txt" -o \
        -name "pytest_phase*.txt" -o \
        -name "test_full_audit*.txt" -o \
        -name "test_safeguards_*.txt" -o \
        -name "test_mb_*.txt" -o \
        -name "chantier*_output.txt" -o \
        -name "verify_output.txt" -o \
        -name "failures_list.txt" -o \
        -name "found_files.txt" -o \
        -name "tmp_*.py" -o \
        -name "stress_results_*.csv" -o \
        -name "asym_results_*.csv" \
    \) -print0 2>/dev/null | \
    while IFS= read -r -d '' file; do
        safe_delete_file "$file"
    done

    # Fichiers audit massifs (> 1GB)
    find "$PROJECT_DIR" -maxdepth 1 -type f -size +1G -print0 2>/dev/null | \
    while IFS= read -r -d '' file; do
        local basename
        basename=$(basename "$file")
        # Double vérification: ne pas supprimer les fichiers protégés
        local is_protected=false
        for pf in "${PROTECTED_FILES[@]}"; do
            if [ "$basename" = "$pf" ]; then
                is_protected=true
                break
            fi
        done
        if [ "$is_protected" = false ]; then
            log "  ⚠️  FICHIER MASSIF détecté: $file ($(bytes_to_human "$(get_size_bytes "$file")"))"
            if [ "$AGGRESSIVE" = true ]; then
                safe_delete_file "$file"
            else
                log "  ℹ️  Mode SAFE: fichier conservé. Utilisez --aggressive pour supprimer."
            fi
        fi
    done

    log "  ✔ Fichiers temporaires nettoyés"
}

# ─── Étape 5: Nettoyage Docker ──────────────────────────────────────────────
clean_docker() {
    log_section "NETTOYAGE DOCKER"

    if ! command -v docker &>/dev/null; then
        log "  ℹ️  Docker non détecté, étape ignorée"
        return 0
    fi

    if ! docker info &>/dev/null; then
        log "  ℹ️  Docker daemon non accessible, étape ignorée"
        return 0
    fi

    if [ "$DRY_RUN" = true ]; then
        log "  [DRY-RUN] Docker system df:"
        docker system df 2>/dev/null || true
        log "  [DRY-RUN] Containers arrêtés:"
        docker ps -a --filter "status=exited" --format "  {{.Names}} ({{.Size}})" 2>/dev/null || true
        log "  [DRY-RUN] Images dangling:"
        docker images --filter "dangling=true" --format "  {{.Repository}}:{{.Tag}} ({{.Size}})" 2>/dev/null || true
    else
        if [ "$AGGRESSIVE" = true ]; then
            log "  🔴 Mode AGRESSIF: docker system prune -a --volumes"
            docker system prune -a --volumes -f 2>/dev/null || true
        else
            log "  🟢 Mode SAFE: docker system prune (sans volumes)"
            # Supprime containers arrêtés, réseaux inutilisés, images dangling, build cache
            docker container prune -f 2>/dev/null || true
            docker image prune -f 2>/dev/null || true
            docker network prune -f 2>/dev/null || true
            docker builder prune -f 2>/dev/null || true
        fi
    fi

    log "  ✔ Docker nettoyé"
}

# ─── Étape 6: Nettoyage données de marché anciennes ─────────────────────────
clean_market_data() {
    log_section "NETTOYAGE DONNÉES DE MARCHÉ ANCIENNES"

    local market_dirs=("${PROJECT_DIR}/market_data" "${PROJECT_DIR}/data")
    for mdir in "${market_dirs[@]}"; do
        if [ -d "$mdir" ]; then
            log "  Scan: $mdir"
            # Supprimer les fichiers de données de plus de 30 jours
            find "$mdir" -type f \( -name "*.csv" -o -name "*.json" -o -name "*.parquet" -o -name "*.pkl" \) \
                -mtime +${LOG_RETENTION_DAYS} -print0 2>/dev/null | \
            while IFS= read -r -d '' file; do
                safe_delete_file "$file"
            done
        fi
    done

    log "  ✔ Données de marché anciennes nettoyées"
}

# ─── Étape 7 (Aggressive): Nettoyage node_modules ───────────────────────────
clean_node_modules() {
    if [ "$AGGRESSIVE" = true ]; then
        log_section "NETTOYAGE NODE_MODULES (AGRESSIF)"
        local nm="${PROJECT_DIR}/node_modules"
        if [ -d "$nm" ]; then
            local size
            size=$(get_size_bytes "$nm")
            if [ "$DRY_RUN" = true ]; then
                log "  [DRY-RUN] Supprimerait node_modules ($(bytes_to_human "$size"))"
                log "  [DRY-RUN] → Exécuter 'npm install' après pour réinstaller"
            else
                rm -rf "$nm"
                log "  ✅ node_modules supprimé ($(bytes_to_human "$size"))"
                log "  ⚠️  Exécuter 'npm install' pour réinstaller les dépendances"
            fi
            TOTAL_FREED=$((TOTAL_FREED + size))
        fi
    fi
}

# ─── Étape 8: Nettoyage dist/build ──────────────────────────────────────────
clean_build_artifacts() {
    log_section "NETTOYAGE ARTEFACTS DE BUILD"

    for build_dir in "dist" "build" ".next" ".nuxt"; do
        local target="${PROJECT_DIR}/${build_dir}"
        if [ -d "$target" ]; then
            safe_delete_dir "$target"
        fi
    done

    log "  ✔ Artefacts de build nettoyés"
}

###############################################################################
# ─── EXÉCUTION PRINCIPALE ───────────────────────────────────────────────────
###############################################################################

main() {
    log "╔══════════════════════════════════════════════════════════╗"
    log "║       TRADING BOT CLEANUP — $(timestamp)       ║"
    log "║  Mode: $([ "$AGGRESSIVE" = true ] && echo "🔴 AGRESSIF" || echo "🟢 SAFE")  |  Dry-run: $([ "$DRY_RUN" = true ] && echo "OUI" || echo "NON")             ║"
    log "╚══════════════════════════════════════════════════════════╝"
    log "Répertoire projet: $PROJECT_DIR"

    # Confirmation mode agressif
    if [ "$AGGRESSIVE" = true ] && [ "$DRY_RUN" = false ]; then
        echo ""
        echo "⚠️  MODE AGRESSIF activé. Cela va:"
        echo "   - Supprimer TOUTES les images Docker, volumes, containers"
        echo "   - Supprimer node_modules"
        echo "   - Supprimer les fichiers massifs (>1GB)"
        echo "   - Supprimer dist/"
        echo ""
        read -p "Confirmer ? (tapez 'OUI' en majuscules): " confirm
        if [ "$confirm" != "OUI" ]; then
            log "❌ Annulé par l'utilisateur"
            exit 0
        fi
    fi

    check_disk_space
    clean_python_caches
    clean_old_logs
    rotate_large_logs
    clean_temp_files
    clean_docker
    clean_market_data
    clean_node_modules
    clean_build_artifacts

    log ""
    log "╔══════════════════════════════════════════════════════════╗"
    log "║  RÉSUMÉ                                                 ║"
    log "╚══════════════════════════════════════════════════════════╝"
    log "Espace récupéré estimé: $(bytes_to_human $TOTAL_FREED)"
    log "Mode: $([ "$AGGRESSIVE" = true ] && echo "AGRESSIF" || echo "SAFE")"
    log "Dry-run: $([ "$DRY_RUN" = true ] && echo "OUI (aucune suppression)" || echo "NON (suppressions effectuées)")"

    check_disk_space
    log "Nettoyage terminé avec succès ✔"
}

main "$@"
