from django.contrib.auth.decorators import login_required
from datetime import date,timedelta
from django.db.models import Sum
from django.shortcuts import render , redirect, get_object_or_404
from .models import *
from .utils import get_member_status
from django.http import JsonResponse
from django.contrib import messages
from datetime import datetime
from django.views.decorators.http import require_POST
from django.utils import timezone
from accounts.models import User
from accounts.decorators import owner_required, owner_or_trainer
from frontend.invoice_utils import generate_invoice_number
import calendar
from django.db.models.functions import ExtractMonth
from calendar import month_name
from django.core.files.base import ContentFile
import base64
from django.conf import settings
from django.db.models import Sum, Max, OuterRef, Subquery, DecimalField, Value ,Q
from django.db.models.functions import Coalesce
from django.db.models import F, Value, DecimalField, ExpressionWrapper, Q
from django.db.models.functions import Coalesce, Greatest
from . import backup
from django.contrib.auth import get_user_model
from django.contrib.auth import update_session_auth_hash


User = get_user_model()


@login_required
@require_POST
def owner_reset_password(request):
    if getattr(request.user, "role", "") not in ["ADMIN", "OWNER"]:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    new_password = (request.POST.get("password") or "").strip()
    confirm_password = (request.POST.get("confirm_password") or "").strip()

    if len(new_password) < 6:
        return JsonResponse({"success": False, "error": "Password must be at least 6 characters"}, status=400)

    if new_password != confirm_password:
        return JsonResponse({"success": False, "error": "Passwords do not match"}, status=400)

    request.user.set_password(new_password)
    request.user.save(update_fields=["password"])

    # ✅ IMPORTANT: prevents auto logout after password change
    update_session_auth_hash(request, request.user)

    return JsonResponse({"success": True})




@login_required
def reset_user_password(request, user_id):

    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Invalid request"}, status=400)

    # Only owner/admin allowed
    if request.user.role != "ADMIN":
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    gym = request.user.gym
    user = User.objects.filter(id=user_id, gym=gym).first()

    if not user:
        return JsonResponse({"success": False, "error": "User not found"})

    new_password = request.POST.get("password")

    if not new_password:
        return JsonResponse({"success": False, "error": "Password required"})

    user.set_password(new_password)
    user.save(update_fields=["password"])

    return JsonResponse({"success": True})






def update_member_statuses(gym):
    today = timezone.localdate()
    expiring_limit = today + timedelta(days=7)

    # Expired
    Member.objects.filter(
        gym=gym,
        expiry_date__lt=today
    ).exclude(status="expired").update(status="expired")

    # Expiring Soon
    Member.objects.filter(
        gym=gym,
        expiry_date__gte=today,
        expiry_date__lte=expiring_limit
    ).exclude(status="expiring").update(status="expiring")

    # Active
    Member.objects.filter(
        gym=gym,
        expiry_date__gt=expiring_limit
    ).exclude(status="active").update(status="active")


def _month_range(any_day: date):
    """Return (start_date, end_date) inclusive for that month."""
    start = any_day.replace(day=1)
    last_day = calendar.monthrange(any_day.year, any_day.month)[1]
    end = any_day.replace(day=last_day)
    return start, end


def _last_month_any_day(today: date):
    """Return a date inside last month."""
    if today.month == 1:
        return date(today.year - 1, 12, 1)
    return date(today.year, today.month - 1, 1)

def dashboard_stats(request):
    gym = request.user.gym
    today = timezone.localdate()
    expiring_limit_days = 7
    expiring_to = today + timedelta(days=expiring_limit_days)

    range_key = (request.GET.get("range") or "current").lower()  # current/last/all
    branch_id = (request.GET.get("branch") or "").strip()
    chart_year = int(request.GET.get("year") or today.year)      # ✅ for charts

    # -----------------------------
    # Base querysets (always gym-scoped)
    # -----------------------------
    members_qs = Member.objects.filter(gym=gym,is_deleted=False).select_related("branch", "plan")
    payments_qs = Payment.objects.filter(gym=gym).select_related("member", "member__branch", "plan")

    # Branch filter (optional)
    if branch_id:
        members_qs = members_qs.filter(branch_id=branch_id)
        payments_qs = payments_qs.filter(member__branch_id=branch_id)

    # -----------------------------
    # Range filter for revenue + "new members" + "renewals count"
    # -----------------------------
    if range_key == "current":
        start, end = _month_range(today)
        payments_range = payments_qs.filter(payment_date__gte=start, payment_date__lte=end)
        new_members_qs = members_qs.filter(join_date__gte=start, join_date__lte=end)

    elif range_key == "last":
        last_month_day = _last_month_any_day(today)
        start, end = _month_range(last_month_day)
        payments_range = payments_qs.filter(payment_date__gte=start, payment_date__lte=end)
        new_members_qs = members_qs.filter(join_date__gte=start, join_date__lte=end)

    else:  # all
        payments_range = payments_qs
        new_members_qs = members_qs

    # -----------------------------
    # Member status counts (ALWAYS based on today)
    # -----------------------------
    total_members = members_qs.count()

    expired_qs = members_qs.filter(expiry_date__lt=today)
    expiring_qs = members_qs.filter(expiry_date__gte=today, expiry_date__lte=expiring_to)
    active_qs = members_qs.filter(expiry_date__gt=expiring_to)

    expired_count = expired_qs.count()
    expiring_count = expiring_qs.count()
    active_count = active_qs.count()

    # -----------------------------
    # Revenue + payment mode split (range based)
    # -----------------------------
    revenue = payments_range.aggregate(total=Sum("amount"))["total"] or 0
    cash_total = payments_range.filter(payment_mode="cash").aggregate(total=Sum("amount"))["total"] or 0
    upi_total = payments_range.filter(payment_mode="upi").aggregate(total=Sum("amount"))["total"] or 0

    renewals_count = payments_range.count()
    new_members_count = new_members_qs.count()

    # -----------------------------
    # Tables (Top 10)
    # -----------------------------
    expiring_list = []
    for m in expiring_qs.order_by("expiry_date")[:10]:
        days_left = (m.expiry_date - today).days
        expiring_list.append({
            "id": m.id,
            "name": m.name,
            "phone": m.phone,
            "branch": m.branch.name if m.branch else "",
            "expiry": m.expiry_date.strftime("%Y-%m-%d"),
            "days_left": days_left,
        })

    expired_list = []
    for m in expired_qs.order_by("-expiry_date")[:10]:
        days_over = (today - m.expiry_date).days
        expired_list.append({
            "id": m.id,
            "name": m.name,
            "phone": m.phone,
            "branch": m.branch.name if m.branch else "",
            "expiry": m.expiry_date.strftime("%Y-%m-%d"),
            "days_over": days_over,
        })

    recent_renewals = []
    for p in payments_range.order_by("-payment_date", "-id")[:10]:
        recent_renewals.append({
            "payment_id": p.id,
            "member_id": p.member_id,
            "name": p.member.name if p.member else "",
            "phone": p.member.phone if p.member else "",
            "branch": p.member.branch.name if (p.member and p.member.branch) else "",
            "amount": float(p.amount or 0),
            "mode": p.payment_mode,
            "date": p.payment_date.strftime("%Y-%m-%d") if p.payment_date else "",
            "invoice_no": getattr(p, "invoice_no", "") or "",
        })

    branches = list(Branch.objects.filter(gym=gym).values("id", "name").order_by("name"))

    # -----------------------------
    # ✅ CHARTS (Jan-Dec) + Cash/UPI (year based)
    # -----------------------------
    payments_year = payments_qs.filter(payment_date__year=chart_year)

    month_rows = (
        payments_year
        .annotate(m=ExtractMonth("payment_date"))
        .values("m")
        .annotate(total=Sum("amount"))
        .order_by("m")
    )

    month_map = {r["m"]: float(r["total"] or 0) for r in month_rows}
    chart_labels = [month_name[i][:3] for i in range(1, 13)]
    chart_totals = [month_map.get(i, 0) for i in range(1, 13)]

    chart_cash = payments_year.filter(payment_mode="cash").aggregate(t=Sum("amount"))["t"] or 0
    chart_upi  = payments_year.filter(payment_mode="upi").aggregate(t=Sum("amount"))["t"] or 0
        # -----------------------------
