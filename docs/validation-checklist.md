# Validation checklist — bench to Checkpoint C

Owner-tracked working document (created 2026-08-08). Tick items as
they pass, add dates and notes inline; record detailed measurements
in `docs/findings.md`. The phases are ordered by dependency: nothing
actuates in production until Phase 1 is clean, and Phase 1 must be
**re-run after every firmware OTA** — the watchdog and mode-flip
semantics are undocumented firmware behaviour (spec §8).

---

## Phase 0 — update the running instance (~15 min)

- [X] HACS → Battery Opt → Redownload (main), restart HA
- [X] Configure → re-point the optional sensors at the corrected
      measurement entities:
  - [X] House load sensor (forecast warms up over ~4 weeks of
        history; the flat fallback covers it meanwhile)
  - [X] Battery power sensor — verify the sign: **positive must mean
        charging** (feeds the charge loop AND realised savings)
  - [X] Grid import power sensor
  - [X] Grid import energy sensor
- [X] Paste the new dashboard YAML from the README (both cards) —
      the old `charge_w` / `prices_eur_kwh` attributes are gone
- [X] Sanity pass on the entities:
  - [X] `sensor.battery_opt_soc_forecast` trajectory starts at the
        chained seed (a summer Saturday starts at 100%, not 27%)
  - [X] `sensor.battery_opt_plan` → `schedule` is a segment list;
        after ~14:30 it includes tomorrow's segments
  - [X] `sensor.battery_opt_current_price` → `prices` segments split
        exactly at TAR boundaries; spot-check one value against
        `docs/tariff-reference.md`
  - [X] `sensor.battery_opt_realised_savings` present, not
        `unavailable`
  - [X] `button.battery_opt_recalculate_plan` refreshes the sensors
  - [X] `button.battery_opt_apply_plan` writes the current state (or
        nothing, if already commanded)

## Phase 1 — spec §8 on-device checklist (gates ALL actuation)

Re-run this whole block after every firmware OTA. Record each result
in `docs/findings.md`.

- [X] 1. **Watchdog kill-test**: kill the integration with
      force-charge active; time the self-stop (~15–30 s expected).
      Determines whether shutdown safety writes are belt-and-braces
      or load-bearing
      → **2026-08-11: NO watchdog.** Network cable pulled during an
      RS485-controlled charge; charging continued for over 2 min —
      the reported ~15–30 s self-stop never fired. Treated as: no
      watchdog on this firmware. The 42011 charge-to-SoC backstop
      (item 3) is the load-bearing stop for a dead integration
      mid-charge
- [X] 2. **43000 semantics**: writable directly; entering force mode
      flips it to `manual`; releasing external control alone does
      NOT restore anti-feed
      → 2026-08-11: entering force mode does flip it; releasing
      external control *appeared* to restore anti-feed on this
      firmware, contrary to the community report — but nothing is
      assumed: the executor keeps force-setting 43000 on every
      transition
- [X] 3. **42011 backstop**: charge-to-SoC works alongside
      force-charge (charge stops at the target)
- [X] 4. **44001 write**: accepts 27% on this firmware (expected
      MISSING on the V3 — a clean failure is itself the answer)
      → 2026-08-11: not exposed by the integration for the V3
      (upstream register map: MISSING) — the expected clean absence.
      Cutoff config fields stay empty; the device's internal minimum
      is the run-time floor (ADR-0008)
- [X] 5. **DISCHARGE → HOLD**: anti-feed disengages cleanly when
      external control takes over
- [X] 6. **Meter-pairing loss during anti-feed**: discharge stops
      dead (safe) or misbehaves?
      → 2026-08-11: stops dead — no CT/meter data means no output.
      Safe
- [X] 7. **Polling keepalive**: polling at the configured scan
      interval suppresses the watchdog indefinitely (hold force mode
      ≥10 min with polling on)
      → 2026-08-11: moot — there is no watchdog to suppress (item 1)

## Phase 2 — bench drills (after Phase 1 passes)

- [X] **Task 15 spike drill**: kettle/AC during a charge window —
      total grid import never exceeds 4400 W on the meter; setpoint
      recovers after the spike
- [X] **Task 15 fallback drill**: grid power sensor made unavailable
      mid-charge — static 2000 W fallback observed
      (`charge_loop_fallback: true` on the plan sensor), no
      `healthy` flap, automatic recovery
- [ ] **Task 9 power-off test**: battery powered off — `healthy`
      goes off via the 3-strike policy at the next state-transition
      write (a few hours' latency is by design, ADR-0008)
- [X] **Task 14 48 h bench soak**: all three states cycling on the
      real schedule, executor actuation ON — zero export on the
      meter for the full 48 h

## Phase 3 — 2-week static soak (Checkpoint C)

- [ ] 2 weeks in production on the static plan
- [ ] Zero export recorded over the whole window
- [ ] SoC never below 27% (on the Marstek's own SoC sensor)
- [ ] Ponta coverage ≥95% (summer criterion; watch the SoC
      forecast-vs-real overlay daily — the day-chaining fix is what
      makes summer coverage possible at all)
- [ ] **Human review of the soak results** — the Checkpoint C gate.
      Only after it: Task 12 (dynamic actuation swap), then 1 week
      of dry-run comparison before going live

## Ongoing / calendar-driven

- [ ] **First invoice month reconciled manually** against
      `sensor.battery_opt_cost_today`'s monthly statistic (Task 13
      verification); the monthly notification on the 1st gives
      realised-vs-forecast with the ±10% flag
- [ ] **Load-forecast accuracy** (Task 11): after ~4 weeks on the
      corrected sensor, `sensor.battery_opt_load_mae` trending down
      to a sane error
- [ ] **Seasonal switches** (last Sundays of March/October): perform
      the manual calendar verification the integration's
      notification asks for

---

## Log

| Date | Item | Result / notes |
|---|---|---|
| 2026-08-11 | Phase 1 item 1 | **No watchdog**: force-charge survived a network cut for >2 min. The 42011 backstop is load-bearing |
| 2026-08-11 | Phase 1 item 2 | Releasing external control appeared to restore anti-feed; assume nothing — 43000 stays force-set on every transition |
| 2026-08-11 | Phase 1 item 4 | 44001 not exposed for the V3 (upstream MISSING) — expected; device internal minimum governs |
| 2026-08-11 | Phase 1 item 6 | Pairing/CT loss → output stops dead (no CT = no generation). Safe |
| 2026-08-11 | Phase 1 item 7 | Moot — no watchdog (item 1) |
