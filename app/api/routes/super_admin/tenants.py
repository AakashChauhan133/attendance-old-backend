"""
Super Admin: tenant (organization) management.

A tenant's manager signs in with an ordinary user account (role "tenant_admin": email + password) -
there is no API-key login. The super admin creates the first manager together with the tenant and can
add further managers or reset a manager's password.
"""
import secrets
from datetime import date
import string
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.api.dependencies import require_super_admin
from app.core.security import hash_password, validate_password_strength
from app.db.session import get_db
from app.models.domain import Tenant, User

router = APIRouter()


# =========================
# SCHEMAS
# =========================

class TenantCreate(BaseModel):
    """Creating a tenant also creates its first manager login (email + password)."""
    organization_name: str
    manager_email: EmailStr
    manager_password: Optional[str] = None  # generated (and returned once) when omitted
    manager_name: Optional[str] = None


class ManagerCreate(BaseModel):
    email: EmailStr
    name: Optional[str] = None
    password: Optional[str] = None  # generated (and returned once) when omitted


class PasswordReset(BaseModel):
    password: Optional[str] = None  # generated (and returned once) when omitted


# =========================
# HELPERS
# =========================

def _generate_password(length: int = 14) -> str:
    """Random password that satisfies the strength rules (upper, lower, digit, symbol)."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if validate_password_strength(pw)[0] and any(c in "!@#$%^&*" for c in pw):
            return pw


def _resolve_password(supplied: Optional[str]) -> tuple[str, bool]:
    """Return (password, was_generated); raise 400 when a supplied password is too weak."""
    if supplied:
        ok, msg = validate_password_strength(supplied)
        if not ok:
            raise HTTPException(400, msg)
        return supplied, False
    return _generate_password(), True


async def _email_taken(db: AsyncSession, email: str) -> bool:
    result = await db.execute(select(User.id).where(func.lower(User.email) == email.lower()))
    return result.first() is not None


async def _get_tenant(db: AsyncSession, tenant_id: int) -> Tenant:
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalars().first()
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    return tenant


async def _managers_of(db: AsyncSession, tenant_ids: list[int]) -> dict[int, list[dict]]:
    if not tenant_ids:
        return {}
    result = await db.execute(
        select(User).where(User.role == "tenant_admin", User.tenant_id.in_(tenant_ids)).order_by(User.id)
    )
    out: dict[int, list[dict]] = {}
    for u in result.scalars().all():
        out.setdefault(u.tenant_id, []).append(
            {"id": u.id, "name": u.name, "email": u.email, "is_active": u.is_active}
        )
    return out


def _new_manager(tenant_id: int, name: str, email: str, password: str) -> User:
    return User(
        tenant_id=tenant_id,
        name=name,
        email=email.lower(),
        password_hash=hash_password(password),
        role="tenant_admin",
        is_active=True,
    )


# =========================
# ROUTES
# =========================

@router.get("/tenants")
async def list_tenants(
    current_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db)
):
    """Super Admin: List all tenants (organizations) with their manager logins"""
    result = await db.execute(select(Tenant).order_by(Tenant.name))
    tenants = result.scalars().all()
    managers = await _managers_of(db, [t.id for t in tenants])
    return [
        {"id": t.id, "name": t.name, "created_at": t.created_at, "managers": managers.get(t.id, [])}
        for t in tenants
    ]


@router.post("/tenants")
async def create_tenant(
    data: TenantCreate,
    current_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db)
):
    """Super Admin: Create a tenant together with its first manager (email + password)."""
    name = data.organization_name.strip()
    if not name:
        raise HTTPException(400, "Organization name is required")

    existing = await db.execute(select(Tenant).where(func.lower(Tenant.name) == name.lower()))
    if existing.scalars().first():
        raise HTTPException(400, "Organization name already exists")
    if await _email_taken(db, data.manager_email):
        raise HTTPException(400, "Email already in use")
    password, generated = _resolve_password(data.manager_password)

    # tenants.api_key is a legacy NOT NULL column; it is never exposed or accepted for authentication.
    tenant = Tenant(name=name, api_key=secrets.token_hex(16))
    db.add(tenant)
    try:
        await db.flush()
        manager = _new_manager(tenant.id, data.manager_name or f"{name} Admin", data.manager_email, password)
        db.add(manager)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(400, "Could not create tenant (duplicate name or email)")
    await db.refresh(tenant)
    await db.refresh(manager)

    body = {
        "message": "Tenant created successfully",
        "tenant": {"id": tenant.id, "name": tenant.name},
        "manager": {"id": manager.id, "name": manager.name, "email": manager.email},
    }
    if generated:
        body["manager"]["password"] = password  # shown once
    return body


@router.get("/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: int,
    current_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db)
):
    """Super Admin: Get tenant details"""
    tenant = await _get_tenant(db, tenant_id)

    stats = await db.execute(text("""
        SELECT
            (SELECT COUNT(*) FROM departments WHERE tenant_id = :t) AS department_count,
            (SELECT COUNT(*) FROM users WHERE tenant_id = :t AND role = 'org_admin') AS org_admin_count,
            (SELECT COUNT(*) FROM users WHERE tenant_id = :t AND role = 'employee') AS employee_count
    """), {"t": tenant_id})
    s = stats.mappings().first()
    managers = await _managers_of(db, [tenant_id])

    return {
        "id": tenant.id,
        "name": tenant.name,
        "created_at": tenant.created_at,
        "managers": managers.get(tenant_id, []),
        "stats": {
            "departments": s["department_count"],
            "org_admins": s["org_admin_count"],
            "employees": s["employee_count"],
        },
    }


@router.post("/tenants/{tenant_id}/managers")
async def add_tenant_manager(
    tenant_id: int,
    data: ManagerCreate,
    current_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db)
):
    """Super Admin: Add a manager (tenant admin login) to a tenant"""
    tenant = await _get_tenant(db, tenant_id)
    if await _email_taken(db, data.email):
        raise HTTPException(400, "Email already in use")
    password, generated = _resolve_password(data.password)

    manager = _new_manager(tenant.id, data.name or f"{tenant.name} Admin", data.email, password)
    db.add(manager)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(400, "Could not create manager (duplicate email)")
    await db.refresh(manager)

    body = {"message": "Manager created", "manager": {"id": manager.id, "name": manager.name, "email": manager.email}}
    if generated:
        body["manager"]["password"] = password
    return body


@router.post("/tenants/{tenant_id}/managers/{user_id}/reset-password")
async def reset_manager_password(
    tenant_id: int,
    user_id: int,
    data: PasswordReset,
    current_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db)
):
    """Super Admin: Set a new password for a tenant manager (generated and returned once if omitted)"""
    await _get_tenant(db, tenant_id)
    result = await db.execute(
        select(User).where(User.id == user_id, User.tenant_id == tenant_id, User.role == "tenant_admin")
    )
    manager = result.scalars().first()
    if not manager:
        raise HTTPException(404, "Manager not found for this tenant")

    password, generated = _resolve_password(data.password)
    manager.password_hash = hash_password(password)
    manager.is_active = True
    # sign the manager out everywhere
    await db.execute(text("UPDATE refresh_tokens SET is_revoked = TRUE WHERE user_id = :u"), {"u": manager.id})
    await db.commit()

    body = {"message": "Password reset", "manager": {"id": manager.id, "email": manager.email}}
    if generated:
        body["manager"]["password"] = password
    return body


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(
    tenant_id: int,
    current_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db)
):
    """Super Admin: Delete a tenant and its manager logins.
    Refused (409) while the tenant still has other data (employees, departments, devices, logs ...)."""
    await _get_tenant(db, tenant_id)
    try:
        # explicit order: sessions -> manager logins -> tenant (no ORM relationships exist to order them)
        await db.execute(text(
            "DELETE FROM refresh_tokens WHERE user_id IN "
            "(SELECT id FROM users WHERE tenant_id = :t AND role = 'tenant_admin')"), {"t": tenant_id})
        await db.execute(text("DELETE FROM users WHERE tenant_id = :t AND role = 'tenant_admin'"), {"t": tenant_id})
        await db.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        await db.commit()
    except IntegrityError:
        await db.rollback()  # also restores the manager logins
        raise HTTPException(409, "Tenant still has data (employees, departments, devices, attendance ...). Remove it first.")
    return {"message": "Tenant deleted successfully"}


@router.get("/tenants/{tenant_id}/departments")
async def list_tenant_departments(
    tenant_id: int,
    _: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """Super admin: departments of a tenant (for the report filter)."""
    if not (await db.execute(select(Tenant.id).where(Tenant.id == tenant_id))).first():
        raise HTTPException(404, "Tenant not found")
    res = await db.execute(text(
        "SELECT department_id AS id, department_name AS name FROM departments "
        "WHERE tenant_id = :t ORDER BY department_name"), {"t": tenant_id})
    return [dict(r) for r in res.mappings().all()]


@router.get("/reports/attendance/download")
async def download_attendance_report(
    tenant_id: int,
    month: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    dept_id: Optional[int] = None,
    format: str = "xlsx",
    _: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """Super admin: Excel (default, ?format=csv for CSV) of check-in / check-out / total time for any tenant (optionally one department).
    ?month=YYYY-MM OR ?start_date=&end_date=."""
    from app.services.attendance_report_service import resolve_range, respond
    start, end = resolve_range(month, start_date, end_date)
    if not (await db.execute(select(Tenant.id).where(Tenant.id == tenant_id))).first():
        raise HTTPException(404, "Tenant not found")
    if dept_id is not None:
        ok = await db.execute(text("SELECT 1 FROM departments WHERE department_id = :d AND tenant_id = :t"),
                              {"d": dept_id, "t": tenant_id})
        if not ok.first():
            raise HTTPException(404, "Department not found")
    return await respond(db, tenant_id, start, end, dept_id, format)
