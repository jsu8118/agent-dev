"""Shared constants for the Kestrel Pumps & Controls dataset (all fictional)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1]
AS_OF = dt.date(2026, 9, 15)          # "today" in the course world (a Tuesday)
SEED = 20260915


def d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


# --------------------------------------------------------------------------- customers
# (id, name, industry, tier, country, contact, domain, account_manager)
CUSTOMERS = [
    ("C-1001", "Bluewater Municipal Utilities", "water utility", "strategic", "US", "Dana Whitfield", "bluewater-utilities.example", "Priya Raman"),
    ("C-1002", "Cobalt Chemical Works", "chemicals", "key", "US", "Marcus Hale", "cobaltchem.example", "Priya Raman"),
    ("C-1003", "Summit Ridge Mining", "mining", "key", "CA", "Elise Tremblay", "summitridge.example", "Tom Becker"),
    ("C-1004", "GreenValley Irrigation Co-op", "agriculture", "standard", "US", "Jorge Medina", "greenvalley-coop.example", "Tom Becker"),
    ("C-1005", "Harbor Foods Processing", "food & beverage", "key", "US", "Aisha Karim", "harborfoods.example", "Lena Ortiz"),
    ("C-1006", "Northgate HVAC Distributors", "distribution", "strategic", "US", "Kevin O'Neill", "northgate-hvac.example", "Lena Ortiz"),
    ("C-1007", "Aurora Pharma Manufacturing", "pharmaceuticals", "key", "IE", "Siobhan Byrne", "aurorapharma.example", "Henrik Vos"),
    ("C-1008", "Delta Paper & Pulp", "pulp & paper", "standard", "US", "Rick Albrecht", "deltapaper.example", "Tom Becker"),
    ("C-1009", "Riverbend Brewing Co.", "food & beverage", "standard", "US", "Hannah Cole", "riverbendbrewing.example", "Lena Ortiz"),
    ("C-1010", "Ironclad Steel Fabricators", "metals", "standard", "US", "Victor Petrov", "ironclad-steel.example", "Tom Becker"),
    ("C-1011", "Pacific Desalination Partners", "water utility", "strategic", "AU", "Grace Liu", "pacificdesal.example", "Henrik Vos"),
    ("C-1012", "Lumen Data Centers", "data centers", "key", "US", "Omar Haddad", "lumen-dc.example", "Priya Raman"),
    ("C-1013", "Coastal Shipyards Ltd", "marine", "standard", "GB", "Fiona MacLeod", "coastalshipyards.example", "Henrik Vos"),
    ("C-1014", "Midland Oil Services", "oil & gas", "key", "US", "Travis Greer", "midlandoil.example", "Priya Raman"),
    ("C-1015", "Evergreen Parks District", "municipal", "standard", "US", "Nora Quinn", "evergreenparks.example", "Tom Becker"),
    ("C-1016", "Keystone Mechanical Contractors", "contractor", "standard", "US", "Luis Romero", "keystone-mech.example", "Lena Ortiz"),
    ("C-1017", "Silverline Textiles", "textiles", "standard", "MX", "Camila Reyes", "silverline-tex.example", "Lena Ortiz"),
    ("C-1018", "Polar Cold Storage", "refrigeration", "standard", "NO", "Ingrid Solberg", "polarcold.example", "Henrik Vos"),
    ("C-1019", "Granite Bay Water District", "water utility", "key", "US", "Paul Stanton", "granitebay-water.example", "Priya Raman"),
    ("C-1020", "Orion Semiconductor Fab", "semiconductors", "strategic", "US", "Mei Chen", "orion-semi.example", "Priya Raman"),
    ("C-1021", "Westfield Hospital Group", "healthcare facilities", "standard", "US", "Brian Foster", "westfield-health.example", "Lena Ortiz"),
    ("C-1022", "Trident Offshore Engineering", "offshore", "key", "GB", "Callum Reid", "trident-offshore.example", "Henrik Vos"),
    ("C-1023", "Cedar Creek Dairy", "food & beverage", "standard", "US", "Molly Jensen", "cedarcreekdairy.example", "Tom Becker"),
    ("C-1024", "Vanguard Fire Protection", "fire protection", "standard", "US", "Derek Walsh", "vanguardfire.example", "Tom Becker"),
    ("C-1025", "Meridian Hotels & Resorts", "hospitality", "standard", "ES", "Lucia Fernandez", "meridianhotels.example", "Henrik Vos"),
]
CUSTOMER_BY_ID = {c[0]: c for c in CUSTOMERS}
TIER_DISCOUNT = {"strategic": 0.12, "key": 0.08, "standard": 0.0}

# --------------------------------------------------------------------------- products
# (sku, name, line, family, list_price, warranty_months, lead_time_days, weight_kg, configurable)
PRODUCTS = [
    ("KP-100-S", "KP-100 compact end-suction pump, 5.5 kW", "pump", "KP-100", 2450.00, 24, 3, 95, 0),
    ("KP-250-S", "KP-250 end-suction pump, 22 kW", "pump", "KP-250", 8900.00, 24, 3, 385, 0),
    ("KP-250-X", "KP-250-X ATEX explosion-proof pump, 22 kW", "pump", "KP-250", 11750.00, 24, 35, 410, 0),
    ("KP-400-S", "KP-400 horizontal split-case pump, 75 kW", "pump", "KP-400", 24500.00, 24, 42, 1650, 0),
    ("KP-600-M", "KP-600 multistage high-pressure pump, 110 kW", "pump", "KP-600", 38900.00, 24, 56, 2100, 0),
    ("KV-20-B", "KV-20 2-inch ball valve, 316 stainless", "valve", "KV-20", 310.00, 18, 2, 4, 0),
    ("KV-50-F", "KV-50 5-inch butterfly valve", "valve", "KV-50", 1280.00, 18, 2, 18, 0),
    ("KV-80-G", "KV-80 8-inch gate valve", "valve", "KV-80", 2950.00, 18, 5, 96, 0),
    ("KC-1", "KC-1 VFD pump controller, 22 kW", "controller", "KC-1", 3400.00, 12, 5, 21, 1),
    ("KC-2", "KC-2 smart pump controller (Modbus/TCP, cloud telemetry)", "controller", "KC-2", 5200.00, 12, 7, 23, 1),
    ("VS-10", "VS-10 vibration sensor with 10 m cable", "sensor", "VS-10", 420.00, 12, 2, 1, 0),
    ("PT-40", "PT-40 pressure transmitter 0-40 bar", "sensor", "PT-40", 380.00, 12, 2, 1, 0),
    ("FT-60", "FT-60 electromagnetic flow meter DN100", "sensor", "FT-60", 1150.00, 12, 7, 14, 0),
    ("MS-100", "Mechanical seal kit for KP-100", "spare_part", "MS", 290.00, 6, 2, 1, 0),
    ("MS-250", "Mechanical seal cartridge kit for KP-250", "spare_part", "MS", 640.00, 6, 2, 3, 0),
    ("MS-400", "Mechanical seal kit for KP-400", "spare_part", "MS", 980.00, 6, 2, 4, 0),
    ("IMP-250-A", "Impeller 250 mm, bronze", "spare_part", "IMP", 1420.00, 6, 5, 11, 0),
    ("IMP-250-D", "Impeller 250 mm, duplex stainless steel", "spare_part", "IMP", 2240.00, 6, 10, 11, 0),
    ("BRG-6309", "Deep groove ball bearing 6309 C3", "spare_part", "BRG", 85.00, 6, 2, 1, 0),
    ("BRG-6312", "Deep groove ball bearing 6312 C3", "spare_part", "BRG", 120.00, 6, 2, 2, 0),
    ("CPL-250", "Coupling with elastomer element for KP-250", "spare_part", "CPL", 310.00, 6, 2, 6, 0),
    ("GSK-KIT-250", "Gasket kit for KP-250", "spare_part", "GSK", 95.00, 6, 2, 1, 0),
    ("ORK-100", "O-ring kit for KP-100", "spare_part", "ORK", 40.00, 6, 2, 1, 0),
    ("LUB-EP2", "Bearing grease cartridge EP2, 400 g", "spare_part", "LUB", 22.00, 6, 1, 1, 0),
    ("FLT-SUC-100", "Suction strainer, 100 mesh", "spare_part", "FLT", 260.00, 6, 2, 7, 0),
    ("KC-1-FAN", "Cooling fan module for KC-1/KC-2", "spare_part", "FAN", 185.00, 6, 2, 1, 0),
    ("SVC-INSTALL", "On-site installation service (per day)", "service", "SVC", 1600.00, 0, 10, 0, 0),
    ("SVC-ALIGN", "Laser alignment service (per pump)", "service", "SVC", 900.00, 0, 7, 0, 0),
]
PRODUCT_BY_SKU = {p[0]: p for p in PRODUCTS}

WAREHOUSES = ["WH-EAST", "WH-WEST", "WH-EU"]
CARRIERS = {"freight": "NorthLine Freight", "parcel": "SwiftParcel", "intl": "BlueRiver Logistics"}
INTL_COUNTRIES = {"IE", "GB", "NO", "ES", "AU", "MX", "CA"}

# --------------------------------------------------------------------------- suppliers (AP)
# (id, name, country, currency, payment_terms, bank_last4, remit_email, tax_rate)
SUPPLIERS = [
    ("S-201", "Ferrous Castings Inc.", "US", "USD", "Net 45", "4471", "ar@ferrouscastings.example", 0.0),
    ("S-202", "Precision Seals GmbH", "DE", "EUR", "Net 30", "9920", "rechnung@precisionseals.example", 0.0),
    ("S-203", "Allied Bearings Co.", "US", "USD", "Net 30", "1188", "billing@alliedbearings.example", 0.0),
    ("S-204", "VoltDrive Electronics", "US", "USD", "Net 60", "3056", "ar@voltdrive.example", 0.0),
    ("S-205", "Coastal Freight Services", "US", "USD", "Net 15", "7742", "invoices@coastalfreight.example", 0.0),
    ("S-206", "Summit Industrial Supply", "US", "USD", "Net 30", "5519", "ar@summitindustrial.example", 0.08),
    ("S-207", "Nordic Alloys AB", "SE", "EUR", "Net 30", "6603", "faktura@nordicalloys.example", 0.0),
    ("S-208", "PackRight Packaging", "US", "USD", "Net 30", "2894", "billing@packright.example", 0.08),
]
