from fastapi import Header, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.db.session import get_db
from app.models.domain import Device, User, Tenant
from app.core.security import decode_token
import datetime
import logging

# Configure logger for authentication tracking
logger = logging.getLogger(__name__)

# Security Schemes
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=True)

# ✅ ADD THIS - Optional version (doesn't raise error automatically)
oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

# =================================================================
# 1. CORE IDENTITY DEPENDENCIES
# =================================================================

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
) -> User:
    """Standard JWT User retrieval"""
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(401, "Invalid or expired token")
    
    user_id = payload.get("user_id")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    
    if not user or not user.is_active:
        raise HTTPException(401, "User not found or inactive")
    
    return user

async def get_current_active_identity(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
):
    """Any logged-in, active user (JWT) - used by shared routes such as notifications."""
    payload = decode_token(token)
    if payload and payload.get("type") == "access":
        result = await db.execute(select(User).where(User.id == payload.get("user_id")))
        user = result.scalars().first()
        if user and user.is_active:
            return user
    raise HTTPException(status_code=401, detail="Valid authentication required for this resource")

# =================================================================
# 2. ROLE-BASED ACCESS CONTROL (RBAC)
# =================================================================

def require_role(*allowed_roles: str):
    """Generic role requirement decorator"""
    async def checker(current_user: User = Depends(get_current_user)):
        if current_user.role not in allowed_roles:
            raise HTTPException(403, f"Requires one of: {allowed_roles}")
        return current_user
    return checker

async def require_super_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "super_admin":
        raise HTTPException(403, "Only super admin can access this")
    return current_user

async def require_org_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "org_admin":
        raise HTTPException(403, "Only org admin can access this")
    return current_user

async def require_employee(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "employee":
        raise HTTPException(403, "Only employees can access this")
    return current_user

# =================================================================
# 3. TENANT & ORGANIZATION VALIDATION
# =================================================================

async def require_tenant_admin(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Tenant:
    """Tenant manager access: a logged-in user with role 'tenant_admin'. Returns the user's Tenant."""
    if current_user.role != "tenant_admin" or not current_user.tenant_id:
        raise HTTPException(403, "Tenant admin access required")
    result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
    tenant = result.scalars().first()
    if not tenant:
        raise HTTPException(401, "Tenant not found")
    return tenant

# =================================================================
# 4. HARDWARE / DEVICE VALIDATION
# =================================================================

async def verify_device(
    x_device_id: str = Header(..., alias="x-device-id"),
    x_secret_key: str = Header(..., alias="x-secret-key"),
    db: AsyncSession = Depends(get_db)
):
    """Biometric Attendance System: Validates physical hardware devices"""
    result = await db.execute(
        select(Device).where(Device.device_id == x_device_id, Device.secret_key == x_secret_key)
    )
    device = result.scalars().first()
    if not device:
        raise HTTPException(401, "Invalid Device Credentials")
    
    # Update device heartbeat
    device.last_seen = datetime.datetime.utcnow()
    device.status = "online"
    await db.commit()
    return device

# =================================================================
# 5. COMPATIBILITY WRAPPERS
# =================================================================

async def get_admin_data(current_user: User = Depends(require_super_admin)):
    """Legacy compatibility for older frontend structures"""
    class CompatibleAdmin:
        def __init__(self, user):
            self.id = user.id
            self.tenant_id = user.tenant_id
            self.username = user.email or user.name
            self.role = "super_admin"
    return CompatibleAdmin(current_user)