from flask import Flask, jsonify, render_template_string, send_file
from pathlib import Path
from datetime import datetime
import threading
import json
import urllib.request
import urllib.parse
import paho.mqtt.client as mqtt

# =========================================================
# 1. CONFIGURATION
# =========================================================

MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC = "busstops/university_moratuwa/passengers"

PROCESSED_DIR = Path.home() / "processed_images"

app = Flask(__name__)

# =========================================================
# 2. BUS HALT INFORMATION
# Coordinates are approximate town-centre locations used for
# locality-level online weather, not physical sensor readings.
# =========================================================

BUS_STOPS = {
    "piliyandala": {
        "name": "Piliyandala",
        "latitude": 6.8010,
        "longitude": 79.9220,
        "active": False
    },
    "university-moratuwa": {
        "name": "University of Moratuwa",
        "latitude": 6.7960,
        "longitude": 79.9010,
        "active": True
    },
    "katubedda": {
        "name": "Katubedda",
        "latitude": 6.7970,
        "longitude": 79.9000,
        "active": False
    },
    "galkissa": {
        "name": "Galkissa",
        "latitude": 6.8400,
        "longitude": 79.8630,
        "active": False
    },
    "bambalapitiya": {
        "name": "Bambalapitiya",
        "latitude": 6.8960,
        "longitude": 79.8560,
        "active": False
    },
    "kollupitiya": {
        "name": "Kollupitiya",
        "latitude": 6.9140,
        "longitude": 79.8510,
        "active": False
    },
    "colombo": {
        "name": "Colombo",
        "latitude": 6.9270,
        "longitude": 79.8610,
        "active": False
    }
}

# =========================================================
# 3. SHARED LIVE DATA
# =========================================================

data_lock = threading.Lock()
latest_data = None
mqtt_connected = False
passenger_history = []
weather_cache = {}
weather_lock = threading.Lock()
WEATHER_CACHE_SECONDS = 600  # 10 minutes


# =========================================================
# 4. MQTT SUBSCRIBER
# =========================================================

def on_connect(client, userdata, flags, reason_code, properties):
    global mqtt_connected

    if reason_code == 0:
        mqtt_connected = True
        print("Dashboard MQTT connected!")
        client.subscribe(MQTT_TOPIC, qos=1)
        print("Subscribed to:", MQTT_TOPIC)
    else:
        mqtt_connected = False
        print("MQTT connection failed:", reason_code)


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    global mqtt_connected
    mqtt_connected = False
    print("MQTT disconnected:", reason_code)


def on_message(client, userdata, message):
    global latest_data

    try:
        received_data = json.loads(message.payload.decode("utf-8"))

        # Ignore unexpected messages that do not contain the count.
        if "passenger_count" not in received_data:
            return

        with data_lock:
            latest_data = received_data
            passenger_history.append({
                "passenger_count": int(received_data["passenger_count"]),
                "timestamp": received_data.get("timestamp", "")
            })

            # Keep the latest 100 readings in dashboard memory.
            if len(passenger_history) > 100:
                passenger_history.pop(0)

        print("\nNEW DASHBOARD DATA RECEIVED")
        print("Bus halt:", received_data.get("bus_halt"))
        print("Passengers:", received_data.get("passenger_count"))
        print("Time:", received_data.get("timestamp"))

    except Exception as error:
        print("MQTT message error:", error)


mqtt_client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="university_moratuwa_dashboard"
)
mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect
mqtt_client.on_message = on_message
mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)


# =========================================================
# 5. WEATHER SUPPORT (OPEN-METEO)
# =========================================================

def weather_description(code):
    descriptions = {
        0: "Clear sky",
        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Fog",
        48: "Depositing rime fog",
        51: "Light drizzle",
        53: "Moderate drizzle",
        55: "Dense drizzle",
        61: "Slight rain",
        63: "Moderate rain",
        65: "Heavy rain",
        71: "Slight snow",
        73: "Moderate snow",
        75: "Heavy snow",
        80: "Rain showers",
        81: "Moderate rain showers",
        82: "Violent rain showers",
        95: "Thunderstorm",
        96: "Thunderstorm with hail",
        99: "Heavy thunderstorm"
    }
    try:
        return descriptions.get(int(code), "Weather unavailable")
    except (TypeError, ValueError):
        return "Weather unavailable"


