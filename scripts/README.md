# Testes de carga (k6)

Requer [k6](https://k6.io/docs/get-started/installation/) instalado. Perfis em `k6_profiles.json`. Payload em `mock_data.json`.

## Como rodar

```powershell
# request-based (HTTP)
k6 run -e type=request -e K6_PROFILE=test k6_load_test.js

# event-based (SQS) — precisa exportar credenciais no shell
Remove-Item Env:AWS_ACCESS_KEY_ID, Env:AWS_SECRET_ACCESS_KEY, Env:AWS_SESSION_TOKEN -ErrorAction SilentlyContinue
aws login
aws configure export-credentials --format powershell | Invoke-Expression
$env:AWS_REGION = "sa-east-1"
k6 run -e type=event -e K6_PROFILE=test k6_load_test.js
```

| Argumento | Valores | Default |
|-----------|---------|---------|
| `type` | `request`, `event` | `request` |
| `K6_PROFILE` | `test`, `steady`, `spike`, `long`, `warmup` | `steady` |

## Repetir um perfil (5x)

```powershell
# request
python run_k6_repeat.py --type request --profile spike

# event
Remove-Item Env:AWS_ACCESS_KEY_ID, Env:AWS_SECRET_ACCESS_KEY, Env:AWS_SESSION_TOKEN -ErrorAction SilentlyContinue
aws login
aws configure export-credentials --format powershell | Invoke-Expression
$env:AWS_REGION = "sa-east-1"
python run_k6_repeat.py --type event --profile spike
```

Opcionais: `--runs 5` (default), `--gap-minutes 5` (default). Log em `k6_repeat.log`.

## Scheduler noturno

Roda **event** e **request em paralelo**: **warmup x1** → gap 5 min → **steady → spike → long** (**5x** cada), com **5 min** entre runs. Log em `k6_schedule.log`.

```powershell
Remove-Item Env:AWS_ACCESS_KEY_ID, Env:AWS_SECRET_ACCESS_KEY, Env:AWS_SESSION_TOKEN -ErrorAction SilentlyContinue
aws login
aws configure export-credentials --format powershell | Invoke-Expression
$env:AWS_REGION = "sa-east-1"
python run_k6_schedule.py
```

Deixe o PC acordado (desative sleep).

## Output

Arquivos em `results/`:

```
k6-{type}-{profile}-{ISO-timestamp}.json
```

Exemplo: `results/k6-request-test-2026-10-04T01-02-24.517Z.json`

```json
{
  "kind": "request",
  "profile": "test",
  "target_rps": 20,
  "duration": "2m",
  "avg_ms": 9.57,
  "p50_ms": 9.10,
  "p95_ms": 11.95,
  "p99_ms": 18.21,
  "failed_rate": 0,
  "failed_count": 0,
  "total_requests": 2401,
  "throughput_req_s": 20.0
}
```

Para `type=event`, os campos de volume são `total_events` e `throughput_events_s`.

## CloudWatch para gold-new

Janela: início da 1ª execução k6 **menos 10 minutos** (steady: **20 minutos**) até o fim da 5ª **mais 5 minutos**:

```powershell
python fetch_gold_cloudwatch.py
```

## Visualizar gold-new

Gráficos em português por perfil (throughput, latência, processamento, falhas, CPU/memória ECS, custo):

```powershell
python fetch_gold_cloudwatch.py
python visualize_gold.py
```

Entrada: `results/gold-new/cloudwatch-{type}-{profile}.csv` (+ `k6-*.json`).  
Também gera `cloudwatch-recursos-{profile}.csv` e PNGs em `results/gold-new/charts/` (inclui `perfil-*-recursos.png`).
