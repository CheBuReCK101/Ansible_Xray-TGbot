#!/usr/bin/env bash
# Генерация всех значений для vault.yml (требует Docker)
set -euo pipefail

echo ""
echo "================================================"
echo "  xray x25519 keypair (Reality)"
echo "================================================"
docker run --rm ghcr.io/xtls/xray-core:latest x25519
echo ""
echo "  privateKey -> vault.yml: xray_private_key"
echo "  publicKey  -> vault.yml: xray_public_key"

echo ""
echo "================================================"
echo "  UUID для клиентов (3 шт.)"
echo "================================================"
for i in 1 2 3; do
    python3 -c "import uuid; print(uuid.uuid4())"
done

echo ""
echo "================================================"
echo "  short_id для клиентов (8 hex-символов)"
echo "================================================"
for i in 1 2 3; do
    openssl rand -hex 4
done

echo ""
echo "-> Вставь значения в group_vars/vps/vault.yml"
echo "-> Затем: ansible-vault encrypt group_vars/vps/vault.yml"
echo ""