# ✅ Security Deposit totals (range based)
# -----------------------------
    security_deposit_total = new_members_qs.aggregate(
        t=Coalesce(
            Sum("security_deposit"),
            Value(0),
            output_field=DecimalField(max_digits=10, decimal_places=2),
        )
    )["t"]
    
    # ✅ Total income = plan payments + deposits (range based)
    total_income = (revenue or 0) + (security_deposit_total or 0)


    return JsonResponse({
        "success": True,
        "range": range_key,
        "branch_id": branch_id,
        "expiring_limit_days": expiring_limit_days,

        "cards": {
            "total_members": total_members,
            "active_members": active_count,
            "expiring_members": expiring_count,
            "expired_members": expired_count,
            "revenue": float(revenue),
            "security_deposit_total": float(security_deposit_total),   # ✅ separate
            "total_income": float(total_income),
            "cash_total": float(cash_total),
            "upi_total": float(upi_total),
            "new_members": new_members_count,
            "renewals": renewals_count,
            # "pending_total": float(pending_total),
            # "pending_members": pending_members_count,
        },

        "tables": {
            "expiring_list": expiring_list,
            "expired_list": expired_list,
            "recent_renewals": recent_renewals,
        },

        # ✅ charts included
        "charts": {
            "year": chart_year,
            "labels": chart_labels,
            "month_totals": chart_totals,
            "cash": float(chart_cash),
            "upi": float(chart_upi),
        },

        "branches": branches,
        
    })
    
@login_required
@owner_or_trainer
def dashboard_charts(request):
    gym = request.user.gym
    year = int(request.GET.get("year", date.today().year))
    branch_id = request.GET.get("branch", "")

    payments = Payment.objects.filter(gym=gym, payment_date__year=year)

    if branch_id:
        payments = payments.filter(member__branch_id=branch_id)

    # --------- Month wise collection ----------
    month_rows = (
        payments
        .annotate(m=ExtractMonth("payment_date"))
        .values("m")
        .annotate(total=Sum("amount"))
        .order_by("m")
    )

    month_map = {r["m"]: float(r["total"] or 0) for r in month_rows}

    labels = [month_name[i][:3] for i in range(1, 13)]
    month_totals = [month_map.get(i, 0) for i in range(1, 13)]

    # --------- Cash vs UPI split ----------
    cash_total = payments.filter(payment_mode="cash").aggregate(t=Sum("amount"))["t"] or 0
    upi_total  = payments.filter(payment_mode="upi").aggregate(t=Sum("amount"))["t"] or 0

    return JsonResponse({
        "labels": labels,
        "month_totals": month_totals,
        "cash": float(cash_total),
        "upi": float(upi_total),
        "year": year,
    })    
    
    
    
    

@login_required
@owner_required
def dashboard(request):
    today = date.today()
    gym = request.user.gym
    update_member_statuses(gym)
    members = Member.objects.filter(gym=gym)

    active_count = 0
    expiring_count = 0
    expired_count = 0

    for m in members:
        status = get_member_status(m.expiry_date)
        if status == 'active':
            active_count += 1
        elif status == 'expired':
            expired_count += 1
        elif status == 'expiring':
            expiring_count += 1
        else:
            expired_count += 1

    today_expiring = members.filter(expiry_date=today)

    monthly_collection = Payment.objects.filter(
        gym=gym,
        payment_date__month=today.month,
        payment_date__year=today.year
    ).aggregate(total=Sum('amount'))['total'] or 0

    context = {
        'total_members': members.count(),
        'active_count': active_count,
        'expiring_count': expiring_count,
        'expired_count': expired_count,
        'today_expiring': today_expiring,
        'monthly_collection': monthly_collection,
    }
    print(context)
    return render(request, 'dashboard.html', context)

from decimal import Decimal

def _to_decimal(value, default="0"):
    try:
        return Decimal(str(value).strip())
    except Exception:
        return Decimal(default)
