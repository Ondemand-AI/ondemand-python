# Telemetria para Ondemand Platform

Documento de decisão técnica: abordagens de observabilidade para a plataforma Ondemand.

**Status:** Aguardando decisão
**Contexto:** Hoje a plataforma usa webhooks para reportar status dos steps. A thoughtful 4.0.1 adicionou OpenTelemetry via `t-obs`. Agora com Temporal, temos novas opções.

---

## Situação Atual (Webhooks)

O que temos hoje:

```
Robot (ondemand-ai)  →  Webhook POST  →  Portal (Express)  →  PostgreSQL
                                                            →  SSE → Frontend
```

- `Streamer` envia status de cada step/record via HTTP POST para o portal
- Portal armazena no banco e notifica frontend via SSE
- Sem traces, sem métricas, sem correlação entre steps
- Funciona, mas não tem visibilidade de performance ou debugging distribuído

**Prós:**
- Simples e já funciona
- Zero dependências extras
- Fácil de debugar (é só HTTP)

**Contras:**
- Sem traces (não dá para ver o fluxo completo de uma execução)
- Sem métricas (latência, throughput, error rates)
- Sem correlação entre steps de um mesmo run
- Se o webhook falha, o dado se perde
- Não escala para debugging de problemas em produção

---

## Approach A: Full OpenTelemetry (como thoughtful 4.0.1)

Adicionar OpenTelemetry completo com Jaeger/Tempo para traces e Prometheus/Grafana para métricas.

### Arquitetura

```
Robot (ondemand-ai)
  │
  ├── OTel SDK (traces + logs + metrics)
  │   └── OTLP Exporter → OTel Collector → Jaeger/Tempo (traces)
  │                                      → Prometheus (metrics)
  │                                      → Loki (logs)
  │
  └── Webhooks (mantém para status updates ao portal)

Worker
  │
  ├── OTel SDK (traces de execução RCC, download de robots)
  │   └── OTLP Exporter → OTel Collector
  │
  └── Temporal SDK (já tem instrumentação OTel built-in)

Portal
  │
  ├── OTel SDK (traces de API, webhooks recebidos)
  │   └── OTLP Exporter → OTel Collector
  │
  └── Grafana (dashboards)
```

### O que thoughtful 4.0.1 faz

```python
# init_supervision() — chamado antes de qualquer logging
from t_obs import init_supervision
init_supervision()  # configura OTel provider, exporters, instrumentors

# StepTracer — trace por step
class StepTracer:
    def start_step(self, step_name):
        self.span = tracer.start_span(step_name)
        # timing, attributes, events

    def end_step(self, status):
        self.span.set_status(status)
        self.span.end()

# TelemetryContext — root span do run inteiro
class TelemetryContext:
    def __enter__(self):
        self.root_span = tracer.start_span("supervision-run")
    def __exit__(self):
        self.root_span.end()
```

### Implementação para Ondemand

**No robot (ondemand-ai):**
```python
# ondemand/telemetry/__init__.py
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

def init_telemetry(service_name: str = "ondemand-robot"):
    provider = TracerProvider(resource=Resource.create({
        "service.name": service_name,
        "ondemand.run_id": os.environ.get("ONDEMAND_RUN_ID", ""),
        "ondemand.org_id": os.environ.get("ORGANIZATION_ID", ""),
    }))
    exporter = OTLPSpanExporter(endpoint=os.environ.get("OTEL_ENDPOINT"))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

# No step_context.py — adicionar spans automáticos
class StepContext:
    def __enter__(self):
        self._span = tracer.start_span(self.step_name)
        # ... existing code

    def __exit__(self):
        self._span.set_attribute("step.status", self.status.value)
        self._span.set_attribute("step.records.total", self.total_records)
        self._span.end()
        # ... existing code
```