def get_weather(slug, stop):
    now = datetime.now().timestamp()

    with weather_lock:
        cached = weather_cache.get(slug)
        if cached and now - cached["cached_at"] < WEATHER_CACHE_SECONDS:
            return cached["data"]

    params = urllib.parse.urlencode({
        "latitude": stop["latitude"],
        "longitude": stop["longitude"],
        "current": (
            "temperature_2m,relative_humidity_2m,apparent_temperature,"
            "precipitation,weather_code,wind_speed_10m"
        ),
        "timezone": "Asia/Colombo"
    })
    url = "https://api.open-meteo.com/v1/forecast?" + params

    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "SmartBusHaltMonitoring/1.0"}
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))

        current = result.get("current", {})
        weather = {
            "available": True,
            "temperature": current.get("temperature_2m"),
            "feels_like": current.get("apparent_temperature"),
            "humidity": current.get("relative_humidity_2m"),
            "precipitation": current.get("precipitation"),
            "wind_speed": current.get("wind_speed_10m"),
            "condition": weather_description(current.get("weather_code")),
            "observed_at": current.get("time", ""),
            "source": "Open-Meteo online weather"
        }

        with weather_lock:
            weather_cache[slug] = {
                "cached_at": now,
                "data": weather
            }
        return weather

    except Exception as error:
        print("Weather request failed for", stop["name"], ":", error)
        return {
            "available": False,
            "condition": "Weather temporarily unavailable",
            "source": "Open-Meteo online weather"
        }


def get_stop_payload(slug):
    stop = BUS_STOPS[slug]
    weather = get_weather(slug, stop)

    with data_lock:
        # Only University of Moratuwa currently has actual MQTT data.
        if slug == "university-moratuwa":
            current_data = dict(latest_data) if latest_data else None
            history = list(passenger_history)
        else:
            current_data = None
            history = []

        mqtt_status = mqtt_connected

    return {
        "slug": slug,
        "name": stop["name"],
        "active": stop["active"],
        "latest": current_data,
        "history": history,
        "weather": weather,
        "mqtt_connected": mqtt_status if stop["active"] else False
    }


# =========================================================
# 6. WEBSITE HTML
# =========================================================

