"""Intercompany cost allocation (SRS §9). The supplier invoice is stored once;
allocations are separate lines and never add to consolidated external spend."""
from decimal import Decimal

from django.db import transaction

from .audit import audit
from .models import AllocationMethod, IntercompanyAllocation
from .tax import q

HUNDRED = Decimal("100")


class AllocationError(Exception):
    pass


def compute(base, lines):
    """Validate lines and compute amounts. Rounding residual goes to the last
    percentage line when percentages total exactly 100 (deterministic rule)."""
    if not lines:
        return [], base
    methods = {l["method"] for l in lines}
    if len(methods) > 1:
        raise AllocationError("Use either percentage or fixed-amount lines, not both.")
    method = methods.pop()
    out = []
    if method == AllocationMethod.PERCENT:
        total_pct = sum(Decimal(l.get("percent") or 0) for l in lines)
        if total_pct > HUNDRED:
            raise AllocationError(f"Percentages total {total_pct}%, which is over 100%.")
        for l in lines:
            pct = Decimal(l.get("percent") or 0)
            out.append({**l, "percent": pct, "amount": q(base * pct / HUNDRED)})
        if total_pct == HUNDRED:
            residual = base - sum(l["amount"] for l in out)
            out[-1]["amount"] += residual
    else:
        for l in lines:
            out.append({**l, "percent": None, "amount": q(l.get("amount") or 0)})
        if sum(l["amount"] for l in out) > base:
            raise AllocationError(f"Fixed allocations exceed the allocation base of {base}.")
    unallocated = base - sum(l["amount"] for l in out)
    return out, unallocated


@transaction.atomic
def replace_allocations(user, expense, lines):
    computed, unallocated = compute(expense.gross_amount, lines)
    before = [(a.beneficiary_entity_id, str(a.amount)) for a in expense.allocations.all()]
    expense.allocations.all().delete()
    for i, l in enumerate(computed, start=1):
        IntercompanyAllocation.objects.create(
            expense=expense, line_no=i, beneficiary_entity=l["beneficiary_entity"], method=l["method"],
            percent=l["percent"], amount=l["amount"], recharge_amount=l.get("recharge_amount") or 0,
            recharge_status=l.get("recharge_status") or "DRAFT", reference=l.get("reference", ""),
            reason=l.get("reason", ""))
    audit(user, "allocation.replace", expense, {
        "before": before, "after": [(str(l["beneficiary_entity"].pk), str(l["amount"])) for l in computed],
        "unallocated": str(unallocated)})
    return unallocated
