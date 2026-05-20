"""
dashboard.py
Generates an interactive HTML dashboard with charts filtered by carrier.
Uses Chart.js (CDN) - just open the HTML in your browser.

Usage:
    py -3 dashboard.py
"""

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from config import CSV_OUT, HTML_DASHBOARD

CSV_IN   = CSV_OUT
HTML_OUT = HTML_DASHBOARD


def to_float(val):
    if not val:
        return None
    try:
        return float(val.replace(",", "").replace("$", ""))
    except ValueError:
        return None


def load_data():
    with open(CSV_IN, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    rows = load_data()
    print(f"Loaded {len(rows)} records from CSV")

    # Prepare data as JSON for the frontend
    # Each row becomes a JS object with the required fields
    js_rows = []
    for r in rows:
        due = to_float(r.get("due_amount")) or 0
        total = to_float(r.get("total_charges")) or 0
        fuel = to_float(r.get("fuel_surcharge")) or 0
        # Sanity check: fuel_surcharge cannot be greater than total_charges
        if fuel > total and total > 0:
            fuel = 0
        js_rows.append({
            "filename": r["filename"],
            "carrier": r["carrier"],
            "date": r.get("date", ""),
            "due_amount": due,
            "total_charges": total,
            "fuel_surcharge": fuel,
            "discount": to_float(r.get("discount")) or 0,
            "weight": to_float(r.get("weight")) or 0,
            "origin": r.get("origin", ""),
            "destination": r.get("destination", ""),
            "shipper_name": r.get("shipper_name", ""),
            "consignee_name": r.get("consignee_name", ""),
            "category": r["filename"].split("_")[1] if "_" in r["filename"] else "",
            "confidence": r.get("extraction_confidence", ""),
        })

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Invoice Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f0f2f5;
    color: #1a1a2e;
    padding: 20px;
  }}
  .header {{
    background: linear-gradient(135deg, #0f3460 0%, #16213e 100%);
    color: white;
    padding: 30px 40px;
    border-radius: 12px;
    margin-bottom: 24px;
  }}
  .header h1 {{ font-size: 26px; margin-bottom: 6px; }}
  .header p {{ opacity: 0.7; font-size: 13px; }}

  .filters {{
    background: white;
    border-radius: 10px;
    padding: 16px 24px;
    margin-bottom: 20px;
    display: flex;
    gap: 16px;
    align-items: center;
    flex-wrap: wrap;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
  }}
  .filters label {{ font-size: 12px; font-weight: 600; color: #374151; }}
  .filters select {{
    padding: 8px 14px;
    border: 1px solid #d1d5db;
    border-radius: 8px;
    font-size: 13px;
    cursor: pointer;
    outline: none;
    min-width: 140px;
  }}
  .filters select:focus {{ border-color: #2563eb; box-shadow: 0 0 0 2px rgba(37,99,235,0.15); }}
  .filter-info {{ margin-left: auto; font-size: 12px; color: #6b7280; }}

  .kpis {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 14px;
    margin-bottom: 24px;
  }}
  .kpi {{
    background: white;
    border-radius: 10px;
    padding: 18px;
    text-align: center;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    transition: transform 0.2s;
  }}
  .kpi:hover {{ transform: translateY(-2px); }}
  .kpi .value {{ font-size: 26px; font-weight: 700; color: #0f3460; }}
  .kpi .label {{ font-size: 10px; color: #666; text-transform: uppercase; margin-top: 4px; letter-spacing: 0.5px; }}
  .kpi.green .value {{ color: #059669; }}
  .kpi.blue .value {{ color: #2563eb; }}
  .kpi.purple .value {{ color: #7c3aed; }}
  .kpi.orange .value {{ color: #d97706; }}

  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
    gap: 20px;
    margin-bottom: 24px;
  }}
  .card {{
    background: white;
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
  }}
  .card h3 {{
    font-size: 13px;
    color: #374151;
    margin-bottom: 14px;
    padding-bottom: 8px;
    border-bottom: 1px solid #f1f5f9;
  }}
  .card canvas {{ max-height: 260px; }}

  .table-card {{
    background: white;
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    margin-bottom: 24px;
    overflow-x: auto;
  }}
  .table-card h3 {{ font-size: 13px; color: #374151; margin-bottom: 12px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 11px; }}
  th {{ background: #f8fafc; padding: 8px 10px; text-align: left; font-weight: 600; color: #475569; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #f1f5f9; }}
  tr:hover td {{ background: #f8fafc; }}

  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 10px; font-weight: 600;
  }}
  .badge-SAIA {{ background: #dbeafe; color: #1d4ed8; }}
  .badge-DAYTON {{ background: #dcfce7; color: #166534; }}
  .badge-FEDEX {{ background: #fef3c7; color: #92400e; }}
  .badge-AAA_COOPER {{ background: #fce7f3; color: #9d174d; }}
</style>
</head>
<body>

<div class="header">
  <h1>Invoice Dashboard</h1>
  <p>Extracted data from {len(rows)} freight invoices | Interactive filters</p>
</div>

<div class="filters">
  <label>Carrier:</label>
  <select id="fCarrier" onchange="updateDashboard()">
    <option value="">All</option>
    <option value="SAIA">SAIA</option>
    <option value="DAYTON">DAYTON</option>
    <option value="FEDEX">FEDEX</option>
    <option value="AAA_COOPER">AAA COOPER</option>
  </select>
  <label>Category:</label>
  <select id="fCategory" onchange="updateDashboard()">
    <option value="">All</option>
  </select>
  <label>Origin:</label>
  <select id="fOrigin" onchange="updateDashboard()">
    <option value="">All</option>
  </select>
  <label>Confidence:</label>
  <select id="fConfidence" onchange="updateDashboard()">
    <option value="">All</option>
    <option value="HIGH">HIGH</option>
    <option value="MEDIUM">MEDIUM</option>
    <option value="LOW">LOW</option>
  </select>
  <span class="filter-info" id="filterInfo">{len(rows)} invoices</span>
</div>

<div class="kpis">
  <div class="kpi green"><div class="value" id="kpiTotal">-</div><div class="label">Total Billed</div></div>
  <div class="kpi blue"><div class="value" id="kpiCount">-</div><div class="label">Invoices</div></div>
  <div class="kpi purple"><div class="value" id="kpiAvg">-</div><div class="label">Average</div></div>
  <div class="kpi orange"><div class="value" id="kpiFuel">-</div><div class="label">Avg Fuel</div></div>
  <div class="kpi"><div class="value" id="kpiWeight">-</div><div class="label">Avg Weight</div></div>
  <div class="kpi"><div class="value" id="kpiOrigins">-</div><div class="label">Origins</div></div>
</div>

<div class="grid">
  <div class="card"><h3>Invoices by Carrier</h3><canvas id="cCarrier"></canvas></div>
  <div class="card"><h3>Total Amount by Carrier ($)</h3><canvas id="cAmount"></canvas></div>
  <div class="card"><h3>Top 15 Origins</h3><canvas id="cOrigin"></canvas></div>
  <div class="card"><h3>Top 15 Destinations</h3><canvas id="cDest"></canvas></div>
  <div class="card"><h3>Weight Distribution (lbs)</h3><canvas id="cWeight"></canvas></div>
  <div class="card"><h3>Amount by Date</h3><canvas id="cTimeline"></canvas></div>
</div>

<div class="table-card">
  <h3>Top 10 Invoices by Amount</h3>
  <table>
    <thead><tr><th>File</th><th>Carrier</th><th>Amount</th><th>Origin</th><th>Destination</th><th>Weight</th></tr></thead>
    <tbody id="topTable"></tbody>
  </table>
</div>

<script>
const ALL_DATA = {json.dumps(js_rows)};
const COLORS = ['#3b82f6','#10b981','#f59e0b','#ec4899','#8b5cf6','#06b6d4','#64748b','#0ea5e9'];

// Populate dropdowns
const categories = [...new Set(ALL_DATA.map(r => r.category))].sort();
const origins = [...new Set(ALL_DATA.map(r => r.origin).filter(Boolean))].sort();
const selCat = document.getElementById('fCategory');
categories.forEach(c => {{ const o = document.createElement('option'); o.value = c; o.textContent = c; selCat.appendChild(o); }});
const selOrig = document.getElementById('fOrigin');
origins.forEach(c => {{ const o = document.createElement('option'); o.value = c; o.textContent = c; selOrig.appendChild(o); }});

// Charts
let charts = {{}};
function makeChart(id, type, data, options = {{}}) {{
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), {{ type, data, options }});
}}

function updateDashboard() {{
  const carrier = document.getElementById('fCarrier').value;
  const category = document.getElementById('fCategory').value;
  const origin = document.getElementById('fOrigin').value;
  const confidence = document.getElementById('fConfidence').value;

  let filtered = ALL_DATA;
  if (carrier) filtered = filtered.filter(r => r.carrier === carrier);
  if (category) filtered = filtered.filter(r => r.category === category);
  if (origin) filtered = filtered.filter(r => r.origin === origin);
  if (confidence) filtered = filtered.filter(r => r.confidence === confidence);

  document.getElementById('filterInfo').textContent = filtered.length + ' invoices';

  // KPIs
  const totalAmt = filtered.reduce((s, r) => s + r.due_amount, 0);
  const avgAmt = filtered.length ? totalAmt / filtered.length : 0;
  const fuels = filtered.filter(r => r.fuel_surcharge > 0);
  const avgFuel = fuels.length ? fuels.reduce((s, r) => s + r.fuel_surcharge, 0) / fuels.length : 0;
  const weights = filtered.filter(r => r.weight > 0);
  const avgWeight = weights.length ? weights.reduce((s, r) => s + r.weight, 0) / weights.length : 0;
  const uniqueOrigins = new Set(filtered.map(r => r.origin).filter(Boolean)).size;

  document.getElementById('kpiTotal').textContent = '$' + totalAmt.toLocaleString('en', {{maximumFractionDigits: 0}});
  document.getElementById('kpiCount').textContent = filtered.length;
  document.getElementById('kpiAvg').textContent = '$' + avgAmt.toLocaleString('en', {{maximumFractionDigits: 0}});
  document.getElementById('kpiFuel').textContent = '$' + avgFuel.toFixed(2);
  document.getElementById('kpiWeight').textContent = Math.round(avgWeight) + ' lbs';
  document.getElementById('kpiOrigins').textContent = uniqueOrigins;

  // Carrier chart
  const carrierCounts = {{}};
  const carrierAmounts = {{}};
  filtered.forEach(r => {{
    carrierCounts[r.carrier] = (carrierCounts[r.carrier] || 0) + 1;
    carrierAmounts[r.carrier] = (carrierAmounts[r.carrier] || 0) + r.due_amount;
  }});
  makeChart('cCarrier', 'doughnut', {{
    labels: Object.keys(carrierCounts),
    datasets: [{{ data: Object.values(carrierCounts), backgroundColor: COLORS }}]
  }}, {{ plugins: {{ legend: {{ position: 'bottom' }} }} }});

  makeChart('cAmount', 'bar', {{
    labels: Object.keys(carrierAmounts),
    datasets: [{{ label: 'Total $', data: Object.values(carrierAmounts).map(v => Math.round(v)), backgroundColor: COLORS }}]
  }}, {{ plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ beginAtZero: true }} }} }});

  // Origins
  const originCounts = {{}};
  filtered.forEach(r => {{ if (r.origin) originCounts[r.origin] = (originCounts[r.origin] || 0) + 1; }});
  const topOrigins = Object.entries(originCounts).sort((a, b) => b[1] - a[1]).slice(0, 15);
  makeChart('cOrigin', 'bar', {{
    labels: topOrigins.map(x => x[0]),
    datasets: [{{ label: 'Invoices', data: topOrigins.map(x => x[1]), backgroundColor: '#3b82f6' }}]
  }}, {{ indexAxis: 'y', plugins: {{ legend: {{ display: false }} }} }});

  // Destinations
  const destCounts = {{}};
  filtered.forEach(r => {{ if (r.destination) destCounts[r.destination] = (destCounts[r.destination] || 0) + 1; }});
  const topDests = Object.entries(destCounts).sort((a, b) => b[1] - a[1]).slice(0, 15);
  makeChart('cDest', 'bar', {{
    labels: topDests.map(x => x[0]),
    datasets: [{{ label: 'Invoices', data: topDests.map(x => x[1]), backgroundColor: '#10b981' }}]
  }}, {{ indexAxis: 'y', plugins: {{ legend: {{ display: false }} }} }});

  // Weight distribution
  const wRanges = {{'0-200': 0, '201-500': 0, '501-1000': 0, '1001-3000': 0, '3001-5000': 0, '5000+': 0}};
  filtered.forEach(r => {{
    if (r.weight <= 0) return;
    if (r.weight <= 200) wRanges['0-200']++;
    else if (r.weight <= 500) wRanges['201-500']++;
    else if (r.weight <= 1000) wRanges['501-1000']++;
    else if (r.weight <= 3000) wRanges['1001-3000']++;
    else if (r.weight <= 5000) wRanges['3001-5000']++;
    else wRanges['5000+']++;
  }});
  makeChart('cWeight', 'bar', {{
    labels: Object.keys(wRanges),
    datasets: [{{ label: 'Invoices', data: Object.values(wRanges), backgroundColor: '#8b5cf6' }}]
  }}, {{ plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ beginAtZero: true }} }} }});

  // Timeline
  const dateAmounts = {{}};
  filtered.forEach(r => {{ if (r.date) dateAmounts[r.date] = (dateAmounts[r.date] || 0) + r.due_amount; }});
  const sortedDates = Object.keys(dateAmounts).sort((a, b) => {{
    const pa = a.split('/'); const pb = b.split('/');
    const da = new Date(2000 + parseInt(pa[2] || '25'), parseInt(pa[0]) - 1, parseInt(pa[1]));
    const db = new Date(2000 + parseInt(pb[2] || '25'), parseInt(pb[0]) - 1, parseInt(pb[1]));
    return da - db;
  }}).slice(-30);
  makeChart('cTimeline', 'line', {{
    labels: sortedDates,
    datasets: [{{
      label: 'Amount $',
      data: sortedDates.map(d => Math.round(dateAmounts[d])),
      borderColor: '#3b82f6',
      backgroundColor: 'rgba(59,130,246,0.1)',
      fill: true, tension: 0.3
    }}]
  }}, {{ scales: {{ x: {{ ticks: {{ maxRotation: 45 }} }} }} }});

  // Top 10 table
  const top10 = [...filtered].sort((a, b) => b.due_amount - a.due_amount).slice(0, 10);
  const tbody = document.getElementById('topTable');
  tbody.innerHTML = top10.map(r => `
    <tr>
      <td>${{r.filename.substring(0, 40)}}</td>
      <td><span class="badge badge-${{r.carrier}}">${{r.carrier}}</span></td>
      <td>${{r.due_amount.toLocaleString('en', {{style:'currency', currency:'USD'}})}}</td>
      <td>${{r.origin}}</td>
      <td>${{r.destination}}</td>
      <td>${{r.weight ? Math.round(r.weight) + ' lbs' : '-'}}</td>
    </tr>
  `).join('');
}}

// Initial render
updateDashboard();
</script>
</body>
</html>"""

    with open(HTML_OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nDashboard generated: {HTML_OUT}")
    print("Open it in your browser. Filters update all charts in real time.")


if __name__ == "__main__":
    main()
