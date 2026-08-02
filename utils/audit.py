from sqlalchemy.ext.asyncio import AsyncSession

from models.audit_log import AuditLog


async def log_audit(
    db: AsyncSession,
    action: str,
    actor_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    details: dict | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details or {},
        ip_address=ip_address,
    )
    db.add(entry)
    await db.flush()
    return entry
