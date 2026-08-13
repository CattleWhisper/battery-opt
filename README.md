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

**Dynamic actuation (Task 12).** The capped-greedy plan always runs
as an advisory dry-run (`sensor.battery_opt_plan`'s `schedule`,
`sensor.battery_opt_vs_static`). Whether the executor *actuates* it
is the **Dry-run** config option: ON (the default) keeps actuation on
the static seasonal schedule; OFF actuates the greedy, falling back
to the static schedule whenever prices are missing or a plan fails
validation. The plan sensor's `executor_plan_source` attribute shows
which plan is actually driving the battery (`static` / `greedy` /
`static-fallback`). Leave dry-run on until the Checkpoint C review
passes.

**Optional entities**, each degrading gracefully when unset:

| Config field | Enables |
|---|---|
| House load sensor (W or kWh) | Real load forecast instead of the flat 1.04 kW |
| Grid import energy sensor (kWh) | `sensor.battery_opt_cost_today` |
| Grid import power sensor (W) + battery power sensor (W) | The ADR-0007 charge-power loop (without them CHARGE uses a safe static 2000 W); the battery power sensor alone also enables `sensor.battery_opt_realised_savings`. The battery sensor follows the **HA battery convention: positive = discharging, negative = charging** |
| Charge-to-SoC number | Firmware charge backstop — effectively **required** in active mode: the bench kill-test found no firmware watchdog, so this backstop is what stops a charge if the integration dies mid-window |
| SOC cutoff numbers | Setup-time firmware cutoffs — the discharge cutoff is the run-time floor where it exists; the numbers do not exist on the Venus E V3, so leave them empty there |

## Entities

All grouped under one **Battery Opt** service device.

