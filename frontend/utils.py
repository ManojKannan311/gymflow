from datetime import date, timedelta

def get_member_status(expiry_date):
    today = date.today()
    if expiry_date < today:
        return 'expired'
    elif expiry_date <= today + timedelta(days=3):
        return 'expiring'
    return 'active'

def apply_branch_scope(request, qs):
    """
    Owner: full gym scope
    Trainer: only their branch
    """
    if request.user.role == "TRAINER":
        return qs.filter(branch_id=request.user.branch_id)
    return qs
