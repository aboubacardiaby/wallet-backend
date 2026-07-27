"""regions: states/provinces for a subset of priority countries

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-27
"""
import uuid
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

# country_code -> [(name, code_or_None), ...]
_REGIONS = {
    "SN": [(n, None) for n in [
        "Dakar", "Diourbel", "Fatick", "Kaffrine", "Kaolack", "Kédougou", "Kolda",
        "Louga", "Matam", "Saint-Louis", "Sédhiou", "Tambacounda", "Thiès", "Ziguinchor",
    ]],
    "CI": [(n, None) for n in [
        "Abidjan", "Bas-Sassandra", "Comoé", "Denguélé", "Gôh-Djiboua", "Lacs", "Lagunes",
        "Montagnes", "Sassandra-Marahoué", "Savanes", "Vallée du Bandama", "Woroba",
        "Yamoussoukro", "Zanzan",
    ]],
    "ML": [(n, None) for n in [
        "Bamako", "Kayes", "Koulikoro", "Sikasso", "Ségou", "Mopti", "Tombouctou",
        "Gao", "Kidal", "Ménaka", "Taoudénit",
    ]],
    "GN": [(n, None) for n in [
        "Boké", "Conakry", "Faranah", "Kankan", "Kindia", "Labé", "Mamou", "Nzérékoré",
    ]],
    "BF": [(n, None) for n in [
        "Boucle du Mouhoun", "Cascades", "Centre", "Centre-Est", "Centre-Nord",
        "Centre-Ouest", "Centre-Sud", "Est", "Hauts-Bassins", "Nord",
        "Plateau-Central", "Sahel", "Sud-Ouest",
    ]],
    "NE": [(n, None) for n in [
        "Agadez", "Diffa", "Dosso", "Maradi", "Niamey", "Tahoua", "Tillabéri", "Zinder",
    ]],
    "TG": [(n, None) for n in ["Maritime", "Plateaux", "Centrale", "Kara", "Savanes"]],
    "BJ": [(n, None) for n in [
        "Alibori", "Atacora", "Atlantique", "Borgou", "Collines", "Donga",
        "Kouffo", "Littoral", "Mono", "Ouémé", "Plateau", "Zou",
    ]],
    "MR": [(n, None) for n in [
        "Hodh Ech Chargui", "Hodh El Gharbi", "Assaba", "Gorgol", "Brakna", "Trarza",
        "Adrar", "Dakhlet Nouadhibou", "Tagant", "Guidimaka", "Tiris Zemmour",
        "Inchiri", "Nouakchott",
    ]],
    "GM": [(n, None) for n in [
        "Banjul", "Kanifing", "West Coast", "Lower River", "North Bank",
        "Central River", "Upper River",
    ]],
    "GW": [(n, None) for n in [
        "Bafatá", "Biombo", "Bissau", "Bolama", "Cacheu", "Gabú", "Oio", "Quinara", "Tombali",
    ]],
    "CV": [(n, None) for n in [
        "Boa Vista", "Brava", "Fogo", "Maio", "Sal", "Santo Antão",
        "São Nicolau", "São Vicente", "Santiago",
    ]],
    "SL": [(n, None) for n in ["Eastern", "Northern", "North West", "Southern", "Western Area"]],
    "LR": [(n, None) for n in [
        "Bomi", "Bong", "Gbarpolu", "Grand Bassa", "Grand Cape Mount", "Grand Gedeh",
        "Grand Kru", "Lofa", "Margibi", "Maryland", "Montserrado", "Nimba",
        "River Cess", "River Gee", "Sinoe",
    ]],
    "GH": [(n, None) for n in [
        "Ahafo", "Ashanti", "Bono", "Bono East", "Central", "Eastern", "Greater Accra",
        "North East", "Northern", "Oti", "Savannah", "Upper East", "Upper West",
        "Volta", "Western", "Western North",
    ]],
    "NG": [(n, None) for n in [
        "Abia", "Adamawa", "Akwa Ibom", "Anambra", "Bauchi", "Bayelsa", "Benue", "Borno",
        "Cross River", "Delta", "Ebonyi", "Edo", "Ekiti", "Enugu", "Gombe", "Imo",
        "Jigawa", "Kaduna", "Kano", "Katsina", "Kebbi", "Kogi", "Kwara", "Lagos",
        "Nasarawa", "Niger", "Ogun", "Ondo", "Osun", "Oyo", "Plateau", "Rivers",
        "Sokoto", "Taraba", "Yobe", "Zamfara", "Federal Capital Territory",
    ]],
    "US": [
        ("Alabama", "AL"), ("Alaska", "AK"), ("Arizona", "AZ"), ("Arkansas", "AR"),
        ("California", "CA"), ("Colorado", "CO"), ("Connecticut", "CT"), ("Delaware", "DE"),
        ("Florida", "FL"), ("Georgia", "GA"), ("Hawaii", "HI"), ("Idaho", "ID"),
        ("Illinois", "IL"), ("Indiana", "IN"), ("Iowa", "IA"), ("Kansas", "KS"),
        ("Kentucky", "KY"), ("Louisiana", "LA"), ("Maine", "ME"), ("Maryland", "MD"),
        ("Massachusetts", "MA"), ("Michigan", "MI"), ("Minnesota", "MN"), ("Mississippi", "MS"),
        ("Missouri", "MO"), ("Montana", "MT"), ("Nebraska", "NE"), ("Nevada", "NV"),
        ("New Hampshire", "NH"), ("New Jersey", "NJ"), ("New Mexico", "NM"), ("New York", "NY"),
        ("North Carolina", "NC"), ("North Dakota", "ND"), ("Ohio", "OH"), ("Oklahoma", "OK"),
        ("Oregon", "OR"), ("Pennsylvania", "PA"), ("Rhode Island", "RI"), ("South Carolina", "SC"),
        ("South Dakota", "SD"), ("Tennessee", "TN"), ("Texas", "TX"), ("Utah", "UT"),
        ("Vermont", "VT"), ("Virginia", "VA"), ("Washington", "WA"), ("West Virginia", "WV"),
        ("Wisconsin", "WI"), ("Wyoming", "WY"), ("District of Columbia", "DC"),
    ],
    "CA": [
        ("Alberta", "AB"), ("British Columbia", "BC"), ("Manitoba", "MB"),
        ("New Brunswick", "NB"), ("Newfoundland and Labrador", "NL"), ("Nova Scotia", "NS"),
        ("Ontario", "ON"), ("Prince Edward Island", "PE"), ("Quebec", "QC"),
        ("Saskatchewan", "SK"), ("Northwest Territories", "NT"), ("Nunavut", "NU"),
        ("Yukon", "YT"),
    ],
    "GB": [(n, None) for n in ["England", "Scotland", "Wales", "Northern Ireland"]],
    "FR": [(n, None) for n in [
        "Auvergne-Rhône-Alpes", "Bourgogne-Franche-Comté", "Bretagne", "Centre-Val de Loire",
        "Corse", "Grand Est", "Hauts-de-France", "Île-de-France", "Normandie",
        "Nouvelle-Aquitaine", "Occitanie", "Pays de la Loire", "Provence-Alpes-Côte d'Azur",
    ]],
}


def upgrade() -> None:
    op.create_table(
        "regions",
        sa.Column("id",           PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("country_code", sa.String(2),   sa.ForeignKey("countries.code"), nullable=False),
        sa.Column("name",         sa.String(100), nullable=False),
        sa.Column("code",         sa.String(10),  nullable=True),
        sa.Column("is_active",    sa.Boolean(),   server_default="true", nullable=False),
        sa.Column("created_at",   sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at",   sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_regions_country_code", "regions", ["country_code"])

    regions_table = sa.table(
        "regions",
        sa.column("id", PG_UUID(as_uuid=True)),
        sa.column("country_code", sa.String),
        sa.column("name", sa.String),
        sa.column("code", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.utcnow()
    rows = [
        {
            "id": uuid.uuid4(), "country_code": country_code, "name": name,
            "code": code, "created_at": now, "updated_at": now,
        }
        for country_code, entries in _REGIONS.items()
        for name, code in entries
    ]
    op.bulk_insert(regions_table, rows)


def downgrade() -> None:
    op.drop_index("ix_regions_country_code", table_name="regions")
    op.drop_table("regions")
