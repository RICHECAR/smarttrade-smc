#!/usr/bin/env python3
"""
SMC Trading Bot — UNIQUE Entry Point
=====================================
Ce fichier est le SEUL point de lancement autorisé.

Usage:
    python run.py                   # Production (port 8000)
    python run.py --reload          # Development avec hot-reload
    python run.py --port 8001       # Port alternatif

NE PAS utiliser:
    - python -m uvicorn smc_futures.orchestrator:app ...  # DEPRECATED
    - python -m uvicorn smc_futures.api_smc_engine:app ... # Utiliser run.py

Author: Trading Bot Framework
"""

import os
import sys
import socket
import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("run")


def is_port_in_use(port: int) -> bool:
    """Check if a port is already bound (prevents WinError 10048)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.bind(("0.0.0.0", port))
            return False  # Port is free
        except OSError:
            return True  # Port is in use


def find_process_on_port(port: int) -> str:
    """Try to identify process using the port (Windows only)."""
    if sys.platform == "win32":
        try:
            import subprocess
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                timeout=5,
                encoding="utf-8",
                errors="replace"  # Handle non-UTF8 characters gracefully
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    if len(parts) >= 5:
                        return f"PID {parts[-1]}"
        except Exception:
            pass
    return "unknown"


def main():
    parser = argparse.ArgumentParser(description="SMC Trading Bot Launcher")
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=int(os.getenv("SMC_API_PORT", "8000")),
        help="Port to bind (default: 8000 or SMC_API_PORT env)"
    )
    parser.add_argument(
        "--host", "-H",
        type=str,
        default="0.0.0.0",
        help="Host to bind (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--reload", "-r",
        action="store_true",
        help="Enable auto-reload for development"
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=1,
        help="Number of workers (default: 1, cannot use with --reload)"
    )
    args = parser.parse_args()

    # ─────────────────────────────────────────────────────
    # PRE-FLIGHT CHECK: Port already in use?
    # ─────────────────────────────────────────────────────
    if is_port_in_use(args.port):
        process_info = find_process_on_port(args.port)
        logger.error("=" * 60)
        logger.error(f"PORT {args.port} IS ALREADY IN USE!")
        logger.error(f"Process: {process_info}")
        logger.error("")
        logger.error("Solutions:")
        logger.error(f"  1. Kill the process: taskkill /PID <pid> /F")
        logger.error(f"  2. Use different port: python run.py --port 8001")
        logger.error(f"  3. Wait for TIME_WAIT sockets to clear (~60 seconds)")
        logger.error("=" * 60)
        sys.exit(1)

    # ─────────────────────────────────────────────────────
    # LAUNCH: Single uvicorn instance
    # ─────────────────────────────────────────────────────
    import uvicorn

    logger.info("=" * 60)
    logger.info("SMC Trading Bot Starting...")
    logger.info(f"  Entrypoint: smc_futures.api_smc_engine:app")
    logger.info(f"  Host: {args.host}")
    logger.info(f"  Port: {args.port}")
    logger.info(f"  Reload: {args.reload}")
    logger.info(f"  Workers: {args.workers}")
    logger.info("=" * 60)

    # CRITICAL: Use string import path for --reload compatibility
    uvicorn.run(
        "smc_futures.api_smc_engine:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers if not args.reload else 1,
        log_level="info",
        # Windows compatibility: watchfiles is more reliable than watchgod
        reload_excludes=["*.pyc", "__pycache__", ".git", "node_modules", "*.db", "*.sqlite*"],
    )


if __name__ == "__main__":
    main()