@login_required
@owner_or_trainer
def Add_members(request):
    gym = request.user.gym

    if request.method == "GET":
        branches = Branch.objects.filter(gym=gym).values("name", "id")
        return render(request, "Add_members.html", {"branches": branches})

    # -------------------------
    # POST
    # -------------------------
    name = (request.POST.get("name") or "").strip()
    phone = (request.POST.get("phone") or "").strip()
    branch_id = (request.POST.get("branch") or "").strip()
    plan_id = (request.POST.get("plan") or "").strip()

    join_date_str = request.POST.get("join_date")
    start_date_str = request.POST.get("Start_date")  # membership start date

    payment_method = (request.POST.get("Payment_method") or "").strip()

    # IMPORTANT: keep RAW paid input to detect empty
    paid_amount_raw = request.POST.get("paid_amount")
    paid_amount = _to_decimal(paid_amount_raw, default="0")

    discount_amount = _to_decimal(request.POST.get("discount_amount"), default="0")
    discount_reason = (request.POST.get("discount_reason") or "").strip()
    referral_name = (request.POST.get("referral_name") or "").strip()
    
    # security deposit (advance)
    advance_amount = _to_decimal(request.POST.get("security_deposit"), default="0")

    # 🔐 validate branch + plan
    branch = get_object_or_404(Branch, id=branch_id, gym=gym)
    plan = get_object_or_404(MembershipPlan, id=plan_id, branch=branch, is_active=True)

    # ✅ dates
    try:
        join_date = datetime.strptime(join_date_str, "%Y-%m-%d").date()
    except Exception:
        messages.error(request, "Invalid Join Date.")
        return redirect("Add_members")

    # start_date can be optional, fallback to join_date
    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except Exception:
            messages.error(request, "Invalid Start Date.")
            return redirect("Add_members")
    else:
        start_date = join_date

    # ✅ expiry based on START date (correct for renewals/manual starts)
    expiry_date = start_date + timedelta(days=plan.duration_days - 1)

    # ✅ duplicate phone check
    if Member.objects.filter(gym=gym, phone=phone).exists():
        messages.error(request, "Phone number already exists!")
        return redirect("Add_members")

    # ✅ photo upload (file OR camera)
    photo = request.FILES.get("photo")
    photo_base64 = request.POST.get("captured_photo")
    photo_file = None

    if photo_base64:
        try:
            fmt, imgstr = photo_base64.split(";base64,")
            ext = fmt.split("/")[-1]
            photo_file = ContentFile(base64.b64decode(imgstr), name=f"{phone}.{ext}")
        except Exception:
            messages.error(request, "Invalid captured photo data.")
            return redirect("Add_members")
    elif photo:
        photo_file = photo

    # -------------------------
    # ✅ Financial validations
    # -------------------------
    plan_price = Decimal(plan.price)

    if advance_amount < 0:
        messages.error(request, "Security deposit cannot be negative.")
        return redirect("Add_members")

    if discount_amount < 0:
        messages.error(request, "Discount cannot be negative.")
        return redirect("Add_members")

    if discount_amount > plan_price:
        messages.error(request, "Discount cannot exceed plan amount.")
        return redirect("Add_members")

    final_amount = plan_price - discount_amount  # payable after discount

    # ✅ if user left paid_amount EMPTY => full payment
    if paid_amount_raw in (None, "", "None"):
        paid_amount = final_amount

    if paid_amount < 0:
        messages.error(request, "Paid amount cannot be negative.")
        return redirect("Add_members")

    if paid_amount > final_amount:
        messages.error(request, "Paid amount cannot exceed final amount (after discount).")
        return redirect("Add_members")

    pending = final_amount - paid_amount  # ✅ correct pending

    inv = generate_invoice_number(gym)
    
    if discount_amount >= 0:
        final_price =Decimal(plan.price) - discount_amount
    else:
        final_price=Decimal(plan.price)

    # ✅ CREATE MEMBER
    member = Member.objects.create(
        gym=gym,
        branch=branch,
        name=name.capitalize(),
        phone=phone,
        join_date=join_date,
        plan=plan,
        start_date=start_date,
        expiry_date=expiry_date,
        photo=photo_file,
        security_deposit=advance_amount,
    )

    # ✅ CREATE PAYMENT (only if something paid)
    # If paid_amount is 0, you can still store record, but usually better to store only if >0
    if paid_amount > 0:
        Payment.objects.create(
            gym=gym,
            member=member,
            plan=plan,
            amount=paid_amount,
            payment_mode=payment_method,
            payment_date=timezone.localdate(),
            coverage_start=start_date,
            coverage_end=expiry_date,
            invoice_no=inv,
            plan_price=plan.price,
            discount_amount=discount_amount,
            discount_reason=discount_reason,
            referral_name=referral_name,
            final_amount=final_price,
            created_by=request.user,    
        )

    # WhatsApp message (use final amounts)
    msg = (
        f"Thank You for choosing {gym} 💪\n\n"
        f"Hello {member.name} 👋\n"
        f"✅ Member Added Successfully!\n\n"
        f"🧾 Receipt No: {inv}\n"
        f"📦 Plan: {plan.name}\n"
        f"💰 Plan Amount: ₹{plan_price}\n"
        f"🏷️ Discount: ₹{discount_amount}\n"
        f"✅ Final Amount: ₹{final_amount}\n"
        f"💵 Paid Now: ₹{paid_amount}\n"
        f"⚠️ Pending: ₹{pending}\n"
        f"💳 Method: {payment_method}\n"
        f"📅 Validity: {start_date} → {expiry_date}\n"
        f"💼 Security Deposit: ₹{advance_amount}\n"
        f"Thank you 💪"
    )

    whatsapp_url = f"https://wa.me/91{member.phone}?text={quote(msg)}"
    request.session["whatsapp_url"] = whatsapp_url

    messages.success(request, "Member added & payment recorded ✅")
    return redirect("member_list")

# For Getting the Baranch based Planes and Price.
@login_required
@owner_or_trainer
def get_plan(request):
    gym = request.user.gym
    branch_id = request.GET.get("branch_id")

    plans = MembershipPlan.objects.filter(
        branch_id=branch_id,
        branch__gym=gym,
        is_active=True
    ).values("id", "name", "price", "duration_days")

    return JsonResponse(list(plans), safe=False)

# Members List
@login_required
@owner_or_trainer
def member_list(request):
    whatsapp_url =request.session.pop("whatsapp_url", None)
    gym = request.user.gym
    update_member_statuses(gym)
    members = Member.objects.select_related(
        "branch", "plan"
    ).filter(gym=gym,is_deleted=False).only("id", "name", "phone", "join_date", "expiry_date", "status", "photo", "branch__name", "plan__name").order_by("-id")
    

    return render(request, "List_members.html", {
        "members": members,
        "whatsapp_url": whatsapp_url,
    })
    
@login_required
@owner_or_trainer
def plans(request):
    gym = request.user.gym
    plans = MembershipPlan.objects.filter(
        branch__gym=gym
    ).select_related("branch")
    sets = []
    for p in plans:
        sets.append(p.is_active)
        
        print(p.is_active)
        # print(p.name, p.is_active)
    return render(request, "Planes.html",{"plans":plans})

@login_required
@owner_or_trainer
def plans(request):
    gym = request.user.gym
    plans = MembershipPlan.objects.filter(
        branch__gym=gym
    ).select_related("branch")
    add_new = Branch.objects.filter(gym__name=gym).values("id","name")

    return render(request, "Planes.html",{"plans":plans , "add_new":add_new})

@login_required
@owner_required
def Add_plan(request):
    gym = request.user.gym
    if request.method == "GET":
        add_new = Branch.objects.filter(gym__name=gym).values("id","name")
        return render(request, "Add_plan.html",{"add_new":add_new})
    
    if request.method == "POST":
        branch_id = request.POST.get("branch")
        name = (request.POST.get("Plan_name") or "").strip()
        duration = request.POST.get("Plan_duration")
        price = request.POST.get("Plan_amount")

        # ✅ validate branch belongs to this gym
        branch = get_object_or_404(Branch, id=branch_id, gym=gym)

        # ✅ basic validations
        if not name:
            messages.error(request, "Plan name is required.")
            return redirect("Add_plan")

        # try:
        #     start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        #     end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        # except Exception:
        #     messages.error(request, "Please select valid start and end dates.")
        #     return redirect("Add_plan")

        # if end_date < start_date:
        #     messages.error(request, "End date must be after start date.")
        #     return redirect("Add_plan")

        # duration_days = (end_date - start_date).days + 1  # inclusive

        # ✅ prevent duplicate plan name in same branch
        if MembershipPlan.objects.filter(branch=branch, name__iexact=name).exists():
            messages.error(request, "This plan name already exists for the selected branch.")
            return redirect("Add_plan")

        # ✅ create plan
        MembershipPlan.objects.create(
            branch=branch,
            name=name,
            duration_days=duration,
            price=price,
            is_active=True,
        )

        messages.success(request, "Plan created successfully!")
        return redirect("Add_plan")
    

    return render(request, "add_plan.html", {"add_new": add_new})
        
        
@login_required
@require_POST
@owner_required
def toggle_plan(request, pk):
    gym = request.user.gym

    # ✅ only allow toggling plans under logged-in user's gym
    plan = get_object_or_404(
        MembershipPlan,
        id=pk,
        branch__gym=gym
    )
    
    is_active_str = request.POST.get("is_active", "").lower()
    print(is_active_str,f"{pk}")
    plan.is_active = is_active_str in ["true", "1", "on", "yes"]
    plan.save(update_fields=["is_active"])

    return JsonResponse({"success": True, "is_active": plan.is_active})
        

