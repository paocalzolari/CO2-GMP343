#!/bin/bash
# run_tests.sh — Esegue la suite pytest di CO2-GMP343.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m pytest tests/ -v --tb=short "$@"
