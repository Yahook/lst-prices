#!/bin/bash
# обёрточный скрипт для запуска lst_prices.py с локальным Python

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# если рядом есть venv — используем его python
if [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python3"
else
    PYTHON="$(which python3)"
fi

echo "Используется Python: $PYTHON"

"$PYTHON" "$SCRIPT_DIR/lst_prices.py" --pools-json "$SCRIPT_DIR/pools.json"
