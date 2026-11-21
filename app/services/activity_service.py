"""
Activity feed service.

Builds a unified, most-recent-first activity timeline (leave requests, attendance
check-ins/outs, new employees) for a tenant, optionally limited to one department.
Used by the tenant and org-admin ``/activity`` endpoints.
"""
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def get_activity_feed(
    db: AsyncSession,
    tenant_id: int,
    dept_id: Optional[int] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return up to ``limit`` recent activity items.

    Each item has: event_type ('leave' | 'attendance' | 'employee'), title, message,
    actor_name, entity_type, entity_id, created_at.
    """
    limit = max(1, min(int(limit), 200))

    result = await db.execute(
        text(
            """
            SELECT * FROM (
                -- Leave requests and their latest state change
                SELECT
                    'leave'::text AS event_type,
                    CASE l.status
                        WHEN 'approved' THEN 'Leave approved'
                        WHEN 'approved_by_dept' THEN 'Leave approved by department'
                        WHEN 'rejected' THEN 'Leave rejected'
                        WHEN 'cancelled' THEN 'Leave cancelled'
                        ELSE 'Leave requested'
                    END AS title,
                    u.name || ' - ' || l.leave_type || ' leave ('
                        || l.start_date::date || ' to ' || l.end_date::date || ')' AS message,
                    u.name AS actor_name,
                    'Leave'::text AS entity_type,
                    l.leave_id AS entity_id,
                    COALESCE(l.rejected_at, l.approved_at, l.dept_approved_at, l.created_at) AS created_at
                FROM leaves l
                JOIN users u ON u.id = l.employee_id
                WHERE l.tenant_id = :tenant_id
                  AND (CAST(:dept_id AS INTEGER) IS NULL OR u.dept_id = CAST(:dept_id AS INTEGER))

                UNION ALL

                -- Attendance check-ins / check-outs
                SELECT
                    'attendance'::text AS event_type,
                    CASE WHEN a.record_type = 'OUT' THEN 'Checked out' ELSE 'Checked in' END AS title,
                    u.name || ' ' || CASE WHEN a.record_type = 'OUT' THEN 'checked out' ELSE 'checked in' END
                        || ' on device ' || a.device_id AS message,
                    u.name AS actor_name,
                    'AttendanceLog'::text AS entity_type,
                    a.id AS entity_id,
                    a.timestamp AS created_at
                FROM attendance_logs a
                JOIN users u ON u.id = a.user_id
                WHERE a.tenant_id = :tenant_id
                  AND (CAST(:dept_id AS INTEGER) IS NULL OR u.dept_id = CAST(:dept_id AS INTEGER))

                UNION ALL

                -- New employees
                SELECT
                    'employee'::text AS event_type,
                    'Employee added' AS title,
                    u.name || COALESCE(' (' || u.employee_code || ')', '') || ' was added' AS message,
                    u.name AS actor_name,
                    'User'::text AS entity_type,
                    u.id AS entity_id,
                    u.created_at AS created_at
                FROM users u
                WHERE u.tenant_id = :tenant_id
                  AND u.role = 'employee'
                  AND (CAST(:dept_id AS INTEGER) IS NULL OR u.dept_id = CAST(:dept_id AS INTEGER))
            ) feed
            WHERE created_at IS NOT NULL
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"tenant_id": tenant_id, "dept_id": dept_id, "limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]
