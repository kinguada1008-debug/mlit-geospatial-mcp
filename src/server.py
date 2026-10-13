import json
import logging
import uuid

"""
MCPサーバーのエントリポイント。
ツールのリスト取得やツール呼び出しのハンドラを提供する。
"""


import anyio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from request_processor.handler import handle_request
from tools import API_SPECS, TOOLS
from utils.payload import build_payload

# ロガー設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


server = Server("mlit-geospatial-mcp")


LOOKUP_TOKYO_SPATIAL_TOOL = types.Tool(
    name="lookup_tokyo_spatial",
    description=(
        "【超高速ローカル空間検索】外部API通信ゼロ（所要時間1〜3ミリ秒）。\n"
        "東京都の指定座標 (lat, lon) から、物件カルテ・SORAI診断レポートに必要な公的オープンデータ全7系統を一撃で判定して即答します。\n"
        "1. 用途地域（建蔽率・容積率）\n"
        "2. 地価公示標準地価格（最新相場・駅距離）\n"
        "3. 小学校区・中学校区（学校名・所在地・自治体）\n"
        "4. 洪水浸水想定区域（最大浸水深ランク・河川名）\n"
        "5. 土砂災害警戒区域（急傾斜等・イエロー/レッド区域判定）\n"
        "6. 近傍医療機関（半径1.2km・標榜科目・救急指定）\n"
        "7. 近傍保育施設（半径1.0km）\n"
        "※境界線上や複数用途地域にまたがる場合は ambiguous として複数候補を安全に返却します。"
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "lat": {
                "type": "number",
                "description": "対象地点の緯度（10進法、小数。例: 35.662846）",
            },
            "lon": {
                "type": "number",
                "description": "対象地点の経度（10進法、小数。例: 139.718817）",
            },
            "comprehensive": {
                "type": "boolean",
                "description": "Trueの場合、追加6系統（防火・液状化・盛土・高潮津波・将来人口推計・地区計画）を含めた全13系統の総合カルテを返却します（デフォルト: False）。",
            },
        },
        "required": ["lat", "lon"],
    },
)


@server.list_tools()
async def handle_list_tools():
    """
    利用可能なツール一覧を取得する。

    Returns:
        TOOLS:ツール定義のリスト
    """
    return [LOOKUP_TOKYO_SPATIAL_TOOL] + TOOLS


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """
    MCPから呼び出されたtoolを実行する。
    """
    rid = uuid.uuid4().hex
    logger.info(f"Tool called: {name} (request_id: {rid})")

    if name == "lookup_tokyo_spatial":
        try:
            from spatial_query import TokyoSpatialDB
        except ImportError:
            from src.spatial_query import TokyoSpatialDB

        lat = float(arguments["lat"])
        lon = float(arguments["lon"])
        is_comprehensive = bool(arguments.get("comprehensive", False))
        db = TokyoSpatialDB()
        try:
            if is_comprehensive:
                result = db.lookup_comprehensive_13layers(lat, lon)
            else:
                result = db.lookup_carte_7layers(lat, lon)
        finally:
            db.close()

        return [
            types.TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2),
            )
        ]

    spec = API_SPECS[name]
    payload = build_payload(
        spec=spec,
        args=arguments,
    )
    logger.info(f"payload:{payload}")
    result = await handle_request(payload)
    return [
        types.TextContent(
            type="text",
            text=json.dumps(result, ensure_ascii=False, indent=2),
        )
    ]


async def _main() -> None:
    """
    MCPサーバーのメイン処理。
    イベントループを実行する。
    """
    async with stdio_server() as (read, write):
        caps = server.get_capabilities(
            notification_options=NotificationOptions(), experimental_capabilities={}
        )

        init_opts = InitializationOptions(
            server_name="mlit-geospatial-mcp", server_version="0.1.0", capabilities=caps
        )

        logger.info("MCP server starting...")
        await server.run(read, write, init_opts)


if __name__ == "__main__":
    anyio.run(_main)
