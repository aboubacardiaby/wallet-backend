import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import DeclarativeBase


def row_to_dict(row: DeclarativeBase, exclude: tuple = ()) -> dict:
    # Build column-name → Python-attribute-key map so columns like
    # metadata_ (mapped as "metadata") are accessed by the right attr.
    mapper = row.__class__.__mapper__
    col_to_attr: dict[str, str] = {}
    for prop in mapper.column_attrs:
        for col in prop.columns:
            col_to_attr[col.name] = prop.key

    result = {}
    for col in row.__table__.columns:
        attr_name = col_to_attr.get(col.name, col.name)
        # exclude accepts either the column name or the Python attr name
        if col.name in exclude or attr_name in exclude:
            continue
        val = getattr(row, attr_name)
        if val is None:
            result[col.name] = None
        elif isinstance(val, uuid.UUID):
            result[col.name] = str(val)
        elif isinstance(val, datetime):
            result[col.name] = val.isoformat()
        elif isinstance(val, Decimal):
            result[col.name] = float(val)
        else:
            result[col.name] = val
    return result
