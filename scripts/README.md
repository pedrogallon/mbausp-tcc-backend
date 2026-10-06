# Scripts de carga e análise

Requer [k6](https://k6.io/docs/get-started/installation/). Perfis em `k6_profiles.json`, payload em `mock_data.json`.

**AWS no shell** (necessário para `type=event` e fetch CloudWatch):

```powershell
Remove-Item Env:AWS_ACCESS_KEY_ID, Env:AWS_SECRET_ACCESS_KEY, Env:AWS_SESSION_TOKEN -ErrorAction SilentlyContinue
aws login
aws configure export-credentials --format powershell | Invoke-Expression
$env:AWS_REGION = "sa-east-1"
```

## Load test

```powershell
# uma execução
k6 run -e type=request -e K6_PROFILE=steady k6_load_test.js
k6 run -e type=event  -e K6_PROFILE=spike  k6_load_test.js   

# repetir perfil (default: 5x, gap 5 min)
python run_k6_repeat.py --type request --profile spike
python run_k6_repeat.py --type event   --profile steady     
                                  
```

| Flag | Valores | Default |
|------|---------|---------|
| `type` / `--type` | `request`, `event` | `request` |
| `K6_PROFILE` / `--profile` | `steady`, `spike`, `long`, … | `steady` |
| `--runs` | inteiro | `5` |
| `--gap-minutes` | inteiro | `5` |

Saída: `results/k6-{type}-{profile}-{timestamp}.json`

## Fetch (CloudWatch → CSV)

Lê os `k6-*.json` em `results/`, monta a janela (1ª run − padding → última run + padding) e grava `cloudwatch-{type}-{profile}.csv`.

```powershell
python fetch_results_cloudwatch.py
python fetch_results_cloudwatch.py --profile steady   # só um perfil

# Janela:
python fetch_cloudwatch_metrics.py --start ... --end ... --output results/cloudwatch-....csv
```

## Plot (CSV + k6 → gráficos)

```powershell
python plot_results.py
```

Entrada: `results/cloudwatch-*.csv` + `results/k6-*.json`  
Saída: `results/charts/*.png` (e `cloudwatch-recursos-*.csv` em `results/`)
