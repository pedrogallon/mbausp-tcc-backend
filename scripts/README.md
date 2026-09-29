# Testes de Carga

## Instruções

```bash
pip install -r requirements.txt
aws configure # aws login
python load_test.py --type event --profile long 

# --type {request,event}
# --profile {steady,spike,long}

```

## Perfis de Carga
- **Steady:** 100 req/s por 30 minutos
- **Spike:** 1000 req/s por 10 minutos
- **Long:** 50 req/s por 60 minutos

### Output
```
======================================================================
HTTP SUSTAINED LOAD TEST
======================================================================
Endpoint: http://localhost:8080/actuator/health
Duração: 1800s (30m)
Target RPS: 100
Concurrency: 20
Timeout: 30s
Payload Size: 15.23 KB
Expected Total Requests: ~180000
======================================================================

Test started at 14:30:45
Will run until 15:00:45

======================================================================
RESULTS
======================================================================
Total Time: 1800.45s
Actual RPS: 99.98
Total Requests: 180000
Successful: 180000
Failed: 0
Success Rate: 100.00%

Response Times (seconds):
  Average: 0.150s
  Min: 0.087s
  Max: 0.234s
  P95: 0.201s

Status Codes:
  200: 180000
======================================================================
```

## Configuração de perfis

Edite `profiles.json` para personalizar os perfis de carga. Cada perfil define:
- `Duração_seconds`: **Por quanto tempo** o teste é executado
- `target_rps`: **Requisições/mensagens por segundo alvo**
- `requests_per_batch`: **Quantas requisições** por ciclo de lote
- `batch_interval_seconds`: **Intervalo entre lotes** (normalmente 1 segundo)
- `concurrency`: **Número de threads concorrentes**
- `timeout`: **Tempo limite** da requisição/mensagem em segundos