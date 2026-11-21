from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import text, func
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime, timedelta
from app.db.session import get_db
from app.api.dependencies import require_tenant_admin, verify_device
from app.api.routes.attendance import mark_attendance
from app.models.domain import Tenant, Device, Command, User
from app.mqtt.client import mqtt_manager

router = APIRouter()


# =========================
# SCHEMAS
# =========================

class DeviceCreate(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=100, description="Unique device identifier")
    secret_key: str = Field(..., min_length=8, description="Secret key for device authentication")


class DeviceUpdate(BaseModel):
    device_id: Optional[str] = Field(None, min_length=1, max_length=100)
    secret_key: Optional[str] = Field(None, min_length=8)


class FireCommandRequest(BaseModel):
    device_id: str
    command: str  # 'enroll' or 'delete'
    target_id: int = None


class DeviceHeartbeatResponse(BaseModel):
    status: str
    message: str
    timestamp: str


class DeviceMarkAttendanceResponse(BaseModel):
    success: bool
    message: str
    record_type: str
    user_name: str
    finger_id: int


# =========================
# HELPER FUNCTION
# =========================

async def check_and_update_device_status(device: Device, db: AsyncSession):
    """Check if device heartbeat is stale and update status accordingly"""
    if device.status == "online" and device.last_seen:
        stale_cutoff = datetime.utcnow() - timedelta(minutes=2)
        if device.last_seen < stale_cutoff:
            device.status = "offline"
            await db.commit()
            return False
    return device.status == "online"


# =========================
# ESP DEVICE ENDPOINTS (Called by hardware)
# =========================

@router.post("/device/mark")
async def device_mark_attendance(
    finger_id: int,
    background_tasks: BackgroundTasks,
    device = Depends(verify_device),
    db: AsyncSession = Depends(get_db)
):
    """ESP device punch (legacy path). Same behaviour as POST /api/device/mark - the old implementation of
    this route queried a non-existent "attendance" table and could never succeed."""
    return await mark_attendance(finger_id, background_tasks, device, db)


@router.post("/device/heartbeat")
async def device_heartbeat(
    device_id: str = Header(..., alias="x-device-id"),
    secret_key: str = Header(..., alias="x-secret-key"),
    db: AsyncSession = Depends(get_db)
):
    """ESP Device sends heartbeat every 30 seconds"""
    
    result = await db.execute(
        select(Device).where(
            Device.device_id == device_id,
            Device.secret_key == secret_key
        )
    )
    device = result.scalars().first()
    
    if not device:
        raise HTTPException(401, "Invalid device credentials")
    
    # Update last_seen and ensure status is online
    device.last_seen = func.now()
    device.status = "online"
    
    await db.commit()
    
    return {
        "status": "ok",
        "message": "Heartbeat received",
        "timestamp": datetime.utcnow().isoformat()
    }


@router.patch("/device/status")
async def device_update_status(
    status: str,
    device_id: str = Header(..., alias="x-device-id"),
    secret_key: str = Header(..., alias="x-secret-key"),
    db: AsyncSession = Depends(get_db)
):
    """ESP updates its online/offline status"""
    
    if status not in ["online", "offline"]:
        raise HTTPException(400, "Status must be 'online' or 'offline'")
    
    result = await db.execute(
        select(Device).where(
            Device.device_id == device_id,
            Device.secret_key == secret_key
        )
    )
    device = result.scalars().first()
    
    if not device:
        raise HTTPException(401, "Invalid device credentials")
    
    device.status = status
    if status == "online":
        device.last_seen = func.now()
    
    await db.commit()
    
    return {
        "status": "ok",
        "device_status": status
    }


# =========================
# TENANT DEVICE MANAGEMENT ENDPOINTS
# =========================

