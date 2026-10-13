"""
build_tokyo_spatial_db.py
SORAI 診断レポート用: 東京都の公的オープンデータ（全7系統）を一括取得し、
R*Tree空間インデックス付きの SQLite データベース (tokyo_spatial.sqlite) を構築するスクリプト。

【収録レイヤー】
1. 用途地域 (A29)
2. 地価公示 (L01)
3. 小中学校区 (A27, A32)
4. 洪水浸水想定区域 (A31)
5. 土砂災害警戒区域 (A33)
6. 医療機関 (P04)
7. 保育・幼児教育施設 (P14)
"""

import argparse
import datetime
import glob
import json
import os
import sqlite3
import sys
import zipfile
from pathlib import Path
import requests
import geopandas as gpd
from shapely.validation import make_valid

DATA_SOURCES = {
    "zoning": {
        "url": "https://nlftp.mlit.go.jp/ksj/gml/data/A29/A29-19/A29-19_13_GML.zip",
        "name": "用途地域 (A29-19)",
        "year": "2019"
    },
    "land_price": {
        "url": "https://nlftp.mlit.go.jp/ksj/gml/data/L01/L01-24/L01-24_13_GML.zip",
        "name": "地価公示 (L01-24)",
        "year": "2024"
    },
    "school_elem": {
        "url": "https://nlftp.mlit.go.jp/ksj/gml/data/A27/A27-23/A27-23_13_GML.zip",
        "name": "小学校区 (A27-23)",
        "year": "2023"
    },
    "school_jhs": {
        "url": "https://nlftp.mlit.go.jp/ksj/gml/data/A32/A32-23/A32-23_13_GML.zip",
        "name": "中学校区 (A32-23)",
        "year": "2023"
    },
    "flood": {
        "url": "https://nlftp.mlit.go.jp/ksj/gml/data/A31/A31-21/A31-21_13_GML.zip",
        "name": "洪水浸水想定区域 (A31-21)",
        "year": "2021"
    },
    "landslide": {
        "url": "https://nlftp.mlit.go.jp/ksj/gml/data/A33/A33-22/A33-22_13_GML.zip",
        "name": "土砂災害警戒区域 (A33-22)",
        "year": "2022"
    },
    "medical": {
        "url": "https://nlftp.mlit.go.jp/ksj/gml/data/P04/P04-20/P04-20_13_GML.zip",
        "name": "医療機関 (P04-20)",
        "year": "2020"
    },
    "nursery": {
        "url": "https://nlftp.mlit.go.jp/ksj/gml/data/P14/P14-21/P14-21_13_GML.zip",
        "name": "保育施設 (P14-21)",
        "year": "2021"
    }
}

ZONING_CODE_MAP = {
    "1": "第一種低層住居専用地域", "01": "第一種低層住居専用地域",
    "2": "第二種低層住居専用地域", "02": "第二種低層住居専用地域",
    "3": "第一種中高層住居専用地域", "03": "第一種中高層住居専用地域",
    "4": "第二種中高層住居専用地域", "04": "第二種中高層住居専用地域",
    "5": "第一種住居地域", "05": "第一種住居地域",
    "6": "第二種住居地域", "06": "第二種住居地域",
    "7": "準住居地域", "07": "準住居地域",
    "8": "近隣商業地域", "08": "近隣商業地域",
    "9": "商業地域", "09": "商業地域",
    "10": "準工業地域",
    "11": "工業地域",
    "12": "工業専用地域",
    "21": "田園住居地域"
}

FLOOD_DEPTH_MAP = {
    1: "0.5m未満", "1": "0.5m未満",
    2: "0.5〜3.0m未満", "2": "0.5〜3.0m未満",
    3: "3.0〜5.0m未満", "3": "3.0〜5.0m未満",
    4: "5.0〜10.0m未満", "4": "5.0〜10.0m未満",
    5: "10.0〜20.0m未満", "5": "10.0〜20.0m未満",
    6: "20.0m以上", "6": "20.0m以上"
}

def download_file(url: str, dest_path: Path):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.exists():
        print(f"   ⏩ キャッシュ利用: {dest_path.name}")
        return
    print(f"   📥 ダウンロード中: {url}")
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(url, headers=headers, stream=True, timeout=180)
    res.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in res.iter_content(chunk_size=65536):
            f.write(chunk)