@login_required
def plan_data(request, pk):
    gym = request.user.gym

    plan = get_object_or_404(
        MembershipPlan,
        id=pk,
        branch__gym=gym
    )

    data = {
        "branch_id": plan.branch.id,
        "name": plan.name,
        "duration_days":plan.duration_days,
        "start_date": plan.start_date.strftime("%Y-%m-%d") if plan.start_date else "",
        "end_date": plan.end_date.strftime("%Y-%m-%d") if plan.end_date else "",
        "price": str(plan.price),
    }
    print(pk)
    return JsonResponse(data)

@owner_required
def update_plan(request, pk):
    gym = request.user.gym

    plan = get_object_or_404(
        MembershipPlan,
        id=pk,
        branch__gym=gym
    )

    if request.method == "POST":
        branch_id = request.POST.get("branch")
        name = request.POST.get("Plan_name")
        start = request.POST.get("Start_date")
        end = request.POST.get("End_date")
        price = request.POST.get("Plan_amount")
        duration = request.POST.get("Plan_duration")
        branch = get_object_or_404(Branch, id=branch_id, gym=gym)

        plan.branch = branch
        plan.name = name
        plan.duration_days=duration
        plan.price = price
        plan.save()

        return JsonResponse({"success": True})
        

@owner_required
def delete_plan(request, pk):
    gym = request.user.gym

    plan = get_object_or_404(
        MembershipPlan,
        id=pk,
        branch__gym=gym
    )
    plan.delete()
    return JsonResponse({"success": True})

@login_required
@owner_required
def delete_member(request, pk):
    gym = request.user.gym

    member = get_object_or_404(
        Member,
        id=pk,
        gym=gym
    )
    member.is_deleted = True
    member.save()
    print("save")
    return JsonResponse({"success": True})


@login_required
@owner_or_trainer
def Member_data(request, pk):
    gym = request.user.gym

    member = get_object_or_404(
        Member,
        id=pk,
        gym=gym
    )

    data = {
        'ids':member.id,
        "branch_id": member.branch.name,
        "name": member.name,
        "duration_days":member.phone,
        "joining_date":member.join_date
    }
    print(pk)
    return JsonResponse(data)
@owner_or_trainer
def update_member(request, pk):
    gym = request.user.gym

    member = get_object_or_404(Member, id=pk, gym=gym)

    if request.method == "POST":
        name = (request.POST.get("member_name") or "").strip().capitalize()
        phone = (request.POST.get("Phone_number") or "").strip()

        if not name or not phone:
            return JsonResponse({
                "success": False,
                "error": "Name and phone are required."
            })

        # optional duplicate check
        if Member.objects.filter(phone=phone, gym=gym).exclude(id=member.id).exists():
            return JsonResponse({
                "success": False,
                "error": "Phone already exists for another member."
            })

        member.name = name
        member.phone = phone
        member.save()

        return JsonResponse({"success": True})

    return JsonResponse({"success": False, "error": "Invalid request"})

@owner_or_trainer
def member_full_details(request, pk):
    gym = request.user.gym

    member = get_object_or_404(
        Member.objects.select_related("branch", "plan", "gym"),
        id=pk,
        gym=gym
    )

    payments_qs = (
        Payment.objects
        .filter(gym=gym, member=member)
        .select_related("plan", "created_by")  # ✅ include created_by if you want to display
        .order_by("-payment_date", "-id")
    )

    money = DecimalField(max_digits=12, decimal_places=2)

    # ✅ Summary aggregation (safe for decimals)
    agg = payments_qs.aggregate(
        total_paid=Coalesce(Sum("amount"), Value(Decimal("0.00")), output_field=money),
        total_discount=Coalesce(Sum("discount_amount"), Value(Decimal("0.00")), output_field=money),
        total_final=Coalesce(Sum("final_amount"), Value(Decimal("0.00")), output_field=money),
    )

    total_paid = agg["total_paid"] or Decimal("0.00")
    total_discount = agg["total_discount"] or Decimal("0.00")
    total_final = agg["total_final"] or Decimal("0.00")

    payments = []
    last_payment_date = None

    for p in payments_qs:
        if last_payment_date is None and p.payment_date:
            last_payment_date = p.payment_date

        payments.append({
            "id": p.id,
            "amount": float(p.amount or 0),
            "invoice_no": p.invoice_no,
            "payment_mode": p.payment_mode,
            "payment_mode_label": p.get_payment_mode_display(),
            "payment_date": p.payment_date.strftime("%Y-%m-%d") if p.payment_date else None,

            "coverage_start": p.coverage_start.strftime("%Y-%m-%d") if p.coverage_start else None,
            "coverage_end": p.coverage_end.strftime("%Y-%m-%d") if p.coverage_end else None,

            "plan_id": p.plan_id,
            "plan_name": p.plan.name if p.plan else None,
            "plan_price": float(p.plan_price or (p.plan.price if p.plan else 0) or 0),

            # ✅ NEW: Discount details
            "discount_amount": float(p.discount_amount or 0),
            "final_amount": float(p.final_amount or 0),
            "discount_reason": p.discount_reason,
            "referral_name": p.referral_name,

            # ✅ NEW: who gave discount/payment
            "created_by_id": p.created_by_id,
            "created_by_name": (
                (p.created_by.first_name or p.created_by.username)
                if p.created_by else None
            ),
            "created_by_role": (getattr(p.created_by, "role", None) if p.created_by else None),
        })

    data = {
        "member": {
            "id": member.id,
            "name": member.name,
            "phone": member.phone,
            "join_date": member.join_date.strftime("%Y-%m-%d") if member.join_date else None,

            "status": member.status,
            "status_label": member.get_status_display(),

            "gym_id": member.gym_id,
            "gym_name": member.gym.name if member.gym else None,

            "branch_id": member.branch_id,
            "branch_name": member.branch.name if member.branch else None,

            "plan_id": member.plan_id,
            "plan_name": member.plan.name if member.plan else None,
            "plan_duration_days": member.plan.duration_days if member.plan else None,
            "plan_price": float(member.plan.price) if member.plan else None,

            "start_date": member.start_date.strftime("%Y-%m-%d") if member.start_date else None,
            "expiry_date": member.expiry_date.strftime("%Y-%m-%d") if member.expiry_date else None,
        },
        "payments": payments,
        "summary": {
            "payments_count": payments_qs.count(),
            "total_paid": float(total_paid),
            "total_discount": float(total_discount),         # ✅ NEW
            "total_final_amount": float(total_final),       # ✅ NEW (after discount)
            "last_payment_date": last_payment_date.strftime("%Y-%m-%d") if last_payment_date else None,
        }
    }

    return JsonResponse(data)


# Renewval Payment:
from urllib.parse import quote

