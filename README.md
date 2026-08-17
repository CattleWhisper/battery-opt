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

Both plans keep the CHARGE state **armed through the whole cheap
window** even after their model reaches capacity (real load runs
above forecast and the charge loop throttles under house load, so the
planned energy is a floor, not a guarantee): the charge loop drives
full power and the battery's own firmware target stops at actual
full. Armed quarters appear in `schedule` / `static_schedule` as ~0 W
charge segments — by design. The greedy **chains its own end across
days** (persisted, restart-proof): today starts where yesterday's
greedy ended — after a sell-down that means low, so it plans the
night's cheap charge before each morning ponta — and the regime
default (static chain under dry-run, reserve floor under dynamic)
applies only when no yesterday record exists.

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
| `sensor.battery_opt_soc_forecast` | Planned SoC for the current quarter (%, same unit as the Marstek's own SoC sensor — overlay the two to compare forecast vs real); full day trajectory in attributes, plus `greedy_trajectory_pct` / `static_trajectory_pct` for the both-plans overlay — spanning 48 h once tomorrow's preview builds. Trajectories include the measured standby self-discharge (`self_discharge_w` option, default 19 W) |
| `sensor.battery_opt_best_periods` | Start of the next best period to run high-power appliances (timestamp). Periods are **maximal** cheap stretches — every run of quarters at or below the day's minimum + 20% of its price range, at least 30 min long, top 3, in time order. Attributes carry `periods` / `tomorrow_periods` (`{start, end, avg_price_eur_kwh}`), the mirrored `expensive_periods` / `tomorrow_expensive_periods` (top of the range — the "avoid these" tier), each day's cheap cutoff and average price — same semantics as the `battery_opt.get_best_periods` service |
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
its `prices` attribute and `sensor.battery_opt_plan` carries both
plans: the advisory greedy in `schedule` and the fixed seasonal
baseline in `static_schedule`. All three are flat lists of segments —
`{start, end, …}` with full ISO timestamps — covering today and, once
OMIE publishes D+1 (~13:30 CET), tomorrow as well: price segments
carry `price_eur_kwh` + `tar_period` (split at every TAR boundary, so
each value is directly checkable against the tariff table), schedule
segments carry `direction` + `power_w` (hold windows are simply
absent). One
[ApexCharts card](https://github.com/RomRider/apexcharts-card) (HACS)
graphs all of it: the price, and each plan as ONE signed series —
charge positive, discharge negative, holds as explicit zeros. This is
the Task 12 dry-run comparison: while **Dry-run** is on, the *static*
line is what the executor actuates and the *greedy* columns are what
dynamic mode would do instead; after Checkpoint C the roles swap.

Both plan generators walk the span at 15-min steps from midnight of
`plan_date` to the end of the last day any segment covers, emitting 0
wherever no window covers the instant. The zeros matter twice: a line
series bridges gaps straight across otherwise, and the header's
`before_now` value would linger on the previous window instead of
showing 0 during a hold.

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Battery Opt — plan
  # Header states show the value AT the now-marker (in_header:
  # before_now per series). The legend value cannot do that — it is
  # always the series' LAST datapoint (end of tomorrow), so it is
  # turned off per series below.
  show_states: true
  colorize_states: true
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
    # Header/tooltip decimals (the card's default of 1 rounds
    # 0.276 -> "0.3"); matches the price axis's 3 decimals.
    float_precision: 3
    show:
      legend_value: false
      in_header: before_now
    data_generator: |
      const pts = [];
      (entity.attributes.prices || []).forEach(s => {
        const end = new Date(s.end).getTime();
        for (let t = new Date(s.start).getTime(); t < end; t += 900000)
          pts.push([t, s.price_eur_kwh]);
      });
      return pts;
  - entity: sensor.battery_opt_plan
    name: Greedy (W)
    type: column
    yaxis_id: power
    extend_to: false
    show:
      legend_value: false
      in_header: before_now
    data_generator: |
      const segs = entity.attributes.schedule || [];
      if (!segs.length) return [];
      const spans = segs.map(s => [
        new Date(s.start).getTime(),
        new Date(s.end).getTime(),
        (s.direction === "charge" ? 1 : -1) * s.power_w,
      ]);
      const t0 = new Date(entity.attributes.plan_date + "T00:00:00").getTime();
      // Midnight after the last covered day (-1 ms so an
      // exactly-midnight end does not add a whole day of zeros).
      const last = new Date(Math.max(...spans.map(s => s[1])) - 1);
      const tEnd = new Date(
        last.getFullYear(), last.getMonth(), last.getDate() + 1
      ).getTime();
      const pts = [];
      for (let t = t0; t < tEnd; t += 900000) {
        const hit = spans.find(s => t >= s[0] && t < s[1]);
        pts.push([t, hit ? hit[2] : 0]);
      }
      return pts;
  - entity: sensor.battery_opt_plan
    name: Static (W)
    type: line
    curve: stepline
    stroke_width: 2
    yaxis_id: power
    extend_to: false
    show:
      legend_value: false
      in_header: before_now
    data_generator: |
      const segs = entity.attributes.static_schedule || [];
      if (!segs.length) return [];
      const spans = segs.map(s => [
        new Date(s.start).getTime(),
        new Date(s.end).getTime(),
        (s.direction === "charge" ? 1 : -1) * s.power_w,
      ]);
      const t0 = new Date(entity.attributes.plan_date + "T00:00:00").getTime();
      const last = new Date(Math.max(...spans.map(s => s[1])) - 1);
      const tEnd = new Date(
        last.getFullYear(), last.getMonth(), last.getDate() + 1
      ).getTime();
      const pts = [];
      for (let t = t0; t < tEnd; t += 900000) {
        const hit = spans.find(s => t >= s[0] && t < s[1]);
        pts.push([t, hit ? hit[2] : 0]);
      }
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

**SoC — forecast vs real:** `sensor.battery_opt_soc_forecast` carries
the planned SoC for the current quarter (%, same unit as the Marstek
SoC sensor) and the whole planned day in its `trajectory_pct` /
`trajectory_kwh` attributes (97 boundary values; index i = start of
quarter i — the ACTUATED plan's trajectory when a battery is
configured). It also carries both plans separately —
`greedy_trajectory_pct` and `static_trajectory_pct` — and once
tomorrow's preview builds (~13:30) those two span the full 48 h (193
boundary values), so the overlay covers the same window as the plan
card. Both lines are continuous across midnight: the static chains
its own end, and tomorrow's greedy is seeded from today's greedy end
— so after a full sell-down the preview shows the overnight charge
the greedy would plan from that low start. All forecast lines include
the battery's measured standby self-discharge (the `self_discharge_w`
option, default 19 W — tune it in the integration options as your own
measurements accumulate), so a held charge sags gently toward the
reserve floor instead of pretending to hold flat. The plan sensor's
`executor_plan_source` says which of the two is actually driving the
battery. This is the comparison Checkpoint C watches:

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Battery Opt — SoC forecast vs real
  show_states: true
  colorize_states: true