def extract_zip(zip_path: Path, extract_dir: Path) -> Path:
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(extract_dir)
    return extract_dir

def init_database(db_path: Path) -> sqlite3.Connection:
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # 1. 用途地域 (Zoning)
    cur.execute("""
    CREATE TABLE zoning (
        id INTEGER PRIMARY KEY,
        name TEXT,
        code TEXT,
        building_coverage INTEGER,
        floor_area_ratio INTEGER,
        city_code TEXT,
        geom_wkb BLOB
    )
    """)
    cur.execute("CREATE VIRTUAL TABLE zoning_idx USING rtree(id, minX, maxX, minY, maxY)")

    # 2. 地価公示 (Land Price)
    cur.execute("""
    CREATE TABLE land_price (
        id INTEGER PRIMARY KEY,
        price INTEGER,
        change_rate REAL,
        address TEXT,
        station_name TEXT,
        station_distance INTEGER,
        lat REAL,
        lon REAL
    )
    """)
    cur.execute("CREATE VIRTUAL TABLE land_price_idx USING rtree(id, minX, maxX, minY, maxY)")

    # 3. 小中学校区 (School District)
    cur.execute("""
    CREATE TABLE school_district (
        id INTEGER PRIMARY KEY,
        school_type TEXT,
        school_name TEXT,
        school_address TEXT,
        city_name TEXT,
        geom_wkb BLOB
    )
    """)
    cur.execute("CREATE VIRTUAL TABLE school_district_idx USING rtree(id, minX, maxX, minY, maxY)")

    # 4. 洪水浸水想定 (Flood Risk)
    cur.execute("""
    CREATE TABLE flood_risk (
        id INTEGER PRIMARY KEY,
        river_name TEXT,
        rank_code INTEGER,
        depth_label TEXT,
        geom_wkb BLOB
    )
    """)
    cur.execute("CREATE VIRTUAL TABLE flood_risk_idx USING rtree(id, minX, maxX, minY, maxY)")

    # 5. 土砂災害警戒区域 (Landslide Risk)
    cur.execute("""
    CREATE TABLE landslide_risk (
        id INTEGER PRIMARY KEY,
        hazard_type TEXT,
        zone_type TEXT,
        address TEXT,
        geom_wkb BLOB
    )
    """)
    cur.execute("CREATE VIRTUAL TABLE landslide_risk_idx USING rtree(id, minX, maxX, minY, maxY)")

    # 6. 医療機関 (Medical Facilities)
    cur.execute("""
    CREATE TABLE medical_facility (
        id INTEGER PRIMARY KEY,
        name TEXT,
        facility_type TEXT,
        address TEXT,
        departments TEXT,
        is_emergency INTEGER,
        lat REAL,
        lon REAL
    )
    """)
    cur.execute("CREATE VIRTUAL TABLE medical_facility_idx USING rtree(id, minX, maxX, minY, maxY)")

    # 7. 保育・幼児教育施設 (Nursery Facilities)
    cur.execute("""
    CREATE TABLE nursery_facility (
        id INTEGER PRIMARY KEY,
        name TEXT,
        address TEXT,
        facility_type_code TEXT,
        lat REAL,
        lon REAL
    )
    """)
    cur.execute("CREATE VIRTUAL TABLE nursery_facility_idx USING rtree(id, minX, maxX, minY, maxY)")

    # 8. メタデータ
    cur.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    return conn

