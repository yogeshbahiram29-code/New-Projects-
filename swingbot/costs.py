"""Indian delivery (CNC) trading charges.

Numbers are approximate defaults from settings.yaml - verify them against
your broker's contract notes and update the config if they differ.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostModel:
    brokerage_pct: float = 0.0
    brokerage_min: float = 0.0
    brokerage_max: float = 0.0
    dp_charge_per_sell: float = 15.93
    stt_pct: float = 0.10
    stamp_duty_buy_pct: float = 0.015
    exchange_txn_pct: float = 0.00297
    sebi_fee_pct: float = 0.0001
    gst_pct: float = 18.0
    slippage_pct: float = 0.10

    @classmethod
    def from_settings(cls, costs_cfg: dict, broker: str) -> "CostModel":
        b = costs_cfg.get(broker, costs_cfg.get("paper", {}))
        return cls(
            brokerage_pct=float(b.get("brokerage_pct", 0.0)),
            brokerage_min=float(b.get("brokerage_min", 0.0)),
            brokerage_max=float(b.get("brokerage_max", 0.0)),
            dp_charge_per_sell=float(b.get("dp_charge_per_sell", 15.93)),
            stt_pct=float(costs_cfg.get("stt_pct", 0.10)),
            stamp_duty_buy_pct=float(costs_cfg.get("stamp_duty_buy_pct", 0.015)),
            exchange_txn_pct=float(costs_cfg.get("exchange_txn_pct", 0.00297)),
            sebi_fee_pct=float(costs_cfg.get("sebi_fee_pct", 0.0001)),
            gst_pct=float(costs_cfg.get("gst_pct", 18.0)),
            slippage_pct=float(costs_cfg.get("slippage_pct", 0.10)),
        )

    def brokerage(self, value: float) -> float:
        if self.brokerage_pct <= 0 or value <= 0:
            return 0.0
        b = value * self.brokerage_pct / 100
        b = max(b, self.brokerage_min)
        if self.brokerage_max > 0:
            b = min(b, self.brokerage_max)
        return b

    def _common(self, value: float) -> float:
        brk = self.brokerage(value)
        exch = value * self.exchange_txn_pct / 100
        sebi = value * self.sebi_fee_pct / 100
        gst = (brk + exch + sebi) * self.gst_pct / 100
        stt = value * self.stt_pct / 100
        return brk + exch + sebi + gst + stt

    def buy_cost(self, value: float) -> float:
        if value <= 0:
            return 0.0
        return self._common(value) + value * self.stamp_duty_buy_pct / 100

    def sell_cost(self, value: float) -> float:
        if value <= 0:
            return 0.0
        return self._common(value) + self.dp_charge_per_sell

    def round_trip(self, entry_value: float, exit_value: float | None = None) -> float:
        exit_value = entry_value if exit_value is None else exit_value
        return self.buy_cost(entry_value) + self.sell_cost(exit_value)
