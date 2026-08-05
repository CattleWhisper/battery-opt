"""
Domain logic, free of Home Assistant imports (ADR-0001).

Nothing in this package may import `homeassistant`. This is what allows
the same code to run under pytest, in the backtest, and in production.
"""