def process_zoning(work_dir: Path, conn: sqlite3.Connection):
    print("\n🏙️ 1/7 用途地域 (A29) を処理中...")
    zip_path = work_dir / "zoning.zip"
    download_file(DATA_SOURCES["zoning"]["url"], zip_path)
    ext_dir = extract_zip(zip_path, work_dir / "zoning_raw")

    shp_13000 = list(ext_dir.rglob("*13000.shp"))
    target_shps = shp_13000 if shp_13000 else list(ext_dir.rglob("*.shp"))
    if not target_shps:
        return

    gdf = gpd.read_file(target_shps[0])
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    gdf["geometry"] = gdf["geometry"].apply(make_valid)

    cur = conn.cursor()
    inserted = 0
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        raw_code = str(row.get("A29_004", "")).strip()
        name = ZONING_CODE_MAP.get(raw_code, str(row.get("A29_005", "")))
        try:
            cov = int(row.get("A29_006", 0) or 0)
        except Exception:
            cov = 0
        try:
            far = int(row.get("A29_007", 0) or 0)
        except Exception:
            far = 0
        city = str(row.get("A29_001", "") or "")
        bounds = geom.bounds
        row_id = inserted + 1

        cur.execute(
            "INSERT INTO zoning (id, name, code, building_coverage, floor_area_ratio, city_code, geom_wkb) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row_id, name, raw_code, cov, far, city, geom.wkb)
        )
        cur.execute(
            "INSERT INTO zoning_idx (id, minX, maxX, minY, maxY) VALUES (?, ?, ?, ?, ?)",
            (row_id, bounds[0], bounds[2], bounds[1], bounds[3])
        )
        inserted += 1
    conn.commit()
    print(f"   ✅ 用途地域 {inserted:,} 件完了")

def process_land_price(work_dir: Path, conn: sqlite3.Connection):
    print("\n💰 2/7 地価公示 (L01) を処理中...")
    zip_path = work_dir / "land_price.zip"
    download_file(DATA_SOURCES["land_price"]["url"], zip_path)
    ext_dir = extract_zip(zip_path, work_dir / "land_price_raw")

    geojsons = list(ext_dir.rglob("*.geojson"))
    gdf = gpd.read_file(geojsons[0]) if geojsons else gpd.read_file(list(ext_dir.rglob("*.shp"))[0])
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    cur = conn.cursor()
    inserted = 0
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        lon, lat = geom.x, geom.y
        try:
            price = int(row.get("L01_008", 0) or 0)
        except Exception:
            price = 0
        try:
            rate = float(row.get("L01_009", 0.0) or 0.0)
        except Exception:
            rate = 0.0
        addr = str(row.get("L01_025", "") or "").strip()
        st_name = str(row.get("L01_048", "") or "").strip()
        try:
            st_dist_str = str(row.get("L01_049", "0") or "0").replace(",", "").strip()
            st_dist = int(st_dist_str) if st_dist_str.isdigit() else 0
        except Exception:
            st_dist = 0

        row_id = inserted + 1
        cur.execute(
            "INSERT INTO land_price (id, price, change_rate, address, station_name, station_distance, lat, lon) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, price, rate, addr, st_name, st_dist, lat, lon)
        )
        cur.execute(
            "INSERT INTO land_price_idx (id, minX, maxX, minY, maxY) VALUES (?, ?, ?, ?, ?)",
            (row_id, lon, lon, lat, lat)
        )
        inserted += 1
    conn.commit()
    print(f"   ✅ 地価公示 {inserted:,} 件完了")

def process_schools(work_dir: Path, conn: sqlite3.Connection):
    print("\n🏫 3/7 小中学校区 (A27, A32) を処理中...")
    cur = conn.cursor()
    inserted = 0
    for stype, key in [("elem", "school_elem"), ("jhs", "school_jhs")]:
        zip_path = work_dir / f"{key}.zip"
        download_file(DATA_SOURCES[key]["url"], zip_path)
        ext_dir = extract_zip(zip_path, work_dir / f"{key}_raw")
        shps = list(ext_dir.rglob("*.shp"))
        if not shps:
            continue
        gdf = gpd.read_file(shps[0])
        if gdf.crs is None or gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        gdf["geometry"] = gdf["geometry"].apply(make_valid)

        for _, row in gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            sname = str(row.get("A27_004", row.get("A32_004", row.get("学校名", ""))) or "").strip()
            saddr = str(row.get("A27_005", row.get("A32_005", row.get("所在地", ""))) or "").strip()
            city = str(row.get("A27_001", row.get("A32_001", row.get("行政区域名", ""))) or "").strip()
            bounds = geom.bounds
            inserted += 1
            row_id = inserted
            cur.execute(
                "INSERT INTO school_district (id, school_type, school_name, school_address, city_name, geom_wkb) VALUES (?, ?, ?, ?, ?, ?)",
                (row_id, stype, sname, saddr, city, geom.wkb)
            )
            cur.execute(
                "INSERT INTO school_district_idx (id, minX, maxX, minY, maxY) VALUES (?, ?, ?, ?, ?)",
                (row_id, bounds[0], bounds[2], bounds[1], bounds[3])
            )
    conn.commit()
    print(f"   ✅ 小中学校区 {inserted:,} 件完了")