graph_span: 48h
span:
  start: day
now:
  show: true
  label: now
series:
  - entity: sensor.marstek_battery_state_of_charge
    name: Real (%)
    type: line
    extend_to: now
    show:
      legend_value: false
      in_header: before_now
  - entity: sensor.battery_opt_soc_forecast
    name: Static (%)
    type: line
    extend_to: false
    show:
      legend_value: false
      in_header: before_now
    data_generator: |
      const t = entity.attributes.static_trajectory_pct || [];
      const dayStart = new Date(entity.attributes.plan_date + "T00:00:00");
      return t.map((v, i) => [dayStart.getTime() + i * 15 * 60 * 1000, v]);
  - entity: sensor.battery_opt_soc_forecast
    name: Greedy (%)
    type: line
    extend_to: false
    show:
      legend_value: false
      in_header: before_now
    data_generator: |
      const t = entity.attributes.greedy_trajectory_pct || [];
      const dayStart = new Date(entity.attributes.plan_date + "T00:00:00");
      return t.map((v, i) => [dayStart.getTime() + i * 15 * 60 * 1000, v]);
yaxis:
  - min: 0
    max: 100
```

(Point the first series at your Marstek SoC entity id. While Dry-run
is on, the real SoC should track the *static* line; after the flip,
the *greedy* one.)

**Energy dashboard:** `sensor.battery_opt_current_price` is declared
exactly like core OMIE's own price sensor (EUR/kWh, `state_class`
measurement), so Settings → Dashboards → Energy accepts it directly
as the grid consumption tab's "use an entity with current price"
source — no template sensor needed. If a grid-import energy sensor is
configured (`CONF_GRID_ENERGY_SENSOR`), `sensor.battery_opt_cost_today`
can be added there too as a daily cost entity (`state_class` TOTAL,
resets at local midnight).

## Best periods for appliances

The `battery_opt.get_best_periods` service finds the cheap periods of
a day's delivered prices — where to run the dishwasher, washing
machine or dryer. Each period is **maximal**: a whole contiguous
stretch of cheap quarter-hours, never a fixed-duration clip out of
its middle. On a day where a ponta block splits the morning dip from
the afternoon valley, the response reads like *08:45–09:15 and
12:15–16:00* — the full stretches, in time order.

"Cheap" is relative to the day itself: a quarter qualifies at or
below `min + threshold% × (max − min)`. The default threshold (20%)
keeps the deep valleys and the dips just above them, and drops the
merely-average night plateau; it scales with the day's own spread, so
flat days honestly report "any time" and volatile days only their
true valleys. Fields (all optional): `day` (`today` / `tomorrow` —
tomorrow only after OMIE publishes, ~13:30 CET), `min_duration`
(stretches shorter than this are dropped; default 30 min),
`threshold` (percent of the day's price range; default 20), `count`
(at most this many periods, cheapest kept; default 3) and `after` /
`before` time-of-day bounds — cheapness is judged *within* the
bounds, so "from 08:00 on" ranks against what is actually reachable.
The response lists `periods` in time order
(`start` / `end` / `avg_price_eur_kwh`) plus `day_avg_price_eur_kwh`
and `threshold_price_eur_kwh` (the cutoff used). It is deliberately
price-only: the delivered price is the correct marginal signal for
the extra grid energy an appliance draws, whether the battery covers
it or not.

`sensor.battery_opt_best_periods` is the same detection as a sensor,
at the defaults: state = start of the next period that has not ended
yet, both days' lists (and cheap cutoffs) in the attributes. Add it
to the plan card to see the periods on the price line — flat strokes
at each period's average price:

```yaml
  - entity: sensor.battery_opt_best_periods
    name: Best periods (EUR/kWh)
    type: line
    yaxis_id: price
    stroke_width: 5
    extend_to: false
    show:
      legend_value: false
      in_header: false
    data_generator: |
      const ps = [
        ...(entity.attributes.periods || []),
        ...(entity.attributes.tomorrow_periods || []),
      ].sort((a, b) => new Date(a.start) - new Date(b.start));
      return ps.flatMap((p) => [
        [new Date(p.start).getTime(), p.avg_price_eur_kwh],
        [new Date(p.end).getTime(), p.avg_price_eur_kwh],
        [new Date(p.end).getTime() + 1, null],
      ]);