@login_required
@owner_or_trainer
def renew_member_plan(request, pk):
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "Method not allowed"}, status=405)

    gym = request.user.gym
    member = get_object_or_404(Member, id=pk, gym=gym)

    plan_id = (request.POST.get("plan_id") or "").strip()
    payment_mode = (request.POST.get("payment_mode") or "").strip()  # 'cash'/'upi'

    # manual start date for renewal
    manual_select_raw = (request.POST.get("renew_Plane_current_expiry") or "").strip()
    try:
        manual_select = datetime.strptime(manual_select_raw, "%Y-%m-%d").date()
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid start date"}, status=400)

    selected_plan = get_object_or_404(MembershipPlan, id=plan_id, branch=member.branch)

    # ---- Discount inputs ----
    discount_amount = _to_decimal(request.POST.get("discount_amount"), default="0")
    discount_reason = (request.POST.get("discount_reason") or "").strip()
    referral_name = (request.POST.get("referral_name") or "").strip()

    # ---- Paid input (keep raw to detect empty) ----
    paid_amount_raw = request.POST.get("paid_amount")
    paid_amount = _to_decimal(paid_amount_raw, default="0")

    # ---- Plan price + expiry ----
    plan_price = Decimal(selected_plan.price)
    coverage_start = manual_select
    coverage_end = coverage_start + timedelta(days=selected_plan.duration_days - 1)

    # ---- Validations ----
    if payment_mode not in ["cash", "upi"]:
        return JsonResponse({"success": False, "message": "Invalid payment mode"}, status=400)

    if discount_amount < 0:
        return JsonResponse({"success": False, "message": "Discount cannot be negative"}, status=400)

    if discount_amount > plan_price:
        return JsonResponse({"success": False, "message": "Discount cannot exceed plan amount"}, status=400)

    final_amount = plan_price - discount_amount  # payable after discount

    # ✅ If paid_amount left EMPTY => full final amount
    if paid_amount_raw in (None, "", "None"):
        paid_amount = final_amount

    if paid_amount < 0:
        return JsonResponse({"success": False, "message": "Paid amount cannot be negative"}, status=400)

    if paid_amount > final_amount:
        return JsonResponse({"success": False, "message": "Paid amount cannot exceed final amount"}, status=400)

    pending = final_amount - paid_amount

    inv = generate_invoice_number(gym)

    # ✅ Save payment (even if 0 you can skip; I keep only if >0)
    if paid_amount > 0:
        Payment.objects.create(
            gym=gym,
            member=member,
            plan=selected_plan,
            amount=paid_amount,
            payment_mode=payment_mode,
            payment_date=timezone.localdate(),
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            invoice_no=inv,

            # ✅ Discount fields (important)
            plan_price=plan_price,
            discount_amount=discount_amount,
            discount_reason=discount_reason,
            referral_name=referral_name,
            final_amount=final_amount,   # payable after discount
            created_by=request.user,
        )
    else:
        # optional: if you want a record even when paid=0, remove this else
        pass

    # ✅ Update member plan cycle
    member.plan = selected_plan
    member.start_date = coverage_start
    member.expiry_date = coverage_end
    member.status = "active"
    member.save()

    # WhatsApp message
    msg = (
        f"Thank You for choosing {gym} 💪\n\n"
        f"Hello {member.name} 👋\n"
        f"✅ Plan Renewed Successfully!\n\n"
        f"🧾 Receipt No: {inv}\n"
        f"📦 Plan: {selected_plan.name}\n"
        f"💰 Plan Amount: ₹{plan_price}\n"
        f"🏷️ Discount: ₹{discount_amount}\n"
        f"✅ Final Amount: ₹{final_amount}\n"
        f"💵 Paid Now: ₹{paid_amount}\n"
        f"⚠️ Pending: ₹{pending}\n"
        f"💳 Method: {payment_mode}\n"
        f"📅 Validity: {coverage_start} → {coverage_end}\n"
        f"Thank you 💪"
    )

    whatsapp_url = f"https://wa.me/91{member.phone}?text={quote(msg)}"

    return JsonResponse({
        "success": True,
        "invoice_no": inv,
        "coverage_start": str(coverage_start),
        "coverage_end": str(coverage_end),
        "plan_name": selected_plan.name,
        "final_amount": str(final_amount),
        "discount_amount": str(discount_amount),
        "pending": str(pending),
        "whatsapp_url": whatsapp_url,
    })
    
    
@login_required
@owner_or_trainer
def branch_plans(request, pk):
    gym = request.user.gym

    branch = get_object_or_404(Branch, id=pk, gym=gym)

    plans = MembershipPlan.objects.filter(
        branch=branch,
        is_active=True
    ).order_by("duration_days")

    data = [
        {
            "id": p.id,
            "name": p.name,
            "duration_days": p.duration_days,
            "price": float(p.price),
        }
        for p in plans
    ]

    return JsonResponse(data, safe=False)


@login_required
@owner_or_trainer
def renewals_page(request):
    gym = request.user.gym
    update_member_statuses(gym)
    today = timezone.localdate()

    # filters
    q = request.GET.get("q", "").strip()
    branch_id = request.GET.get("branch", "").strip()

    # dropdown value should be: active / expiring / expired / all
    status = request.GET.get("status", "expiring").strip()

    # base queryset
    members = Member.objects.filter(gym=gym , is_deleted=False).select_related("branch", "plan")

    # search by name OR phone
    if q:
        members = members.filter(Q(name__icontains=q) | Q(phone__icontains=q))

    # branch filter
    if branch_id:
        members = members.filter(branch_id=branch_id)

    # expiring window
    expiring_limit = today + timedelta(days=7)

    # filter by status (based on expiry_date = source of truth)
    if status == "expired":
        members = members.filter(expiry_date__lt=today)
    elif status == "expiring":
        members = members.filter(expiry_date__gte=today, expiry_date__lte=expiring_limit)
    elif status == "active":
        members = members.filter(expiry_date__gt=expiring_limit)
    elif status == "all":
        pass

    branches = Branch.objects.filter(gym=gym).order_by("name")

    context = {
        "members": members.order_by("expiry_date"),
        "branches": branches,
        "q": q,
        "branch_id": branch_id,
        "status": status,
        "today": today,
        "expiring_limit": expiring_limit,
    }
    print(context)

    return render(request, "renewals.html", context)



@login_required
@owner_or_trainer
def paid_members_page(request):
    gym = request.user.gym

    today = date.today()
    month = int(request.GET.get("month", today.month))
    year = int(request.GET.get("year", today.year))

    q = request.GET.get("q", "").strip()
    branch_id = request.GET.get("branch", "").strip()

    # Base payments for month/year
    payments = (
        Payment.objects
        .filter(gym=gym, payment_date__year=year, payment_date__month=month)
        .select_related("member", "member__branch")
    )

    if branch_id:
        payments = payments.filter(member__branch_id=branch_id)

    if q:
        payments = payments.filter(
            Q(member__name__icontains=q) | Q(member__phone__icontains=q)
        )

    # ✅ Aggregate by member (one row per member)
    member_totals = (
    payments
    .values("member_id")
    .annotate(
        total_paid=Coalesce(
            Sum("amount"),
            Value(0),
            output_field=DecimalField(max_digits=8, decimal_places=2)
        ),
        last_payment=Max("payment_date"),
    )
)

    # Subquery to attach totals to each member
    total_paid_sq = Subquery(
        member_totals.filter(member_id=OuterRef("pk")).values("total_paid")[:1]
    )
    last_payment_sq = Subquery(
        member_totals.filter(member_id=OuterRef("pk")).values("last_payment")[:1]
    )

    # ✅ Get members list with photo.url available
    members_qs = (
        Member.objects
        .filter(gym=gym, payments__in=payments, is_deleted=False)
        .select_related("branch", "plan")
        .distinct()
        .annotate(
            total_paid=total_paid_sq,
            last_payment=last_payment_sq,
        )
        .order_by("-total_paid", "name")
    )

    total_collection = payments.aggregate(total=Sum("amount"))["total"] or 0
    branches = Branch.objects.filter(gym=gym).order_by("name")

    context = {
        "paid_members": members_qs,   # ✅ now each item is a Member object
        "branches": branches,
        "q": q,
        "branch_id": branch_id,
        "month": month,
        "year": year,
        "total_collection": total_collection,
    }
    return render(request, "paid_members.html", context)

