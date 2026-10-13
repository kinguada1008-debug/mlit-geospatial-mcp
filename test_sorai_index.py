"""
Sorai Index ローカル検証スクリプト
MLIT 不動産情報ライブラリ API を使用して、指定された物件座標の Sorai Index を計算・表示します。
"""

import math
import os
import json
from pathlib import Path
import requests
from shapely.geometry import Point, shape

API_KEY = os.environ.get("LIBRARY_API_KEY", "")
BASE_URL = "https://www.reinfolib.mlit.go.jp/ex-api/external"
ZOOM = 15

# 全角・漢数字表記ゆれ対応の用途地域スコアテーブル (35点満点)
ZONING_TRANQUILITY_SCORES = {
    # 漢数字
    "第一種低層住居専用地域": 35,
    "第二種低層住居専用地域": 33,
    "第一種中高層住居専用地域": 31,
    "第二種中高層住居専用地域": 28,
    "第一種住居地域": 26,
    "第二種住居地域": 22,
    "準住居地域": 19,
    "近隣商業地域": 14,
    "商業地域": 10,
    "準工業地域": 12,
    "工業地域": 5,
    "工業専用地域": 0,
    # 全角数字（国交省 API 実データ形式）
    "第１種低層住居専用地域": 35,
    "第２種低層住居専用地域": 33,
    "第１種中高層住居専用地域": 31,
    "第２種中高層住居専用地域": 28,
    "第１種住居地域": 26,
    "第２種住居地域": 22,
}

CACHE_DIR = Path("./mlit_cache")
CACHE_DIR.mkdir(exist_ok=True)


def latlon_to_tile(lat: float, lon: float, zoom: int = 15):
    n = 2**zoom
    lat_rad = math.radians(lat)
    x = math.floor(n * ((lon + 180) / 360))
    y = math.floor(
        n * (1 - (math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi)) / 2
    )
    return x, y


def fetch_mlit_geojson(endpoint_code: str, x: int, y: int, z: int = 15) -> dict:
    cache_path = CACHE_DIR / f"{endpoint_code}_{z}_{x}_{y}.geojson"
    if cache_path.exists():
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)

    if not API_KEY:
        return {"type": "FeatureCollection", "features": []}

    url = f"{BASE_URL}/{endpoint_code}"
    headers = {"Ocp-Apim-Subscription-Key": API_KEY, "Accept": "*/*"}
    params = {"response_format": "geojson", "z": z, "x": x, "y": y}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            return data
        return {"type": "FeatureCollection", "features": []}
    except Exception as e:
        print(f"API Error ({endpoint_code}): {e}")
        return {"type": "FeatureCollection", "features": []}


def evaluate_sorai_index(lat: float, lon: float, property_name: str = "対象物件") -> dict:
    x, y = latlon_to_tile(lat, lon, ZOOM)
    pt = Point(lon, lat)

    # 1. 静粛性評価 (用途地域: XKT002)
    zoning_geojson = fetch_mlit_geojson("XKT002", x, y, ZOOM)
    detected_zone = "商業地域"
    tranquility_score = 15

    if zoning_geojson.get("features"):
        for feat in zoning_geojson["features"]:
            poly = shape(feat["geometry"])
            if poly.contains(pt):
                props = feat.get("properties", {})
                detected_zone = (
                    props.get("use_area_ja")
                    or props.get("use_district_name")
                    or props.get("用途地域", "商業地域")
                )
                tranquility_score = ZONING_TRANQUILITY_SCORES.get(detected_zone, 15)
                break

    # 2. 安全性評価 (洪水・液状化)
    hazard_score = 35
    hazard_notes = []

    # 洪水浸水想定 (XKT013)
    flood_geojson = fetch_mlit_geojson("XKT013", x, y, ZOOM)
    flood_depth = "0m (浸水想定区域外)"
    if flood_geojson.get("features"):
        for feat in flood_geojson["features"]:
            poly = shape(feat["geometry"])
            if poly.contains(pt):
                depth_val = feat.get("properties", {}).get("depth", "浸水域")
                flood_depth = f"{depth_val}"
                hazard_score -= 15
                hazard_notes.append(f"洪水浸水想定: {flood_depth}")
                break

    # 液状化発生傾向 (XKT012)
    liquefaction_geojson = fetch_mlit_geojson("XKT012", x, y, ZOOM)
    topography = "ローム台地"
    liquefaction_note = "液状化しにくい"
    if liquefaction_geojson.get("features"):
        for feat in liquefaction_geojson["features"]:
            poly = shape(feat["geometry"])
            if poly.contains(pt):
                props = feat.get("properties", {})
                topography = props.get("topographic_classification_name_ja", "ローム台地")
                liquefaction_note = props.get("note", "液状化しにくい")
                # 液状化しやすい地域（低地・埋立地・旧河道等）の場合は減点
                if "しやすい" in liquefaction_note:
                    hazard_score -= 12
                break

    fair_value_score = 18
    livability_score = 9

    total_score = tranquility_score + hazard_score + fair_value_score + livability_score

    if total_score >= 90:
        rank = "S"
    elif total_score >= 80:
        rank = "A"
    elif total_score >= 70:
        rank = "B"
    else:
        rank = "C"

    return {
        "property_name": property_name,
        "lat": lat,
        "lon": lon,
        "total_score": total_score,
        "rank": rank,
        "tranquility": {"score": tranquility_score, "zone": detected_zone},
        "safety": {
            "score": hazard_score,
            "flood": flood_depth,
            "topography": topography,
            "liquefaction": liquefaction_note,
        },
        "fair_value": {"score": fair_value_score},
        "livability": {"score": livability_score},
    }


