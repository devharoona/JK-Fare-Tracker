import json
from pathlib import Path

import streamlit as st

from jk_fare_tracker import (
    APP_NAME,
    DB_PATH,
    FareDatabase,
    fetch_and_store_jkrtc,
    seed_government_rates,
    utc_now,
)

st.set_page_config(page_title="JK Fare Tracker", page_icon="🚌", layout="wide")
st.title("🚌 JK Fare Tracker")
st.caption("Public transport fare information for Jammu & Kashmir")

db = FareDatabase(DB_PATH)

def query_fares(origin: str, destination: str, mode: str):
    clauses = ["status = 'ACTIVE'"]
    params = []

    if origin:
        clauses.append("LOWER(origin) LIKE ?")
        params.append(f"%{origin.lower()}%")

    if destination:
        clauses.append("LOWER(destination) LIKE ?")
        params.append(f"%{destination.lower()}%")

    if mode:
        clauses.append("LOWER(mode) = ?")
        params.append(mode.lower())

    sql = f"""
    SELECT source, mode, vehicle_class, origin, destination, fare, unit, observed_at
    FROM fares
    WHERE {' AND '.join(clauses)}
    ORDER BY mode, origin, destination, vehicle_class
    """
    return [dict(r) for r in db.connection.execute(sql, params).fetchall()]

col1, col2 = st.columns(2)
with col1:
    if st.button("🔄 Update from official sources"):
        result = fetch_and_store_jkrtc(db)
        st.success(f"Updated. Records: {result['records']}, changed sources: {result['changes']}")
with col2:
    if st.button("🏛️ Seed government reference fares"):
        seed_government_rates(db)
        st.success("Government reference fares saved.")

st.divider()
st.subheader("Search fares")

c1, c2, c3 = st.columns(3)
origin = c1.text_input("From", "")
destination = c2.text_input("To", "")
mode = c3.text_input("Mode (example: bus, electric_auto)", "")

rows = query_fares(origin, destination, mode)

if rows:
    st.dataframe(rows, use_container_width=True)
else:
    st.info("No matching fare records.")

payload = {
    "application": APP_NAME,
    "generated_at": utc_now(),
    "fares": rows,
}
st.download_button(
    "⬇️ Download current view (JSON)",
    data=json.dumps(payload, indent=2, ensure_ascii=False),
    file_name="jk_fares.json",
    mime="application/json",
)

db.close()