@login_required
@owner_or_trainer
def unpaid_members_page(request):
    gym = request.user.gym
    today = timezone.localdate()
    alert_limit = today + timedelta(days=7)

    q = request.GET.get("q", "").strip()
    branch_id = request.GET.get("branch", "").strip()
    status = request.GET.get("status", "all").strip()   # expired / expiring / all

    members = Member.objects.filter(
        gym=gym,
        expiry_date__lte=alert_limit,  # includes expiring + expired
        is_deleted=False
    ).select_related("branch", "plan")

    if q:
        members = members.filter(Q(name__icontains=q) | Q(phone__icontains=q))

    if branch_id:
        members = members.filter(branch_id=branch_id)

    # status filter inside unpaid window
    if status == "expired":
        members = members.filter(expiry_date__lt=today)
    elif status == "expiring":
        members = members.filter(expiry_date__gte=today, expiry_date__lte=alert_limit)

    branches = Branch.objects.filter(gym=gym).order_by("name")

    context = {
        "members": members.order_by("expiry_date"),
        "branches": branches,
        "q": q,
        "branch_id": branch_id,
        "status": status,
        "today": today,
        "alert_limit": alert_limit,
    }
    return render(request, "unpaid_members.html", context)

@login_required
@owner_required
def gym_profile(request):
    gym = request.user.gym

    branches = Branch.objects.filter(gym=gym).order_by("name")
    trainers = User.objects.filter(gym=gym, role="TRAINER").select_related("branch").order_by("first_name", "username")
    invoice_setting, _ = InvoiceSettings.objects.get_or_create(
        gym=gym,
        defaults={"prefix": "INV", "next_number": 1001, "padding": 4}
    )
    return render(request, "gym_profile.html", {
        "gym": gym,
        "branches": branches,
        "trainers": trainers,
         "invoice_setting": invoice_setting
    })


@require_POST
@owner_required
@login_required
def create_branch(request):
    if request.user.gym.plan_type == "single":
        return JsonResponse({"Error": "Upgrade the Plan"})
    
    if request.user.role != "ADMIN":
        return JsonResponse({"success": False, "message": "Not allowed"})
    gym = request.user.gym
    name = request.POST.get("name", "").strip()
    address = request.POST.get("address", "").strip()
    phone = request.POST.get("phone", "").strip()

    if not name:
        return JsonResponse({"success": False, "message": "Branch name is required"})

    Branch.objects.create(gym=gym, name=name, address=address, phone=phone)
    return JsonResponse({"success": True})


@require_POST
@login_required
@owner_required
def create_trainer(request):
    if request.user.role != "ADMIN":
        return JsonResponse({"success": False, "message": "Not allowed"})

    gym = request.user.gym

    username = request.POST.get("username", "").strip()
    password = request.POST.get("password", "").strip()
    full_name = request.POST.get("name", "").strip()
    phone = request.POST.get("phone", "").strip()
    branch_id = request.POST.get("branch_id", "").strip()

    if not username or not password:
        return JsonResponse({"success": False, "message": "Username & Password required"})

    if User.objects.filter(username=username).exists():
        return JsonResponse({"success": False, "message": "Username already exists"})

    first_name = full_name
    branch = None
    if branch_id:
        branch = get_object_or_404(Branch, id=branch_id, gym=gym)

    trainer = User.objects.create_user(
        username=username,
        password=password,
        first_name=first_name,
    )
    trainer.gym = gym
    trainer.role = "TRAINER"
    trainer.branch = branch
    trainer.save()

    return JsonResponse({"success": True})


@require_POST
@login_required
@owner_required

def assign_trainer_branch(request, pk):
    if request.user.role != "ADMIN":
        return JsonResponse({"success": False, "message": "Not allowed"})

    gym = request.user.gym
    trainer = get_object_or_404(User, id=pk, gym=gym, role="TRAINER")

    branch_id = request.POST.get("branch_id", "").strip()

    if not branch_id:
        trainer.branch = None
        trainer.save()
        return JsonResponse({"success": True})

    branch = get_object_or_404(Branch, id=branch_id, gym=gym)
    trainer.branch = branch
    trainer.save()

    return JsonResponse({"success": True})


@require_POST
@login_required
@owner_required
def toggle_trainer(request, pk):
    if request.user.role != "ADMIN":
        return JsonResponse({"success": False, "message": "Not allowed"})

    gym = request.user.gym
    trainer = get_object_or_404(User, id=pk, gym=gym, role="TRAINER")
    trainer.is_active = not trainer.is_active
    trainer.save()
    return JsonResponse({"success": True, "is_active": trainer.is_active})


@require_POST
@login_required
@owner_required
def update_trainer(request, pk):
    if request.user.role != "ADMIN":
        return JsonResponse({"success": False, "message": "Not allowed"})

    gym = request.user.gym
    trainer = get_object_or_404(User, id=pk, gym=gym, role="TRAINER")

    name = request.POST.get("name", "").strip()
    phone = request.POST.get("phone", "").strip()
    branch_id = request.POST.get("branch_id", "").strip()

    if name:
        trainer.first_name = name

    # only if phone field exists in User model
    if hasattr(trainer, "phone"):
        trainer.phone = phone

    if branch_id:
        branch = get_object_or_404(Branch, id=branch_id, gym=gym)
        trainer.branch = branch
    else:
        trainer.branch = None

    trainer.save()
    return JsonResponse({"success": True})


@require_POST
@login_required
@owner_required
def delete_trainer(request, pk):
    if request.user.role != "ADMIN":
        return JsonResponse({"success": False, "message": "Not allowed"})

    gym = request.user.gym
    trainer = get_object_or_404(User, id=pk, gym=gym, role="TRAINER")

    # safer: don't allow deleting yourself
    if trainer.id == request.user.id:
        return JsonResponse({"success": False, "message": "You cannot delete yourself"})

    trainer.delete()
    return JsonResponse({"success": True})

@require_POST
@login_required
@owner_required
def invoice_settings_view(request):
    gym = request.user.gym
    settings, _ = InvoiceSettings.objects.get_or_create(gym=gym)

    if request.method == "POST":
        prefix = request.POST.get("prefix",settings.prefix).strip()
        next_number = int(request.POST.get("next_number", settings.next_number))
        padding = int(request.POST.get("padding", settings.padding))
        
        settings.prefix = prefix
        settings.next_number = next_number
        settings.padding = padding
        settings.save()
        print(prefix,next_number,padding)
        messages.success(request, "Invoice settings updated ✅")
        return redirect("gym_profile")

    return render(request, "invoice_settings.html", {"settings": settings})

