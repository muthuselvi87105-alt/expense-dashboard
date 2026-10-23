"""Expense lifecycle (SRS §5.1, FR-WF-001..010)."""
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import access
from .audit import audit
from .models import EmployeeProfile, Notification, Role, RoleAssignment, Status, WorkflowEvent

MANAGER_ROLES = {Role.MANAGER, Role.SUPER_ADMIN}
FINANCE_ACT_ROLES = {Role.FINANCE_ADMIN, Role.ACCOUNTANT, Role.SUPER_ADMIN}


class WorkflowError(Exception):
    pass


@dataclass(frozen=True)
class Transition:
    from_states: frozenset
    to_state: str
    actor: str  # "submitter" | "manager" | "finance" | "finance_admin"
    comment_required: bool = False


T = Transition
TRANSITIONS = {
    "submit": T(frozenset({Status.DRAFT, Status.RETURNED}), Status.SUBMITTED, "submitter"),
    "start_review": T(frozenset({Status.SUBMITTED}), Status.MANAGER_REVIEW, "manager"),
    "approve": T(frozenset({Status.SUBMITTED, Status.MANAGER_REVIEW}), Status.MANAGER_APPROVED, "manager"),
    "start_verification": T(frozenset({Status.MANAGER_APPROVED}), Status.FINANCE_VERIFICATION, "finance"),
    "verify": T(frozenset({Status.MANAGER_APPROVED, Status.FINANCE_VERIFICATION}), Status.FINANCE_VERIFIED, "finance"),
    "return": T(frozenset({Status.SUBMITTED, Status.MANAGER_REVIEW, Status.MANAGER_APPROVED,
                           Status.FINANCE_VERIFICATION}), Status.RETURNED, "reviewer", True),
    "reject": T(frozenset({Status.SUBMITTED, Status.MANAGER_REVIEW, Status.MANAGER_APPROVED,
                           Status.FINANCE_VERIFICATION}), Status.REJECTED, "reviewer", True),
    "cancel": T(frozenset({Status.DRAFT, Status.RETURNED}), Status.CANCELLED, "submitter", True),
    # Controlled amendment of a locked record: back to Returned with a reason.
    "reopen": T(frozenset({Status.FINANCE_VERIFIED, Status.REJECTED}), Status.RETURNED, "finance_admin", True),
}

MANAGER_STAGE = {Status.SUBMITTED, Status.MANAGER_REVIEW}


def _is_manager_for(user, expense):
    if not access.can_act_in_entity(user, MANAGER_ROLES, expense):
        return False
    if access.has_role(user, Role.SUPER_ADMIN):
        return True
    limit = access.manager_limit(user, expense)
    return limit is None or expense.gross_amount <= limit


def _is_finance_for(user, expense):
    return access.can_act_in_entity(user, FINANCE_ACT_ROLES, expense)


def check(user, expense, action):
    """Return None if allowed, otherwise a reason string."""
    t = TRANSITIONS.get(action)
    if not t:
        return "Unknown action."
    if expense.status not in t.from_states:
        return f"Cannot {action} an expense in status {expense.get_status_display()}."
    own = expense.submitter_id == user.id
    if t.actor == "submitter":
        if own or access.can_act_in_entity(user, {Role.FINANCE_ADMIN, Role.SUPER_ADMIN}, expense):
            return None
        return "Only the submitter or a Finance Admin can do this."
    if own and not settings.EXPENSE_SETTINGS["ALLOW_SELF_APPROVAL"]:
        return "You cannot review your own expense."
    if t.actor == "manager":
        if not access.can_act_in_entity(user, MANAGER_ROLES, expense):
            return "You are not an approver for this entity."
        if not _is_manager_for(user, expense):
            return "Amount exceeds your approval limit."
        return None
    if t.actor == "finance":
        return None if _is_finance_for(user, expense) else "Finance role required for this entity."
    if t.actor == "finance_admin":
        ok = access.can_act_in_entity(user, {Role.FINANCE_ADMIN, Role.SUPER_ADMIN}, expense)
        return None if ok else "Finance Admin role required."
    if t.actor == "reviewer":
        if expense.status in MANAGER_STAGE:
            return None if _is_manager_for(user, expense) else "You are not an approver for this expense."
        return None if _is_finance_for(user, expense) else "Finance role required for this entity."
    return "Not permitted."


def available_actions(user, expense):
    return [a for a in TRANSITIONS if check(user, expense, a) is None]


def _snapshot(expense):
    return {"gross_amount": str(expense.gross_amount), "taxable_value": str(expense.taxable_value),
            "gst": str(expense.total_gst), "itc_status": expense.itc_status,
            "verified_itc_amount": str(expense.verified_itc_amount), "version": expense.version_number}


def _role_users(role_list, expense):
    qs = RoleAssignment.objects.filter(role__in=role_list, user__is_active=True).filter(
        Q(entity_id=expense.paying_entity_id) | Q(entity__isnull=True))
    return set(qs.values_list("user_id", flat=True))


def _notify(expense, action, actor):
    msg = f"{expense.ref} “{expense.title}” {expense.get_status_display().lower()}"
    recipients = set()
    if action == "submit":
        prof = EmployeeProfile.objects.filter(user=expense.submitter).first()
        if prof and prof.manager_id:
            recipients.add(prof.manager_id)
        else:
            recipients |= _role_users([Role.MANAGER], expense)
        msg = f"Approval requested: {expense.ref} “{expense.title}” ({expense.currency} {expense.gross_amount})"
    elif action == "approve":
        recipients |= _role_users([Role.FINANCE_ADMIN, Role.ACCOUNTANT], expense)
        recipients.add(expense.submitter_id)
    else:
        recipients.add(expense.submitter_id)
    recipients.discard(actor.id)
    Notification.objects.bulk_create([Notification(user_id=u, expense=expense, message=msg) for u in recipients])


@transaction.atomic
def transition(user, expense, action, comment=""):
    reason = check(user, expense, action)
    if reason:
        raise WorkflowError(reason)
    t = TRANSITIONS[action]
    if t.comment_required and not comment.strip():
        raise WorkflowError("A comment is required for this action.")
    if action == "submit":
        problems = submission_problems(expense)
        if problems:
            raise WorkflowError(" ".join(problems))
    from_state = expense.status
    expense.status = t.to_state
    expense.version_number += 1
    expense.save(update_fields=["status", "version_number", "updated_at"])
    WorkflowEvent.objects.create(expense=expense, actor=user, action=action, from_state=from_state,
                                 to_state=t.to_state, comment=comment, snapshot=_snapshot(expense))
    audit(user, f"expense.{action}", expense, {"from": from_state, "to": t.to_state, "comment": comment})
    _notify(expense, action, user)
    return expense


def submission_problems(expense):
    """Basic consistency before submission (FR-EXP-005, FR-MDM-006)."""
    problems = []
    if expense.gross_amount <= 0:
        problems.append("Amount must be greater than zero.")
    if expense.expense_date > timezone.localdate():
        problems.append("Expense date cannot be in the future.")
    if expense.category.receipt_required and not expense.attachments.exists():
        problems.append(f"A receipt is required for category {expense.category.name}.")
    limit = expense.category.per_claim_limit
    if limit is not None and expense.gross_amount > limit:
        problems.append(f"Amount exceeds the {expense.category.name} limit of {limit}.")
    if expense.invoice_number and not expense.invoice_date:
        problems.append("Invoice date is required when an invoice number is given.")
    return problems
