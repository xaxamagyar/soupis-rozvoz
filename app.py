import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import requests
import io
import time
import os
from urllib.parse import quote
from datetime import datetime, timedelta, time as datetime_time
from streamlit_sortables import sort_items
# OPRAVENO: Importujeme moderní fpdf2 (třída FPDF), která umí UTF-8 a online Linux servery
from fpdf import FPDF
import matplotlib.pyplot as plt

st.set_page_config(page_title="Plánovač tras pro řidiče", layout="wide")
st.title("🚚 Inteligentní plánovač tras (Kompletní stabilizace online)")
st.write("Chyťte a přesuňte řádky s objednávkou myší, upravte poznámky, smažte nepotřebné a vygenerujte přehledné PDF.")

# --- SIDEBAR: NASTAVENÍ ČASŮ A API ---
st.sidebar.header("⚙️ Nastavení výpočtu")

mapy_api_key = st.sidebar.text_input(
    "Mapy.cz REST API klíč", 
    value="3FDgcWrx0FfOCW9IxM7-g1VJYCV-h8Dqv4vkV7wPrD8",
    type="password"
)

start_time = st.sidebar.time_input(
    "Čas výjezdu řidiče ze skladu", 
    datetime_time(8, 0)
)

unload_time_min = st.sidebar.slider(
    "Doba zdržení na zastávce (vykládka v min)", 
    0, 60, 15
)

st.sidebar.markdown("---")
st.sidebar.header("📍 Pojmenování bodů trasy")
start_point_name = st.sidebar.text_input("Název výchozího bodu", value="SKLAD (Výjezd)")
end_point_name = st.sidebar.text_input("Název cílového bodu", value="SKLAD (Návrat)")

st.sidebar.markdown("---")
st.sidebar.header("💰 Pokladna / Finance")
kasac_value = st.sidebar.number_input("Částka do kasáče (Kč)", min_value=0, value=0, step=100, help="Částka, kterou dostal řidič při odjezdu ze skladu.")

# --- FUNKCE PRO ZAOKROUHLOVÁNÍ ČASU NAHORU NA 15 MINUT ---
def round_up_to_15_minutes(dt):
    minutes_to_add = (15 - dt.minute % 15) % 15
    if minutes_to_add == 0 and dt.second == 0:
        return dt
    if minutes_to_add == 0: 
        minutes_to_add = 15
    return dt + timedelta(minutes=minutes_to_add) - timedelta(seconds=dt.second, microseconds=dt.microsecond)


# --- 1. NAHRÁNÍ SOUBORŮ ---
col1, col2 = st.columns(2)
with col1:
    shoptet_files = st.file_uploader("1. Nahrajte Shoptet exporty (můžete vybrat i VÍCE souborů)", type=["xlsx", "csv"], accept_multiple_files=True)
with col2:
    gpx_file = st.file_uploader("2. Nahrajte trasu z Mapy.cz (GPX)", type=["gpx"])


# --- FUNKCE PRO API ---

def geocode_mapy_cz(address_str, api_key):
    if not api_key: return None, None
    url = f"https://api.mapy.cz/v1/geocode?query={quote(address_str)}&apikey={api_key}"
    try:
        r = requests.get(url, timeout=5)
        data = r.json()
        if "items" in data and len(data["items"]) > 0:
            pos = data["items"][0]["position"]
            return float(pos["lat"]), float(pos["lon"])
    except:
        pass
    return None, None

def get_driving_data(lat1, lon1, lat2, lon2, api_key=""):
    if api_key:
        url = f"https://api.mapy.cz/v1/routing/route?start={lon1},{lat1}&end={lon2},{lat2}&routeType=car_fast&apikey={api_key}"
        try:
            r = requests.get(url, timeout=5)
            data = r.json()
            if "length" in data and "duration" in data:
                return float(data["length"] / 1000.0), float(data["duration"] / 60.0)
        except:
            pass 
    
    fallback_dist = geodesic((lat1, lon1), (lat2, lon2)).kilometers
    return float(fallback_dist), float((fallback_dist / 50.0) * 60)


