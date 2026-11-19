# data-qd-173 | EV charging session settlement

Allocates metered energy intervals to charging sessions and computes tariff charges in integer cents. Python 3.11+; standard library only.

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
.venv/bin/charge-settle --input fixtures/input.json --output run/settlement.json
.venv/bin/python -m unittest discover -s tests -v
```

Fixture data is synthetic. Contract details and limitations are in `docs/contract.md`. MIT licensed.

Fixture summary: 2 sessions settle to 58 cents total.
