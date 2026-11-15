"""
MCPリクエストハンドラ。

このモジュールは、MCPから受け取ったpayloadを処理し、不動産ライブラリAPI呼び出しを行う。

Functions:
    handle_request(payload: dict) -> dict:
        MCPからのリクエストを処理し、結果を返す。
"""

import logging

from request_processor.models.api_models import RequestModel
from request_processor.service.geospatial_service import GeospatialService

logger = logging.getLogger(__name__)


async def handle_request(payload: dict) -> dict:
    """
    MCPから受け取ったpayloadを処理し、外部API呼び出しを行う。

    Args:
        payload (dict[str, Any]): MCPから渡されたpayload

    Returns:
        dict[str, Any]: 処理結果
            - status: "success" または "error"
            - data: 成功時はAPIレスポンス、失敗時はエラーメッセージ
    """

    logger.info("handle_request started")

    try:
        req = RequestModel(**payload)

        service_instance = GeospatialService()
        res = await service_instance.process_request(req)

        return {"status": "success", "data": res}

    except Exception as e:
        logger.error(f"処理エラー:{e}")
        return {"status": "error", "data": str(e)}