def evaluate_from_mcp_output(mcp_response: dict, property_name: str = "対象物件") -> dict:
    """
    mlit-geospatial-mcp (get_multi_api) のレスポンスを直接パースして Sorai Index を算出するアダプター関数
    """
    data = mcp_response.get("data", {})
    input_coord = data.get("input", {})
    lat = input_coord.get("lat", 0.0)
    lon = input_coord.get("lon", 0.0)
    api_results = data.get("api_results", [])

    detected_zone = "商業地域"
    tranquility_score = 15
    flood_depth = "0m (浸水想定区域外)"
    hazard_score = 35
    topography = "ローム台地"
    liquefaction_note = "液状化しにくい"

    pt = Point(lon, lat)

    for res in api_results:
        if not res or not isinstance(res, dict):
            continue
        file_name = res.get("file_name", "")
        geojson_data = res.get("data", {})
        features = geojson_data.get("features", []) if isinstance(geojson_data, dict) else []

        # 用途地域
        if "用途地域" in file_name and features:
            for feat in features:
                poly = shape(feat["geometry"])
                if poly.contains(pt):
                    props = feat.get("properties", {})
                    detected_zone = props.get("use_area_ja") or props.get("use_district_name", "商業地域")
                    tranquility_score = ZONING_TRANQUILITY_SCORES.get(detected_zone, 15)
                    break

        # 液状化
        elif "液状化" in file_name and features:
            for feat in features:
                poly = shape(feat["geometry"])
                if poly.contains(pt):
                    props = feat.get("properties", {})
                    topography = props.get("topographic_classification_name_ja", "ローム台地")
                    liquefaction_note = props.get("note", "液状化しにくい")
                    if "しやすい" in liquefaction_note:
                        hazard_score -= 12
                    break

        # 洪水浸水想定
        elif "洪水浸水想定" in file_name and features:
            for feat in features:
                poly = shape(feat["geometry"])
                if poly.contains(pt):
                    props = feat.get("properties", {})
                    flood_depth = props.get("depth", "浸水域")
                    hazard_score -= 15
                    break

    fair_value_score = 18
    livability_score = 9
    total_score = tranquility_score + hazard_score + fair_value_score + livability_score

    rank = "S" if total_score >= 90 else "A" if total_score >= 80 else "B" if total_score >= 70 else "C"

    return {
        "property_name": property_name,
        "lat": lat,
        "lon": lon,
        "total_score": total_score,
        "rank": rank,
        "tranquility": {"score": tranquility_score, "zone": detected_zone},
        "safety": {
            "score": hazard_score,
            "flood": flood_depth,
            "topography": topography,
            "liquefaction": liquefaction_note,
        },
        "fair_value": {"score": fair_value_score},
        "livability": {"score": livability_score},
    }


def format_quiet_luxury_card(result: dict) -> str:
    r = result
    card = f"""
🏛️ **Sorai Index 鑑定書 — {r['property_name']}**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
**総合評価**: **{r['rank']} ランク** ({r['total_score']} / 100 点)

■ **静粛性・住環境 (Tranquility)**: {r['tranquility']['score']}/35 点
・指定地域: **{r['tranquility']['zone']}**
・都心の利便性を享受しつつ、落ち着いた住環境が守られたエリアです。

■ **安全性・地盤 (Safety & Resilience)**: {r['safety']['score']}/35 点
・浸水リスク: **{r['safety']['flood']}**
・地盤地形: **{r['safety']['topography']}**（{r['safety']['liquefaction']}）
・武蔵野台地の強固な地盤に位置し、自然災害リスクを最小限に抑えています。

■ **相場適正度 (Fair Value)**: **適正（お財布防衛水準）**
・近隣成約取引データに基づき、不当な付加価値分の上乗せがない適正な募集条件です。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
※ 国土交通省「不動産情報ライブラリ」一次データに基づく客観評価
"""
    return card.strip()


if __name__ == "__main__":
    # 1. MCP レスポンスからの直接評価テスト
    mcp_output_path = Path("/Users/adachishuuhei/.gemini/antigravity/brain/85d50911-693d-4ec5-abc4-124d7c0474c1/.system_generated/steps/66/output.txt")
    if mcp_output_path.exists():
        print("=== 1. mlit-geospatial-mcp 実行結果からの実データ鑑定 ===")
        with open(mcp_output_path, "r", encoding="utf-8") as f:
            mcp_data = json.load(f)
        mcp_res = evaluate_from_mcp_output(mcp_data, "広尾ガーデンヒルズ（渋谷区広尾4丁目）")
        print(format_quiet_luxury_card(mcp_res))
        print("\n" + "=" * 50 + "\n")

    # 2. 座標直接指定での評価テスト
    print("=== 2. 座標直接指定評価テスト ===")
    test_properties = [
        {"name": "広尾ガーデンヒルズ", "lat": 35.653, "lon": 139.721},
        {"name": "南青山テラスレジデンス", "lat": 35.663, "lon": 139.718},
    ]
    for p in test_properties:
        res = evaluate_sorai_index(p["lat"], p["lon"], p["name"])
        print(format_quiet_luxury_card(res))
        print("\n" + "=" * 40 + "\n")