| Entity | What it shows |
|---|---|
| `sensor.battery_opt_plan` | Current action (`charge` / `discharge` / `hold`); attributes carry `schedule` — the advisory plan as merged charge/discharge windows (`start`/`end`/`direction`/`power_w`, hold omitted) spanning today and, once published, tomorrow — `static_schedule` (the static baseline in the same format, for the plan-comparison graph), the static-fallback flag, the charge-loop setpoint/fallback and, in active mode, `executor_plan_source` (what the executor is actuating) |
| `sensor.battery_opt_current_price` | Delivered price now per the EDP Indexada formula (€/kWh, excl. fixed terms and VAT); attributes carry `prices` — merged segments (`start`/`end`/`price_eur_kwh`/`tar_period`, split at every TAR boundary) spanning today and tomorrow; Energy-dashboard-ready |
| `sensor.battery_opt_soc_forecast` | Planned SoC for the current quarter (%, same unit as the Marstek's own SoC sensor — overlay the two to compare forecast vs real); full day trajectory in attributes |
| `sensor.battery_opt_forecast_savings` | Forecast saving today vs not cycling (EUR) |
| `sensor.battery_opt_vs_static` | Forecast gain of the dynamic plan over the fixed seasonal schedule (EUR) — the metric that justifies the project |
| `sensor.battery_opt_cost_today` | Grid-import cost today incl. the daily fixed terms, excl. VAT (needs the grid energy sensor) |
| `sensor.battery_opt_realised_savings` | Realised saving today from **measured** battery flows — discharge value − charge cost − wear, integrated from the battery power sensor (needs it configured); month-to-date realised vs forecast and their deviation in attributes; a monthly report notification flags deviations beyond ±10% |
| `sensor.battery_opt_load_mae` | Load-forecast error (W), computed at each day close (needs the load sensor) |
| `binary_sensor.battery_opt_healthy` | Safe-to-actuate: off on driver failure or an invalid plan (active mode; missing prices degrade to the static plan instead, marked `fallback: static`), or on missing prices in planning-only mode — the executor never actuates while off |
| `switch.battery_opt_executor_actuation` | **Manual override** (active mode): off = the executor keeps planning and validating but skips every battery write, so you can drive the battery yourself; re-enabling replays the full transition sequence. Default on, restored across restarts |
| `switch.battery_opt_charge_loop_actuation` | Manual override for the charge-power loop: off = it keeps computing but writes no setpoints (shown only when the loop's sensors are configured) |
| `button.battery_opt_recalculate_plan` | Force an immediate full recomputation — refetch prices, rebuild the load forecast, re-solve today's plan and tomorrow's preview — without waiting for the 15-minute poll. Recomputes only; actuation stays with the executor (next quarter tick, or press Apply plan) |
| `button.battery_opt_apply_plan` | Run an executor tick now (active mode): apply the current quarter's state without waiting for the boundary. It is the real tick — validation, the override switch and the health latch all apply — and it writes nothing if the state is already commanded. Handy right after re-enabling the actuation switch |

## Dashboards

`sensor.battery_opt_current_price` carries the delivered prices in
its `prices` attribute and `sensor.battery_opt_plan` carries the plan
in `schedule`. Both are flat lists of segments — `{start, end, …}`
with full ISO timestamps — covering today and, once OMIE publishes
D+1 (~13:30 CET), tomorrow as well: price segments carry
`price_eur_kwh` + `tar_period` (split at every TAR boundary, so each
value is directly checkable against the tariff table), schedule
segments carry `direction` + `power_w` (hold windows are simply
absent). An
[ApexCharts card](https://github.com/RomRider/apexcharts-card) (HACS)
can graph both against each other — set `graph_span: 48h` to see
tomorrow once it arrives:

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Battery Opt — plan
graph_span: 48h
span:
  start: day
now:
  show: true
  label: now
series:
  - entity: sensor.battery_opt_current_price
    name: Price (EUR/kWh)
    type: line
    yaxis_id: price
    # Without this the card's default (extend_to: end) drags the last
    # value flat to the edge of graph_span once the data runs out.
    extend_to: false
    data_generator: |
      const pts = [];
      (entity.attributes.prices || []).forEach(s => {
        const end = new Date(s.end).getTime();
        for (let t = new Date(s.start).getTime(); t < end; t += 900000)
          pts.push([t, s.price_eur_kwh]);
      });
      return pts;
  - entity: sensor.battery_opt_plan
    name: Charge (W)
    type: column
    yaxis_id: power
    extend_to: false
    data_generator: |
      const pts = [];
      (entity.attributes.schedule || [])
        .filter(s => s.direction === "charge")
        .forEach(s => {
          const end = new Date(s.end).getTime();
          for (let t = new Date(s.start).getTime(); t < end; t += 900000)
            pts.push([t, s.power_w]);
        });
      return pts;
  - entity: sensor.battery_opt_plan
    name: Discharge (W)
    type: column
    yaxis_id: power
    extend_to: false
    data_generator: |
      const pts = [];
      (entity.attributes.schedule || [])
        .filter(s => s.direction === "discharge")
        .forEach(s => {
          const end = new Date(s.end).getTime();
          for (let t = new Date(s.start).getTime(); t < end; t += 900000)
            pts.push([t, -s.power_w]);
        });
      return pts;
yaxis:
  - id: price
    # Soft zero: the axis always starts at 0 but still extends below
    # for negative OMIE prices — never assume prices >= 0.
    min: ~0
    decimals: 3
    apex_config:
      title:
        text: EUR/kWh
  - id: power
    min: -2500
    max: 2500
    opposite: true
    apex_config:
      title:
        text: W
```

**Greedy vs static:** `sensor.battery_opt_plan` also carries
`static_schedule` — the fixed seasonal baseline in the same segment
format — so a second card can overlay the two plans. This is the
Task 12 dry-run comparison: while **Dry-run** is on, the *static*
series is what the executor actuates and the *greedy* columns are
what dynamic mode would do instead; after Checkpoint C the roles
swap. Charge is drawn positive, discharge negative, for both:

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Battery Opt — greedy vs static
graph_span: 48h
span:
  start: day
now:
  show: true
  label: now
series:
  - entity: sensor.battery_opt_plan
    name: Greedy (W)
    type: column
    extend_to: false
    data_generator: |
      const pts = [];
      (entity.attributes.schedule || []).forEach(s => {
        const sign = s.direction === "charge" ? 1 : -1;
        const end = new Date(s.end).getTime();
        for (let t = new Date(s.start).getTime(); t < end; t += 900000)
          pts.push([t, sign * s.power_w]);
      });
      return pts;
  - entity: sensor.battery_opt_plan
    name: Static (W)
    type: line
    curve: stepline
    stroke_width: 2
    extend_to: false
    data_generator: |
      const pts = [];
      (entity.attributes.static_schedule || []).forEach(s => {
        const sign = s.direction === "charge" ? 1 : -1;
        const end = new Date(s.end).getTime();
        for (let t = new Date(s.start).getTime(); t < end; t += 900000)
          pts.push([t, sign * s.power_w]);
      });
      return pts;
yaxis:
  - min: -2500
    max: 2500
    apex_config:
      title:
        text: W
```

(The stepline drops to gaps between windows rather than drawing a
zero baseline — hold periods are simply absent from both series, as
in the `schedule` attribute itself.)

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
now:
  show: true
  label: now
series:
  - entity: sensor.battery_opt_soc_forecast
    name: Forecast (%)
    type: line
    extend_to: false
    data_generator: |
      const t = entity.attributes.trajectory_pct || [];
      const dayStart = new Date(entity.attributes.plan_date + "T00:00:00");
      return t.map((v, i) => [dayStart.getTime() + i * 15 * 60 * 1000, v]);
  - entity: sensor.marstek_battery_state_of_charge
    name: Real (%)
    type: line
    extend_to: now
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
