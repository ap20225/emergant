from decimal import Decimal, ROUND_HALF_UP


def cents(amount):
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


async def financials(store, simulated=False):
    payments = await store.rows("payments", {"simulated": simulated}, 10000)
    expenses = await store.rows("expenses", {"simulated": simulated}, 10000)
    s = await store.settings()
    verified = sum(p["amount_cents"] for p in payments if p["payment_status"] == "VERIFIED")
    gross = verified
    pending = sum(p["amount_cents"] for p in payments if p["payment_status"] == "PENDING")
    spent = sum(p["amount_cents"] for p in expenses)
    usage = [] if simulated else await store.rows('model_usage', limit=10000)
    model_nano = sum(u['actual_cost_nano_usd'] for u in usage if isinstance(u.get('actual_cost_nano_usd'), int))
    pending_model = any(u.get('actual_cost_nano_usd') is None for u in usage)
    # Preserve sub-cent provider charges; no local estimate is recognized as an invoice.
    spent += model_nano / 10000000
    profit = verified - spent
    reserve = max(0, verified * s["reserve_percent"] // 100)
    reinvestment = max(0, verified * s["reinvestment_percent"] // 100)
    experiment = max(0, verified - reserve - reinvestment)
    available = max(0, min(cents(s["authorized_budget"]) - spent, verified - reserve - spent))
    if pending_model:
        available = 0
    completed = await store.rows("jobs", {"simulated": simulated, "status": {"$in": ["MERGED", "PAID", "UNPAID", "REJECTED", "FAILED", "CANCELLED"]}}, 10000)
    hours = sum(j.get("elapsed_seconds", 0) for j in completed) / 3600
    paid_count = len([p for p in payments if p["payment_status"] == "VERIFIED"])
    return {"gross_revenue": gross / 100, "pending_receipts": pending / 100, "verified_revenue": verified / 100, "expenses": spent / 100,
        "net_profit": profit / 100, "reserve": reserve / 100, "reinvestment": reinvestment / 100,
        "experimentation": experiment / 100, "available_budget": available / 100,
        "authorized_budget": s["authorized_budget"], "verified_payments": paid_count,
        "roi": round(profit / spent * 100, 1) if spent else None,
        "cost_per_success": round(spent / 100 / paid_count, 2) if paid_count else None,
        "revenue_per_hour": round(verified / 100 / hours, 2) if hours else None,
        "level": sum(verified >= n for n in [2500, 10000, 50000, 200000]),
        "model_cost_nano_usd": model_nano if usage and not pending_model else None,
        "model_billing_pending": pending_model, "model_requests": len(usage),
        "simulated": simulated, "payments": payments, "expense_entries": expenses}