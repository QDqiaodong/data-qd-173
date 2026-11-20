# data-qd-173 | EV charging session settlement

Allocates metered energy intervals to charging sessions and computes tariff charges in integer cents. Python 3.11+; standard library only.

Two modes share one code base:

* **Legacy static mode** – `tariffs` + `sessions` in, settlement out. Behavior and output are unchanged from the original implementation.
* **Continuous correction-safe mode** – batches of cumulative meter readings, correction records and billing-period lifecycle actions are applied on top of a replayable state JSON. Station residuals, withheld energy, immutable closed statements and signed next-period adjustments are reported separately.
* **Collection & admission workbench** (`charging.workbench`, CLI `charge-access`) – a separate, resumable JSON state machine that imports closed-line/adjustment credentials from the settlement engine (read-only), registers customer receipts, write-offs and chargebacks, and grants/starts/finishes station charging permits based on overdue debt. It never writes back to the settlement state. See [`docs/workbench.md`](docs/workbench.md).
* **Fleet & load-shed planning** (`charging.planning`, CLI `charge-plan`) – a local, forward-only scheduler that turns fleet charging needs and mutually-exclusive grid load-shed offers into one auditable baseline schedule, per-offer accept decisions and a prefix-locked contingency plan per accepted offer. It issues no control commands and produces no billable energy, and never touches settlement/workbench state. See [`docs/planning.md`](docs/planning.md).

## Install and test

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
.venv/bin/python -m unittest discover -s tests -v
```

Without `uv`, the suite also runs directly from the project root:

```sh
python3 -m unittest discover -s tests -v
```

## Legacy static mode

```sh
.venv/bin/charge-settle --input fixtures/input.json --output run/settlement.json
```

Fixture summary: 2 sessions settle to 58 cents total.

## Continuous mode

```sh
# round 1: register station/session/tariffs, first readings, open a period
.venv/bin/charge-settle \
  --batch fixtures/batches/batch-01-register.json \
  --output-state run/state.json --output run/result-1.json

# round 2: close January, open February
.venv/bin/charge-settle \
  --batch fixtures/batches/batch-02-close.json \
  --state run/state.json --output-state run/state.json --output run/result-2.json

# round 3: correct the station cumulative reading 1000 -> 1200 Wh
.venv/bin/charge-settle \
  --batch fixtures/batches/batch-03-correct.json \
  --state run/state.json --output-state run/state.json --output run/result-3.json

# round 4: reverse correction restores the balance; opposite adjustment is posted
.venv/bin/charge-settle \
  --batch fixtures/batches/batch-04-reverse.json \
  --state run/state.json --output-state run/state.json --output run/result-4.json
```

After round 3 the result contains, simultaneously: a `200 Wh` station
residual, the session listed under `withheld_energy` (no longer billable),
the frozen January statement line still at `10` cents, and a `-10` cent
adjustment in the open February period. Re-running any batch with the same
`batch_id` returns `"replayed": true` and leaves the state byte-identical;
an invalid batch exits `2` and leaves every output file untouched.

The full semantic contract – state, batch, billing period, statement line,
adjustment, `record_id`, `batch_id` and correction-chain definitions, replay
rules, the `no_open_period` boundary and error codes – is in
[`docs/continuous-settlement.md`](docs/continuous-settlement.md). Tariff
splitting and rounding rules remain documented in
[`docs/contract.md`](docs/contract.md).

## Collection & admission workbench

The workbench answers which invoice a receipt settled, how much remains after
a partial chargeback, and whether a customer may start charging at a station
now. It keeps its own resumable state and only reads the settlement state:

```sh
# multi-round runnable example (writes only into a temporary directory)
bash examples/workbench-demo.sh

# or invoke rounds directly; --state may be omitted on the first round
python -m charging.workbench.cli \
  --round fixtures/workbench/round-01.json \
  --state run/wb-state.json \
  --output-state run/wb-state.json \
  --output run/wb-result-01.json
```

Highlights: receivable = original `charge_cents` plus all signed adjustments
referencing the line (internal `reflected_charge_cents` is ignored); receipts
conserve `effective write-off + unallocated + cumulative chargebacks =
received`; permits are non-monetary station-admission credentials with
`issued → started → finished` / `invalidated` lifecycle; debt-driven
invalidation is irreversible and distinguishable from expiry; every
`operation_id` is idempotently replayable with its original receipt. Full
contract, JSON shapes and error codes: [`docs/workbench.md`](docs/workbench.md).

## Fleet & load-shed planning

Plan one station over one UTC window: a baseline charging schedule that meets
every vehicle's exact energy need, plus accept/reject decisions for grid
load-shed offers and a contingency schedule per accepted offer. Offers are
mutually-exclusive activation scenarios (at most one fires, notice arrives at
activation), so every contingency must match the baseline **vehicle-by-vehicle
and slot-by-slot before its activation start**; the solver picks the highest
total reward, breaking ties by the lexicographically smallest sorted offer-id
sequence.

```sh
python -m charging.planning.cli \
  --fleet fixtures/planning/fleet-headline.json \
  --offers fixtures/planning/offers-headline.json \
  --output run/plan.json

# runnable walkthrough of the conflict, tie-break, post-event catch-up,
# combination-beats-single and both failure modes
bash examples/planning-demo.sh
```

Invalid input and an infeasible baseline exit `2` with distinct codes
(`invalid_input`, `baseline_infeasible`, plus `search_limit_exceeded`) and
leave any existing output file untouched. Full contract, JSON shapes,
feasibility method (integer Dinic max-flow plus exact prefix-locked search)
and scale notes: [`docs/planning.md`](docs/planning.md).

## Layout

```
charging/core.py             legacy settle() plus the shared per-session charging rule
charging/engine.py           continuous state machine: apply_batch(parent_state, batch)
charging/cli.py              charge-settle entry point for both settlement modes
charging/workbench/engine.py collection & admission state machine: apply_round(parent_state, round_input)
charging/workbench/cli.py    charge-access entry point (own resumable JSON state)
charging/planning/engine.py  fleet/load-shed joint scheduler: plan(fleet_input, offers_input)
charging/planning/cli.py     charge-plan entry point (pure planning, no control, no billing)
fixtures/batches/            synthetic settlement batches
fixtures/workbench/          synthetic workbench round inputs (no generated state is committed)
fixtures/planning/           synthetic fleet/offers inputs and committed example plans
examples/workbench-demo.sh   multi-round CLI demonstration of the admission lifecycle
examples/planning-demo.sh    CLI demonstration of joint scheduling and both failure modes
tests/                       legacy, continuous-mode, workbench and planning tests, incl. CLI subprocess
```

Fixture data is synthetic. MIT licensed.
