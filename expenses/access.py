"""Role and entity scope. Deny by default (SRS §3).

Every queryset used by the API, dashboard and exports goes through
`expense_queryset_for`, so the UI, exports and attachment downloads all see
the same scope.
"""
from django.db.models import Q

from .models import Expense, LegalEntity, Role

FINANCE_ROLES = {Role.SUPER_ADMIN, Role.FINANCE_ADMIN, Role.ACCOUNTANT}
READ_ALL_ROLES = FINANCE_ROLES | {Role.MANAGER, Role.VIEWER, Role.AUDITOR}
IMPORT_ROLES = {Role.SUPER_ADMIN, Role.FINANCE_ADMIN}
CREATE_ROLES = {Role.SUPER_ADMIN, Role.FINANCE_ADMIN, Role.ACCOUNTANT, Role.MANAGER, Role.EMPLOYEE}


def assignments(user):
    if not user.is_authenticated or not user.is_active:
        return []
    cache = getattr(user, "_role_cache", None)
    if cache is None:
        cache = list(user.role_assignments.select_related("entity"))
        user._role_cache = cache
    return cache


def roles(user):
    return {a.role for a in assignments(user)}


def has_role(user, *wanted):
    return bool(roles(user) & set(wanted))


def entity_ids_for(user, role_set):
    """Entity ids where the user holds one of role_set. None means all entities."""
    ids = set()
    for a in assignments(user):
        if a.role in role_set:
            if a.role == Role.SUPER_ADMIN or a.entity_id is None:
                return None
            ids.add(a.entity_id)
    return ids


def visible_entities(user):
    all_roles = {r for r in Role}
    ids = entity_ids_for(user, all_roles)
    qs = LegalEntity.objects.all()
    return qs if ids is None else qs.filter(id__in=ids)


def _entity_q(ids):
    return Q(paying_entity_id__in=ids) | Q(beneficiary_entity_id__in=ids) | Q(allocations__beneficiary_entity_id__in=ids)


def expense_queryset_for(user):
    if not assignments(user):
        return Expense.objects.none()
    q = Q(submitter=user) if has_role(user, *CREATE_ROLES) else Q(pk__in=[])
    wide = entity_ids_for(user, READ_ALL_ROLES)
    if wide is None:
        return Expense.objects.all()
    if wide:
        q |= _entity_q(wide)
    return Expense.objects.filter(q).distinct()


def can_act_in_entity(user, role_set, expense):
    ids = entity_ids_for(user, role_set)
    if ids is None:
        return True
    return expense.paying_entity_id in ids or (expense.beneficiary_entity_id in ids)


def manager_limit(user, expense):
    """Highest approval limit the user holds for this expense's entity (None = unlimited)."""
    limits = []
    for a in assignments(user):
        if a.role == Role.MANAGER and (a.entity_id is None or a.entity_id == expense.paying_entity_id):
            if a.approval_limit is None:
                return None
            limits.append(a.approval_limit)
    return max(limits) if limits else 0


def can_edit(user, expense):
    if expense.is_locked:
        return False
    if expense.submitter_id == user.id and has_role(user, *CREATE_ROLES):
        return True
    return can_act_in_entity(user, {Role.SUPER_ADMIN, Role.FINANCE_ADMIN}, expense)