HTML_PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Smart Bus Halt Monitoring</title>
<style>
:root {
    --navy: #10213c;
    --blue: #2563eb;
    --green: #059669;
    --muted: #64748b;
    --background: #f1f5f9;
    --border: #e2e8f0;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: Arial, sans-serif;
    background: var(--background);
    color: #172033;
}
header {
    background: var(--navy);
    color: white;
    padding: 23px 5%;
}
.header-content {
    max-width: 1250px;
    margin: auto;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
}
header h1 { font-size: 25px; margin-bottom: 7px; }
header p { color: #cbd5e1; font-size: 13px; }
.clock { text-align: right; font-size: 13px; line-height: 1.8; }
.container { max-width: 1250px; margin: 28px auto; padding: 0 20px; }
.section-title { margin: 22px 0 16px; }
.section-title h2 { font-size: 21px; margin-bottom: 6px; }
.section-title p { color: var(--muted); font-size: 13px; }
.summary {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
    margin: 20px 0 28px;
}
.summary-card, .panel, .bus-card {
    background: white;
    border: 1px solid var(--border);
    border-radius: 14px;
    box-shadow: 0 3px 12px #0f172a0b;
}
.summary-card { padding: 20px; }
.summary-card p { color: var(--muted); font-size: 13px; margin-bottom: 10px; }
.summary-card h3 { font-size: 28px; }
.green { color: var(--green); }
.blue { color: var(--blue); }
.bus-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 17px;
}
.bus-card {
    display: block;
    color: inherit;
    text-decoration: none;
    padding: 20px;
    transition: transform .15s, box-shadow .15s;
}
.bus-card:hover {
    transform: translateY(-3px);
    box-shadow: 0 8px 22px #0f172a14;
}
.bus-card.active { border: 2px solid #10b981; cursor: pointer; }
.bus-card.inactive { opacity: .88; }
.bus-top { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; }
.bus-card h3 { font-size: 17px; margin-bottom: 10px; }
.status {
    display: inline-block; padding: 5px 9px; border-radius: 20px;
    font-size: 10px; font-weight: bold; white-space: nowrap;
    background: #dcfce7; color: #15803d;
}
.status.inactive { background: #e2e8f0; color: #64748b; }
.passenger-number { font-size: 38px; font-weight: bold; margin: 13px 0 3px; color: var(--green); }
.card-note { color: var(--muted); font-size: 12px; }
.weather-line { margin-top: 15px; padding-top: 13px; border-top: 1px solid #edf0f4; }
.weather-temp { font-size: 21px; font-weight: bold; }
.weather-desc { color: var(--muted); font-size: 12px; margin-top: 4px; }
.weather-small { color: var(--muted); font-size: 11px; margin-top: 6px; }
.last-updated { color: var(--muted); font-size: 11px; margin-top: 12px; }
.click-note { color: var(--blue); font-size: 12px; margin-top: 13px; font-weight: bold; }
.back-link {
    display: inline-block; margin-bottom: 18px; color: var(--blue);
    text-decoration: none; font-weight: bold; font-size: 14px;
}
.detail-heading {
    display:flex; justify-content:space-between; align-items:center;
    gap:15px; flex-wrap:wrap; margin-bottom:20px;
}
.detail-heading h2 { font-size: 25px; margin-bottom: 6px; }
.detail-heading p { color: var(--muted); font-size: 13px; }
.detail-grid {
    display:grid; grid-template-columns: repeat(2, minmax(0, 1fr));
    gap:18px; margin:18px 0;
}
.panel { padding:22px; min-width:0; }
.panel h3 { margin-bottom:16px; font-size:17px; }
.metric-grid { display:grid; grid-template-columns:repeat(2,1fr); gap:12px; }
.metric {
    background:#f8fafc; border:1px solid #edf0f4; border-radius:11px;
    padding:17px;
}
.metric-label { color:var(--muted); font-size:12px; margin-bottom:8px; }
.metric-value { font-size:29px; font-weight:bold; }
.metric-sub { color:var(--muted); font-size:11px; margin-top:5px; }
.image-box {
    background:#e2e8f0; border-radius:10px; min-height:230px;
    display:flex; align-items:center; justify-content:center; overflow:hidden;
}
.image-box img { width:100%; height:auto; display:block; }
.placeholder { color:var(--muted); padding:25px; text-align:center; font-size:13px; }
.chart-box { height:260px; }
canvas { width:100%; height:100%; }
.info-line { color:var(--muted); font-size:12px; line-height:1.9; margin-top:12px; }
.connection { display:flex; align-items:center; gap:9px; font-size:13px; margin:12px 0; }
.dot { width:9px; height:9px; border-radius:50%; background:#94a3b8; flex:none; }
.dot.online { background:#10b981; }
.notice {
    background:#eff6ff; border:1px solid #bfdbfe; color:#1e40af;
    padding:14px 16px; border-radius:10px; font-size:13px; line-height:1.6;
    margin:15px 0 20px;
}
footer { text-align:center; padding:25px; color:var(--muted); font-size:12px; }
.hidden { display:none !important; }
@media(max-width:850px) {
    .bus-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
    .detail-grid { grid-template-columns:1fr; }
}
@media(max-width:550px) {
    .summary, .bus-grid, .metric-grid { grid-template-columns:1fr; }
    .clock { text-align:left; }
}
</style>
</head>
<body>
<header>
  <div class="header-content">
    <div>
      <h1>Smart Bus Halt Monitoring</h1>
      <p>IoT-Based Passenger Counting and Weather Monitoring System</p>
    </div>
    <div class="clock">
      <div id="date">Loading date...</div>
      <strong id="clock">Loading time...</strong>
    </div>
  </div>
</header>

<main class="container">
  <section id="overview-page">
    <div class="section-title">
      <h2>Bus Stop Overview</h2>
      <p>Passenger counts and current online weather for each selected location</p>
    </div>

    <div class="summary">
      <div class="summary-card"><p>Total bus stops</p><h3>7</h3></div>
      <div class="summary-card"><p>Active passenger-monitoring locations</p><h3 class="green">1</h3></div>
      <div class="summary-card"><p>University of Moratuwa passenger count</p><h3 class="blue" id="overview-count">--</h3></div>
    </div>

    <div class="bus-grid" id="bus-grid">
      <div class="placeholder">Loading bus stops and weather...</div>
    </div>
    <div class="notice">
      Weather is retrieved from the Open-Meteo online weather service for the approximate area of each bus stop.
      It is not measured by a physical weather sensor at the bus stop. Weather is cached for 10 minutes.
    </div>
  </section>

  <section id="detail-page" class="hidden">
    <a class="back-link" href="/" id="back-link">← Back to all bus stops</a>
    <div class="detail-heading">
      <div>
        <h2 id="detail-name">Bus stop</h2>
        <p id="detail-subtitle">Location monitoring details</p>
      </div>
      <span class="status" id="detail-status">ACTIVE</span>
    </div>

    <div id="coming-soon-message" class="notice hidden">
      Passenger monitoring has not been installed at this bus stop yet. Live passenger counts,
      camera detections and history will appear here when its device is connected.
      The weather section can still display online locality-level weather.
    </div>

    <div class="detail-grid">
      <div class="panel">
        <h3>Passenger Monitoring</h3>
        <div class="metric-grid">
          <div class="metric">
            <div class="metric-label">Current passenger count</div>
            <div class="metric-value green" id="detail-count">--</div>
            <div class="metric-sub" id="detail-count-time">Waiting for data</div>
          </div>
          <div class="metric">
            <div class="metric-label">Monitoring status</div>
            <div class="metric-value" style="font-size:20px" id="detail-live-status">Waiting</div>
            <div class="metric-sub">MQTT passenger updates</div>
          </div>
        </div>
      </div>

      <div class="panel">
        <h3>Current Weather</h3>
        <div class="metric-grid">
          <div class="metric">
            <div class="metric-label">Temperature</div>
            <div class="metric-value" id="detail-temperature">--</div>
            <div class="metric-sub" id="detail-condition">Loading weather...</div>
          </div>
          <div class="metric">
            <div class="metric-label">Humidity</div>
            <div class="metric-value" id="detail-humidity">--</div>
            <div class="metric-sub">Relative humidity</div>
          </div>
        </div>
        <div class="info-line" id="weather-extra"></div>
      </div>

      <div class="panel">
        <h3>Latest Camera Detection</h3>
        <div class="image-box">
          <img id="latest-image" class="hidden" src="" alt="Latest YOLO passenger detection">
          <div class="placeholder" id="image-placeholder">No processed camera image available.</div>
        </div>
        <p class="info-line" id="image-caption">Latest annotated image from Raspberry Pi YOLOv8n processing.</p>
      </div>

      <div class="panel">
        <h3>Passenger Count History</h3>
        <div class="chart-box"><canvas id="history-chart"></canvas></div>
        <p class="info-line" id="history-caption">History is updated when new MQTT readings arrive.</p>
      </div>

      <div class="panel">
        <h3>System Connection Status</h3>
        <div class="connection"><span class="dot" id="mqtt-dot"></span> MQTT dashboard subscription: <strong id="mqtt-status">Checking...</strong></div>
        <div class="connection"><span class="dot" id="data-dot"></span> Latest passenger data: <strong id="data-status">Waiting...</strong></div>
        <p class="info-line">MQTT topic: busstops/university_moratuwa/passengers</p>
        <p class="info-line">The ESP32 camera and Raspberry Pi processing are part of the existing working image-processing pipeline.</p>
      </div>
    </div>
  </section>
</main>

<footer>Smart Bus Halt Monitoring System | IoT Module | University of Moratuwa</footer>

<script>
const stopNames = [
  ["piliyandala", "Piliyandala"],
  ["university-moratuwa", "University of Moratuwa"],
  ["katubedda", "Katubedda"],
  ["galkissa", "Galkissa"],
  ["bambalapitiya", "Bambalapitiya"],
  ["kollupitiya", "Kollupitiya"],
  ["colombo", "Colombo"]
];

let currentSlug = null;
let currentDetailData = null;

function updateClock() {
  const now = new Date();
  document.getElementById("date").textContent =
    now.toLocaleDateString("en-LK", {weekday:"long", year:"numeric", month:"long", day:"numeric"});
  document.getElementById("clock").textContent = now.toLocaleTimeString("en-LK");
}
updateClock();
setInterval(updateClock, 1000);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
  }[ch]));
}

function weatherCardMarkup(weather) {
  if (!weather || !weather.available) {
    return `<div class="weather-line"><div class="weather-temp">--</div>
      <div class="weather-desc">Weather unavailable</div></div>`;
  }
  const temp = weather.temperature == null ? "--" : `${weather.temperature}°C`;
  return `<div class="weather-line">
    <div class="weather-temp">${escapeHtml(temp)}</div>
    <div class="weather-desc">${escapeHtml(weather.condition)}</div>
    <div class="weather-small">Humidity: ${escapeHtml(weather.humidity ?? "--")}%</div>
  </div>`;
}

async function loadOverview() {
  const response = await fetch("/api/overview");
  const data = await response.json();
  const grid = document.getElementById("bus-grid");
  grid.innerHTML = "";

  for (const stop of data.stops) {
    const card = document.createElement("a");
    card.className = "bus-card " + (stop.active ? "active" : "inactive");
    card.href = `/bus-stop/${encodeURIComponent(stop.slug)}`;
    const count = stop.latest ? stop.latest.passenger_count : "--";
    const status = stop.active ? "ACTIVE" : "COMING SOON";
    const updated = stop.latest ? `Last updated: ${escapeHtml(stop.latest.timestamp || "Unknown")}` :
                    (stop.active ? "Waiting for first passenger reading" : "No passenger sensor connected");
    card.innerHTML = `
      <div class="bus-top">
        <h3>${escapeHtml(stop.name)}</h3>
        <span class="status ${stop.active ? "" : "inactive"}">${status}</span>
      </div>
      <div class="passenger-number">${escapeHtml(count)}</div>
      <p class="card-note">Current detected passengers</p>
      ${weatherCardMarkup(stop.weather)}
      <p class="last-updated">${updated}</p>
      <p class="click-note">${stop.active ? "Open monitoring details →" : "View location details →"}</p>`;
    grid.appendChild(card);
  }

  const uom = data.stops.find(stop => stop.slug === "university-moratuwa");
  document.getElementById("overview-count").textContent =
    uom && uom.latest ? uom.latest.passenger_count : "--";
}

function showPageForRoute() {
  const match = window.location.pathname.match(/^\/bus-stop\/([^/]+)\/?$/);
  const overview = document.getElementById("overview-page");
  const detail = document.getElementById("detail-page");

  if (match) {
    currentSlug = decodeURIComponent(match[1]);
    if (!stopNames.some(item => item[0] === currentSlug)) {
      window.location.href = "/";
      return;
    }
    overview.classList.add("hidden");
    detail.classList.remove("hidden");
    loadDetail();
  } else {
    currentSlug = null;
    detail.classList.add("hidden");
    overview.classList.remove("hidden");
    loadOverview();
  }
}

function setDot(dotId, online) {
  const dot = document.getElementById(dotId);
  dot.className = online ? "dot online" : "dot";
}

function drawChart(history) {
  const canvas = document.getElementById("history-chart");
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;

  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * ratio);
  canvas.height = Math.round(rect.height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);

  const width = rect.width, height = rect.height;
  ctx.clearRect(0, 0, width, height);

  if (!history || history.length === 0) {
    ctx.fillStyle = "#64748b";
    ctx.font = "13px Arial";
    ctx.fillText("No passenger history available yet.", 12, 30);
    return;
  }

  const values = history.map(item => Number(item.passenger_count) || 0);
  const maxValue = Math.max(5, ...values);
  const left = 36, right = 12, top = 15, bottom = 28;
  const chartWidth = width - left - right;
  const chartHeight = height - top - bottom;

  ctx.strokeStyle = "#e2e8f0";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = top + chartHeight * i / 4;
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(width-right, y); ctx.stroke();
    ctx.fillStyle = "#64748b"; ctx.font = "10px Arial"; ctx.textAlign = "right";
    ctx.fillText(Math.round(maxValue * (4-i) / 4), left-7, y+4);
  }

  ctx.strokeStyle = "#059669";
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  values.forEach((value, index) => {
    const x = values.length === 1 ? left + chartWidth/2 :
      left + chartWidth * index/(values.length-1);
    const y = top + chartHeight - (value/maxValue)*chartHeight;
    if (index === 0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  });
  ctx.stroke();

  values.forEach((value, index) => {
    const x = values.length === 1 ? left + chartWidth/2 :
      left + chartWidth * index/(values.length-1);
    const y = top + chartHeight - (value/maxValue)*chartHeight;
    ctx.fillStyle = "#059669";
    ctx.beginPath(); ctx.arc(x,y,3.5,0,Math.PI*2); ctx.fill();
  });
}

