#!/usr/bin/env bash
# Генерация N UUID (по умолчанию 1)
# Использование: ./scripts/gen-uuid.sh 3
N=${1:-1}
for i in $(seq 1 "$N"); do
    python3 -c "import uuid; print(uuid.uuid4())"
done
