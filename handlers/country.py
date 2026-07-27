from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from models.country import Country
from models.region import Region
from utils import row_to_dict

router = APIRouter(tags=["countries"])


@router.get("/countries")
async def list_countries(
    active_only: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
):
    q = select(Country).order_by(Country.name)
    if active_only:
        q = q.where(Country.is_active.is_(True))
    rows = await db.scalars(q)
    return {"countries": [row_to_dict(c) for c in rows]}


@router.get("/regions")
async def list_regions(
    country_code: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(Region)
        .where(Region.country_code == country_code.upper(), Region.is_active.is_(True))
        .order_by(Region.name)
    )
    rows = await db.scalars(q)
    return {"regions": [row_to_dict(r) for r in rows]}