def process_flood(work_dir: Path, conn: sqlite3.Connection):
    print("\n🌊 4/7 洪水浸水想定区域 (A31) を処理中...")
    zip_path = work_dir / "flood.zip"
    download_file(DATA_SOURCES["flood"]["url"], zip_path)
    ext_dir = extract_zip(zip_path, work_dir / "flood_raw")

    geojsons = list(ext_dir.rglob("*.geojson"))
    print(f"   河川別GeoJSON: {len(geojsons)} ファイル")

    cur = conn.cursor()
    inserted = 0
    for gjson in geojsons:
        try:
            gdf = gpd.read_file(gjson)
            if gdf.crs is None or gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs(epsg=4326)
            gdf["geometry"] = gdf["geometry"].apply(make_valid)

            for _, row in gdf.iterrows():
                geom = row.geometry
                if geom is None or geom.is_empty:
                    continue
                rname = str(row.get("A31_202", "") or "").strip()
                try:
                    rcode = int(row.get("A31_205", 0) or 0)
                except Exception:
                    rcode = 0
                dlabel = FLOOD_DEPTH_MAP.get(rcode, "浸水想定あり")
                bounds = geom.bounds
                inserted += 1
                row_id = inserted
                cur.execute(
                    "INSERT INTO flood_risk (id, river_name, rank_code, depth_label, geom_wkb) VALUES (?, ?, ?, ?, ?)",
                    (row_id, rname, rcode, dlabel, geom.wkb)
                )
                cur.execute(
                    "INSERT INTO flood_risk_idx (id, minX, maxX, minY, maxY) VALUES (?, ?, ?, ?, ?)",
                    (row_id, bounds[0], bounds[2], bounds[1], bounds[3])
                )
        except Exception as e:
            continue
    conn.commit()
    print(f"   ✅ 洪水浸水想定 {inserted:,} 件完了")

def process_landslide(work_dir: Path, conn: sqlite3.Connection):
    print("\n⛰️ 5/7 土砂災害警戒区域 (A33) を処理中...")
    zip_path = work_dir / "landslide.zip"
    download_file(DATA_SOURCES["landslide"]["url"], zip_path)
    ext_dir = extract_zip(zip_path, work_dir / "landslide_raw")

    geojsons = list(ext_dir.rglob("*.geojson"))
    target = geojsons[0] if geojsons else list(ext_dir.rglob("*.shp"))[0]
    gdf = gpd.read_file(target)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    gdf["geometry"] = gdf["geometry"].apply(make_valid)

    haz_map = {1: "急傾斜地崩壊", 2: "土石流", 3: "地すべり"}
    zone_map = {1: "警戒区域(イエロー)", 2: "特別警戒区域(レッド)"}

    cur = conn.cursor()
    inserted = 0
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        try:
            htype = haz_map.get(int(row.get("A33_001", 0) or 0), "土砂災害")
            ztype = zone_map.get(int(row.get("A33_002", 0) or 0), "警戒区域")
        except Exception:
            htype, ztype = "土砂災害", "警戒区域"
        addr = str(row.get("A33_006", "") or "").strip()
        bounds = geom.bounds
        inserted += 1
        row_id = inserted
        cur.execute(
            "INSERT INTO landslide_risk (id, hazard_type, zone_type, address, geom_wkb) VALUES (?, ?, ?, ?, ?)",
            (row_id, htype, ztype, addr, geom.wkb)
        )
        cur.execute(
            "INSERT INTO landslide_risk_idx (id, minX, maxX, minY, maxY) VALUES (?, ?, ?, ?, ?)",
            (row_id, bounds[0], bounds[2], bounds[1], bounds[3])
        )
    conn.commit()
    print(f"   ✅ 土砂災害警戒区域 {inserted:,} 件完了")

