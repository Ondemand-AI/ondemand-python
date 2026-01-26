# Ondemand Python Library

Python library for building automation agents on the Ondemand platform.

## Installation

### From GitHub (Private)

```bash
# Using pip with GitHub token
pip install git+https://${GITHUB_TOKEN}@github.com/Ondemand-AI/ondemand-python.git@main

# Or in requirements.txt
git+https://${GITHUB_TOKEN}@github.com/Ondemand-AI/ondemand-python.git@main
```

### For RCC Robots

Add to your `conda.yaml`:
```yaml
dependencies:
  - python=3.10
  - pip=23.0
  - pip:
      - git+https://${GITHUB_TOKEN}@github.com/Ondemand-AI/ondemand-python.git@main
```

Or use the install shim pattern (see robo-demo for example).

## Usage

### Getting Run Inputs

```python
from ondemand.shared.cli import get_inputs

# Get inputs (automatically saved to output/inputs_received_{run_id}.json for auditing)
inputs = get_inputs()

# Access individual parameters
empresa = inputs.get("empresa", "Default Corp")
competencia = inputs.get("competencia", "2024-01")
cnpj = inputs.get("cnpj")
```

### Reporting Status

```python
from ondemand.supervisor import Supervisor

# Initialize supervisor (automatically gets run_id from CLI args)
supervisor = Supervisor()

# Report progress
supervisor.report_step("extracting", "Extraindo dados do portal...")
supervisor.report_step("processing", "Processando 150 notas fiscais...")

# Report completion
supervisor.complete(outputs={"total_processed": 150})

# Or report failure
supervisor.fail("Erro ao acessar portal: timeout")
```

### Full Example (process.py)

```python
from ondemand.shared.cli import get_inputs, parse_args
from ondemand.supervisor import Supervisor

def main():
    # Parse CLI args (gets run_id, webhook_url, api_key)
    run_id, webhook_url, api_key = parse_args()

    # Get inputs (saved to file for auditing)
    inputs = get_inputs()

    # Initialize supervisor for status reporting
    supervisor = Supervisor()

    try:
        supervisor.report_step("starting", "Iniciando processamento...")

        # Your automation logic here
        empresa = inputs.get("empresa")
        competencia = inputs.get("competencia")

        # ... do work ...

        supervisor.complete(outputs={"status": "success"})

    except Exception as e:
        supervisor.fail(str(e))
        raise

if __name__ == "__main__":
    main()
```

## Local Testing

You can test your robot locally without the full worker setup:

### Using Inline JSON

```bash
python src/process.py --inputs '{"empresa": "Test Corp", "competencia": "2024-01"}'
```

### Using Input File

Create `test_inputs.json`:
```json
{
  "empresa": "Test Corp",
  "competencia": "2024-01",
  "cnpj": "12345678000199"
}
```

Run:
```bash
python src/process.py --inputs-file test_inputs.json
```

### With RCC

```bash
rcc task run --task Process -- --inputs '{"empresa": "Test"}'
```

## Input Sources (Priority)

1. **CLI `--inputs`** - JSON string (for local testing)
2. **CLI `--inputs-file`** - Path to JSON file (for local testing)
3. **`ONDEMAND_INPUTS` env var** - Set by worker in production

## Auditing

Inputs are automatically saved to `output/inputs_received_{run_id}.json`:

```json
{
  "run_id": "abc123",
  "received_at": "2024-01-15T10:30:00Z",
  "inputs": {
    "empresa": "Test Corp",
    "competencia": "2024-01"
  }
}
```

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `ONDEMAND_API_KEY` | API key for webhook authentication | Yes (in production) |
| `ONDEMAND_APP_URL` | Base URL for app (default: `https://app.ondemand-ai.com.br`) | No |
| `ONDEMAND_INPUTS` | JSON inputs from worker | Set by worker |

## CLI Arguments

| Argument | Description |
|----------|-------------|
| `--run-id` | Run ID (passed by worker, required for status reporting) |
| `--inputs` | JSON string with inputs (for local testing) |
| `--inputs-file` | Path to JSON file with inputs (for local testing) |
| `--webhook-url` | Override webhook URL (optional) |
| `--api-key` | Override API key (optional) |

## Package Structure

```
ondemand/
├── __init__.py
├── shared/
│   ├── __init__.py
│   └── cli.py          # CLI parsing and input retrieval
├── supervisor/
│   ├── __init__.py
│   └── connector.py    # Status reporting to portal
└── utils/
    └── ...             # Utility functions
```

## Development

```bash
# Clone repo
git clone https://github.com/Ondemand-AI/ondemand-python.git
cd ondemand-python

# Install in development mode
pip install -e .

# Run tests
pytest
```

## Version History

- **0.1.0** - Initial release with CLI parsing, input retrieval, and supervisor reporting