```

**Traffic-light day strip** — a compact card painting the whole day
(48 h once tomorrow publishes) green / yellow / red: green = the
cheap periods, red = the mirrored expensive tier
(`expensive_periods`, maximal runs at or above the day's maximum
minus 20% of its range — typically the ponta blocks), yellow =
everything in between, computed as the complement:

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Battery Opt — day tiers
graph_span: 48h
span:
  start: day
now:
  show: true
  label: now
apex_config:
  chart:
    height: 140
  legend:
    show: false
yaxis:
  - min: 0
    max: 1
    show: false
all_series_config:
  type: area
  curve: stepline
  stroke_width: 0
  opacity: 1
  extend_to: false
  show:
    legend_value: false
    in_header: false
series:
  - entity: sensor.battery_opt_best_periods
    name: Cheap
    color: '#43a047'
    data_generator: |
      const ps = [
        ...(entity.attributes.periods || []),
        ...(entity.attributes.tomorrow_periods || []),
      ];
      return ps.flatMap((p) => [
        [new Date(p.start).getTime(), 1],
        [new Date(p.end).getTime(), 1],
        [new Date(p.end).getTime() + 1, null],
      ]);
  - entity: sensor.battery_opt_best_periods
    name: Expensive
    color: '#e53935'
    data_generator: |
      const ps = [
        ...(entity.attributes.expensive_periods || []),
        ...(entity.attributes.tomorrow_expensive_periods || []),
      ];
      return ps.flatMap((p) => [
        [new Date(p.start).getTime(), 1],
        [new Date(p.end).getTime(), 1],
        [new Date(p.end).getTime() + 1, null],
      ]);
  - entity: sensor.battery_opt_best_periods
    name: Mid
    color: '#fbc02d'
    data_generator: |
      const a = entity.attributes;
      const covered = [
        ...(a.periods || []),
        ...(a.tomorrow_periods || []),
        ...(a.expensive_periods || []),
        ...(a.tomorrow_expensive_periods || []),
      ]
        .map((p) => [new Date(p.start).getTime(), new Date(p.end).getTime()])
        .sort((x, y) => x[0] - y[0]);
      const dayStart = new Date(a.plan_date + "T00:00:00").getTime();
      const days = a.tomorrow_day_avg_price_eur_kwh != null ? 2 : 1;
      const spanEnd = dayStart + days * 24 * 3600 * 1000;
      const data = [];
      let cursor = dayStart;
      for (const [s, e] of covered) {
        if (s > cursor) data.push([cursor, 1], [s, 1], [s + 1, null]);
        cursor = Math.max(cursor, e);
      }
      if (cursor < spanEnd) {
        data.push([cursor, 1], [spanEnd, 1], [spanEnd + 1, null]);
      }
      return data;
```

(The strip honestly shows only what the tiers detect: on a flat day
the whole strip is green — any time is fine — and days without a
published tomorrow simply end at midnight.)

And the daily notification — a morning digest of today's periods you
can still use (for a tomorrow-evening digest instead, trigger after
~13:35 with `day: tomorrow` and no `after`):

```yaml
alias: Daily best appliance periods
triggers:
  - trigger: time
    at: "08:00:00"
actions:
  - action: battery_opt.get_best_periods
    data:
      day: today
      after: "08:00:00"
    response_variable: best
  - action: notify.mobile_app_your_phone
    data:
      title: Cheapest periods today
      message: >-
        {% for p in best.periods -%}
        {{ as_timestamp(p.start) | timestamp_custom('%H:%M') }}–{{
        as_timestamp(p.end) | timestamp_custom('%H:%M') }}:
        {{ '%.3f' | format(p.avg_price_eur_kwh) }} €/kWh
        {{ '\n' }}
        {%- endfor %}
        Day average {{ '%.3f' | format(best.day_avg_price_eur_kwh) }} €/kWh
mode: single
```

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
