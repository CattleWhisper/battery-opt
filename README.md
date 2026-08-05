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
