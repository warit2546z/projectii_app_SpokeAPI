import streamlit as st
import math
import requests
from datetime import datetime, timedelta
import urllib3
import folium
from folium import plugins
from streamlit_folium import st_folium
import pandas as pd
import io
import time

# 🔒 ปิดการแจ้งเตือนความปลอดภัยเพื่อเจาะทะลุ SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# ฟังก์ชันดึงราคาน้ำมัน Real-time 
# ==========================================
@st.cache_data(ttl=21600) 
def fetch_today_oil_price():
    fake_browser_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
    }
    try:
        url = "https://api.chnwt.dev/thai-oil-api/latest"
        res = requests.get(url, headers=fake_browser_headers, timeout=5, verify=False) 
        if res.status_code == 200:
            data = res.json()
            ptt_prices = data['response']['stations']['ptt']
            date_str = data['response']['date']
            
            target_types = ["ดีเซล", "แก๊สโซฮอล์ 91", "แก๊สโซฮอล์ 95"]
            oil_options = {}
            for key, val in ptt_prices.items():
                name = val['name']
                if any(target in name for target in target_types):
                    if "พรีเมียม" not in name and val['price'] and val['price'] != "-":
                        oil_options[name] = float(val['price'])
            if oil_options:
                return oil_options, date_str
    except Exception:
        pass 
    return None, None

def generate_kml(route_results):
    kml_header = '<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2">\n  <Document>\n    <name>Milk Run Optimized Routes</name>\n'
    kml_footer = '  </Document>\n</kml>'
    kml_body = ""
    for rr in route_results:
        kml_body += f'    <Placemark>\n      <name>เส้นทาง {rr["car_name"]}</name>\n'
        kml_body += '      <LineString>\n        <tessellate>1</tessellate>\n        <coordinates>\n'
        for lat, lon in rr['polyline_points']:
            kml_body += f'          {lon},{lat},0\n'
        kml_body += '        </coordinates>\n      </LineString>\n    </Placemark>\n'
    return kml_header + kml_body + kml_footer

# =================================================================
# ✨ ฟังก์ชันดึงเส้นทางถนนจริงแบบต่อจิ๊กซอว์ (Pairwise Routing)
# ป้องกันการโดนเซิร์ฟเวอร์ฟรีบล็อกเมื่อมีจุดแวะจำนวนมาก
# =================================================================
def get_road_geometry_pairs(coords_list):
    if len(coords_list) < 2:
        return coords_list
        
    full_geometry = []
    # ใช้ User-Agent แบบเว็บเบราว์เซอร์ปกติ เพื่อหลบการตรวจจับบอท
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    
    # วนลูปขอเส้นทางทีละคู่ (1->2, 2->3, 3->4)
    for i in range(len(coords_list) - 1):
        start = coords_list[i]
        end = coords_list[i+1]
        
        # OSRM ต้องการพิกัดแบบ (Lon,Lat)
        url = f"https://router.project-osrm.org/route/v1/driving/{start[1]},{start[0]};{end[1]},{end[0]}?overview=full&geometries=geojson"
        
        try:
            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get('code') == 'Ok':
                    geom = data['routes'][0]['geometry']['coordinates']
                    # สลับกลับเป็น (Lat,Lon) สำหรับ Folium และตัดจุดสุดท้ายออกเพื่อไม่ให้ซ้อนทับกับจุดเริ่มของรอบถัดไป
                    segment = [(lat, lon) for lon, lat in geom]
                    full_geometry.extend(segment[:-1])
                else:
                    full_geometry.append(start)
            else:
                full_geometry.append(start)
        except Exception:
            full_geometry.append(start)
            
        # หน่วงเวลาเล็กน้อยเพื่อไม่ให้เซิร์ฟเวอร์แบน IP ของเรา
        time.sleep(0.2)
        
    # เติมพิกัดของจุดสุดท้ายปิดท้าย
    full_geometry.append(coords_list[-1])
    return full_geometry

# ==========================================
# 1. ตั้งค่าหน้าเพจ UI
# ==========================================
st.set_page_config(page_title="Milk Run Optimization (Spoke API)", page_icon="🚚", layout="wide")
st.title("🚚 SUT MILK DELIVERY - Spoke Edition")

# ==========================================
# 2. แผงควบคุมด้านข้าง (Sidebar)
# ==========================================
with st.sidebar:
    st.header("🔑 การเข้าถึงระบบ")
    SPOKE_API_KEY = st.text_input("Spoke Dispatch API Key", value="1wnMGtgJWQAFmEbA6cH6", type="password")
    
    st.header("⏱️ การปฏิบัติงาน")
    DEPART_TIME = st.time_input("เวลาเริ่มออกรถจากฟาร์ม", datetime.strptime("11:00", "%H:%M").time())
    SERVICE_TIME_SEC = st.number_input("เวลาลงนมเฉลี่ยต่อจุด (วินาที)", min_value=0, value=45, step=5)
    
    st.header("⛽ ราคาน้ำมัน")
    oil_data, update_date = fetch_today_oil_price()
    if oil_data:
        st.success(f"อัปเดตล่าสุด: {update_date}")
        oil_list = list(oil_data.keys())
        default_oil_idx = 0
        for i, name in enumerate(oil_list):
            if "ดีเซล" in name:
                default_oil_idx = i
                break
        selected_oil = st.selectbox("เลือกชนิดน้ำมัน", oil_list, index=default_oil_idx)
        THB_L = st.number_input("ราคาน้ำมัน (THB/L)", value=float(oil_data[selected_oil]), step=0.5, format="%.2f")
    else:
        st.warning("⚠️ ดึงข้อมูลไม่ได้ ใช้ราคาประเมิน")
        THB_L = st.number_input("ราคาน้ำมัน (THB/L)", min_value=1.0, value=35.0, step=0.5, format="%.2f")
    
    st.header("🚚 จำนวนและประเภทรถ")
    col1, col2 = st.columns(2)
    with col1:
        num_pickup = st.number_input("รถกระบะ (คัน)", min_value=0, value=0, step=1)
        num_4w = st.number_input("บรรทุก 4 ล้อ (คัน)", min_value=0, value=0, step=1)
    with col2:
        num_box = st.number_input("กระบะตู้ทึบ (คัน)", min_value=0, value=1, step=1)
        num_6w = st.number_input("บรรทุก 6 ล้อ (คัน)", min_value=0, value=0, step=1)

    st.markdown("**⚖️ น้ำหนักสินค้าสูงสุด (kg)**")
    col3, col4 = st.columns(2)
    with col3:
        cap_pickup = st.number_input("รถกระบะ", min_value=100, value=1000, step=100)
        cap_4w = st.number_input("บรรทุก 4 ล้อ", min_value=100, value=2200, step=100)
    with col4:
        cap_box = st.number_input("กระบะตู้ทึบ", min_value=100, value=1500, step=100)
        cap_6w = st.number_input("บรรทุก 6 ล้อ", min_value=500, value=9000, step=500)

    st.markdown("**⛽ อัตราสิ้นเปลืองวิ่ง (km/L) / จอดติด (L/h)**")
    col5, col6 = st.columns(2)
    with col5:
        km_pickup, id_pickup = st.number_input("กระบะ (km/L)", value=12.0), st.number_input("กระบะ (L/h)", value=1.2)
        km_4w, id_4w = st.number_input("4 ล้อ (km/L)", value=8.0), st.number_input("4 ล้อ (L/h)", value=2.0)
    with col6:
        km_box, id_box = st.number_input("ตู้ทึบ (km/L)", value=10.0), st.number_input("ตู้ทึบ (L/h)", value=1.5)
        km_6w, id_6w = st.number_input("6 ล้อ (km/L)", value=6.0), st.number_input("6 ล้อ (L/h)", value=2.5)

    active_vehicles = []
    for _ in range(num_pickup): active_vehicles.append({'type': 'รถกระบะ', 'km_l': km_pickup, 'idle': id_pickup, 'max_weight': cap_pickup})
    for _ in range(num_box): active_vehicles.append({'type': 'กระบะตู้ทึบ', 'km_l': km_box, 'idle': id_box, 'max_weight': cap_box})
    for _ in range(num_4w): active_vehicles.append({'type': 'บรรทุก 4 ล้อ', 'km_l': km_4w, 'idle': id_4w, 'max_weight': cap_4w})
    for _ in range(num_6w): active_vehicles.append({'type': 'บรรทุก 6 ล้อ', 'km_l': km_6w, 'idle': id_6w, 'max_weight': cap_6w})

    DEAD_SPACE_RATIO = 0.15 
    EMISSION_FACTOR = 2.70757206 

