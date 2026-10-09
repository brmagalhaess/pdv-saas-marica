# Checklist de teste e promoção do PDV2

Este checklist separa o que já pode ser testado localmente do que precisa de evidência de operação real. Ele evita que o produto seja vendido como fiscal antes da homologação.

## 1. Teste local reproduzível

```bash
python3 scripts/init_local.py
set -a; . .runtime/local.env; set +a
docker compose -f compose.local.yaml up -d --build
docker compose -f compose.local.yaml run --rm cloud python manage.py migrate --noinput
docker compose -f compose.local.yaml run --rm cloud python manage.py test core operations -v 1
docker compose -f compose.local.yaml run --rm terminal python -m unittest terminal.tests.test_terminal terminal.tests.test_receipts -q
docker compose -f compose.local.yaml run --rm cloud python -m unittest discover -s /app/shared/tests -q
# backup criptografado + restauração isolada; para produção acrescente
# --compose compose.production.yaml --env-file .env.production --require-external
python3 scripts/verify_backup.py
```

Critérios: migração sem erro; testes verdes; `http://127.0.0.1:8000/health/` responde; `http://127.0.0.1:8010/` abre; o terminal pareia, abre caixa, vende sem rede, reimprime e sincroniza ao reconectar.

## 2. Piloto não fiscal em Maricá

- usar uma empresa, uma loja e um terminal por piloto;
- validar leitor USB, Epson TM-T20, gaveta, balança e impressora de etiquetas em bancada;
- cadastrar estoque inicial no painel e conferir o mesmo saldo no terminal;
- desligar a internet durante vendas e observar `Offline`, fila e licença;
- reconectar e conferir vendas, pagamentos, estoque e conflitos no painel;
- testar fechamento de caixa, desconto com PIN, cancelamento e reimpressão;
- registrar tempo de checkout, falhas de impressão e chamados por dia;
- manter suporte acompanhado por 30 dias antes de adicionar o segundo terminal.

## 3. Portões antes de venda comercial

- [ ] domínio, DNS, HTTPS, SMTP e segredos externos configurados;
- [ ] migrações executadas em banco PostgreSQL de produção;
- [ ] backup Restic externo e restauração em ambiente limpo comprovados (o procedimento está automatizado em `scripts/verify_backup.py`; execute com o `.env.production` depois de configurar o bucket externo);
- [ ] alerta de saúde cloud/worker/backup/sincronização configurado;
- [x] MFA TOTP dos administradores da plataforma implementado; o primeiro acesso de cada administrador exige cadastro e guarda dos códigos de recuperação;
- [ ] NFC-e RJ homologada, autorizada, cancelada e contingência testada com contador;
- [ ] TEF ou Pix dinâmico escolhido, homologado, conciliado e estornado;
- [ ] matriz de hardware aprovada por modelo e procedimento de suporte escrito;
- [ ] instalador/serviço local inicia automaticamente na máquina do cliente;
- [ ] contrato informa claramente “comprovante não fiscal” ou “NFC-e homologada” por plano.

Sem os itens fiscais e de pagamento, comercializar apenas o pacote de **piloto não fiscal**, com limite de clientes e suporte supervisionado.
