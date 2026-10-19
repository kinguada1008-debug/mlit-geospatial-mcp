"""
build_tokyo_spatial_db.py
SorAi 物件診断用: 東京都の公的オープンデータ（全7系統）を一括取得し、
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
import builtins
import datetime
import fcntl
import glob
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
try:
    import geopandas as gpd
except ImportError:
    gpd = None
import requests
from shapely.geometry import shape
from shapely.validation import make_valid


def print(*args, **kwargs):
    """MCP サーバー規律準拠: すべての進捗ログ出力を stderr に統一"""
    kwargs.setdefault("file", sys.stderr)
    builtins.print(*args, **kwargs)


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
    },
    "planning_a55": {
        "url": "https://nlftp.mlit.go.jp/ksj/gml/data/A55/A55-24/A55-24_13000_GEOJSON.zip",
        "name": "都市計画決定GISデータ (A55-24: 防火・準防火・地区計画・高度利用・高度地区)",
        "year": "2024"
    },
    "embankment_a54": {
        "url": "https://nlftp.mlit.go.jp/ksj/gml/data/A54/A54-23/A54-23_13_GEOJSON.zip",
        "name": "大規模盛土造成地 (A54-23)",
        "year": "2023"
    },
    "storm_surge_a49": {
        "url": "https://nlftp.mlit.go.jp/ksj/gml/data/A49/A49-20/A49-20_13_GML.zip",
        "name": "高潮浸水想定区域 (A49-20)",
        "year": "2020"
    },
    "tsunami_a40": {
        "url": "https://nlftp.mlit.go.jp/ksj/gml/data/A40/A40-23/A40-23_13_GML.zip",
        "name": "津波浸水想定 (A40-23)",
        "year": "2023"
    },
    "population_mesh250r6": {
        "url": "https://nlftp.mlit.go.jp/ksj/gml/data/m250r6/m250r6-24/250m_mesh_2024_13_SHP.zip",
        "name": "将来推計人口250mメッシュ (R6推計 / 2020-2050)",
        "year": "2024"
    },
    "liquefaction_wm2020": {
        "url": "https://www.j-shis.bosai.go.jp/labs/wm2020/data/Z-WM2020-JAPAN-M250.zip",
        "name": "地形・地盤分類250mメッシュ / 国交省都市局 液状化発生傾向基準 (WM2020)",
        "year": "2020"
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

# J-SHIS 微地形区分 (JCODE 0..24) と 国土交通省都市局「地形区分に基づく液状化の発生傾向図」対応表
# relative_score: 1 (発生傾向: 極めて低い) 〜 5 (発生傾向: 高い), None (水域・評価対象外)
JCODE_LIQUEFACTION_MAP: Dict[int, Tuple[str, str, Optional[int], str, str]] = {
    0:  ("沿岸海域", "評価対象外（水域）", None, "not_assessed", "外洋沿岸の水域"),
    1:  ("山地", "液状化の発生傾向が極めて低い", 1, "classified", "先第三系・第三系の岩石からなる山地"),
    2:  ("山麓地", "液状化の発生傾向が極めて低い", 1, "classified", "山地・丘陵に接する緩傾斜地"),
    3:  ("丘陵", "液状化の発生傾向が極めて低い", 1, "classified", "起伏量200m以下の斜面からなる丘陵地"),
    4:  ("火山地", "液状化の発生傾向が極めて低い", 1, "classified", "第四紀火山岩類からなる火山地"),
    5:  ("火山山麓地", "液状化の発生傾向が極めて低い", 1, "classified", "火山地周縁の緩傾斜地"),
    6:  ("火山性丘陵", "液状化の発生傾向が極めて低い", 1, "classified", "火砕流堆積地等の丘陵"),
    7:  ("岩石台地", "液状化の発生傾向が極めて低い", 1, "classified", "表層堆積物が薄い河岸・海岸段丘"),
    8:  ("砂礫質台地", "液状化の発生傾向が極めて低い", 1, "classified", "更新統の砂礫層・砂質土層からなる段丘台地"),
    9:  ("火山灰台地（ローム台地）", "液状化の発生傾向が極めて低い", 1, "classified", "関東ローム層等の火山灰土層からなる堅固な台地"),
    10: ("谷底低地", "液状化の発生傾向が中程度", 3, "classified", "台地・丘陵間に分布する川沿いの沖積低地"),
    11: ("扇状地", "液状化の発生傾向が中程度", 3, "classified", "河川が山地から低地に出る砂礫質の堆積地"),
    12: ("自然堤防", "液状化の発生傾向がやや高い", 4, "classified", "河道沿いに砂質土が堆積した微高地"),
    13: ("後背湿地", "液状化の発生傾向が中程度", 3, "classified", "自然堤防背後の軟弱粘性土・腐植土を含む沖積低地"),
    14: ("旧河道・旧池沼", "液状化の発生傾向が高い", 5, "classified", "過去の河川流路または池沼であった凹地（ゆるい砂質土）"),
    15: ("三角州・海岸低地", "液状化の発生傾向がやや高い", 4, "classified", "河川河口部・海岸付近の低平な沖積低地"),
    16: ("砂州・砂礫州", "液状化の発生傾向が中程度", 3, "classified", "汀線沿いに形成された砂・砂礫の微高地"),
    17: ("砂丘", "液状化の発生傾向が低い", 2, "classified", "風成砂が堆積した微高地（末端緩斜面では注意）"),
    18: ("砂丘・砂州間低地", "液状化の発生傾向が高い", 5, "classified", "砂州や砂丘の間の低地（堤間低地）"),
    19: ("干拓地", "液状化の発生傾向がやや高い", 4, "classified", "浅海底や湖底を排水・陸化させた低地"),
    20: ("埋立地", "液状化の発生傾向が高い", 5, "classified", "水面下を盛土により陸化させた土地（地盤改良有無の確認推奨）"),
    21: ("磯・岩礁", "液状化の発生傾向が極めて低い", 1, "classified", "基盤岩が露出する海岸"),
    22: ("河原", "液状化の発生傾向が中程度", 3, "classified", "常時水流がない河道の砂礫地"),
    23: ("河道", "評価対象外（水域）", None, "not_assessed", "常時水流がある河川水域"),
    24: ("湖沼", "評価対象外（水域）", None, "not_assessed", "静止した内陸水域"),
}

COASTAL_DEPTH_PARSE_MAP: Dict[str, Tuple[Optional[float], Optional[float]]] = {
    "～0.3m未満": (0.0, 0.3),
    "0.3m未満": (0.0, 0.3),
    "0.3m以上 ～ 0.5m未満": (0.3, 0.5),
    "0.3m以上0.5m未満": (0.3, 0.5),
    "0.5m以上 ～ 1m未満": (0.5, 1.0),
    "0.5m以上1m未満": (0.5, 1.0),
    "1m以上 ～ 3m未満": (1.0, 3.0),
    "1m以上3m未満": (1.0, 3.0),
    "3m以上 ～ 5m未満": (3.0, 5.0),
    "3m以上5m未満": (3.0, 5.0),
    "5m以上 ～ 10m未満": (5.0, 10.0),
    "5m以上10m未満": (5.0, 10.0),
    "10m以上 ～ 20m未満": (10.0, 20.0),
    "10m以上20m未満": (10.0, 20.0),
    "20m以上": (20.0, None),
}

MAX_DOWNLOAD_BYTES = 120 * 1024 * 1024  # 120MB 上限 (Fail-Closed)


def download_file(url: str, dest_path: Path, max_bytes: int = MAX_DOWNLOAD_BYTES):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.exists():
        print(f"   ⏩ キャッシュ利用: {dest_path.name}")
        return
    print(f"   📥 ダウンロード中: {url}")
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(url, headers=headers, stream=True, timeout=180)
    res.raise_for_status()
    tmp_dest = dest_path.with_name(f"{dest_path.name}.{os.getpid()}.tmp")
    total_bytes = 0
    try:
        with open(tmp_dest, "wb") as f:
            for chunk in res.iter_content(chunk_size=65536):
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise RuntimeError(f"Download exceeded max_bytes limit ({max_bytes} bytes): {url}")
                f.write(chunk)
        tmp_dest.replace(dest_path)
    except Exception:
        if tmp_dest.exists():
            tmp_dest.unlink()
        raise


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

    # 8. 都市計画詳細 (Planning Zone: 防火・準防火・地区計画・高度利用)
    cur.execute("""
    CREATE TABLE planning_zone (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        name TEXT,
        source_code TEXT,
        city_code TEXT,
        geom_wkb BLOB NOT NULL
    )
    """)
    cur.execute("CREATE VIRTUAL TABLE planning_zone_idx USING rtree(id, minX, maxX, minY, maxY)")

    # 9. 大規模盛土造成地 (Embankment: 谷埋め型・腹付け型造成地)
    cur.execute("""
    CREATE TABLE embankment (
        id INTEGER PRIMARY KEY,
        fill_code TEXT NOT NULL,
        fill_number TEXT,
        city_code TEXT,
        geom_wkb BLOB NOT NULL
    )
    """)
    cur.execute("CREATE VIRTUAL TABLE embankment_idx USING rtree(id, minX, maxX, minY, maxY)")

    # 10. 沿岸ハザード (Coastal Inundation: 高潮・津波浸水想定)
    cur.execute("""
    CREATE TABLE coastal_inundation (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        scenario_key TEXT,
        depth_raw TEXT,
        depth_min_m REAL,
        depth_max_m REAL,
        geom_wkb BLOB NOT NULL
    )
    """)
    cur.execute("CREATE VIRTUAL TABLE coastal_inundation_idx USING rtree(id, minX, maxX, minY, maxY)")

    # 11. 液状化発生傾向メッシュ (Liquefaction: 地形区分250mメッシュ)
    cur.execute("""
    CREATE TABLE liquefaction_mesh (
        mesh_code TEXT PRIMARY KEY,
        landform_name TEXT,
        tendency_label TEXT,
        relative_score INTEGER,
        assessment TEXT,
        note TEXT
    ) WITHOUT ROWID
    """)

    # 12. 将来推計人口メッシュ (Population Projection: 250mメッシュ 2020年比2050年予測)
    cur.execute("""
    CREATE TABLE population_mesh (
        mesh_code TEXT PRIMARY KEY,
        city_code TEXT,
        population_2020 REAL,
        population_2050 REAL,
        disclosure_2020 TEXT,
        disclosure_2050 TEXT
    ) WITHOUT ROWID
    """)

    # 13. メタデータ
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

def _extract_polygons(geom) -> List:
    if isinstance(geom, dict):
        geom = shape(geom)
    if geom is None or geom.is_empty:
        return []
    if not geom.is_valid:
        geom = make_valid(geom)
    if geom is None or geom.is_empty:
        return []
    gtype = geom.geom_type
    if gtype == "Polygon":
        return [geom]
    if gtype in ("MultiPolygon", "GeometryCollection"):
        polys = []
        for sub in geom.geoms:
            polys.extend(_extract_polygons(sub))
        return polys
    return []



def ensure_6layers_schema(conn: sqlite3.Connection):
    cur = conn.cursor()
    for tbl in [
        "planning_zone", "planning_zone_idx",
        "embankment", "embankment_idx",
        "coastal_inundation", "coastal_inundation_idx",
        "liquefaction_mesh", "population_mesh"
    ]:
        cur.execute(f"DROP TABLE IF EXISTS {tbl}")

    cur.execute("""
    CREATE TABLE planning_zone (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        name TEXT,
        source_code TEXT,
        city_code TEXT,
        geom_wkb BLOB NOT NULL
    )
    """)
    cur.execute("CREATE VIRTUAL TABLE planning_zone_idx USING rtree(id, minX, maxX, minY, maxY)")
    cur.execute("CREATE INDEX idx_planning_zone_kind ON planning_zone(kind)")

    cur.execute("""
    CREATE TABLE embankment (
        id INTEGER PRIMARY KEY,
        fill_code TEXT NOT NULL,
        fill_number TEXT,
        city_code TEXT,
        geom_wkb BLOB NOT NULL
    )
    """)
    cur.execute("CREATE VIRTUAL TABLE embankment_idx USING rtree(id, minX, maxX, minY, maxY)")

    cur.execute("""
    CREATE TABLE coastal_inundation (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        scenario_key TEXT,
        depth_raw TEXT,
        depth_min_m REAL,
        depth_max_m REAL,
        geom_wkb BLOB NOT NULL
    )
    """)
    cur.execute("CREATE VIRTUAL TABLE coastal_inundation_idx USING rtree(id, minX, maxX, minY, maxY)")
    cur.execute("CREATE INDEX idx_coastal_kind ON coastal_inundation(kind)")

    cur.execute("""
    CREATE TABLE liquefaction_mesh (
        mesh_code TEXT PRIMARY KEY,
        landform_name TEXT,
        tendency_label TEXT,
        relative_score INTEGER,
        assessment TEXT,
        note TEXT
    ) WITHOUT ROWID
    """)

    cur.execute("""
    CREATE TABLE population_mesh (
        mesh_code TEXT PRIMARY KEY,
        city_code TEXT,
        population_2020 REAL,
        population_2050 REAL,
        disclosure_2020 TEXT,
        disclosure_2050 TEXT
    ) WITHOUT ROWID
    """)

    cur.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()


MAX_UNCOMPRESSED_GEOJSON_BYTES = 150 * 1024 * 1024
MAX_GEOJSON_FEATURES = 200_000


def _read_geojson_features_from_zip(z: zipfile.ZipFile, member: str) -> List[dict]:
    info = z.getinfo(member)
    if info.file_size > MAX_UNCOMPRESSED_GEOJSON_BYTES:
        raise RuntimeError(
            f"Uncompressed GeoJSON entry exceeds limit ({info.file_size} > {MAX_UNCOMPRESSED_GEOJSON_BYTES}): {member}"
        )
    data = json.loads(z.read(member).decode("utf-8", errors="replace"))
    features = data.get("features") or []
    if len(features) > MAX_GEOJSON_FEATURES:
        raise RuntimeError(
            f"GeoJSON feature count exceeds limit ({len(features)} > {MAX_GEOJSON_FEATURES}): {member}"
        )
    return features


def process_planning_zones(work_dir: Path, conn: sqlite3.Connection):
    print("\n🔥 8 & 13/13 都市計画決定GISデータ (A55: 防火・準防火・地区計画・高度利用・高度地区) を処理中...")
    zip_path = work_dir / "a55.zip"
    download_file(DATA_SOURCES["planning_a55"]["url"], zip_path)

    cur = conn.cursor()
    row_id = 0
    covered_cities: Dict[str, Set[str]] = {
        "bouka": set(),
        "chikukei": set(),
        "koudori": set(),
        "koudoti": set(),
        "tkbt": set(),
    }
    counts_by_kind: Dict[str, int] = {}

    with zipfile.ZipFile(zip_path, "r") as z:
        for member in sorted(z.namelist()):
            if not member.endswith(".geojson"):
                continue
            m_city = re.search(r"(13\d{3})_(bouka|chikukei|koudori|koudoti|tkbt)\.geojson$", member)
            if not m_city:
                continue
            city_code, sublayer = m_city.group(1), m_city.group(2)
            covered_cities[sublayer].add(city_code)

            for feat in _read_geojson_features_from_zip(z, member):
                geom_dict = feat.get("geometry")
                if not geom_dict:
                    continue
                props = feat.get("properties") or {}
                c_code = str(props.get("Citycode") or city_code).strip()

                if sublayer == "bouka":
                    acode = str(props.get("AreaCode") or "").strip()
                    atype = str(props.get("AreaType") or "").strip()
                    if acode == "24" or atype == "防火地域":
                        kind = "fire"
                        name = "防火地域"
                    elif acode == "25" or atype == "準防火地域":
                        kind = "quasi_fire"
                        name = "準防火地域"
                    else:
                        continue
                    scode = acode or ("24" if kind == "fire" else "25")
                elif sublayer == "chikukei":
                    kind = "district_plan"
                    name = str(props.get("DistName") or props.get("DistType") or "地区計画").strip()
                    scode = str(props.get("DistCode") or "1").strip()
                elif sublayer == "koudori":
                    kind = "high_use"
                    name = str(props.get("DistName") or props.get("DistType") or "高度利用地区").strip()
                    scode = str(props.get("DistCode") or "19").strip()
                elif sublayer == "koudoti":
                    kind = "height_district"
                    name = str(props.get("DistName") or props.get("DistType") or "高度地区").strip()
                    scode = str(props.get("DistCode") or "18").strip()
                elif sublayer == "tkbt":
                    kind = "special_use"
                    name = str(props.get("YoutoName") or props.get("DistType") or "特別用途地区").strip()
                    scode = str(props.get("YoutoCode") or props.get("DistCode") or "14").strip()
                else:
                    continue

                raw_geom = shape(geom_dict)
                for poly in _extract_polygons(raw_geom):
                    row_id += 1
                    bounds = poly.bounds
                    cur.execute(
                        "INSERT INTO planning_zone (id, kind, name, source_code, city_code, geom_wkb) VALUES (?, ?, ?, ?, ?, ?)",
                        (row_id, kind, name, scode, c_code, poly.wkb)
                    )
                    cur.execute(
                        "INSERT INTO planning_zone_idx (id, minX, maxX, minY, maxY) VALUES (?, ?, ?, ?, ?)",
                        (row_id, bounds[0], bounds[2], bounds[1], bounds[3])
                    )
                    counts_by_kind[kind] = counts_by_kind.get(kind, 0) + 1

    if row_id == 0:
        raise RuntimeError("都市計画決定GIS (A55) の有効ポリゴンが 0 件です（Fail-Closed）")

    for sublayer, cset in covered_cities.items():
        cur.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            (f"a55_{sublayer}_cities", json.dumps(sorted(cset), ensure_ascii=False))
        )
    conn.commit()
    print(f"   ✅ 都市計画決定GIS (A55) 合計 {row_id:,} ポリゴン完了: {counts_by_kind}")


def process_embankment(work_dir: Path, conn: sqlite3.Connection):
    print("\n⛰️ 9/13 大規模盛土造成地 (A54) を処理中...")
    zip_path = work_dir / "a54.zip"
    download_file(DATA_SOURCES["embankment_a54"]["url"], zip_path)

    cur = conn.cursor()
    row_id = 0
    with zipfile.ZipFile(zip_path, "r") as z:
        for member in sorted(z.namelist()):
            if not member.lower().endswith(".geojson"):
                continue
            for feat in _read_geojson_features_from_zip(z, member):
                geom_dict = feat.get("geometry")
                if not geom_dict:
                    continue
                props = feat.get("properties") or {}
                fill_code = str(props.get("A54_001") or "9").strip()
                city_code = str(props.get("A54_004") or "").strip()
                fill_number = str(props.get("A54_006") or "").strip()

                raw_geom = shape(geom_dict)
                for poly in _extract_polygons(raw_geom):
                    row_id += 1
                    bounds = poly.bounds
                    cur.execute(
                        "INSERT INTO embankment (id, fill_code, fill_number, city_code, geom_wkb) VALUES (?, ?, ?, ?, ?)",
                        (row_id, fill_code, fill_number, city_code, poly.wkb)
                    )
                    cur.execute(
                        "INSERT INTO embankment_idx (id, minX, maxX, minY, maxY) VALUES (?, ?, ?, ?, ?)",
                        (row_id, bounds[0], bounds[2], bounds[1], bounds[3])
                    )
    if row_id == 0:
        raise RuntimeError("大規模盛土造成地 (A54) の有効ポリゴンが 0 件です（Fail-Closed）")
    conn.commit()
    print(f"   ✅ 大規模盛土造成地 (A54) {row_id:,} ポリゴン完了")


def process_coastal_inundation(work_dir: Path, conn: sqlite3.Connection):
    print("\n🌊 10/13 高潮・津波浸水想定区域 (A49 / A40) を処理中...")
    a49_zip = work_dir / "a49.zip"
    a40_zip = work_dir / "a40.zip"
    download_file(DATA_SOURCES["storm_surge_a49"]["url"], a49_zip)
    download_file(DATA_SOURCES["tsunami_a40"]["url"], a40_zip)

    cur = conn.cursor()
    row_id = 0
    counts = {"storm_surge": 0, "tsunami": 0}

    for kind, zpath, scenario_key, prop_key in [
        ("storm_surge", a49_zip, "A49-20_tokyo_max", "A49_003"),
        ("tsunami", a40_zip, "A40-23_tokyo_max", "A40_003"),
    ]:
        with zipfile.ZipFile(zpath, "r") as z:
            for member in sorted(z.namelist()):
                if not member.lower().endswith(".geojson"):
                    continue
                for feat in _read_geojson_features_from_zip(z, member):
                    geom_dict = feat.get("geometry")
                    if not geom_dict:
                        continue
                    props = feat.get("properties") or {}
                    depth_raw = str(props.get(prop_key) or "").strip()
                    dmin, dmax = COASTAL_DEPTH_PARSE_MAP.get(depth_raw, (None, None))

                    raw_geom = shape(geom_dict)
                    for poly in _extract_polygons(raw_geom):
                        row_id += 1
                        bounds = poly.bounds
                        cur.execute(
                            "INSERT INTO coastal_inundation (id, kind, scenario_key, depth_raw, depth_min_m, depth_max_m, geom_wkb) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (row_id, kind, scenario_key, depth_raw, dmin, dmax, poly.wkb)
                        )
                        cur.execute(
                            "INSERT INTO coastal_inundation_idx (id, minX, maxX, minY, maxY) VALUES (?, ?, ?, ?, ?)",
                            (row_id, bounds[0], bounds[2], bounds[1], bounds[3])
                        )
                        counts[kind] += 1
    if counts["storm_surge"] == 0 or counts["tsunami"] == 0:
        raise RuntimeError(f"沿岸ハザードの必須サブレイヤーが欠落しています: {counts}（Fail-Closed）")
    conn.commit()
    print(f"   ✅ 沿岸ハザード (高潮: {counts['storm_surge']:,} / 津波: {counts['tsunami']:,}) 合計 {row_id:,} ポリゴン完了")


def process_population_mesh(work_dir: Path, conn: sqlite3.Connection) -> Set[str]:
    print("\n📈 11/13 将来推計人口 250mメッシュ (R6推計 / 2020-2050) を処理中...")
    zip_path = work_dir / "mesh250r6.zip"
    download_file(DATA_SOURCES["population_mesh250r6"]["url"], zip_path)

    with zipfile.ZipFile(zip_path, "r") as z:
        dbf_members = [m for m in z.namelist() if m.lower().endswith(".dbf")]
        if not dbf_members:
            raise RuntimeError("将来推計人口ZIP内に .dbf ファイルが見つかりません（Fail-Closed）")
        dbf_info = z.getinfo(dbf_members[0])
        if dbf_info.file_size > MAX_UNCOMPRESSED_GEOJSON_BYTES:
            raise RuntimeError(f"DBF size exceeds safety limit: {dbf_info.file_size} bytes")
        dbf_bytes = z.read(dbf_members[0])

    numrec, lenheader, lenrecord = struct.unpack("<4xIHH20x", dbf_bytes[:32])
    fields = []
    offset = 32
    while dbf_bytes[offset] != 0x0D:
        name = dbf_bytes[offset:offset + 11].split(b"\x00", 1)[0].decode("ascii", errors="replace")
        ftype = chr(dbf_bytes[offset + 11])
        flen = dbf_bytes[offset + 16]
        fdec = dbf_bytes[offset + 17]
        fields.append((name, ftype, flen, fdec))
        offset += 32

    cur = conn.cursor()
    inserted = 0
    prefixes6: Set[str] = set()
    pos = lenheader

    for _ in range(numrec):
        rec = dbf_bytes[pos:pos + lenrecord]
        pos += lenrecord
        if rec[0:1] == b"*":
            continue
        row: Dict[str, str] = {}
        fpos = 1
        for name, _, flen, _ in fields:
            row[name] = rec[fpos:fpos + flen].decode("shift_jis", errors="replace").strip()
            fpos += flen

        mesh_id = row.get("MESH_ID", "")
        if len(mesh_id) != 10:
            continue
        prefixes6.add(mesh_id[:6])
        shicode = row.get("SHICODE", "")

        try:
            p2020 = float(row["PTN_2020"]) if row.get("PTN_2020", "") != "" else None
        except ValueError:
            p2020 = None
        try:
            p2050 = float(row["PTN_2050"]) if row.get("PTN_2050", "") != "" else None
        except ValueError:
            p2050 = None

        disc2020 = row.get("HITOKU2020", "") or ("aggregated:" + row["GASSAN2020"] if row.get("GASSAN2020") else "normal")
        disc2050 = row.get("HITOKU2050", "") or ("aggregated:" + row["GASSAN2050"] if row.get("GASSAN2050") else "normal")

        cur.execute(
            "INSERT OR REPLACE INTO population_mesh (mesh_code, city_code, population_2020, population_2050, disclosure_2020, disclosure_2050) VALUES (?, ?, ?, ?, ?, ?)",
            (mesh_id, shicode, p2020, p2050, disc2020, disc2050)
        )
        inserted += 1

    if inserted == 0:
        raise RuntimeError("将来推計人口250mメッシュの投入件数が 0 件です（Fail-Closed）")
    conn.commit()
    print(f"   ✅ 将来推計人口250mメッシュ {inserted:,} メッシュ完了 (2次メッシュブロック数: {len(prefixes6)})")
    return prefixes6


MAX_TOKYO_MESH_ROWS = 100_000


def _resolve_tokyo_wm2020_csv(work_dir: Path, prefixes6: Set[str]) -> Path:
    """
    東京都対象の2次メッシュ領域のみに事前分割された wm2020_tokyo.csv を検証・返却する。
    広域・全国データの全件走査ループを禁止（Fail-Closed）し、対象プレフィックスが空の場合や
    東京都分割済みCSVが未配置の場合は即座に停止する。
    """
    if not prefixes6:
        raise RuntimeError("対象地域の2次メッシュプレフィックス集合 (prefixes6) が空です（Fail-Closed）")

    candidates = [
        work_dir / "wm2020_tokyo.csv",
        Path(__file__).resolve().parent.parent / "data" / "wm2020_tokyo.csv",
    ]
    for p in candidates:
        if p.exists() and p.stat().st_size > 0:
            print(f"   ⏩ 東京都事前分割データ利用: {p}")
            return p

    raise FileNotFoundError(
        "東京都向けに事前分割された微地形区分CSV (wm2020_tokyo.csv) が見つかりません。"
        "広域・全件走査防止規律により全国アーカイブの全行ループは禁止されています。"
    )


def process_liquefaction_mesh(work_dir: Path, conn: sqlite3.Connection, prefixes6: Set[str]):
    print("\n🧱 12/13 液状化発生傾向・微地形区分 250mメッシュ (WM2020 / 国交省都市局基準) を処理中...")
    tokyo_csv_path = _resolve_tokyo_wm2020_csv(work_dir, prefixes6)

    cur = conn.cursor()
    inserted = 0
    batch = []

    with open(tokyo_csv_path, "r", encoding="ascii", errors="ignore") as f:
        for idx, line in enumerate(f, 1):
            if idx > MAX_TOKYO_MESH_ROWS:
                raise RuntimeError(f"Tokyo liquefaction mesh row limit exceeded ({MAX_TOKYO_MESH_ROWS})")
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            mesh_code = line[:10]
            if mesh_code[:6] not in prefixes6:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            try:
                jcode = int(parts[1])
            except ValueError:
                jcode = -1

            avs_note = f" (AVS30={parts[2]}m/s)" if len(parts) >= 3 and parts[2] else ""
            if jcode in JCODE_LIQUEFACTION_MAP:
                lname, tlabel, rscore, assess, base_note = JCODE_LIQUEFACTION_MAP[jcode]
                note = f"{base_note}{avs_note}"
            else:
                lname, tlabel, rscore, assess, note = (
                    f"不明区分({jcode})",
                    "判定不能",
                    None,
                    "unknown_code",
                    f"未知の微地形区分コード: {jcode}"
                )

            batch.append((mesh_code, lname, tlabel, rscore, assess, note))
            inserted += 1
            if len(batch) >= 10000:
                cur.executemany(
                    "INSERT OR REPLACE INTO liquefaction_mesh (mesh_code, landform_name, tendency_label, relative_score, assessment, note) VALUES (?, ?, ?, ?, ?, ?)",
                    batch
                )
                batch.clear()

    if batch:
        cur.executemany(
            "INSERT OR REPLACE INTO liquefaction_mesh (mesh_code, landform_name, tendency_label, relative_score, assessment, note) VALUES (?, ?, ?, ?, ?, ?)",
            batch
        )
    if inserted == 0:
        raise RuntimeError("液状化発生傾向250mメッシュの投入件数が 0 件です（Fail-Closed）")
    conn.commit()
    print(f"   ✅ 液状化発生傾向・微地形区分 250mメッシュ {inserted:,} メッシュ完了")


def save_metadata(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS dataset_meta (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_tables = [
        "zoning", "land_price", "school_district", "flood_risk",
        "landslide_risk", "medical_facility", "nursery_facility",
        "planning_zone", "embankment", "coastal_inundation",
        "liquefaction_mesh", "population_mesh"
    ]
    layer_counts = {}
    for t in all_tables:
        try:
            cnt = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            layer_counts[t] = cnt
        except sqlite3.OperationalError:
            layer_counts[t] = 0

    meta_pairs = [
        ("built_at", now_str),
        ("crs", "EPSG:4326 (WGS84)"),
        ("coverage", "東京都 (Prefecture 13)"),
        ("layers", ", ".join(all_tables)),
        ("layer_counts_json", json.dumps(layer_counts, ensure_ascii=False)),
    ]
    for k, v in meta_pairs:
        cur.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (k, v))
        cur.execute("INSERT OR REPLACE INTO dataset_meta (key, value, updated_at) VALUES (?, ?, ?)", (k, v, now_str))
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Build complete tokyo_spatial.sqlite (13 layers)")
    parser.add_argument("--output", default="data/tokyo_spatial.sqlite", help="出力先SQLiteパス")
    parser.add_argument("--work-dir", default="/tmp/sorai_spatial_work", help="作業用一時ディレクトリ")
    parser.add_argument(
        "--append-6layers",
        action="store_true",
        help="既存の基本7系統DBを保持したまま、追加6系統をアトミックに構築・統合する"
    )
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = out_path.with_name(f"{out_path.name}.lock")
    tmp_path = out_path.with_name(f"{out_path.stem}.{os.getpid()}.sqlite.tmp")

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if tmp_path.exists():
                tmp_path.unlink()

            if args.append_6layers and out_path.exists():
                print("==================================================")
                print("🚀 SorAi 物件診断用 東京都空間DB (追加6系統 ➔ 全13系統) アトミック統合ビルド開始")
                print(f"📦 対象DB: {out_path.resolve()}")
                print("==================================================")
                conn = sqlite3.connect(str(tmp_path))
                try:
                    with sqlite3.connect(str(out_path)) as src_conn:
                        src_conn.backup(conn)
                    ensure_6layers_schema(conn)
                    process_planning_zones(work_dir, conn)
                    process_embankment(work_dir, conn)
                    process_coastal_inundation(work_dir, conn)
                    prefixes6 = process_population_mesh(work_dir, conn)
                    process_liquefaction_mesh(work_dir, conn, prefixes6)
                    save_metadata(conn)

                    chk = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    if chk != "ok":
                        raise RuntimeError(f"SQLite integrity_check failed: {chk}")
                finally:
                    conn.close()

                tmp_path.replace(out_path)
                db_size_mb = out_path.stat().st_size / (1024 * 1024)
                print(f"\n🎉 全13系統の統合ビルド・整合性検証（integrity_check=ok）成功！")
                print(f"📊 データベースサイズ: {db_size_mb:.2f} MB")
                return

            print("==================================================")
            print("🚀 SorAi 物件診断用 東京都空間DB (全13系統) フルビルド開始")
            print(f"📦 出力先: {out_path.resolve()}")
            print("==================================================")

            import geopandas as gpd  # noqa: F401
            globals()["gpd"] = gpd

            conn = init_database(tmp_path)
            try:
                process_zoning(work_dir, conn)
                process_land_price(work_dir, conn)
                process_schools(work_dir, conn)
                process_flood(work_dir, conn)
                process_landslide(work_dir, conn)
                process_medical(work_dir, conn)
                process_nursery(work_dir, conn)
                ensure_6layers_schema(conn)
                process_planning_zones(work_dir, conn)
                process_embankment(work_dir, conn)
                process_coastal_inundation(work_dir, conn)
                prefixes6 = process_population_mesh(work_dir, conn)
                process_liquefaction_mesh(work_dir, conn, prefixes6)
                save_metadata(conn)

                chk = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if chk != "ok":
                    raise RuntimeError(f"SQLite integrity_check failed: {chk}")
            finally:
                conn.close()

            tmp_path.replace(out_path)
            print("\n🎉 全13系統のフルビルド成功！")
            db_size_mb = out_path.stat().st_size / (1024 * 1024)
            print(f"📊 データベースサイズ: {db_size_mb:.2f} MB")
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    main()