**Infra adicional (docker-compose):**
```yaml
otel-collector:
  image: otel/opentelemetry-collector-contrib:latest
  ports:
    - "4317:4317"   # OTLP gRPC
    - "4318:4318"   # OTLP HTTP
  volumes:
    - ./otel-config.yaml:/etc/otelcol/config.yaml

jaeger:
  image: jaegertracing/all-in-one:latest
  ports:
    - "16686:16686"  # UI
    - "14268:14268"  # collector

prometheus:
  image: prom/prometheus:latest
  ports:
    - "9090:9090"
  volumes:
    - ./prometheus.yml:/etc/prometheus/prometheus.yml

grafana:
  image: grafana/grafana:latest
  ports:
    - "3001:3000"
  volumes:
    - grafana-data:/var/lib/grafana
```

**Dependências extras no ondemand-ai:**
```toml
[project.optional-dependencies]
telemetry = [
    "opentelemetry-api>=1.20.0",
    "opentelemetry-sdk>=1.20.0",
    "opentelemetry-exporter-otlp>=1.20.0",
    "opentelemetry-instrumentation-requests>=0.41b0",
    "opentelemetry-instrumentation-httpx>=0.41b0",
]
```

### Prós
- Visibilidade total: traces end-to-end do run inteiro (portal → worker → robot → cada step)
- Métricas automáticas: latência por step, error rates, throughput
- Correlação: trace ID propaga entre todos os serviços
- Padrão da indústria: Grafana/Jaeger são ferramentas maduras
- Temporal SDK tem instrumentação OTel nativa (traces de workflows/activities grátis)
- Debug em produção: "por que esse step demorou 5min?" → olha o trace
- Histórico de performance para detectar degradação

### Contras
- 4 containers extras (collector, jaeger, prometheus, grafana) na VPS
- ~1.5GB RAM adicional no mínimo
- Complexidade de configuração: otel-config.yaml, prometheus.yml, datasources Grafana
- Dependências extras no pacote Python (opentelemetry-*)
- Curva de aprendizado para quem não conhece OTel
- Precisa instrumentar manualmente httpx/requests se quiser traces de chamadas HTTP
- Overengineering se só temos poucos robôs rodando

---

## Approach B: Temporal Built-in Observability

Usar o que o Temporal já oferece nativamente sem adicionar OTel.

### O que Temporal já dá de graça

1. **Workflow History** — cada workflow tem um event history completo:
   - Quando cada activity começou e terminou
   - Inputs e outputs de cada activity
   - Signals recebidos (approvals)
   - Erros e retries
   - Timers e timeouts

2. **Temporal UI** (já rodando em temporal.ondemand-ai.com.br):
   - Timeline visual de cada workflow
   - Event history completo
   - Stack traces de workflows pendentes
   - Search por workflow ID, tipo, status

3. **Temporal Metrics** (endpoint `/metrics` built-in):
   - `temporal_workflow_task_schedule_to_start_latency` — latência do queue
   - `temporal_activity_execution_latency` — tempo de cada activity
   - `temporal_workflow_completed` / `temporal_workflow_failed` — taxas de sucesso
   - `temporal_activity_schedule_to_start_latency` — backlog de activities

### Implementação

Quase nada a fazer — só expor as métricas para Prometheus:

```yaml
# docker-compose.yml — adicionar scraping das métricas do Temporal
prometheus:
  image: prom/prometheus:latest
  volumes:
    - ./prometheus.yml:/etc/prometheus/prometheus.yml

# prometheus.yml
scrape_configs:
  - job_name: 'temporal'
    static_configs:
      - targets: ['temporal:7233']
    metrics_path: /metrics

grafana:
  image: grafana/grafana:latest
```

**No worker** — habilitar métricas do SDK:
```python
# src/temporal_worker.py
from temporalio.runtime import Runtime, TelemetryConfig, PrometheusConfig

runtime = Runtime(telemetry=TelemetryConfig(
    metrics=PrometheusConfig(bind_address="0.0.0.0:9000")
))

client = await Client.connect(
    temporal_address,
    runtime=runtime,
)
```

