import re


def evaluate(opportunity, settings):
    o, s = opportunity, settings
    signals = {
        "reward": min(o["reward"] / 500, 1), "acceptance": o["probability_of_success"],
        "difficulty": (10 - o["estimated_difficulty"]) / 9,
        "competition": 1 - o["competition"], "repository_health": o["repository_health"],
        "payment_reliability": o["probability_of_payment"], "compatibility": o["agent_compatibility"],
    }
    expected_revenue = o["reward"] * o["probability_of_success"] * o["probability_of_payment"]
    profit = expected_revenue - o["estimated_compute_cost"] - o["estimated_tool_cost"]
    reasons = []
    content = f"{o['title']} {o['description']} {o.get('payment_terms', '')}"
    dangerous = re.search(r"(?i)credential theft|steal.{0,30}(password|wallet|crypto)|ransomware|phishing|exfiltrat|unauthorized (access|exploit)|seed phrase|private key|curl.{0,80}\|\s*(sh|bash)|rm\s+-rf|disable.{0,15}(security|auth)", content)
    if dangerous:
        reasons.append("Security policy: suspicious or unauthorized request; requires rejection/review")
    checks = [
        (o["reward"] < s["min_reward"], "Reward below minimum"),
        (o["estimated_hours"] > s["max_estimated_hours"], "Estimated hours exceed limit"),
        (o["probability_of_success"] < s["min_success_probability"], "Success probability below minimum"),
        (profit < s["min_expected_profit"], "Expected profit below minimum"),
        (o["estimated_compute_cost"] > s["max_compute_cost"], "Compute cost exceeds authorized threshold"),
        (o["risk_score"] > s["max_risk_score"], "Risk exceeds maximum"),
        (not o.get("payment_terms"), "Payment terms are unclear"),
    ]
    reasons.extend(reason for failed, reason in checks if failed)
    return {"score": round(sum(signals[k] * s["weights"][k] for k in signals), 1),
        "expected_revenue": round(expected_revenue, 2), "expected_profit": round(profit, 2),
        "expected_value_per_hour": round(profit / o["estimated_hours"], 2), "signals": signals,
        "qualified": not reasons, "reasons": reasons, "security_flagged": bool(dangerous),
        "estimate_basis": "Controlled fixture assumptions" if o.get("simulated") else "Unverified estimates; not a payment guarantee"}