function updateDetailUI(data) {
  currentDetailData = data;
  document.getElementById("detail-name").textContent = data.name;
  document.getElementById("detail-subtitle").textContent =
    data.active ? "Live passenger monitoring and locality weather" : "Location weather and monitoring availability";

  const status = document.getElementById("detail-status");
  status.textContent = data.active ? "ACTIVE" : "COMING SOON";
  status.className = data.active ? "status" : "status inactive";
  document.getElementById("coming-soon-message").classList.toggle("hidden", data.active);

  if (data.latest) {
    document.getElementById("detail-count").textContent = data.latest.passenger_count;
    document.getElementById("detail-count-time").textContent = "Updated: " + (data.latest.timestamp || "Unknown");
    document.getElementById("detail-live-status").textContent = "Live data";
  } else {
    document.getElementById("detail-count").textContent = "--";
    document.getElementById("detail-count-time").textContent =
      data.active ? "Waiting for first MQTT reading" : "No monitoring device";
    document.getElementById("detail-live-status").textContent =
      data.active ? "Waiting" : "Not installed";
  }

  const weather = data.weather || {};
  document.getElementById("detail-temperature").textContent =
    weather.available && weather.temperature != null ? `${weather.temperature}°C` : "--";
  document.getElementById("detail-condition").textContent =
    weather.condition || "Weather unavailable";
  document.getElementById("detail-humidity").textContent =
    weather.available && weather.humidity != null ? `${weather.humidity}%` : "--";

  const extra = [];
  if (weather.available) {
    if (weather.feels_like != null) extra.push(`Feels like: ${weather.feels_like}°C`);
    if (weather.wind_speed != null) extra.push(`Wind: ${weather.wind_speed} km/h`);
    if (weather.precipitation != null) extra.push(`Precipitation: ${weather.precipitation} mm`);
    if (weather.observed_at) extra.push(`Weather time: ${weather.observed_at}`);
  }
  extra.push("Source: Open-Meteo online weather");
  document.getElementById("weather-extra").textContent = extra.join("  |  ");

  const active = data.active;
  const mqttOK = active && data.mqtt_connected;
  setDot("mqtt-dot", mqttOK);
  document.getElementById("mqtt-status").textContent =
    active ? (mqttOK ? "Connected" : "Disconnected") : "Not configured";
  setDot("data-dot", !!data.latest);
  document.getElementById("data-status").textContent =
    data.latest ? "Received" : (active ? "Waiting for data" : "No device");

  if (active) {
    const image = document.getElementById("latest-image");
    image.onload = () => {
      image.classList.remove("hidden");
      document.getElementById("image-placeholder").classList.add("hidden");
    };
    image.onerror = () => {
      image.classList.add("hidden");
      document.getElementById("image-placeholder").classList.remove("hidden");
    };
    image.src = "/latest-image?t=" + Date.now();
    document.getElementById("image-caption").textContent =
      data.latest ? `Latest passenger detection. MQTT reading: ${data.latest.timestamp || "time unavailable"}` :
      "Waiting for the first processed camera image.";
    document.getElementById("history-caption").textContent =
      `${data.history.length} recent readings held in dashboard memory.`;
    drawChart(data.history);
  } else {
    document.getElementById("latest-image").classList.add("hidden");
    document.getElementById("image-placeholder").classList.remove("hidden");
    document.getElementById("image-placeholder").textContent =
      "Camera monitoring is not yet installed at this bus stop.";
    document.getElementById("image-caption").textContent = "No camera feed is configured for this location.";
    document.getElementById("history-caption").textContent = "History will be available when a passenger-counting device is installed.";
    drawChart([]);
  }
}