from django.db.models import (
    Sum, Value, F, Q, OuterRef, Subquery,
    DecimalField, ExpressionWrapper
)
from django.db.models.functions import Coalesce, Greatest

@owner_or_trainer
def pending_payments_page(request):
    gym = request.user.gym
    q = (request.GET.get("q") or "").strip()
    branch_id = (request.GET.get("branch") or "").strip()

    money = DecimalField(max_digits=10, decimal_places=2)
    zero = Value(Decimal("0.00"))

    members_qs = (
        Member.objects
        .filter(gym=gym, is_deleted=False, plan__isnull=False)
        .select_related("plan", "branch")
    )

    if q:
        members_qs = members_qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))

    if branch_id:
        members_qs = members_qs.filter(branch_id=branch_id)

    # ✅ Paid sum for CURRENT cycle only
    paid_subq = (
        Payment.objects
        .filter(
            gym=gym,
            member_id=OuterRef("pk"),
            coverage_start=OuterRef("start_date"),
            coverage_end=OuterRef("expiry_date"),
        )
        .values("member_id")
        .annotate(s=Coalesce(Sum("amount"), zero, output_field=money))
        .values("s")[:1]
    )

    # ✅ Discount for CURRENT cycle (use MAX, not "latest")
    # If your discount is only given once, MAX is correct.
    discount_subq = (
        Payment.objects
        .filter(
            gym=gym,
            member_id=OuterRef("pk"),
            coverage_start=OuterRef("start_date"),
            coverage_end=OuterRef("expiry_date"),
        )
        .values("member_id")
        .annotate(d=Coalesce(Max("discount_amount"), zero, output_field=money))
        .values("d")[:1]
    )

    members_qs = (
        members_qs
        .annotate(
            plan_price=Coalesce(F("plan__price"), zero, output_field=money),
            paid_total=Coalesce(Subquery(paid_subq, output_field=money), zero, output_field=money),
            discount_amount=Coalesce(Subquery(discount_subq, output_field=money), zero, output_field=money),
        )
        .annotate(
            final_amount=Greatest(
                ExpressionWrapper(F("plan_price") - F("discount_amount"), output_field=money),
                zero,
                output_field=money,
            ),
            balance=Greatest(
                ExpressionWrapper(F("final_amount") - F("paid_total"), output_field=money),
                zero,
                output_field=money,
            ),
        )
        .filter(balance__gt=0)
        .order_by("-balance", "name")
    )

    pending_list = []
    for m in members_qs:
        pending_list.append({
            "member_id": m.id,
            "name": m.name,
            "phone": m.phone,
            "branch": m.branch.name if m.branch else "",
            "plan": m.plan.name if m.plan else "",
            "plan_price": m.plan_price,
            "paid": m.paid_total,
            "discount_amount": m.discount_amount,
            "final_amount": m.final_amount,
            "balance": m.balance,
            "photo": m.photo.url if m.photo else "",
        })

    branches = Branch.objects.filter(gym=gym).order_by("name")

    total_pending = sum((x["balance"] for x in pending_list), Decimal("0.00"))
    pending_members_count = len(pending_list)

    return render(request, "pending_payments.html", {
        "pending_list": pending_list,
        "branches": branches,
        "q": q,
        "branch_id": branch_id,
        "total_pending": total_pending,
        "pending_members_count": pending_members_count,
    })

from django.db import transaction

@owner_or_trainer
def pending_payment_data(request, member_id):
    gym = request.user.gym
    member = get_object_or_404(
        Member.objects.select_related("plan", "branch"),
        id=member_id, gym=gym, is_deleted=False
    )

    if not member.plan:
        return JsonResponse({"success": False, "error": "Member has no plan"}, status=400)

    money = DecimalField(max_digits=12, decimal_places=2)
    plan_price = member.plan.price or Decimal("0.00")

    agg = (
        Payment.objects
        .filter(
            gym=gym,
            member=member,
            plan=member.plan,
            coverage_start=member.start_date,
            coverage_end=member.expiry_date,
        )
        .aggregate(
            total_paid=Coalesce(Sum("amount"), Value(Decimal("0.00")), output_field=money),
            total_discount=Coalesce(Sum("discount_amount"), Value(Decimal("0.00")), output_field=money),
        )
    )

    total_paid = agg["total_paid"]
    total_discount = agg["total_discount"]

    payable = plan_price - total_discount
    if payable < 0:
        payable = Decimal("0.00")

    balance = payable - total_paid
    if balance < 0:
        balance = Decimal("0.00")

    return JsonResponse({
        "success": True,
        "member": {
            "id": member.id,
            "name": member.name,
            "phone": member.phone,
            "branch": member.branch.name if member.branch else "",
        },
        "plan": {
            "id": member.plan.id,
            "name": member.plan.name,
            "price": str(plan_price),
            "start_date": member.start_date.strftime("%Y-%m-%d") if member.start_date else None,
            "expiry_date": member.expiry_date.strftime("%Y-%m-%d") if member.expiry_date else None,
        },
        "payments": {
            "total_paid": str(total_paid),
            "total_discount": str(total_discount),
            "payable": str(payable),
            "balance": str(balance),
        }
    })


@owner_or_trainer
def pending_payment_pay(request, member_id):
    gym = request.user.gym
    member = get_object_or_404(
        Member.objects.select_related("plan"),
        id=member_id,
        gym=gym,
        is_deleted=False
    )

    if not member.plan:
        return JsonResponse({"success": False, "error": "Member has no plan"}, status=400)

    amount_str = (request.POST.get("amount") or "").strip()
    payment_mode = (request.POST.get("payment_mode") or "").strip()

    if amount_str == "" or payment_mode not in ["cash", "upi"]:
        return JsonResponse({"success": False, "error": "Invalid amount or payment mode"}, status=400)

    try:
        amount = Decimal(amount_str)
    except Exception:
        return JsonResponse({"success": False, "error": "Invalid amount"}, status=400)

    if amount <= 0:
        return JsonResponse({"success": False, "error": "Amount must be > 0"}, status=400)

    plan_price = Decimal(member.plan.price)

    # ✅ current cycle payments only
    cycle_qs = Payment.objects.filter(
        gym=gym,
        member=member,
        plan=member.plan,
        coverage_start=member.start_date,
        coverage_end=member.expiry_date
    )

    agg = cycle_qs.aggregate(
        total_paid=Coalesce(Sum("amount"), Decimal("0")),
        discount=Coalesce(Max("discount_amount"), Decimal("0")),  # ✅ avoid double-count
    )

    total_paid = Decimal(agg["total_paid"] or 0)
    discount = Decimal(agg["discount"] or 0)

    # ✅ payable after discount
    final_amount = plan_price - discount
    if final_amount < 0:
        final_amount = Decimal("0")

    balance = final_amount - total_paid
    if balance < 0:
        balance = Decimal("0")

    if amount > balance:
        return JsonResponse({"success": False, "error": f"Amount exceeds balance ₹{balance}"}, status=400)

    inv = generate_invoice_number(gym)

    with transaction.atomic():
        Payment.objects.create(
            gym=gym,
            member=member,
            plan=member.plan,
            amount=amount,
            payment_mode=payment_mode,
            payment_date=timezone.localdate(),
            coverage_start=member.start_date,
            coverage_end=member.expiry_date,
            invoice_no=inv,
            # ✅ discount NOT repeated here (keep 0) to avoid confusion
        )

    new_total_paid = total_paid + amount
    new_balance = final_amount - new_total_paid
    if new_balance < 0:
        new_balance = Decimal("0")

    return JsonResponse({
        "success": True,
        "invoice_no": inv,
        "plan_price": float(plan_price),
        "discount": float(discount),
        "final_amount": float(final_amount),
        "total_paid": float(new_total_paid),
        "balance": float(new_balance),
    })
    
    
