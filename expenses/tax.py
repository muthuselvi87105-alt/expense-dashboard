"""GST calculation support (SRS §8).

The system calculates expected tax from taxable value, rate and GST type,
keeps the source/declared values separately, and flags a variance when they
differ by more than the configured tolerance. It never decides ITC
eligibility.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

from .models import GSTType

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
COMPONENTS = ("cgst_amount", "sgst_utgst_amount", "igst_amount", "cess_amount")
NO_TAX_TYPES = {GSTType.EXEMPT, GSTType.NIL_RATED, GSTType.ZERO_RATED, GSTType.NON_GST}


def q(value):
    return Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def tolerance():
    return Decimal(settings.EXPENSE_SETTINGS["TAX_ROUNDING_TOLERANCE"])


def infer_gst_type(supplier_state, place_of_supply):
    """A hint only: Finance-approved rules decide the actual treatment."""
    if supplier_state and place_of_supply:
        return GSTType.CGST_SGST if supplier_state == place_of_supply else GSTType.IGST
    return ""


def calculate(gst_type, taxable_value, rate, cess=ZERO, other_charges=ZERO, supplier_state="", place_of_supply=""):
    taxable = q(taxable_value)
    rate = Decimal(rate or 0)
    out = {c: ZERO for c in COMPONENTS}
    effective = gst_type
    if gst_type == GSTType.RCM:
        # Supplier does not charge GST under reverse charge; liability is noted, not added to gross.
        effective = infer_gst_type(supplier_state, place_of_supply) or GSTType.IGST
        tax = q(taxable * rate / 100)
        out["rcm_liability"] = tax
    elif gst_type == GSTType.CGST_SGST:
        half = q(taxable * rate / 200)
        out["cgst_amount"] = half
        out["sgst_utgst_amount"] = half
    elif gst_type == GSTType.IGST:
        out["igst_amount"] = q(taxable * rate / 100)
    out["cess_amount"] = q(cess)
    tax_total = sum(out[c] for c in COMPONENTS)
    out["gross_amount"] = q(taxable + tax_total + q(other_charges))
    out["rate"] = rate
    out["effective_type"] = effective
    return out


def apply_tax(expense, declared):
    """Populate calculated values, accepted components and variance on an expense.

    `declared` holds the component/gross values the user or source file supplied
    (only keys actually provided). Missing components are filled from the
    calculation; provided ones are kept as the recorded values.
    """
    if not expense.gst_type and expense.taxable_value:
        expense.gst_type = infer_gst_type(expense.supplier_state_code, expense.place_of_supply_state_code)
    if not expense.gst_type:
        expense.calculated_tax_values = {}
        expense.source_tax_values = {k: str(v) for k, v in declared.items()}
        for field in COMPONENTS:
            setattr(expense, field, q(declared.get(field, ZERO)))
        if "gross_amount" in declared:
            expense.gross_amount = q(declared["gross_amount"])
        else:
            expense.gross_amount = q(expense.taxable_value + expense.total_gst + expense.other_charges)
        expense.tax_variance = False
        expense.potential_itc_amount = ZERO
        return
    if expense.gst_type in NO_TAX_TYPES and not expense.taxable_value and "gross_amount" in declared:
        expense.taxable_value = q(Decimal(declared["gross_amount"]) - expense.other_charges)
    calc = calculate(expense.gst_type, expense.taxable_value, expense.gst_rate, declared.get("cess_amount", ZERO),
                     expense.other_charges, expense.supplier_state_code, expense.place_of_supply_state_code)
    expense.calculated_tax_values = {k: str(v) for k, v in calc.items()}
    expense.source_tax_values = {k: str(v) for k, v in declared.items()}
    variance = False
    tol = tolerance()
    for field in COMPONENTS + ("gross_amount",):
        if field in declared:
            value = q(declared[field])
            if abs(value - calc[field]) > tol:
                variance = True
        else:
            value = calc[field]
        setattr(expense, field, value)
    expense.tax_variance = variance
    # Potential ITC is informational only; verified ITC stays at zero until reviewed (§8.2).
    if expense.gst_type in NO_TAX_TYPES:
        expense.potential_itc_amount = ZERO
    elif expense.gst_type == GSTType.RCM:
        expense.potential_itc_amount = calc.get("rcm_liability", ZERO)
    else:
        expense.potential_itc_amount = expense.total_gst


GSTIN_PATTERN = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