async function loadDetail() {
  if (!currentSlug) return;
  try {
    const response = await fetch(`/api/bus-stop/${encodeURIComponent(currentSlug)}`);
    if (!response.ok) throw new Error("Could not load bus stop data");
    updateDetailUI(await response.json());
  } catch (error) {
    document.getElementById("detail-subtitle").textContent = "Unable to retrieve dashboard data.";
    console.error(error);
  }
}

document.getElementById("back-link").addEventListener("click", event => {
  event.preventDefault();
  history.pushState({}, "", "/");
  showPageForRoute();
});
window.addEventListener("popstate", showPageForRoute);
window.addEventListener("resize", () => {
  if (currentDetailData) drawChart(currentDetailData.history || []);
});

showPageForRoute();
setInterval(() => {
  if (currentSlug) loadDetail();
  else loadOverview();
}, 5000);
</script>
</body>
</html>
"""


# =========================================================
# 7. WEBSITE ROUTES
# =========================================================

@app.route("/")
def dashboard():
    return render_template_string(HTML_PAGE)


@app.route("/bus-stop/<slug>")
def bus_stop_page(slug):
    if slug not in BUS_STOPS:
        return "Bus stop not found", 404
    return render_template_string(HTML_PAGE)


@app.route("/api/overview")
def overview_data():
    stops = []
    for slug, stop in BUS_STOPS.items():
        weather = get_weather(slug, stop)
        with data_lock:
            latest = dict(latest_data) if slug == "university-moratuwa" and latest_data else None

        stops.append({
            "slug": slug,
            "name": stop["name"],
            "active": stop["active"],
            "latest": latest,
            "weather": weather
        })

    return jsonify({"stops": stops})


@app.route("/api/bus-stop/<slug>")
def bus_stop_data(slug):
    if slug not in BUS_STOPS:
        return jsonify({"error": "Bus stop not found"}), 404
    return jsonify(get_stop_payload(slug))


@app.route("/latest-image")
def latest_image():
    images = list(PROCESSED_DIR.glob("detected_*.jpg"))
    if not images:
        return "No processed image available yet", 404

    latest = max(images, key=lambda image: image.stat().st_mtime)
    return send_file(latest, mimetype="image/jpeg")


# =========================================================
# 8. START DASHBOARD
# =========================================================

if __name__ == "__main__":
    print("\n====================================")
    print("SMART BUS HALT MONITORING WEBSITE")
    print("====================================")
    print("Connecting to MQTT broker...")

    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()

    print("Dashboard URL: http://<RASPBERRY_PI_IP>:5001")
    try:
        app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
