#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Better Call CineCrew: Consistent Ultra-Long Narrative-to-Film Generation
# Paper: ECCV 2026
# Copyright (c) 2026 Sixun Dong
# Author: Sixun Dong
# Licensed under the Apache License, Version 2.0
"""
Budget ledger: reserve before dispatch, settle after.

  reserve(project, amount)  BEGIN IMMEDIATE: committed + active reservations + amount <= limit, else BudgetExceeded
  commit(reservation, usd)  settle with the actual (or estimated) cost; the row keeps the original estimate
  release(reservation)      the call never reached the provider (or definitively was not billed)

Because the check and the insert happen under SQLite's write lock, any number of concurrent workers
(threads or processes) cannot overcommit. An unknown price never reserves: the caller must have a
configured conservative upper bound, otherwise the operation is blocked (BudgetBlocked).
"""

from typing import Any, Dict, Optional

from .store import Store, new_id, now


class BudgetExceeded(Exception):
    pass


class BudgetBlocked(Exception):
    """Cost is unknown and no upper bound is configured: the paid call is refused."""


class Budget:
    def __init__(self, store: Store):
        self.store = store

    def limit(self, project_id: str) -> float:
        row = self.store.get("projects", project_id)
        return float(row["budget_limit_usd"]) if row else 0.0

    def summary(self, project_id: str) -> Dict[str, Any]:
        rows = self.store.q("SELECT state, kind, SUM(amount_usd) AS s, COUNT(*) AS n FROM ledger WHERE project_id = ? "
                            "GROUP BY state, kind", (project_id,))
        committed = sum(r["s"] or 0 for r in rows if r["state"] == "committed")
        reserved = sum(r["s"] or 0 for r in rows if r["state"] == "reserved")
        lim = self.limit(project_id)
        by_kind: Dict[str, float] = {}
        for r in rows:
            if r["state"] == "committed":
                by_kind[r["kind"]] = round(by_kind.get(r["kind"], 0) + (r["s"] or 0), 6)
        return {"limit_usd": lim, "committed_usd": round(committed, 6), "reserved_usd": round(reserved, 6),
                "remaining_usd": round(lim - committed - reserved, 6), "committed_by_kind": by_kind}

    def reserve(self, project_id: str, amount: Optional[float], kind: str, job_id: str = None,
                attempt_id: str = None, note: str = "") -> str:
        if amount is None:
            raise BudgetBlocked(f"{kind}: price unknown and no max_usd_per_call configured — refusing a paid call")
        amount = float(amount)
        rid = new_id("res")
        with self.store.tx(immediate=True) as c:
            lim = c.execute("SELECT budget_limit_usd FROM projects WHERE id = ?", (project_id,)).fetchone()
            lim = float(lim[0]) if lim else 0.0
            used = c.execute("SELECT COALESCE(SUM(amount_usd), 0) FROM ledger WHERE project_id = ? AND state IN "
                             "('reserved', 'committed')", (project_id,)).fetchone()[0]
            if amount > 0 and used + amount > lim + 1e-9:
                raise BudgetExceeded(f"{kind}: needs ${amount:.4f}, remaining ${lim - used:.4f} of ${lim:.2f}")
            self.store.insert("ledger", {
                "id": rid, "project_id": project_id, "job_id": job_id, "attempt_id": attempt_id, "kind": kind,
                "state": "reserved", "amount_usd": amount, "estimate_usd": amount, "cost_basis": None, "note": note,
                "created_at": now(), "settled_at": None,
            }, c)
        return rid

    def commit(self, reservation_id: str, actual_usd: Optional[float], basis: str) -> None:
        with self.store.tx(immediate=True) as c:
            row = c.execute("SELECT estimate_usd, state FROM ledger WHERE id = ?", (reservation_id,)).fetchone()
            if row is None or row[1] != "reserved":
                return
            amount = row[0] if actual_usd is None else float(actual_usd)
            self.store.update("ledger", "id", reservation_id, {
                "state": "committed", "amount_usd": amount, "cost_basis": basis if actual_usd is not None else
                "estimate (actual unknown)", "settled_at": now()}, c)

    def release(self, reservation_id: str, note: str = "") -> None:
        with self.store.tx(immediate=True) as c:
            self.store.update("ledger", "id", reservation_id,
                              {"state": "released", "amount_usd": 0.0, "note": note, "settled_at": now()}, c,
                              where_extra=" AND state = 'reserved'")