def process_medical(work_dir: Path, conn: sqlite3.Connection):
    print("\n🏥 6/7 医療機関 (P04) を処理中...")
    zip_path = work_dir / "medical.zip"
    download_file(DATA_SOURCES["medical"]["url"], zip_path)
    ext_dir = extract_zip(zip_path, work_dir / "medical_raw")

    geojsons = list(ext_dir.rglob("*.geojson"))
    target = geojsons[0] if geojsons else list(ext_dir.rglob("*.shp"))[0]
    gdf = gpd.read_file(target)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    type_map = {1: "病院", 2: "診療所", 3: "歯科"}
    cur = conn.cursor()
    inserted = 0
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        lon, lat = geom.x, geom.y
        name = str(row.get("P04_002", "") or "").strip()
        addr = str(row.get("P04_003", "") or "").strip()
        deps = str(row.get("P04_004", "") or "").strip()
        try:
            ftype = type_map.get(int(row.get("P04_001", 0) or 0), "医療施設")
            is_em = 1 if int(row.get("P04_007", 0) or 0) == 1 else 0
        except Exception:
            ftype, is_em = "医療施設", 0

        inserted += 1
        row_id = inserted
        cur.execute(
            "INSERT INTO medical_facility (id, name, facility_type, address, departments, is_emergency, lat, lon) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, name, ftype, addr, deps, is_em, lat, lon)
        )
        cur.execute(
            "INSERT INTO medical_facility_idx (id, minX, maxX, minY, maxY) VALUES (?, ?, ?, ?, ?)",
            (row_id, lon, lon, lat, lat)
        )
    conn.commit()
    print(f"   ✅ 医療機関 {inserted:,} 件完了")

def process_nursery(work_dir: Path, conn: sqlite3.Connection):
    print("\n👶 7/7 保育施設 (P14) を処理中...")
    zip_path = work_dir / "nursery.zip"
    download_file(DATA_SOURCES["nursery"]["url"], zip_path)
    ext_dir = extract_zip(zip_path, work_dir / "nursery_raw")

    geojsons = list(ext_dir.rglob("*.geojson"))
    target = geojsons[0] if geojsons else list(ext_dir.rglob("*.shp"))[0]
    gdf = gpd.read_file(target)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    cur = conn.cursor()
    inserted = 0
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        lon, lat = geom.x, geom.y
        name = str(row.get("P14_008", "") or "").strip()
        city = str(row.get("P14_002", "") or "").strip()
        sub = str(row.get("P14_004", "") or "").strip()
        addr = f"{city}{sub}".strip()
        tcode = str(row.get("P14_006", "") or "").strip()

        inserted += 1
        row_id = inserted
        cur.execute(
            "INSERT INTO nursery_facility (id, name, address, facility_type_code, lat, lon) VALUES (?, ?, ?, ?, ?, ?)",
            (row_id, name, addr, tcode, lat, lon)
        )
        cur.execute(
            "INSERT INTO nursery_facility_idx (id, minX, maxX, minY, maxY) VALUES (?, ?, ?, ?, ?)",
            (row_id, lon, lon, lat, lat)
        )
    conn.commit()
    print(f"   ✅ 保育施設 {inserted:,} 件完了")

def save_metadata(conn: sqlite3.Connection):
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", ("built_at", now_str))
    cur.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", ("crs", "EPSG:4326 (WGS84)"))
    cur.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", ("coverage", "東京都 (Prefecture 13)"))
    cur.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", ("layers", "zoning, land_price, school_district, flood_risk, landslide_risk, medical_facility, nursery_facility"))
    conn.commit()

def main():
    parser = argparse.ArgumentParser(description="Build complete tokyo_spatial.sqlite (7 layers)")
    parser.add_argument("--output", default="data/tokyo_spatial.sqlite", help="出力先SQLiteパス")
    parser.add_argument("--work-dir", default="/tmp/sorai_spatial_work", help="作業用一時ディレクトリ")
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("==================================================")
    print("🚀 SORAI 診断レポート用 東京都空間DB (全7系統) ビルド開始")
    print(f"📦 出力先: {out_path.resolve()}")
    print("==================================================")

    conn = init_database(out_path)
    try:
        process_zoning(work_dir, conn)
        process_land_price(work_dir, conn)
        process_schools(work_dir, conn)
        process_flood(work_dir, conn)
        process_landslide(work_dir, conn)
        process_medical(work_dir, conn)
        process_nursery(work_dir, conn)
        save_metadata(conn)
        print("\n🎉 全7系統のビルド成功！")
        db_size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"📊 データベースサイズ: {db_size_mb:.2f} MB")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