@st.cache_data(show_spinner=False)
def process_initial_data(shoptet_file_list, gpx_bytes, api_key):
    root = ET.fromstring(gpx_bytes)
    namespaces = {'gpx': 'http://www.topografix.com/GPX/1/1'}
    points = root.findall('.//gpx:trkpt', namespaces) or root.findall('.//gpx:wpt', namespaces)
    gpx_list = [(float(p.attrib['lat']), float(p.attrib['lon'])) for p in points]
    
    all_dfs = []
    for file in shoptet_file_list:
        file_bytes = file.getvalue()
        if file.name.lower().endswith('.xlsx'):
            df_shop = pd.read_excel(io.BytesIO(file_bytes))
        else:
            try:
                df_shop = pd.read_csv(io.BytesIO(file_bytes), encoding='utf-8')
            except:
                df_shop = pd.read_csv(io.BytesIO(file_bytes), encoding='cp1250')
        all_dfs.append(df_shop)
        
    df_combined = pd.concat(all_dfs, ignore_index=True)
            
    agg_dict = {
        'deliveryFullName': 'first',
        'deliveryStreetWithHouseNumber': 'first',
        'deliveryCity': 'first',
        'phone': 'first',
        'geisDeliveryPriceToPay': 'first'
    }
    if 'deliveryZip' in df_combined.columns:
        agg_dict['deliveryZip'] = 'first'
        
    df_clean = df_combined.groupby('id').agg(agg_dict).reset_index()

    geolocator = Nominatim(user_agent="shoptet_gpx_planner_fix")
    orders = []
    unmatched_count = 0
    
    for idx, row in df_clean.iterrows():
        street = str(row['deliveryStreetWithHouseNumber']).replace('\n', ' ').replace('\r', '').replace('nan', '').strip()
        city = str(row['deliveryCity']).replace('\n', ' ').replace('\r', '').replace('nan', '').strip()
        zip_code = str(row.get('deliveryZip', '')).replace('\n', ' ').replace('\r', '').replace('nan', '').replace(' ', '').strip()
        
        address_str = f"{street}, {city} {zip_code}, Česká republika".replace(' ,', ',').strip(", ")
        
        lat, lon = None, None
        
        if api_key:
            res_geo = geocode_mapy_cz(address_str, api_key)
            if res_geo:
                lat, lon = res_geo
        
        if lat is None:
            try:
                location = geolocator.geocode(address_str, timeout=5)
                if location: lat, lon = location.latitude, location.longitude
            except:
                pass
        
        is_error = False
        closest_gpx_idx = 0
        if lat is not None and lon is not None:
            final_lat, final_lon = lat, lon
            order_coord = (lat, lon)
            min_dist = float('inf')
            for gpx_idx, gpx_coord in enumerate(gpx_list):
                dist = (order_coord[0] - gpx_coord[0])**2 + (order_coord[1] - gpx_coord[1])**2
                if dist < min_dist:
                    min_dist = dist
                    closest_gpx_idx = gpx_idx
        else:
            final_lat, final_lon = gpx_list[0]
            closest_gpx_idx = 0
            unmatched_count += 1
            is_error = True
            
        item_marker = "⚠️ NENALEZENO:" if is_error else ""
        
        orders.append({
            'Číslo objednávky': str(row['id']),
            'Příjemce': row['deliveryFullName'],
            'Ulice': street,
            'Město': city,
            'PSČ': zip_code,
            'Chyba': item_marker,
            'Telefon': row['phone'],
            'Dobírka (Kč)': row['geisDeliveryPriceToPay'],
            'gpx_index': int(closest_gpx_idx),
            'lat': final_lat,
            'lon': final_lon
        })
        time.sleep(0.05)
        
    if len(orders) == 0:
        return pd.DataFrame(columns=['Číslo objednávky', 'Příjemce', 'Ulice', 'Město', 'PSČ', 'Chyba', 'Telefon', 'Dobírka (Kč)', 'gpx_index', 'lat', 'lon']), gpx_list, 0
        
    df_sorted = pd.DataFrame(orders).sort_values(by='gpx_index').reset_index(drop=True)
    return df_sorted, gpx_list, unmatched_count


# --- HLAVNÍ LOGIKA APLIKACE ---