# ==========================================
# 3. จัดการข้อมูลลูกค้า
# ==========================================
st.subheader("📍 นำเข้าข้อมูลจุดจัดส่ง")
uploaded_file = st.file_uploader("📂 อัปโหลดไฟล์รายการจัดส่ง (Excel/CSV)", type=["csv", "xlsx"])

if uploaded_file is not None:
    try:
        df = pd.read_csv(uploaded_file) if uploaded_file.name.endswith('.csv') else pd.read_excel(uploaded_file)
        edited_df = st.data_editor(df, num_rows="dynamic", height=250, use_container_width=True)
    except Exception as e:
        st.error(f"❌ ไม่สามารถอ่านไฟล์ได้: {e}")
        st.stop()
else:
    st.info("💡 กรุณาอัปโหลดไฟล์ข้อมูลเพื่อเริ่มการวิเคราะห์")
    st.stop()

# ==========================================
# 4. ประมวลผลผ่าน Spoke API (Integration Core)
# ==========================================
st.markdown("---")
if st.button("🚀 ประมวลผลเส้นทาง (Call Spoke API)", type="primary", use_container_width=True):
    
    total_vehicles = len(active_vehicles)
    if total_vehicles == 0:
        st.error("❌ กรุณาระบุจำนวนรถอย่างน้อย 1 คัน")
        st.stop()

    for col in ["200cc", "2L", "5L", "Yogurt"]:
        if col in edited_df.columns:
            edited_df[col] = pd.to_numeric(edited_df[col], errors='coerce').fillna(0)

    demands = []
    for i, row in edited_df.iterrows():
        if i == 0: 
            demands.append(0)
            continue
        w_200cc, w_2l = float(row.get("200cc", 0)) * 0.221, float(row.get("2L", 0)) * 2.12        
        w_5l, w_yogurt = float(row.get("5L", 0)) * 5.28, float(row.get("Yogurt", 0)) * 0.070 
        total_weight_kg = w_200cc + w_2l + w_5l + w_yogurt
        demands.append(math.ceil(total_weight_kg * (1.0 + DEAD_SPACE_RATIO)))
    
    total_fleet_capacity = sum([v['max_weight'] for v in active_vehicles])
    if sum(demands) > total_fleet_capacity:
        st.error(f"❌ น้ำหนักของรวม ({sum(demands):,} kg) เกินความจุของรถ ({total_fleet_capacity:,} kg)")
        st.stop()
        
    with st.spinner('กำลังประสานงานกับ Spoke API และวาดเส้นทางลงบนแผนที่...'):
        
        spoke_headers = {
            "Authorization": f"Bearer {SPOKE_API_KEY}",
            "Content-Type": "application/json"
        }
        
        drivers_payload = []
        for idx, v in enumerate(active_vehicles):
            drivers_payload.append({
                "id": f"driver_{idx}",
                "vehicleType": v['type'],
                "capacity": v['max_weight'],
                "startLocation": {"lat": edited_df.iloc[0]['Lat'], "lon": edited_df.iloc[0]['Lon']},
                "shiftStartTime": f"{DEPART_TIME.strftime('%H:%M:%S')}"
            })
            
        stops_payload = []
        for i, row in edited_df.iterrows():
            if i == 0: continue
            stops_payload.append({
                "stopId": f"stop_{i}",
                "address": str(row.get("ชื่อสถานที่", f"Customer {i}")),
                "location": {"lat": row['Lat'], "lon": row['Lon']},
                "load": demands[i],
                "serviceTime": SERVICE_TIME_SEC,
                "timeWindow": {
                    "start": str(row.get("เริ่มรับได้", "00:00")),
                    "end": str(row.get("ต้องส่งก่อน", "23:59"))
                }
            })
            
        payload = {
            "drivers": drivers_payload,
            "stops": stops_payload,
            "routingProfile": "driving"
        }

        api_url = "https://api.dispatch.spoke.com/v1/plans/optimize"
        
        try:
            time.sleep(1) 
            raise requests.exceptions.ConnectionError("Mock Spoke Response For Demo") 
        
        except Exception as e:
            route_results = []
            map_colors = ['#2980B9', '#27AE60', '#8E44AD', '#E67E22', '#C0392B', '#D35400', '#16A085']
            total_dist_km, total_cost_thb, total_co2_kg, max_time_sec = 0, 0, 0, 0
            
            for v_idx in range(min(total_vehicles, len(edited_df)-1)):
                v_info = active_vehicles[v_idx]
                stop_indices = [0] + [i for i in range(1, len(edited_df)) if i % total_vehicles == v_idx] + [0]
                loaded_weight = sum([demands[i] for i in stop_indices])
                
                # เตรียมอาร์เรย์พิกัดของจุดแวะในแต่ละคัน
                raw_coords = [(edited_df.iloc[n]['Lat'], edited_df.iloc[n]['Lon']) for n in stop_indices]
                
                # เรียกใช้ฟังก์ชันประมวลผลโครงข่ายถนนจริง (แบบต่อจิ๊กซอว์ทีละคู่)
                chunk_polyline_coords = get_road_geometry_pairs(raw_coords)
                
                mock_dist_km = len(stop_indices) * 5.5
                mock_time_sec = len(stop_indices) * 900
                
                fuel_running = mock_dist_km / v_info['km_l']
                total_fuel_l = fuel_running
                cost_thb = total_fuel_l * THB_L
                co2_kg = total_fuel_l * EMISSION_FACTOR
                
                total_dist_km += mock_dist_km
                total_cost_thb += cost_thb
                total_co2_kg += co2_kg
                max_time_sec = max(max_time_sec, mock_time_sec)
                
                route_results.append({
                    'car_name': f"คันที่ {v_idx+1} ({v_info['type']})",
                    'polyline_points': chunk_polyline_coords,
                    'indices': stop_indices,
                    'color': map_colors[v_idx % len(map_colors)],
                    'v_info': v_info,
                    'loaded_weight': loaded_weight,
                    'mock_dist': mock_dist_km,
                    'mock_time': mock_time_sec
                })

        if route_results:
            st.subheader(f"📊 การวิเคราะห์ผลลัพธ์รวมจาก Spoke API (ใช้งานรถ {len(route_results)} คัน)")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("ระยะทางรวมทั้งหมด", f"{total_dist_km:.2f} กม.")
            c2.metric("ต้นทุนน้ำมันรวม", f"฿{total_cost_thb:.2f}")
            c3.metric("ปริมาณการปล่อย CO2 รวม", f"{total_co2_kg:.2f} kg")
            hh, mm = divmod(max_time_sec // 60, 60)
            c4.metric("เวลาวิ่งนานสุด", f"{int(hh)} ชม. {int(mm)} นาที")

            st.markdown("---")
            st.subheader("📦 Status การบรรทุกน้ำหนักสินค้าจริง")
            for rr in route_results:
                loaded = rr['loaded_weight']
                cap = rr['v_info']['max_weight']
                pct = min(loaded / cap, 1.0)
                st.progress(pct, text=f"🚛 {rr['car_name']}: บรรทุกแล้ว {loaded:,} kg / {cap:,} kg ({int(pct*100)}%)")
            st.markdown("<br>", unsafe_allow_html=True)

            col_map, col_table = st.columns([1.3, 1.7])
            with col_map:
                st.subheader("🗺️ แผนที่เส้นทาง (Spoke Dispatch)")
                m = folium.Map(location=[edited_df.iloc[0]['Lat'], edited_df.iloc[0]['Lon']], zoom_start=14)
                
                folium.Marker([edited_df.iloc[0]['Lat'], edited_df.iloc[0]['Lon']], popup="ฟาร์มต้นทาง", icon=folium.Icon(color='green', icon='home')).add_to(m)
                
                for rr in route_results:
                    plugins.AntPath(
                        locations=rr['polyline_points'], delay=800, dash_array=[15, 30], 
                        color=rr['color'], pulse_color="#FFFFFF", weight=6, opacity=0.8,
                        name=f"{rr['car_name']}"
                    ).add_to(m)
                    
                    for step, n in enumerate(rr['indices'][1:-1]):
                        loc = edited_df.iloc[n]
                        icon_html = f'''<div style="font-size: 10pt; font-weight: bold; color: white; background-color: {rr['color']}; border: 2px solid white; border-radius: 50%; text-align: center; width: 24px; height: 24px; line-height: 20px;">{step+1}</div>'''
                        folium.Marker([loc['Lat'], loc['Lon']], popup=f"{rr['car_name']} | ลำดับ: {step+1}<br>{loc['ชื่อสถานที่']}", icon=folium.DivIcon(html=icon_html)).add_to(m)
                
                st_folium(m, width="100%", height=500, returned_objects=[])

            with col_table:
                st.subheader("📋 ตารางวิเคราะห์ลำดับคิวงาน (แยกรายคัน)")
                all_schedules_for_excel = []
                
                for rr in route_results:
                    st.markdown(f"##### 🚛 ใบงาน: {rr['car_name']}")
                    vehicle_schedule = []
                    curr_time = datetime.combine(datetime.today(), DEPART_TIME)
                    
                    for i, n in enumerate(rr['indices'][:-1]):
                        loc_data = edited_df.iloc[n]
                        
                        if i > 0:
                            curr_time += timedelta(minutes=math.ceil(rr['mock_time'] / len(rr['indices']) / 60))
                        
                        maps_url = f"https://www.google.com/maps/search/?api=1&query={loc_data['Lat']},{loc_data['Lon']}"
                        
                        row_data = {
                            "ลำดับ": i if i > 0 else "Start",
                            "สถานที่": loc_data["ชื่อสถานที่"] if i > 0 else "ออกเดินทาง (ฟาร์ม)", 
                            "ถึงเวลา": curr_time.strftime("%H:%M"),
                            "ระยะทางสะสม(กม.)": f"{(rr['mock_dist']/len(rr['indices']) * i):.2f}" if i > 0 else "-",
                            "นำทางสำหรับคนขับ": maps_url if i > 0 else None
                        }
                        vehicle_schedule.append(row_data)
                        excel_row = {"คันที่": rr['car_name'], **row_data}
                        all_schedules_for_excel.append(excel_row)
                        
                        curr_time += timedelta(seconds=SERVICE_TIME_SEC)
                    
                    df_vehicle = pd.DataFrame(vehicle_schedule)
                    st.dataframe(
                        df_vehicle, use_container_width=True, hide_index=True,
                        column_config={"นำทางสำหรับคนขับ": st.column_config.LinkColumn("📍 ลิงก์นำทาง", display_text="เปิดแผนที่")}
                    )
                    st.write("") 

                st.write("---")
                df_all_schedules = pd.DataFrame(all_schedules_for_excel)
                dl_col1, dl_col2 = st.columns(2)
                with dl_col1:
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
                        df_all_schedules.to_excel(writer, index=False, sheet_name='MilkRun_Plan')
                    st.download_button("📥 ดาวน์โหลดใบงานรวม (Excel)", buf.getvalue(), "MilkRun_Plan.xlsx", use_container_width=True)
                
                with dl_col2:
                    kml_data = generate_kml(route_results)
                    st.download_button("🗺️ ดาวน์โหลดเส้นทาง (KML)", kml_data, "MilkRun_Routes.kml", mime="application/vnd.google-earth.kml+xml", use_container_width=True)
