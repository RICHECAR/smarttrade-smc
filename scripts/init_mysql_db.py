"""
Initialisation de la base de données MySQL pour les données de marché.
═══════════════════════════════════════════════════════════════════════

Ce script :
  1. Crée la base de données 'smc_market_data' si elle n'existe pas
  2. Crée toutes les tables (candles, funding_rates, ticker_snapshots, etc.)
  3. Vérifie la connexion et affiche un résumé

Prérequis :
  - MySQL/MariaDB en cours d'exécution (XAMPP, WAMP, standalone)
  - pip install pymysql cryptography sqlalchemy python-dotenv

Usage :
    python scripts/init_mysql_db.py
"""

import sys
import os

# Ajouter le répertoire racine au path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
from dotenv import load_dotenv

load_dotenv()


def create_database():
    """Crée la base de données MySQL si elle n'existe pas."""
    host = os.getenv("MYSQL_HOST", "localhost")
    port = int(os.getenv("MYSQL_PORT", "3306"))
    user = os.getenv("MYSQL_USER", "root")
    password = os.getenv("MYSQL_PASSWORD", "root")
    database = os.getenv("MYSQL_DATABASE", "smc_market_data")

    print(f"╔══════════════════════════════════════════════════════════╗")
    print(f"║   INITIALISATION BASE DE DONNÉES MYSQL                  ║")
    print(f"╚══════════════════════════════════════════════════════════╝")
    print(f"  Host     : {host}:{port}")
    print(f"  User     : {user}")
    print(f"  Database : {database}")
    print()

    # 1. Connexion sans base de données pour la créer
    try:
        conn = pymysql.connect(
            host=host, port=port, user=user, password=password,
            charset="utf8mb4",
        )
        cursor = conn.cursor()
        print("[1/4] ✅ Connexion MySQL réussie")
    except pymysql.Error as e:
        print(f"[1/4] ❌ ERREUR : Impossible de se connecter à MySQL")
        print(f"       → {e}")
        print()
        print("  Vérifiez que MySQL/MariaDB est démarré (XAMPP Control Panel)")
        print("  et que les identifiants sont corrects dans le fichier .env")
        sys.exit(1)

    # 2. Créer la base de données
    try:
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{database}` "
            f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        conn.commit()
        print(f"[2/4] ✅ Base de données '{database}' créée / existante")
    except pymysql.Error as e:
        print(f"[2/4] ❌ ERREUR création DB : {e}")
        sys.exit(1)
    finally:
        cursor.close()
        conn.close()

    # 3. Créer les tables avec SQLAlchemy
    try:
        from backend.db.mysql_models import (
            MarketBase, get_mysql_engine, Candle, FundingRate,
            TickerSnapshot, SignalHistory, BacktestRun, BacktestTrade,
            CollectionStatus,
        )
        engine = get_mysql_engine(echo=False)
        MarketBase.metadata.create_all(bind=engine)
        print("[3/4] ✅ Tables créées avec succès")
    except Exception as e:
        print(f"[3/4] ❌ ERREUR création tables : {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # 4. Vérification — lister les tables
    try:
        conn = pymysql.connect(
            host=host, port=port, user=user, password=password,
            database=database, charset="utf8mb4",
        )
        cursor = conn.cursor()
        cursor.execute("SHOW TABLES")
        tables = [row[0] for row in cursor.fetchall()]

        print(f"[4/4] ✅ Vérification — {len(tables)} tables trouvées :")
        for t in tables:
            cursor.execute(f"SELECT COUNT(*) FROM `{t}`")
            count = cursor.fetchone()[0]
            print(f"       → {t:30s} ({count:>8,} lignes)")

        cursor.close()
        conn.close()
    except pymysql.Error as e:
        print(f"[4/4] ⚠️  Vérification partielle : {e}")

    print()
    print("═" * 58)
    print("  BASE DE DONNÉES PRÊTE POUR LA COLLECTE")
    print("  Prochaine étape : python scripts/collect_market_data.py")
    print("═" * 58)


if __name__ == "__main__":
    create_database()