### Prós
- Quase zero trabalho: Temporal já coleta tudo
- Temporal UI já é um trace viewer para workflows
- Sem dependências extras no pacote Python
- 2 containers extras no máximo (prometheus + grafana) se quiser dashboards
- Event history é durável (sobrevive a restarts)
- Visibilidade de filas, retries, timeouts grátis

### Contras
- Só vê granularidade de activities, não de steps dentro do robot
- Não tem traces do que acontece dentro do RCC (o robot é uma caixa preta)
- Sem correlação entre chamadas HTTP que o robot faz
- Se quiser ver "o step 'Extrair Dados' demorou 3min", precisa olhar os webhooks no banco
- Sem métricas custom (ex: "quantas notas fiscais processadas por minuto")
- Temporal UI não substitui Grafana para dashboards operacionais

---

## Approach C: Hybrid Lightweight (Recomendado)

Combinar Temporal built-in + métricas leves no webhook sem adicionar OTel SDK.

### Conceito

```
Robot (ondemand-ai)
  │
  └── Webhooks (já existem)
       └── Agora incluem: timing, record counts, metadata

Worker
  │
  ├── Temporal SDK metrics (built-in)
  └── Activity-level timing (já no event history)

Portal
  │
  ├── Recebe webhooks com timing data
  ├── Armazena métricas no PostgreSQL
  ├── Calcula aggregates (avg duration por step, error rates)
  └── Expõe dashboards no próprio frontend

Temporal
  │
  └── Event history + metrics endpoint → Prometheus (opcional) → Grafana
```

### Implementação

**1. Enriquecer webhooks com timing (ondemand-ai):**

```python
# ondemand/supervisor/streaming/payloads.py — já temos TimedReport
# Adicionar campos ao payload do webhook:

class StepPayload:
    step_name: str
    status: str
    started_at: datetime      # já existe via TimedReport
    finished_at: datetime     # já existe via TimedReport
    duration_seconds: float   # calculado
    records_total: int
    records_succeeded: int
    records_failed: int
    metadata: dict            # custom data do robot
```

**2. Tabela de métricas no portal (migration):**

```sql
CREATE TABLE step_metrics (
    id SERIAL PRIMARY KEY,
    run_id INTEGER REFERENCES runs(id),
    step_name VARCHAR(255) NOT NULL,
    status VARCHAR(50),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    duration_seconds DECIMAL(10,2),
    records_total INTEGER DEFAULT 0,
    records_succeeded INTEGER DEFAULT 0,
    records_failed INTEGER DEFAULT 0,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_step_metrics_run ON step_metrics(run_id);
CREATE INDEX idx_step_metrics_step ON step_metrics(step_name);
CREATE INDEX idx_step_metrics_created ON step_metrics(created_at);
```

**3. Webhook handler atualizado (portal):**

```javascript
// app/src/routes/webhooks.js — no handler de step update
case 'STEP_COMPLETED':
case 'STEP_FAILED':
    await db.query(`
        INSERT INTO step_metrics (run_id, step_name, status, started_at, finished_at,
                                  duration_seconds, records_total, records_succeeded,
                                  records_failed, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    `, [runId, payload.step_name, payload.status, payload.started_at,
        payload.finished_at, payload.duration_seconds, payload.records_total,
        payload.records_succeeded, payload.records_failed, payload.metadata]);
    break;
```

**4. API de métricas (portal):**

```javascript
// GET /api/metrics/process/:processId
// Retorna: avg duration por step, error rates, throughput
router.get('/metrics/process/:processId', async (req, res) => {
    const metrics = await db.query(`
        SELECT
            sm.step_name,
            COUNT(*) as total_executions,
            AVG(sm.duration_seconds) as avg_duration,
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY sm.duration_seconds) as p95_duration,
            SUM(CASE WHEN sm.status = 'succeeded' THEN 1 ELSE 0 END)::float / COUNT(*) as success_rate,
            AVG(sm.records_total) as avg_records,
            MAX(sm.created_at) as last_run
        FROM step_metrics sm
        JOIN runs r ON r.id = sm.run_id
        WHERE r.process_id = $1
        AND sm.created_at > NOW() - INTERVAL '30 days'
        GROUP BY sm.step_name
        ORDER BY sm.step_name
    `, [req.params.processId]);

    res.json(metrics.rows);
});
```

