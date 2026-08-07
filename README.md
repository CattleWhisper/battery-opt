# Battery Opt

A Home Assistant custom integration that plans charge/discharge of a
**Marstek Venus E 3.0** home battery to arbitrage the Portuguese
regulated network tariff (TAR) between `ponta` / `cheias` / `vazio`
periods — the TAR, not the OMIE market price, is what pays
(see `CONTEXT.md`).

**Status: control code complete, bench verification pending.** The
domain core — tri-horária weekly calendar, EDP Indexada price model,
greedy optimiser, static baseline — is backtested over 11 months of
real quarter-hourly OMIE data and runs entirely outside Home
Assistant. Measured on real data: **€196/yr saving** for the static
seasonal schedule, **€324/yr** for the capped dynamic optimiser
(incl. VAT, net of wear); full numbers in `docs/findings.md`. The
battery control state machine (ADR-0006: CHARGE / HOLD / DISCHARGE,
discharge via the firmware's zero-export anti-feed mode) and the
charge-power closed loop (ADR-0007: charge at the highest power that
keeps grid import under the contracted limit) are implemented; the
on-device checklist in `docs/spec.md` §8 gates production actuation.

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
"Battery Opt", restart, then add the integration.

**Prerequisites (the config flow enforces both):** Home Assistant's
own **OMIE** integration (Settings → Devices & services → Add →
OMIE) — prices come exclusively from its `omie.get_prices_for_date`
service — and HA's timezone set to **Europe/Lisbon** (the tariff
calendar, the OMIE market day and every trigger are Portugal-local).
For actuation, the **marstek_venus_modbus** integration must own the
battery's Modbus connection, with its disabled-by-default entities
enabled (`force_mode`, `rs485_control_mode`, `set_charge_power`, and
`charge_to_soc` if you want the firmware backstop).

**Two modes.** Leave the battery entities empty for
**planning-only** mode: plans, prices and savings are computed and
published, nothing actuates. Add all four battery entities (force
mode select, charge power number, rs485 control switch, work mode
select) via Configure to go **active**. No SoC sensor is configured:
the reserve floor is the battery's to manage (ADR-0008). Everything
is editable later through the options flow; the entry reloads on
save.

**Optional entities**, each degrading gracefully when unset:

| Config field | Enables |
|---|---|
| House load sensor (W or kWh) | Real load forecast instead of the flat 1.04 kW |
| Grid import energy sensor (kWh) | `sensor.battery_opt_cost_today` |
| Grid import power sensor (W) + battery power sensor (W) | The ADR-0007 charge-power loop (without them CHARGE uses a safe static 2000 W) |
| Charge-to-SoC number | Firmware charge backstop (gate on the spec §8 checklist) |
| SOC cutoff numbers | Setup-time firmware cutoffs — the discharge cutoff is the run-time floor where it exists; the numbers do not exist on the Venus E V3, so leave them empty there |

## Entities

All grouped under one **Battery Opt** service device.

| Entity | What it shows |
|---|---|
| `sensor.battery_opt_plan` | Current action (`charge` / `discharge` / `hold`); attributes carry the full day vectors, tomorrow's preview, the static-fallback flag and the charge-loop setpoint/fallback |
| `sensor.battery_opt_current_price` | Delivered price now per the EDP Indexada formula (€/kWh, excl. fixed terms and VAT); day vector + TAR period in attributes; Energy-dashboard-ready |
| `sensor.battery_opt_soc_forecast` | Planned SoC for the current quarter (%, same unit as the Marstek's own SoC sensor — overlay the two to compare forecast vs real); full day trajectory in attributes |
| `sensor.battery_opt_forecast_savings` | Forecast saving today vs not cycling (EUR) |
| `sensor.battery_opt_vs_static` | Forecast gain of the dynamic plan over the fixed seasonal schedule (EUR) — the metric that justifies the project |
| `sensor.battery_opt_cost_today` | Grid-import cost today incl. the daily fixed terms, excl. VAT (needs the grid energy sensor) |
| `sensor.battery_opt_load_mae` | Load-forecast error (W), computed at each day close (needs the load sensor) |
| `binary_sensor.battery_opt_healthy` | Safe-to-actuate: off on driver failure or an invalid plan (active mode; missing prices degrade to the static plan instead, marked `fallback: static`), or on missing prices in planning-only mode — the executor never actuates while off |
| `switch.battery_opt_executor_actuation` | **Manual override** (active mode): off = the executor keeps planning and validating but skips every battery write, so you can drive the battery yourself; re-enabling replays the full transition sequence. Default on, restored across restarts |
| `switch.battery_opt_charge_loop_actuation` | Manual override for the charge-power loop: off = it keeps computing but writes no setpoints (shown only when the loop's sensors are configured) |

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
