#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
1688订单支付API服务

API端点:
1. POST /api/pay-url      - 获取支付链接
2. GET  /api/pay-status/{order_id} - 获取支付状态

启动方式:
    uvicorn app:app --host 0.0.0.0 --port 8000
"""

import time
import json
import hmac
import hashlib
import re
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# 并发查询支付渠道的最大线程数
MAX_PAY_WAY_WORKERS = 10

# ==================== API配置 ====================
API_CONFIG = {
    'app_key': '2019459',
    'secret': 'XgepZVNu5iz',
    'access_token': '1c87e807-03ff-4d1e-a08f-72746cb06c64'
}

# ==================== FastAPI 应用 ====================
app = FastAPI(
    title="1688订单支付API",
    description="提供1688订单支付链接获取和支付状态查询服务",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== 请求/响应模型 ====================
class PayUrlRequest(BaseModel):
    order_ids: List[str] = Field(..., description="订单ID列表")


class PayUrlResponse(BaseModel):
    success: bool
    pay_urls: List[str] = []
    pay_url: Optional[str] = None
    success_order_ids: List[str] = []
    failed_order_ids: List[str] = []
    success_count: int = 0
    failed_count: int = 0
    total_count: int = 0
    error_msg: Optional[str] = None


class PayStatusResponse(BaseModel):
    success: bool
    order_id: str
    pay_status: Optional[str] = None
    error_msg: Optional[str] = None


# ==================== 辅助函数 ====================
def generate_signature(url_path: str, params: dict, secret: str) -> str:
    """生成HMAC-SHA1签名"""
    sorted_params = sorted(params.items())
    query_string = ''.join(f"{k}{v}" for k, v in sorted_params)
    sign_string = url_path + query_string
    signature = hmac.new(
        secret.encode('utf-8'),
        sign_string.encode('utf-8'),
        hashlib.sha1
    ).hexdigest().upper()
    return signature


def is_api_success(result: dict) -> tuple:
    """判断API调用是否成功"""
    success_value = result.get('success')
    if success_value == True or success_value == 'true':
        pay_url = result.get('payUrl') or (
            result.get('result', {}).get('url') if isinstance(result.get('result'), dict) else None)
        return True, pay_url

    if result.get('payUrl'):
        return True, result.get('payUrl')

    if isinstance(result.get('result'), dict) and result['result'].get('url'):
        return True, result['result']['url']

    return False, None


def extract_failed_order_ids(error_msg: str) -> List[str]:
    """从错误消息中提取失败的订单ID列表"""
    match = re.search(r'\[(.*?)\]', error_msg)
    if match:
        order_ids_str = match.group(1)
        failed_ids = [order_id.strip() for order_id in order_ids_str.split(',')]
        return failed_ids
    return []


# ==================== 核心API函数 ====================
def get_order_details(order_id: str) -> dict:
    """获取订单详情"""
    app_key = API_CONFIG['app_key']
    secret = API_CONFIG['secret']
    access_token = API_CONFIG['access_token']

    api_url = f'https://gw.open.1688.com/openapi/param2/1/com.alibaba.trade/alibaba.trade.get.buyerView/{app_key}'
    url_path = f'param2/1/com.alibaba.trade/alibaba.trade.get.buyerView/{app_key}'

    try:
        params = {
            'webSite': '1688',
            'orderId': order_id,
            'includeFields': 'GuaranteesTerms,NativeLogistics,RateDetail,OrderInvoice',
            'attributeKeys': '[]',
            'access_token': access_token,
            '_aop_timestamp': str(int(time.time() * 1000)),
        }

        params['_aop_signature'] = generate_signature(url_path, params, secret)

        response = requests.post(api_url, data=params, timeout=10)
        return response.json()

    except Exception as e:
        return {'error': str(e), 'success': False}


def query_pay_way(order_id: str) -> dict:
    """查询订单可支持的支付渠道"""
    app_key = API_CONFIG['app_key']
    secret = API_CONFIG['secret']
    access_token = API_CONFIG['access_token']

    url_path = f'param2/1/com.alibaba.trade/alibaba.trade.payWay.query/{app_key}'
    api_url = f'https://gw.open.1688.com/openapi/{url_path}'

    try:
        params = {
            'orderId': order_id,
            'access_token': access_token,
            '_aop_timestamp': str(int(time.time() * 1000)),
        }
        params['_aop_signature'] = generate_signature(url_path, params, secret)

        response = requests.post(api_url, data=params, timeout=10)
        return response.json()
    except Exception as e:
        return {'error': str(e), 'success': False}


def _check_single_order_pay_way(order_id: str) -> tuple:
    """检查单个订单是否支持跨境宝支付渠道，返回 (order_id, is_supported)"""
    result = query_pay_way(order_id)
    success_value = result.get('success')

    if success_value == True or success_value == 'true':
        channels = result.get('resultList', {}).get('channels', [])
        has_crossborder = any(ch.get('code') == 20 for ch in channels)
        return order_id, has_crossborder

    return order_id, False


def filter_crossborder_orders(order_ids: List[str]) -> tuple:
    """
    并发检查每个订单是否支持跨境宝(code=20)支付渠道
    使用线程池并发查询，最多同时 MAX_PAY_WAY_WORKERS 个请求
    返回: (supported_orders, unsupported_orders)
    """
    supported = []
    unsupported = []

    with ThreadPoolExecutor(max_workers=MAX_PAY_WAY_WORKERS) as executor:
        futures = {
            executor.submit(_check_single_order_pay_way, order_id): order_id
            for order_id in order_ids
        }

        for future in as_completed(futures):
            order_id, is_supported = future.result()
            if is_supported:
                supported.append(order_id)
            else:
                unsupported.append(order_id)

    return supported, unsupported


def get_crossborder_pay_url(order_id_list: List[str]) -> dict:
    """获取跨境宝支付链接"""
    app_key = API_CONFIG['app_key']
    secret = API_CONFIG['secret']
    access_token = API_CONFIG['access_token']

    api_url = f'https://gw.open.1688.com/openapi/param2/1/com.alibaba.trade/alibaba.crossBorderPay.url.get/{app_key}'
    url_path = f'param2/1/com.alibaba.trade/alibaba.crossBorderPay.url.get/{app_key}'

    max_retries = 3
    retry_count = 0

    while retry_count < max_retries:
        try:
            params = {
                'orderIdList': json.dumps(order_id_list),
                'access_token': access_token,
                '_aop_timestamp': str(int(time.time() * 1000)),
            }

            params['_aop_signature'] = generate_signature(url_path, params, secret)

            headers = {
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
            }

            response = requests.post(
                api_url,
                data=params,
                headers=headers,
                timeout=15
            )
            return response.json()

        except Exception as e:
            retry_count += 1
            if retry_count >= max_retries:
                return {"error": str(e), "success": False}
            time.sleep(1)

    return {"error": "所有重试均失败", "success": False}


# ==================== API 端点 ====================
def _process_pay_url_batch(batch_orders: List[str]) -> dict:
    """
    处理单批次(最多30个)订单的支付链接获取
    返回: {'pay_url': str|None, 'success_ids': [], 'failed_ids': [], 'error_msg': str|None}
    """
    result = get_crossborder_pay_url(batch_orders)

    is_success, pay_url = is_api_success(result)
    if is_success:
        return {'pay_url': pay_url, 'success_ids': list(batch_orders), 'failed_ids': [], 'error_msg': None}

    # 有错误，尝试从错误消息中提取失败的订单
    api_error_msg = result.get('errorMsg', '')
    if api_error_msg:
        api_failed_ids = extract_failed_order_ids(api_error_msg)
        if api_failed_ids:
            api_success_ids = [oid for oid in batch_orders if oid not in api_failed_ids]
            if api_success_ids:
                retry_result = get_crossborder_pay_url(api_success_ids)
                retry_success, retry_pay_url = is_api_success(retry_result)
                if retry_success:
                    return {'pay_url': retry_pay_url, 'success_ids': api_success_ids, 'failed_ids': api_failed_ids, 'error_msg': api_error_msg}
            return {'pay_url': None, 'success_ids': [], 'failed_ids': list(batch_orders), 'error_msg': api_error_msg}
        return {'pay_url': None, 'success_ids': [], 'failed_ids': list(batch_orders), 'error_msg': api_error_msg}

    other_error = result.get('error', '未知错误')
    return {'pay_url': None, 'success_ids': [], 'failed_ids': list(batch_orders), 'error_msg': other_error}


@app.post("/api/pay-url", response_model=PayUrlResponse, summary="获取支付链接")
async def api_get_pay_url(request: PayUrlRequest):
    """
    获取1688跨境宝支付链接

    - **order_ids**: 订单ID列表，支持超过30个订单（自动分批处理）

    返回支付链接及订单处理结果
    """
    order_ids = [str(oid).strip() for oid in request.order_ids if str(oid).strip()]

    if not order_ids:
        raise HTTPException(status_code=400, detail="订单ID列表不能为空")

    # ===== 第一步: 并发检查每个订单是否支持跨境宝支付渠道 =====
    supported_orders, unsupported_orders = filter_crossborder_orders(order_ids)

    error_messages = []
    if unsupported_orders:
        error_messages.append(f"订单不支持跨境宝支付渠道: [{', '.join(unsupported_orders)}]")

    if not supported_orders:
        return PayUrlResponse(
            success=False,
            error_msg='; '.join(error_messages),
            failed_order_ids=unsupported_orders,
            failed_count=len(unsupported_orders),
            total_count=len(order_ids),
            success_count=0,
            success_order_ids=[]
        )

    # ===== 第二步: 按每30个一批分组，获取支付链接 =====
    BATCH_SIZE = 30
    batches = [supported_orders[i:i + BATCH_SIZE] for i in range(0, len(supported_orders), BATCH_SIZE)]

    all_pay_urls = []
    all_success_ids = []
    all_api_failed_ids = []

    for batch in batches:
        batch_result = _process_pay_url_batch(batch)
        if batch_result['pay_url']:
            all_pay_urls.append(batch_result['pay_url'])
        all_success_ids.extend(batch_result['success_ids'])
        all_api_failed_ids.extend(batch_result['failed_ids'])
        if batch_result['error_msg']:
            error_messages.append(batch_result['error_msg'])

    all_failed_ids = unsupported_orders + all_api_failed_ids

    if all_success_ids:
        return PayUrlResponse(
            success=True,
            pay_urls=all_pay_urls,
            pay_url=all_pay_urls[0] if len(all_pay_urls) == 1 else None,
            success_order_ids=all_success_ids,
            success_count=len(all_success_ids),
            failed_order_ids=all_failed_ids,
            failed_count=len(all_failed_ids),
            total_count=len(order_ids),
            error_msg='; '.join(error_messages) if error_messages else None
        )

    return PayUrlResponse(
        success=False,
        error_msg='; '.join(error_messages) if error_messages else '未知错误',
        failed_order_ids=all_failed_ids,
        failed_count=len(all_failed_ids),
        total_count=len(order_ids),
        success_count=0,
        success_order_ids=[]
    )


@app.get("/api/pay-status/{order_id}", response_model=PayStatusResponse, summary="获取支付状态")
async def api_get_pay_status(order_id: str):
    """
    获取订单支付状态

    - **order_id**: 订单ID

    返回订单的支付状态描述，如 "已付款"、"等待买家付款" 等
    """
    if not order_id or not order_id.strip():
        raise HTTPException(status_code=400, detail="订单ID不能为空")

    order_id = order_id.strip()
    result = get_order_details(order_id)

    if result.get('success') == 'true' or result.get('success') == True:
        # 从 productItems[0].statusStr 获取订单状态描述
        product_items = result.get('result', {}).get('productItems', [])
        if product_items and isinstance(product_items, list):
            status_str = product_items[0].get('statusStr', '')
            if status_str:
                return PayStatusResponse(
                    success=True,
                    order_id=order_id,
                    pay_status=status_str
                )
        return PayStatusResponse(
            success=True,
            order_id=order_id,
            pay_status="未知状态",
            error_msg="无法获取订单状态详情"
        )

    error_msg = result.get('errorMsg') or result.get('error') or '获取订单详情失败'
    return PayStatusResponse(
        success=False,
        order_id=order_id,
        error_msg=error_msg
    )


@app.get("/", summary="健康检查")
async def health_check():
    """API健康检查"""
    return {"status": "ok", "service": "1688订单支付API"}


# ==================== 启动入口 ====================
if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
