"""Settings loading.

All tunable numbers live in ``config/settings.yaml``. Secrets (API keys, TOTP
secrets, WhatsApp keys) are read from environment variables only and are never
stored in the YAML file or in git.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS = ROOT / "config" / "settings.yaml"

# Hard ceilings that no config file can override. These exist so that a typo
# such as ``risk_pct: 20`` instead of ``2.0`` can never blow up the account.
HARD_MAX_RISK_PER_TRADE_PCT = 2.0
HARD_MAX_POSITION_PCT = 50.0
HARD_MAX_PORTFOLIO_HEAT_PCT = 8.0
HARD_MAX_STOP_DISTANCE_PCT = 10.0
HARD_MAX_ORDERS_PER_DAY = 20


@dataclass
class RiskProfile:
    """Risk limits for one account-size band.

    Small accounts need a different profile than large ones: fixed charges
    (DP charge per sell, minimum brokerage) are a much bigger share of a
    ₹4,000 position than of a ₹2,00,000 position.
    """

    name: str
    max_equity: float  # profile applies while equity < max_equity
    risk_pct: dict[str, float]  # per regime: bull / neutral / bear
    max_positions: dict[str, int]  # per regime
    max_position_pct: float
    max_portfolio_heat_pct: float
    partial_exit_enabled: bool

    def clamp(self) -> "RiskProfile":
        p = copy.deepcopy(self)
        p.risk_pct = {k: min(max(v, 0.0), HARD_MAX_RISK_PER_TRADE_PCT) for k, v in p.risk_pct.items()}
        p.max_position_pct = min(p.max_position_pct, HARD_MAX_POSITION_PCT)
        p.max_portfolio_heat_pct = min(p.max_portfolio_heat_pct, HARD_MAX_PORTFOLIO_HEAT_PCT)
        return p


@dataclass
class Settings:
    raw: dict[str, Any]
    profiles: list[RiskProfile] = field(default_factory=list)

    # convenient accessors -------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.raw.get(name, {}))

    def profile_for(self, equity: float) -> RiskProfile:
        for p in sorted(self.profiles, key=lambda p: p.max_equity):
            if equity < p.max_equity:
                return p
        return sorted(self.profiles, key=lambda p: p.max_equity)[-1]

    @property
    def mode(self) -> str:
        return str(self.raw.get("mode", "paper"))


def _build_profiles(raw: dict[str, Any]) -> list[RiskProfile]:
    profiles = []
    for name, p in raw.get("risk_profiles", {}).items():
        profiles.append(
            RiskProfile(
                name=name,
                max_equity=float(p.get("max_equity", float("inf"))),
                risk_pct={k: float(v) for k, v in p["risk_pct"].items()},
                max_positions={k: int(v) for k, v in p["max_positions"].items()},
                max_position_pct=float(p["max_position_pct"]),
                max_portfolio_heat_pct=float(p["max_portfolio_heat_pct"]),
                partial_exit_enabled=bool(p.get("partial_exit_enabled", True)),
            ).clamp()
        )
    if not profiles:
        raise ValueError("settings.yaml must define at least one risk profile")
    return profiles


def _validate(raw: dict[str, Any]) -> None:
    risk = raw.get("risk", {})
    if float(risk.get("max_stop_distance_pct", 8)) > HARD_MAX_STOP_DISTANCE_PCT:
        raise ValueError(f"max_stop_distance_pct cannot exceed {HARD_MAX_STOP_DISTANCE_PCT}")
    if float(risk.get("min_reward_risk", 2.0)) < 1.5:
        raise ValueError("min_reward_risk below 1.5 is not allowed")
    if raw.get("mode") not in {"backtest", "paper", "live"}:
        raise ValueError("mode must be one of: backtest, paper, live")
    if raw.get("broker") not in {"paper", "groww", "zerodha"}:
        raise ValueError("broker must be one of: paper, groww, zerodha")
    if raw.get("mode") == "live" and raw.get("broker") == "paper":
        raise ValueError("live mode needs a real broker (groww or zerodha)")


def load_settings(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    path = Path(path) if path else Path(os.environ.get("SWINGBOT_SETTINGS", DEFAULT_SETTINGS))
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if overrides:
        raw = _deep_merge(raw, overrides)
    _validate(raw)
    return Settings(raw=raw, profiles=_build_profiles(raw))


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def env(name: str, required: bool = True) -> str | None:
    value = os.environ.get(name)
    if required and not value:
        raise RuntimeError(
            f"Environment variable {name} is not set. Copy config/.env.example to .env and fill it in."
        )
    return value


def load_dotenv(path: str | Path | None = None) -> None:
    """Minimal .env loader (KEY=VALUE lines) so we don't need python-dotenv."""
    candidates = [Path(path)] if path else [ROOT / ".env", ROOT / "config" / ".env"]
    for p in candidates:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
