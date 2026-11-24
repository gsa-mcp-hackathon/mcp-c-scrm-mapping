# Evals

## Rule-based tool benchmark (no API key needed)

`evals/cscrm_benchmark.py` invokes both MCP tools live against the
USAspending.gov v2 API, times each call, and checks deterministic
expectations (known agencies, UEIs, NAICS codes):

```bash
python evals/cscrm_benchmark.py --json-out evals/last_run.json
```

## Optional LLM judge

With an Anthropic key, `--llm-judge` additionally asks the model whether
each payload plausibly answers the case prompt (mirrors the DEMO repo's
`mcp-evals` setup, without the npm dependency):

```bash
ANTHROPIC_API_KEY=... python evals/cscrm_benchmark.py --llm-judge
```

## Live integration tests

`tests/test_live_integration.py` holds the same expectations as pytest
assertions; gated behind `CSCRM_LIVE_TESTS=1`:

```bash
CSCRM_LIVE_TESTS=1 python -m pytest tests/test_live_integration.py -v
```