**5. Dashboard no frontend (opcional, fase 2):**

- Página de "Métricas" por processo/agente
- Gráfico de duração por step ao longo do tempo
- Error rate com alertas visuais
- Comparação entre runs (este run vs média)

**6. Temporal metrics (opcional):**

Se quiser Grafana para métricas de infra:
```yaml
prometheus:
  image: prom/prometheus:latest
grafana:
  image: grafana/grafana:latest
```

Mas isso é opcional — as métricas de negócio ficam no portal.

### Prós
- Zero dependências extras no pacote Python
- Zero containers extras obrigatórios
- Usa infraestrutura que já existe (webhooks + PostgreSQL)
- Métricas de negócio no próprio portal (onde os usuários já estão)
- Temporal UI para debugging de workflows
- Incremental: começa com o que tem, adiciona Grafana depois se precisar
- Os dados ficam no PostgreSQL = fácil de querying, backup, exportar

### Contras
- Sem traces distribuídos (não vê chamadas HTTP individuais dentro de um step)
- Sem auto-instrumentação (cada métrica é explícita no webhook)
- PostgreSQL como metrics store não é ideal para séries temporais de alta cardinalidade
- Se precisar de traces detalhados no futuro, vai ter que adicionar OTel de qualquer forma

---

## Comparação

| Critério | A: Full OTel | B: Temporal Only | C: Hybrid |
|----------|-------------|-------------------|-----------|
| **Containers extras** | 4 (collector, jaeger, prometheus, grafana) | 0-2 (prometheus, grafana) | 0 (opcional: 2) |
| **RAM adicional** | ~1.5GB | ~200MB | 0 (opcional: ~200MB) |
| **Deps no pacote** | opentelemetry-* (~5 packages) | 0 | 0 |
| **Trabalho de setup** | Alto | Baixo | Médio |
| **Traces end-to-end** | Sim | Só activities | Não |
| **Métricas de negócio** | Sim (custom) | Não | Sim (PostgreSQL) |
| **Métricas de infra** | Sim | Sim (Temporal) | Parcial (Temporal) |
| **Debug em produção** | Excelente | Bom (workflow level) | Bom (step level) |
| **Escala para 100+ robôs** | Sim | Sim | Sim (com limites no PG) |
| **Curva de aprendizado** | Alta | Baixa | Baixa |
| **Vendor lock-in** | Não (OTel é padrão) | Temporal | Não |

---

## Recomendação

**Começar com C (Hybrid)**, evoluir para A se necessário:

1. **Agora:** Enriquecer webhooks com timing + armazenar em `step_metrics` no PostgreSQL. Custo: ~1 dia de trabalho.

2. **Quando tiver 10+ robôs em produção:** Adicionar Prometheus + Grafana para métricas de infra do Temporal e dos workers.

3. **Quando precisar debugar problemas de latência em chamadas HTTP:** Adicionar OTel como dependência opcional (`pip install ondemand-ai[telemetry]`).

A abordagem C dá 80% do valor com 20% do esforço. O upgrade para OTel depois é incremental porque os webhooks continuam funcionando — OTel adiciona traces, não substitui o que já existe.

---

## Referências

- [OpenTelemetry Python SDK](https://opentelemetry.io/docs/languages/python/)
- [Temporal Observability](https://docs.temporal.io/production-deployment/self-hosted-guide#monitoring-and-observability)
- [Temporal SDK Metrics](https://docs.temporal.io/references/sdk-metrics)
- thoughtful 4.0.1 source: `t-obs` package (OpenTelemetry wrapper)
