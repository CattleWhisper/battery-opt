# Battery Opt

A Home Assistant custom integration that plans charge/discharge of a
**Marstek Venus E 3.0** home battery to arbitrage the Portuguese
regulated network tariff (TAR) between `ponta` / `cheias` / `vazio`
periods — the TAR, not the OMIE market price, is what pays
(see `CONTEXT.md`).

**Status: Phase 1 (HA shell) in progress.** The domain core —
tri-horária weekly calendar, EDP Indexada price model, greedy
optimiser, static baseline — is complete, backtested over 11 months
of real quarter-hourly OMIE data, and runs entirely outside Home
Assistant. Measured on real data: **€196/yr saving** for the static
seasonal schedule, **€324/yr** for the capped dynamic optimiser
(incl. VAT, net of wear). Full numbers in `docs/findings.md`.

## Architecture in one paragraph

`custom_components/battery_opt/core/` holds all domain logic and
imports nothing from `homeassistant` (ADR-0001) — the same code runs
under pytest, in the 12-month backtest (`backtest/`), and in
production. The integration is a **planner, not a driver**
(ADR-0004): the battery's single Modbus connection belongs to the
`marstek_venus_modbus` integration, and every actuation goes through
`hass.services.async_call` against its entities. The tariff calendar
is versioned by effective date (ADR-0005) because ERSE revises it.

## Install (HACS)

Add this repository as a custom repository in HACS, install
"Battery Opt", restart, then add the integration. The config flow
asks for the `marstek_venus_modbus` entities (mode select, power
number, SoC sensor), an OMIE price sensor, and the battery
parameters (defaults from `CONTEXT.md`).

## Dashboards

`sensor.battery_opt_current_price` carries the whole day's delivered
price vector in its `prices_eur_kwh` attribute (and, once OMIE
publishes D+1, `tomorrow_prices_eur_kwh`); `sensor.battery_opt_plan`
carries the matching `charge_w` / `discharge_w` vectors (and their
`tomorrow_*` previews). All four are 96-entry arrays, one value per
quarter-hour starting at local midnight (`plan_date`). An
[ApexCharts card](https://github.com/RomRider/apexcharts-card) (HACS)
can graph both against each other:

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Battery Opt — today's plan
graph_span: 24h
span:
  start: day
series:
  - entity: sensor.battery_opt_current_price
    name: Price (EUR/kWh)
    type: line
    yaxis_id: price
    data_generator: |
      const prices = entity.attributes.prices_eur_kwh || [];
      const dayStart = new Date(entity.attributes.plan_date + "T00:00:00");
      return prices.map((p, i) => [dayStart.getTime() + i * 15 * 60 * 1000, p]);
  - entity: sensor.battery_opt_plan
    name: Charge (W)
    type: column
    yaxis_id: power
    data_generator: |
      const w = entity.attributes.charge_w || [];
      const dayStart = new Date(entity.attributes.plan_date + "T00:00:00");
      return w.map((v, i) => [dayStart.getTime() + i * 15 * 60 * 1000, v]);
  - entity: sensor.battery_opt_plan
    name: Discharge (W)
    type: column
    yaxis_id: power
    data_generator: |
      const w = entity.attributes.discharge_w || [];
      const dayStart = new Date(entity.attributes.plan_date + "T00:00:00");
      return w.map((v, i) => [dayStart.getTime() + i * 15 * 60 * 1000, -v]);
yaxis:
  - id: price
    decimals: 3
    apex_config:
      title:
        text: EUR/kWh
  - id: power
    opposite: true
    apex_config:
      title:
        text: W
```

**SoC — forecast vs real:** `sensor.battery_opt_soc_forecast` carries
the planned SoC for the current quarter (%, same unit as the Marstek
SoC sensor) and the whole planned day in its `trajectory_pct` /
`trajectory_kwh` attributes (97 boundary values; index i = start of
quarter i). Overlaying it on the real SoC shows at a glance whether
the battery is following the plan — the comparison Checkpoint C
watches:

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Battery Opt — SoC forecast vs real
graph_span: 24h
span:
  start: day
series:
  - entity: sensor.battery_opt_soc_forecast
    name: Forecast (%)
    type: line
    data_generator: |
      const t = entity.attributes.trajectory_pct || [];
      const dayStart = new Date(entity.attributes.plan_date + "T00:00:00");
      return t.map((v, i) => [dayStart.getTime() + i * 15 * 60 * 1000, v]);
  - entity: sensor.marstek_battery_state_of_charge
    name: Real (%)
    type: line
```

(Point the second series at your Marstek SoC entity id.)

**Energy dashboard:** `sensor.battery_opt_current_price` is declared
exactly like core OMIE's own price sensor (EUR/kWh, `state_class`
measurement), so Settings → Dashboards → Energy accepts it directly
as the grid consumption tab's "use an entity with current price"
source — no template sensor needed. If a grid-import energy sensor is
configured (`CONF_GRID_ENERGY_SENSOR`), `sensor.battery_opt_cost_today`
can be added there too as a daily cost entity (`state_class` TOTAL,
resets at local midnight).

## Development

```bash
scripts/setup      # install dependencies
scripts/lint       # ruff format + check (select = ALL)
pytest             # full suite; backtest data tests skip without the download
scripts/develop    # local Home Assistant with the integration loaded
python backtest/download_omie.py   # fetch the OMIE price history
python backtest/run.py --strategy greedy   # replay a strategy
```

Read `CONTEXT.md` before changing any code; `docs/spec.md`,
`docs/plan.md`, `docs/findings.md` and `docs/adr/` are the working
documents.

## License

MIT — see `LICENSE`.
