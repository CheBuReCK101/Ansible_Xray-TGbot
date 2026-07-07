.PHONY: deploy check ping xray-only bot-only links vault-edit help

VAULT_PASS :=

deploy: ## Полный деплой на VPS
	ansible-playbook site.yml $(VAULT_PASS)

check: ## Dry-run без изменений
	ansible-playbook site.yml $(VAULT_PASS) --check --diff

ping: ## Проверить SSH-доступ к хосту
	ansible all -m ping

xray-only: ## Только роль xray (обновить конфиг/пользователей)
	ansible-playbook site.yml --tags xray $(VAULT_PASS)

bot-only: ## Только роль tgbot
	ansible-playbook site.yml --tags tgbot $(VAULT_PASS)

links: ## Напечатать VLESS-ссылки для всех клиентов
	ansible-playbook gen-links.yml $(VAULT_PASS)

vault-edit: ## Редактировать зашифрованный vault
	ansible-vault edit group_vars/vps/vault.yml

help: ## Эта справка
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-14s\033[0m %s\n", $$1, $$2}'