@router.post("/devices")
async def create_device(
    data: DeviceCreate,
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Tenant Manager: Register a new biometric device"""
    
    # Check if device already exists for this tenant
    existing = await db.execute(
        select(Device).where(
            Device.tenant_id == tenant.id,
            Device.device_id == data.device_id
        )
    )
    if existing.scalars().first():
        raise HTTPException(400, "Device ID already exists for this tenant")
    
    # Create new device
    device = Device(
        tenant_id=tenant.id,
        device_id=data.device_id,
        secret_key=data.secret_key,
        status="offline",
        last_seen=None
    )
    
    db.add(device)
    await db.commit()
    await db.refresh(device)
    
    return {
        "message": "Device registered successfully",
        "device": {
            "id": device.id,
            "device_id": device.device_id,
            "status": device.status,
            "last_seen": device.last_seen
        },
        "credentials": {
            "device_id": device.device_id,
            "secret_key": device.secret_key
        },
        "instruction": "Configure your biometric device with these credentials"
    }


@router.get("/devices")
async def list_devices(
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Tenant Manager: List all devices in organization"""
    
    result = await db.execute(
        select(Device).where(Device.tenant_id == tenant.id).order_by(Device.device_id)
    )
    devices = result.scalars().all()
    
    return [
        {
            "id": d.id,
            "device_id": d.device_id,
            "status": d.status,
            "last_seen": d.last_seen
        }
        for d in devices
    ]


# NOTE: fixed-path routes (/devices/status ...) must be declared BEFORE "/devices/{device_id}" or the
# path parameter swallows "status" and the dashboard gets "Device not found".
@router.get("/devices/status")
@router.get("/devices/status/summary")
async def devices_status(
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Get devices status summary with automatic stale detection"""
    
    # Mark devices as offline if no heartbeat for 2 minutes
    stale_cutoff = datetime.utcnow() - timedelta(minutes=2)
    
    await db.execute(
        text("""
            UPDATE devices 
            SET status = 'offline'
            WHERE tenant_id = :tenant_id
            AND status = 'online'
            AND (last_seen IS NULL OR last_seen < :cutoff)
        """),
        {"tenant_id": tenant.id, "cutoff": stale_cutoff}
    )
    await db.commit()
    
    # Get updated statistics
    result = await db.execute(
        text("""
            SELECT 
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE status = 'online') as online,
                COUNT(*) FILTER (WHERE status != 'online') as offline
            FROM devices
            WHERE tenant_id = :tenant_id
        """),
        {"tenant_id": tenant.id}
    )
    
    stats = result.mappings().first()
    
    return {
        "total": stats["total"] or 0,
        "online": stats["online"] or 0,
        "offline": stats["offline"] or 0
    }


@router.get("/devices/{device_id}")
async def get_device(
    device_id: str,
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Tenant Manager: Get device details"""
    
    result = await db.execute(
        select(Device).where(
            Device.device_id == device_id,
            Device.tenant_id == tenant.id
        )
    )
    device = result.scalars().first()
    
    if not device:
        raise HTTPException(404, "Device not found")
    
    # Get recent commands for this device
    commands = await db.execute(text("""
        SELECT id, command, target_id, status, created_at
        FROM commands
        WHERE device_id = :device_id
        AND tenant_id = :tenant_id
        ORDER BY created_at DESC
        LIMIT 20
    """), {
        "device_id": device_id,
        "tenant_id": tenant.id
    })
    
    return {
        "id": device.id,
        "device_id": device.device_id,
        "secret_key": device.secret_key,
        "status": device.status,
        "last_seen": device.last_seen,
        "recent_commands": commands.mappings().all()
    }


@router.put("/devices/{device_id}")
async def update_device(
    device_id: str,
    data: DeviceUpdate,
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Tenant Manager: Update device details"""
    
    result = await db.execute(
        select(Device).where(
            Device.device_id == device_id,
            Device.tenant_id == tenant.id
        )
    )
    device = result.scalars().first()
    
    if not device:
        raise HTTPException(404, "Device not found")
    
    # Check if new device_id conflicts with existing
    if data.device_id and data.device_id != device.device_id:
        existing = await db.execute(
            select(Device).where(
                Device.tenant_id == tenant.id,
                Device.device_id == data.device_id
            )
        )
        if existing.scalars().first():
            raise HTTPException(400, "Device ID already exists for this tenant")
        device.device_id = data.device_id
    
    if data.secret_key:
        device.secret_key = data.secret_key
    
    await db.commit()
    await db.refresh(device)
    
    return {
        "message": "Device updated successfully",
        "device": {
            "id": device.id,
            "device_id": device.device_id,
            "status": device.status,
            "last_seen": device.last_seen
        }
    }


@router.delete("/devices/{device_id}")
async def delete_device(
    device_id: str,
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Tenant Manager: Delete a device"""
    
    result = await db.execute(
        select(Device).where(
            Device.device_id == device_id,
            Device.tenant_id == tenant.id
        )
    )
    device = result.scalars().first()
    
    if not device:
        raise HTTPException(404, "Device not found")
    
    # Delete associated commands first (foreign key constraint)
    await db.execute(
        text("DELETE FROM commands WHERE device_id = :device_id AND tenant_id = :tenant_id"),
        {"device_id": device_id, "tenant_id": tenant.id}
    )
    
    await db.delete(device)
    await db.commit()
    
    return {"message": "Device deleted successfully"}


@router.patch("/devices/{device_id}/status")
async def update_device_status(
    device_id: str,
    status: str,
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Update device online/offline status (called by device heartbeat)"""
    
    if status not in ["online", "offline"]:
        raise HTTPException(400, "Status must be 'online' or 'offline'")
    
    result = await db.execute(
        select(Device).where(
            Device.device_id == device_id,
            Device.tenant_id == tenant.id
        )
    )
    device = result.scalars().first()
    
    if not device:
        raise HTTPException(404, "Device not found")
    
    device.status = status
    if status == "online":
        device.last_seen = func.now()
    
    await db.commit()
    
    return {
        "message": f"Device status updated to {status}",
        "device_id": device_id,
        "status": status,
        "last_seen": device.last_seen
    }


@router.post("/devices/fire-command")
async def fire_device_command(
    data: FireCommandRequest,
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Tenant Manager: Send command to device (enroll/delete fingerprint)"""
    
    # Verify device exists
    result = await db.execute(
        select(Device).where(
            Device.device_id == data.device_id,
            Device.tenant_id == tenant.id
        )
    )
    device = result.scalars().first()
    
    if not device:
        raise HTTPException(404, "Device not found")
    
    # ✅ Check if device heartbeat is stale (TTL check - 2 minutes)
    stale_cutoff = datetime.utcnow() - timedelta(minutes=2)
    is_heartbeat_stale = device.last_seen and device.last_seen < stale_cutoff
    
    # Update device status based on heartbeat freshness
    if is_heartbeat_stale:
        device.status = "offline"
        await db.commit()
        raise HTTPException(409, f"Device {data.device_id} heartbeat is stale (no heartbeat for >2 minutes). Cannot send command.")
    
    # Check if device is online
    if device.status != "online":
        raise HTTPException(409, f"Device {data.device_id} is offline. Cannot send command.")
    
    # Validate command type
    if data.command not in ["enroll", "delete"]:
        raise HTTPException(400, "Command must be 'enroll' or 'delete'")
    
    # Validate target_id range
    if data.target_id and (data.target_id < 1 or data.target_id > 127):
        raise HTTPException(400, "Finger ID must be between 1 and 127")
    
    # For enroll command, verify finger_id is assigned to an employee
    if data.command == "enroll" and data.target_id:
        emp_result = await db.execute(
            select(User).where(
                User.tenant_id == tenant.id,
                User.finger_id == data.target_id,
                User.is_active == True
            )
        )
        employee = emp_result.scalars().first()
        if not employee:
            raise HTTPException(400, f"No active employee assigned to finger ID {data.target_id}")
    
    # Create command record
    cmd = Command(
        tenant_id=tenant.id,
        device_id=data.device_id,
        command=data.command,
        target_id=data.target_id,
        status="PENDING"
    )
    
    db.add(cmd)
    await db.commit()
    await db.refresh(cmd)
    
    # Publish MQTT command
    try:
        mqtt_manager.publish_command(
            tenant.id,
            data.device_id,
            cmd.id,
            data.command,
            data.target_id
        )
    except Exception as e:
        cmd.status = "FAILED"
        await db.commit()
        raise HTTPException(500, f"Failed to publish MQTT command: {str(e)}")
    
    return {
        "message": f"Command '{data.command}' dispatched successfully",
        "command_id": cmd.id,
        "device_id": data.device_id,
        "target_id": data.target_id,
        "status": "PENDING"
    }


@router.post("/devices/bulk/status")
async def get_devices_bulk_status(
    device_ids: List[str],
    tenant: Tenant = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db)
):
    """Get status for multiple devices at once"""
    
    result = await db.execute(
        select(Device).where(
            Device.tenant_id == tenant.id,
            Device.device_id.in_(device_ids)
        )
    )
    devices = result.scalars().all()
    
    return {
        device.device_id: {
            "status": device.status,
            "last_seen": device.last_seen
        }
        for device in devices
    }