if shoptet_files and gpx_file:
    if 'last_uploaded_files_count' not in st.session_state or st.session_state['last_uploaded_files_count'] != len(shoptet_files):
        st.cache_data.clear()
        st.session_state['last_uploaded_files_count'] = len(shoptet_files)
        if 'initial_processed_data' in st.session_state:
            del st.session_state['initial_processed_data']

    if 'initial_processed_data' not in st.session_state:
        with st.spinner("Geokóduji adresy ze všech nahraných souborů..."):
            df_initial, gpx_list, unmatched = process_initial_data(
                shoptet_files, gpx_file.getvalue(), mapy_api_key
            )
            st.session_state['initial_processed_data'] = df_initial
            st.session_state['gpx_list'] = gpx_list
            if unmatched > 0:
                st.warning(f"⚠️ Pozor: {unmatched} adres se nepodařilo nalézt na mapě.")
    else:
        df_initial = st.session_state['initial_processed_data']
        gpx_list = st.session_state['gpx_list']

    st.subheader("Krok 1: Příprava rozvozu")
    
    with st.expander("❌ Odstranit (smazat) objednávky z dnešního rozvozu"):
        st.write("Zaškrtněte objednávky, které chcete z dnešní trasy úplně vyřadit:")
        to_delete = []
        for _, row in df_initial.iterrows():
            if st.checkbox(f"Smazat obj. {row['Číslo objednávky']} — {row['Příjemce']} ({row['Město']})", key=f"del_{row['Číslo objednávky']}"):
                to_delete.append(row['Číslo objednávky'])
        
        df_filtered = df_initial[~df_initial['Číslo objednávky'].isin(to_delete)].reset_index(drop=True)

    with st.expander("➕ Ručně přidat novou zastávku (mimo e-shop)"):
        with st.form("manual_add_form"):
            st.write("Vyplňte údaje o nové zastávce. Po přidání se zařadí na konec seznamu.")
            col_add1, col_add2, col_add3 = st.columns(3)
            with col_add1:
                man_name = st.text_input("Příjemce / Jméno")
                man_phone = st.text_input("Telefon")
                man_cod = st.number_input("Dobírka (Kč)", min_value=0.0, value=0.0, step=100.0)
            with col_add2:
                man_street = st.text_input("Ulice a č.p. (*povinné)")
                man_city = st.text_input("Město (*povinné)")
                man_zip = st.text_input("PSČ")
            with col_add3:
                man_id = st.text_input("Číslo zásilky (volitelné)", value=f"RUČNĚ-{int(time.time())}")
            
            submitted = st.form_submit_button("Geokódovat a přidat do seznamu")
            
            if submitted:
                if not man_street or not man_city:
                    st.error("Pro přidání zastávky musíte vyplnit alespoň Ulici a Město.")
                else:
                    with st.spinner("Hledám adresu na mapě..."):
                        addr_str = f"{man_street}, {man_city} {man_zip}, Česká republika".replace(' ,', ',').strip(", ")
                        lat, lon = None, None
                        if mapy_api_key:
                            res = geocode_mapy_cz(addr_str, mapy_api_key)
                            if res: lat, lon = res
                        if lat is None:
                            try:
                                loc = Nominatim(user_agent="shoptet_planner_manual").geocode(addr_str, timeout=3)
                                if loc: lat, lon = loc.latitude, loc.longitude
                            except: pass
                        
                        is_err = False
                        if lat is None or lon is None:
                            lat, lon = gpx_list[0] if gpx_list else (0.0, 0.0)
                            is_err = True
                        
                        new_row = pd.DataFrame([{
                            'Číslo objednávky': man_id,
                            'Příjemce': man_name,
                            'Ulice': man_street,
                            'Město': man_city,
                            'PSČ': man_zip,
                            'Chyba': "⚠️ NENALEZENO:",
                            'Telefon': man_phone,
                            'Dobírka (Kč)': man_cod,
                            'gpx_index': 99999,
                            'lat': lat,
                            'lon': lon
                        }])
                        
                        st.session_state['initial_processed_data'] = pd.concat([st.session_state['initial_processed_data'], new_row], ignore_index=True)
                        st.cache_data.clear()
                        st.rerun()

    tab_sort, tab_notes = st.tabs(["🗺️ Seřadit trasu (Myší)", "📝 Dopsat poznámky řidiči"])
    
    with tab_sort:
        st.info("Chyťte řádek s objednávkou myší a přetáhněte ho nahoru nebo dolů pro změnu pořadí.")
        items_list = []
        mapping_dict = {}
        for _, row in df_filtered.iterrows():
            err_prefix = f"{row['Chyba']} " if row['Chyba'] else ""
            item_str = f"Obj: {row['Číslo objednávky']} | 👤 {row['Příjemce']} | {err_prefix}{row['Ulice']}, {row['Město']} | 💰 {row['Dobírka (Kč)']} Kč"
            items_list.append(item_str)
            mapping_dict[item_str] = row.to_dict()
            
        raw_sortables_output = sort_items(items_list, direction='vertical')
        sorted_strings = raw_sortables_output if raw_sortables_output is not None else items_list

    with tab_notes:
        st.info("Zde můžete k seřazeným objednávkám dopsat vzkaz pro řidiče.")
        order_notes = {}
        for s in sorted_strings:
            order_data = mapping_dict[s]
            order_id = order_data['Číslo objednávky']
            prijemce = order_data['Příjemce']
            
            order_notes[order_id] = st.text_input(f"Poznámka k obj. {order_id} ({prijemce}):", key=f"note_{order_id}", placeholder="Zadejte pokyn řidiči...")

    st.markdown("---")
    
    # --- FÁZE 2: POTVRZENÍ A VÝPOČET ČASŮ ---
    if st.button("🚀 Krok 2: Potvrdit POŘADÍ a vypočítat časy", type="primary") or 'calculated_data' in st.session_state:
        
        if 'calculated_data' not in st.session_state or st.button("Přepočítat s novým nastavením"):
            final_rows = [mapping_dict[s] for s in sorted_strings]
            final_df = pd.DataFrame(final_rows)
            
            final_df['Poznámka'] = final_df['Číslo objednávky'].map(order_notes)
            final_df['Poznámka'] = final_df['Poznámka'].apply(lambda x: "" if pd.isna(x) or str(x).strip().lower() in ['none', 'nan'] else str(x).strip())
            
            itinerary = []
            itinerary.append({
                'Číslo objednávky': 'START',
                'Příjemce': start_point_name,
                'Ulice': 'Výchozí bod trasy',
                'Město': '',
                'PSČ': '',
                'Chyba': '',
                'Telefon': '',
                'Dobírka (Kč)': 0,
                'Poznámka': '',
                'lat': gpx_list[0][0],
                'lon': gpx_list[0][1]
            })
            for _, row in final_df.iterrows():
                itinerary.append(row.to_dict())
                
            itinerary.append({
                'Číslo objednávky': 'CÍL',
                'Příjemce': end_point_name,
                'Ulice': 'Cílový bod trasy',
                'Město': '',
                'PSČ': '',
                'Chyba': '',
                'Telefon': '',
                'Dobírka (Kč)': 0,
                'Poznámka': '',
                'lat': gpx_list[-1][0],
                'lon': gpx_list[-1][1]
            })
            
            df_itinerary = pd.DataFrame(itinerary)
            segments_data = []
            
            with st.spinner("Počítám bezpečně časy z Mapy.cz úsek po úseku..."):
                for i in range(len(df_itinerary) - 1):
                    latA, lonA = df_itinerary.loc[i, 'lat'], df_itinerary.loc[i, 'lon']
                    latB, lonB = df_itinerary.loc[i+1, 'lat'], df_itinerary.loc[i+1, 'lon']
                    res_drive = get_driving_data(latA, lonA, latB, lonB, mapy_api_key)
                    segments_data.append((res_drive[0], res_drive[1]))
                    time.sleep(0.2) 
            
            current_dt = datetime.combine(datetime.today(), start_time)
            
            arrival_times = [current_dt.strftime('%H:%M')]
            arrival_windows = ['-']
            distances_to_next = []
            times_to_next = []
            
            for i in range(len(df_itinerary) - 1):
                dist, dur = segments_data[i]
                distances_to_next.append(round(dist, 1))
                times_to_next.append(int(dur))
                
                arrival_dt = current_dt + timedelta(minutes=int(dur))
                next_idx = i + 1
                
                if df_itinerary.loc[next_idx, 'Číslo objednávky'] == 'CÍL':
                    arrival_times.append(arrival_dt.strftime('%H:%M'))
                    arrival_windows.append('-')
                else:
                    arrival_times.append(arrival_dt.strftime('%H:%M'))
                    window_start_dt = round_up_to_15_minutes(arrival_dt)
                    window_end_dt = window_start_dt + timedelta(hours=2)
                    arrival_windows.append(f"{window_start_dt.strftime('%H:%M')} - {window_end_dt.strftime('%H:%M')}")
                    current_dt = arrival_dt + timedelta(minutes=unload_time_min)
                    
            distances_to_next.append(0.0)
            times_to_next.append(0)

            df_itinerary['Čas příjezdu'] = arrival_times
            df_itinerary['Okno příjezdu (2h)'] = arrival_windows
            df_itinerary['Vzdálen k další (km)'] = distances_to_next
            df_itinerary['Čas k další (min)'] = times_to_next
            
            st.session_state['calculated_data'] = df_itinerary

        df_itinerary = st.session_state['calculated_data']
        
        df_web_display = df_itinerary.copy().astype(str)
        for bad_val in ['none', 'nan', '<na>', 'none.', 'nan.']:
            df_web_display.replace(bad_val, "", inplace=True)
            df_web_display.replace(bad_val.upper(), "", inplace=True)
            df_web_display.replace(bad_val.capitalize(), "", inplace=True)
            
        df_final_display = df_web_display[[
            'Číslo objednávky', 'Příjemce', 'Ulice', 'Město', 
            'Telefon', 'Dobírka (Kč)', 'Čas příjezdu', 'Okno příjezdu (2h)',
            'Vzdálen k další (km)', 'Čas k další (min)', 'Poznámka'
        ]]
        
        df_final_display = df_final_display[df_final_display['Číslo objednávky'] != ""].reset_index(drop=True)
        df_final_display.index.name = 'Č. zast.'
        df_final_display = df_final_display.reset_index()
        
        st.success("🎉 Výpočet dokončen!")
        st.dataframe(df_final_display, use_container_width=True)

        # --- GENERÁTOR MAPY S ORIENTAČNÍMI BODY ---
        def generate_map_image(itinerary_df, gpx_coords):
            fig, ax = plt.subplots(figsize=(8, 6))
            
            min_lat, max_lat = 90, -90
            min_lon, max_lon = 180, -180
            
            if gpx_coords:
                lats, lons = zip(*gpx_coords)
                ax.plot(lons, lats, color='#3498db', linewidth=2.5, label="Trasa (GPX)", zorder=2)
                min_lat, max_lat = min(lats), max(lats)
                min_lon, max_lon = min(lons), max(lons)
            
            major_cities = {
                "Praha": (50.088, 14.420), "Brno": (49.195, 16.606), "Ostrava": (49.820, 18.262),
                "Plzeň": (49.738, 13.373), "Liberec": (50.767, 15.056), "Olomouc": (49.593, 17.250),
                "Č. Budějovice": (48.974, 14.474), "H. Králové": (50.210, 15.825), "Pardubice": (50.034, 15.772),
                "Ústí n. L.": (50.661, 14.032), "Karlovy Vary": (50.231, 12.871), "Jihlava": (49.396, 15.591),
                "Zlín": (49.226, 17.666)
            }

            margin = 0.6
            for city, (c_lat, c_lon) in major_cities.items():
                if (min_lat - margin) < c_lat < (max_lat + margin) and (min_lon - margin) < c_lon < (max_lon + margin):
                    ax.scatter(c_lon, c_lat, color='lightgray', s=40, marker='s', zorder=1)
                    ax.annotate(city, (c_lon, c_lat), textcoords="offset points", xytext=(0,6), ha='center', fontsize=8, color='gray', zorder=1)

            lats_stops = itinerary_df['lat'].tolist()
            lons_stops = itinerary_df['lon'].tolist()
            
            ax.scatter(lons_stops, lats_stops, color='#e74c3c', s=70, zorder=5, edgecolors='black')
            
            for i, row in itinerary_df.iterrows():
                label = f"{i}" if row['Číslo objednávky'] not in ['START', 'CÍL'] else row['Příjemce'][:5]
                ax.annotate(label, (row['lon'], row['lat']), textcoords="offset points", xytext=(0,6), ha='center', fontsize=9, fontweight='bold', color='black', zorder=6)

            ax.axis('off')
            plt.tight_layout()

            img_buf = io.BytesIO()
            plt.savefig(img_buf, format='png', dpi=150, bbox_inches='tight')
            img_buf.seek(0)
            plt.close(fig)
            return img_buf

        # --- GENERÁTOR PDF PŘES FPDF2 ---
        total_km = round(df_itinerary['Vzdálen k další (km)'].sum(), 1)
        pure_drive_min = int(df_itinerary['Čas k další (min)'].sum())
        total_hours = f"{pure_drive_min // 60}h {pure_drive_min % 60}min"
        
        def parse_cod(val):
            try: return float(str(val).replace(' ', '').replace('Kč', ''))
            except: return 0.0
        total_cod = sum(parse_cod(x) for x in df_itinerary['Dobírka (Kč)'])

        # Fonty nahrané na vašem GitHubu
        local_font_reg = "ARIAL.TTF"
        local_font_bold = "ARIALBD.TTF"
        
        if os.path.exists(local_font_reg) and os.path.exists(local_font_bold):
            font_family_name = "ArialCustom"
            use_custom_font = True
        else:
            font_family_name = "Helvetica"
            use_custom_font = False

        class DriverPDF(FPDF):
            def header(self):
                self.set_font(font_family_name, "B", 14)
                heading_text = "TRASOVÝ SOUPIS ŘIDIČE (A4)" if use_custom_font else "TRASOVY SOUPIS RIDICE (A4)"
                self.cell(0, 10, heading_text, ln=True, align="C")
                
                self.set_font(font_family_name, "", 9)
                self.set_text_color(100, 100, 100)
                self.cell(0, 5, f"Vygenerováno: {datetime.now().strftime('%d.%m.%Y %H:%M')} | Start: {start_time.strftime('%H:%M')}", ln=True, align="C")
                self.ln(3)
                self.line(10, self.get_y(), 200, self.get_y())
                self.ln(6)

        pdf = DriverPDF(orientation="P", unit="mm", format="A4")
        
        # FIX PRO FPDF2: Používáme parametr uni=True pro čisté kódování UTF-8 v češtině bez PKL cache souborů
        if use_custom_font:
            pdf.add_font("ArialCustom", "", local_font_reg, uni=True)
            pdf.add_font("ArialCustom", "B", local_font_bold, uni=True)
            
        pdf.add_page()
        
        # --- VLOŽENÍ MAPY DO PDF ---
        df_for_map = df_itinerary[df_itinerary['lat'].notna() & df_itinerary['lon'].notna()].copy()
        df_for_map['lat'] = pd.to_numeric(df_for_map['lat'], errors='coerce')
        df_for_map['lon'] = pd.to_numeric(df_for_map['lon'], errors='coerce')
        df_for_map = df_for_map.dropna(subset=['lat', 'lon']).reset_index(drop=True)

        if not df_for_map.empty:
            map_img = generate_map_image(df_for_map, gpx_list)
            temp_img_path = "temp_map_context.png"
            with open(temp_img_path, "wb") as f:
                f.write(map_img.getbuffer())
            
            pdf.image(temp_img_path, x=10, y=pdf.get_y(), w=190)
            pdf.set_y(pdf.get_y() + 135)
            
            if os.path.exists(temp_img_path):
                os.remove(temp_img_path)

        for idx, row in df_itinerary.iterrows():
            has_note = bool(str(row.get('Poznámka', '')).strip()) and str(row.get('Poznámka', '')).lower() != 'none'
            is_start = row['Číslo objednávky'] == 'START'
            is_end = row['Číslo objednávky'] == 'CÍL'
            
            if is_start or is_end:
                addr = str(row['Ulice'])
            else:
                err_prefix = f"({row['Chyba']}) " if row['Chyba'] else ""
                addr = f"{err_prefix}{row['Ulice']}, {row['Město']} {row['PSČ']}"
            
            if not use_custom_font:
                import unicodedata
                addr = ''.join(c for c in unicodedata.normalize('NFD', addr) if unicodedata.category(c) != 'Mn')
                prijemce_clean = ''.join(c for c in unicodedata.normalize('NFD', str(row['Příjemce'])) if unicodedata.category(c) != 'Mn')
                note_clean = ''.join(c for c in unicodedata.normalize('NFD', str(row.get('Poznámka', ''))) if unicodedata.category(c) != 'Mn')
            else:
                prijemce_clean = str(row['Příjemce'])
                note_clean = str(row.get('Poznámka', ''))

            pdf.set_font(font_family_name, "", 9.5)
            words = f"Adresa: {addr}".split(' ')
            lines_count = 1
            current_line_width = 0
            max_col_width = 54
            
            for word in words:
                word_w = pdf.get_string_width(word + " ")
                if current_line_width + word_w > max_col_width:
                    lines_count += 1
                    current_line_width = word_w
                else:
                    current_line_width += word_w
            
            content_height = (lines_count * 4.5) + 11
            if has_note:
                content_height += 8.5
                
            box_height = max(18, content_height)
            base_space = box_height + 10 
            needed_space = base_space + 25 if idx == len(df_itinerary) - 1 else base_space
                
            if (297 - pdf.get_y() - 20) < needed_space:
                pdf.add_page()
            
            start_y = pdf.get_y()
            pdf.set_draw_color(180, 180, 180)
            pdf.set_fill_color(245, 246, 250) if (is_start or is_end) else pdf.set_fill_color(255, 255, 255)
            pdf.rect(10, start_y, 190, box_height, style="DF" if (is_start or is_end) else "D")
            
            pdf.set_y(start_y + 2)
            pdf.set_x(13)
            pdf.set_font(font_family_name, "B", 10.5)
            pdf.set_text_color(44, 62, 80)
            
            title = f"Zastávka č. {idx} - {prijemce_clean}" if not (is_start or is_end) else f"{prijemce_clean}"
            if not (is_start or is_end):
                title += f"  [Obj: {row['Číslo objednávky']}]"
            pdf.cell(0, 5, title, ln=True)
            
            if has_note:
                pdf.ln(0.5)
                pdf.set_x(13)
                pdf.set_fill_color(255, 242, 204)
                pdf.set_draw_color(230, 126, 34)
                pdf.rect(13, pdf.get_y(), 184, 6, style="DF")
                
                pdf.set_x(15)
                pdf.set_font(font_family_name, "B", 9)
                pdf.set_text_color(211, 84, 0)
                pdf.cell(0, 6, f"⚠️ VZKAZ: {note_clean}", ln=True)
                pdf.ln(0.5)
            else:
                pdf.ln(0.5)

            current_y = pdf.get_y()

            pdf.set_y(current_y)
            pdf.set_x(13)
            pdf.set_font(font_family_name, "B", 7.5)
            pdf.set_text_color(120, 120, 120)
            pdf.cell(54, 3.5, "MÍSTO DORUČENÍ" if use_custom_font else "MISTO DORUCENI", ln=True)
            
            pdf.set_x(13)
            pdf.set_font(font_family_name, "", 9)
            pdf.set_text_color(30, 30, 30)
            pdf.multi_cell(54, 4.2, addr)
            
            pdf.set_y(current_y)
            pdf.set_x(70)
            pdf.set_font(font_family_name, "B", 7.5)
            pdf.set_text_color(120, 120, 120)
            pdf.cell(28, 3.5, "TELEFON", ln=True)
            
            pdf.set_y(current_y + 3.5)
            pdf.set_x(70)
            pdf.set_font(font_family_name, "", 9)
            pdf.set_text_color(30, 30, 30)
            pdf.cell(28, 4.5, str(row['Telefon']) if row['Telefon'] and str(row['Telefon']).lower() != 'none' else "-", ln=True)
            
            pdf.set_y(current_y)
            pdf.set_x(101)
            pdf.set_font(font_family_name, "B", 7.5)
            pdf.set_text_color(120, 120, 120)
            pdf.cell(45, 3.5, "ČASOVÝ HARMONOGRAM" if use_custom_font else "CASOVY HARMONOGRAM", ln=True)
            
            pdf.set_y(current_y + 3.5)
            pdf.set_x(101)
            pdf.set_font(font_family_name, "", 9)
            pdf.set_text_color(30, 30, 30)
            if is_start or is_end:
                pdf.cell(45, 4.5, f"Čas: {row['Čas příjezdu']}" if use_custom_font else f"Cas: {row['Čas příjezdu']}", ln=True)
            else:
                pdf.cell(45, 4.5, f"Příjezd cca: {row['Čas příjezdu']}" if use_custom_font else f"Prijezd cca: {row['Čas příjezdu']}", ln=True)
                pdf.set_x(101)
                pdf.set_font(font_family_name, "B", 9)
                pdf.cell(45, 4.5, f"Okno: {row['Okno příjezdu (2h)']}", ln=True)
                
            pdf.set_y(current_y)
            pdf.set_x(148)
            pdf.set_font(font_family_name, "B", 7.5)
            pdf.set_text_color(120, 120, 120)
            pdf.cell(22, 3.5, "K VYBRÁNÍ" if use_custom_font else "K VYBRANI", ln=True)
            
            pdf.set_y(current_y + 3.5)
            pdf.set_x(148)
            cod_val = parse_cod(row['Dobírka (Kč)'])
            if is_start or is_end:
                pdf.cell(22, 4.5, "-", ln=True)
            elif cod_val == 0:
                pdf.set_font(font_family_name, "B", 9.5)
                pdf.set_text_color(46, 204, 113) 
                pdf.cell(22, 4.5, "PLACENO", ln=True)
            else:
                pdf.set_font(font_family_name, "B", 9.5)
                pdf.set_text_color(231, 76, 60) 
                pdf.cell(22, 4.5, f"{int(cod_val)} Kč" if use_custom_font else f"{int(cod_val)} Kc", ln=True)
                
            if not (is_start or is_end):
                pdf.set_draw_color(100, 100, 100)
                pdf.set_line_width(0.4)
                pdf.rect(174, current_y + 0.5, 6, 6)
                pdf.set_line_width(0.2)
                
                pdf.set_draw_color(180, 180, 180)
                pdf.set_fill_color(248, 249, 250)
                pdf.rect(171, current_y + 8, 26, 6, style="DF")
                
                pdf.set_y(current_y + 9)
                pdf.set_x(172)
                pdf.set_font(font_family_name, "", 7.5)
                pdf.set_text_color(110, 110, 110)
                pdf.cell(26, 4, "Čas: __ : __" if use_custom_font else "Cas: __ : __", ln=True)
                
            pdf.set_y(start_y + box_height + 2)
            
            if idx < len(df_itinerary) - 1:
                pdf.ln(2)
                pdf.set_font(font_family_name, "B", 8)
                pdf.set_text_color(150, 150, 151)
                
                segment_text = f"      |      ⏩ Přejezd na další zastávku: {row['Vzdálen k další (km)']} km ({row['Čas k další (min)']} min)" if use_custom_font else f"      |      >> Prejezd na dalsi zastavku: {row['Vzdálen k další (km)']} km ({row['Čas k další (min)']} min)"
                pdf.cell(0, 4, segment_text, ln=True)
                pdf.ln(2)
                
        # Souhrn
        pdf.ln(4)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(3)
        pdf.set_font(font_family_name, "B", 11)
        pdf.set_text_color(44, 62, 80)
        
        pdf.cell(0, 6, "CELKOVÝ SOUHRN TRASY" if use_custom_font else "CELKOVY SOUHRN TRASY", ln=True)
        
        pdf.set_font(font_family_name, "", 10)
        pdf.cell(65, 5, f"Celková vzdálenost: {total_km} km" if use_custom_font else f"Celkova vzdalenost: {total_km} km", ln=False)
        pdf.cell(65, 5, f"Čistý čas jízdy: {total_hours}" if use_custom_font else f"Cisty cas jizdy: {total_hours}", ln=True)
        
        pdf.ln(1)
        pdf.set_font(font_family_name, "B", 10)
        pdf.set_text_color(231, 76, 60)
        pdf.cell(65, 5, f"Vybrat dobírky celkem: {int(total_cod)} Kč" if use_custom_font else f"Vybrat dobirky celkem: {int(total_cod)} Kc", ln=False)
        
        pdf.set_text_color(44, 62, 80)
        pdf.cell(65, 5, f"Kasáč (při odjezdu): {int(kasac_value)} Kč" if use_custom_font else f"Kasac (pri odjezdu): {int(kasac_value)} Kc", ln=True)

        # KROK 2: fpdf2 generuje čisté bajty metodou output() zcela bezpečně a nativně
        pdf_bytes = pdf.output()
        
        col_dl1, col_dl2 = st.columns(2)
        with col_dl1:
            st.download_button(
                "📥 Stáhnout PDF k tisku (A4)",
                data=pdf_bytes,
                file_name="trasovy_soupis_tisk.pdf",
                mime="application/pdf",
                type="primary"
            )
        with col_dl2:
            buffer_xls = io.BytesIO()
            with pd.ExcelWriter(buffer_xls, engine='openpyxl') as writer:
                df_final_display.to_excel(writer, index=False, sheet_name='Trasový soupis')
            st.download_button(
                "📥 Stáhnout XLSX tabulku",
                data=buffer_xls.getvalue(),
                file_name="hotovy_trasovy_soupis.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )