"""Shared SMTP utilities used by admin and notification handlers."""
import asyncio
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def smtp_env() -> dict:
    """Return SMTP config from env vars when SMTP_HOST is set, else empty dict."""
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        return {}
    return {
        "host":       host,
        "port":       int(os.getenv("SMTP_PORT", "587")),
        "username":   os.getenv("SMTP_USERNAME", "").strip(),
        "password":   os.getenv("SMTP_PASSWORD", "").strip(),
        "from_email": os.getenv("SMTP_FROM_EMAIL", "").strip(),
        "from_name":  os.getenv("SMTP_FROM_NAME", "Kalipeh Wallet").strip(),
        "use_tls":    os.getenv("SMTP_USE_TLS", "true").lower() != "false",
        "use_ssl":    os.getenv("SMTP_USE_SSL", "false").lower() == "true",
        "enabled":    os.getenv("SMTP_ENABLED", "true").lower() != "false",
    }


async def resolve_smtp(db: AsyncSession) -> dict:
    """Effective SMTP config as a plain dict. Env vars take priority over DB."""
    env = smtp_env()
    if env:
        return env
    from models.smtp_settings import SmtpConfig
    cfg = await db.scalar(select(SmtpConfig).where(SmtpConfig.id == 1))
    if not cfg:
        return {}
    return {
        "host":       cfg.host,
        "port":       cfg.port,
        "username":   cfg.username,
        "password":   cfg.password,
        "from_email": cfg.from_email,
        "from_name":  cfg.from_name,
        "use_tls":    cfg.use_tls,
        "use_ssl":    cfg.use_ssl,
        "enabled":    cfg.enabled,
    }


def _send_sync(
    host: str, port: int, username: str, password: str,
    from_email: str, from_name: str, use_tls: bool, use_ssl: bool,
    to: str, subject: str, html_body: str,
) -> None:
    """Synchronous SMTP send — safe to run in asyncio.to_thread."""
    msg = MIMEMultipart("alternative")
    msg["From"]    = f"{from_name} <{from_email}>"
    msg["To"]      = to
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    if use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        server = smtplib.SMTP(host, port, timeout=15)
        if use_tls:
            server.ehlo()
            server.starttls()
            server.ehlo()

    try:
        if username and password:
            server.login(username, password)
        server.sendmail(from_email, to, msg.as_string())
    finally:
        server.quit()


async def send_email(smtp: dict, to: str, subject: str, html_body: str) -> None:
    """Send via a resolved SMTP config dict. Silently skips if not enabled/configured."""
    if not smtp.get("enabled") or not smtp.get("host") or not smtp.get("from_email"):
        return
    await asyncio.to_thread(
        _send_sync,
        smtp["host"], smtp["port"],
        smtp.get("username", ""), smtp.get("password", ""),
        smtp["from_email"], smtp.get("from_name", ""),
        smtp.get("use_tls", True), smtp.get("use_ssl", False),
        to, subject, html_body,
    )