@login_required
@owner_or_trainer


def discount_report(request):
    gym = request.user.gym

    q = (request.GET.get("q") or "").strip()
    branch_id = (request.GET.get("branch") or "").strip()
    created_by_id = (request.GET.get("created_by") or "").strip()
    start = (request.GET.get("start") or "").strip()
    end = (request.GET.get("end") or "").strip()

    qs = (
        Payment.objects
        .filter(gym=gym, discount_amount__gt=0)
        .select_related("member", "member__branch", "plan", "created_by")
        .order_by("-payment_date", "-id")
    )

    if q:
        qs = qs.filter(Q(member__name__icontains=q) | Q(member__phone__icontains=q))

    if branch_id:
        qs = qs.filter(member__branch_id=branch_id)

    if created_by_id:
        qs = qs.filter(created_by_id=created_by_id)

    if start:
        qs = qs.filter(payment_date__gte=start)
    if end:
        qs = qs.filter(payment_date__lte=end)

    dec_out = DecimalField(max_digits=12, decimal_places=2)

    summary = qs.aggregate(
        total_discount=Coalesce(Sum("discount_amount"), Value(Decimal("0.00")), output_field=dec_out),
        total_collected=Coalesce(Sum("final_amount"), Value(Decimal("0.00")), output_field=dec_out),
    )

    branches = Branch.objects.filter(gym=gym).order_by("name")
    staff = User.objects.filter(gym=gym).order_by("role", "first_name", "username")

    context = {
        "rows": qs[:800],
        "branches": branches,
        "staff": staff,
        "filters": {
            "q": q,
            "branch": branch_id,
            "created_by": created_by_id,   # ✅ keep dropdown selected
            "start": start,
            "end": end,
        },
        "summary": {
            "total_discount": summary["total_discount"] or Decimal("0.00"),
            "total_collected": summary["total_collected"] or Decimal("0.00"),
            "total_rows": qs.count(),
        }
    }

    return render(request, "discount_report.html", context)




def _invoice_filters_qs(request, base_qs):
    gym = request.user.gym

    q = (request.GET.get("q") or "").strip()
    branch_id = (request.GET.get("branch") or "").strip()
    created_by_id = (request.GET.get("created_by") or "").strip()
    start = (request.GET.get("start") or "").strip()
    end = (request.GET.get("end") or "").strip()

    qs = base_qs.filter(gym=gym).select_related("member", "member__branch", "plan", "created_by")

    if q:
        qs = qs.filter(
            Q(member__name__icontains=q) |
            Q(member__phone__icontains=q) |
            Q(invoice_no__icontains=q)
        )

    if branch_id:
        qs = qs.filter(member__branch_id=branch_id)

    if created_by_id:
        qs = qs.filter(created_by_id=created_by_id)

    if start:
        qs = qs.filter(payment_date__gte=start)
    if end:
        qs = qs.filter(payment_date__lte=end)

    return qs, {
        "q": q,
        "branch": branch_id,
        "created_by": created_by_id,
        "start": start,
        "end": end,
    }


def invoices_page(request):
    # Base queryset: only invoices that have invoice_no (optional)
    base_qs = Payment.objects.exclude(invoice_no__isnull=True).exclude(invoice_no__exact="")

    qs, filters = _invoice_filters_qs(request, base_qs)
    qs = qs.order_by("-payment_date", "-id")

    branches = Branch.objects.filter(gym=request.user.gym).order_by("name")
    staff = User.objects.filter(gym=request.user.gym).order_by("role", "first_name", "username")

    context = {
        "rows": qs[:1000],  # safety limit
        "branches": branches,
        "staff": staff,
        "filters": filters,
    }
    return render(request, "invoices.html", context)



@login_required
def enquiry_page(request):
    gym = request.user.gym

    # -------- CREATE (POST) ----------
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        phone = (request.POST.get("phone") or "").strip()
        branch_id = (request.POST.get("branch") or "").strip()
        source = (request.POST.get("source") or "").strip()
        interested_plan = (request.POST.get("interested_plan") or "").strip()
        note = (request.POST.get("note") or "").strip()
        next_followup = (request.POST.get("next_followup") or "").strip()

        branch = None
        if branch_id:
            branch = get_object_or_404(Branch, id=branch_id, gym=gym)

        if not name or not phone:
            # simple validation
            return redirect("enquiry_page")

        Enquiry.objects.create(
            gym=gym,
            branch=branch,
            name=name,
            phone=phone,
            source=source or None,
            interested_plan=interested_plan or None,
            note=note or None,
            next_followup=next_followup or None,
            created_by=request.user,
        )
        return redirect("enquiry_page")

    # -------- LIST (GET) ----------
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    branch_id = (request.GET.get("branch") or "").strip()
    followup = (request.GET.get("followup") or "").strip()  # today / overdue / upcoming

    qs = (
        Enquiry.objects
        .filter(gym=gym)
        .select_related("branch", "created_by")
        .order_by("-created_at", "-id")
    )

    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))

    if status:
        qs = qs.filter(status=status)

    if branch_id:
        qs = qs.filter(branch_id=branch_id)

    today = timezone.localdate()
    if followup == "today":
        qs = qs.filter(next_followup=today).exclude(status__in=["won", "lost"])
    elif followup == "overdue":
        qs = qs.filter(next_followup__lt=today).exclude(status__in=["won", "lost"])
    elif followup == "upcoming":
        qs = qs.filter(next_followup__gt=today).exclude(status__in=["won", "lost"])

    branches = Branch.objects.filter(gym=gym).order_by("name")

    return render(request, "enquiries.html", {
        "rows": qs[:1000],
        "branches": branches,
        "filters": {
            "q": q,
            "status": status,
            "branch": branch_id,
            "followup": followup,
        },
        "status_choices": Enquiry.STATUS_CHOICES,
    })


@login_required
def enquiry_update(request, pk):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Method not allowed"}, status=405)

    gym = request.user.gym
    e = get_object_or_404(Enquiry, id=pk, gym=gym)

    status = (request.POST.get("status") or "").strip()
    next_followup = (request.POST.get("next_followup") or "").strip()
    note = (request.POST.get("last_followup_note") or "").strip()

    valid = {k for k, _ in Enquiry.STATUS_CHOICES}
    if status and status not in valid:
        return JsonResponse({"success": False, "error": "Invalid status"}, status=400)

    if status:
        e.status = status

    # when closed, clear followup
    if status in [Enquiry.STATUS_WON, Enquiry.STATUS_LOST]:
        e.next_followup = None
    else:
        e.next_followup = next_followup or None

    e.last_followup_note = note or None
    e.save(update_fields=["status", "next_followup", "last_followup_note"])

    return JsonResponse({"success": True})
