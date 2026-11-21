from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, insert
from app.db.session import get_db
from app.api.dependencies import get_current_user, require_role
from app.models.domain import User, Notification

router = APIRouter()


# =========================
# BACKGROUND TASKS
# =========================

async def notify_leave_approved_bg(
    tenant_id: int,
    approver_id: int,
    employee_id: int,
    leave_id: int,
    leave_type: str,
    start_date: str,
    end_date: str,
    db: AsyncSession
):
    """Background task: Notify employee of leave approval"""
    
    await db.execute(insert(Notification).values([{
        "tenant_id": tenant_id,
        "actor_id": approver_id,
        "actor_name": "Department Head",
        "recipient_id": employee_id,
        "event_type": "leave_approved_dept",
        "entity_type": "Leave",
        "entity_id": leave_id,
        "entity_name": f"{leave_type} leave",
        "title": "Leave Approved (Department)",
        "message": f"Your {leave_type} leave from {start_date} to {end_date} was approved by department head",
        "is_read": False
    }]))
    await db.commit()


# =========================
# ROUTES
# =========================

@router.get("/leaves")
async def list_department_leaves(
    current_user: User = Depends(require_role("org_admin")),
    db: AsyncSession = Depends(get_db)
):
    """Org Admin: List leave requests for department"""
    result = await db.execute(text("""
        SELECT l.leave_id, u.name as employee_name, u.employee_code,
            l.leave_type, l.start_date, l.end_date, l.reason, l.status, l.created_at
        FROM leaves l
        JOIN users u ON l.employee_id = u.id
        WHERE l.tenant_id = :tenant_id AND u.dept_id = :dept_id
        ORDER BY l.created_at DESC
    """), {"tenant_id": current_user.tenant_id, "dept_id": current_user.dept_id})
    return result.mappings().all()


@router.patch("/leaves/{leave_id}/approve")
async def approve_leave(
    leave_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_role("org_admin")),
    db: AsyncSession = Depends(get_db)
):
    """Org Admin: Approve leave request (first level)"""
    
    # Get leave details
    leave_info = await db.execute(text("""
        SELECT l.employee_id, l.leave_type, l.start_date, l.end_date, u.name as employee_name
        FROM leaves l
        JOIN users u ON l.employee_id = u.id
        WHERE l.leave_id = :leave_id
    """), {"leave_id": leave_id})
    info = leave_info.mappings().first()
    
    if not info:
        raise HTTPException(404, "Leave request not found")
    
    result = await db.execute(text("""
        UPDATE leaves l
        SET status = 'approved_by_dept', dept_approved_at = NOW(), dept_approved_by = :admin_id
        FROM users u
        WHERE l.leave_id = :leave_id AND l.employee_id = u.id 
        AND u.dept_id = :dept_id AND l.tenant_id = :tenant_id
        RETURNING l.leave_id
    """), {
        "leave_id": leave_id,
        "dept_id": current_user.dept_id,
        "tenant_id": current_user.tenant_id,
        "admin_id": current_user.id
    })
    
    if result.rowcount == 0:
        raise HTTPException(404, "Leave request not found or not in your department")
    
    await db.commit()
    
    # Schedule notification in BACKGROUND
    background_tasks.add_task(
        notify_leave_approved_bg,
        tenant_id=current_user.tenant_id,
        approver_id=current_user.id,
        employee_id=info["employee_id"],
        leave_id=leave_id,
        leave_type=info["leave_type"],
        start_date=str(info["start_date"]),
        end_date=str(info["end_date"]),
        db=db
    )
    
    return {"message": "Leave approved at department level"}


class RejectLeaveRequest(BaseModel):
    reason: Optional[str] = None


@router.patch("/leaves/{leave_id}/reject")
async def reject_leave(
    leave_id: int,
    data: Optional[RejectLeaveRequest] = None,
    reason: Optional[str] = None,
    current_user: User = Depends(require_role("org_admin")),
    db: AsyncSession = Depends(get_db)
):
    """Org Admin: Reject a leave request from own department (reason in JSON body or ?reason=)"""
    reason = (data.reason if data and data.reason else reason) or None

    leave_info = await db.execute(text("""
        SELECT l.employee_id, l.leave_type, l.start_date, l.end_date, l.status
        FROM leaves l
        JOIN users u ON l.employee_id = u.id
        WHERE l.leave_id = :leave_id
        AND l.tenant_id = :tenant_id
        AND u.dept_id = :dept_id
    """), {
        "leave_id": leave_id,
        "tenant_id": current_user.tenant_id,
        "dept_id": current_user.dept_id
    })
    info = leave_info.mappings().first()

    if not info:
        raise HTTPException(404, "Leave request not found or not in your department")

    if info["status"] not in ("pending", "approved_by_dept"):
        raise HTTPException(400, f"Leave request is already {info['status']}")

    await db.execute(text("""
        UPDATE leaves
        SET status = 'rejected', rejection_reason = :reason, rejected_at = NOW()
        WHERE leave_id = :leave_id AND tenant_id = :tenant_id
    """), {"leave_id": leave_id, "tenant_id": current_user.tenant_id, "reason": reason})

    message = (
        f"Your {info['leave_type']} leave from {info['start_date']} to {info['end_date']} "
        f"was rejected by department head"
    )
    if reason:
        message += f". Reason: {reason}"

    db.add(Notification(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        actor_name="Department Head",
        recipient_id=info["employee_id"],
        event_type="leave_rejected_dept",
        entity_type="Leave",
        entity_id=leave_id,
        entity_name=f"{info['leave_type']} leave",
        title="Leave Rejected (Department)",
        message=message,
        is_read=False
    ))
    await db.commit()

    return {"message": "Leave rejected"}