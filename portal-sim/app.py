import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

CONFIG_PATH = os.environ.get('PORTAL_CONFIG', '/app/config/default.json')
SPEC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'openapi.yaml')
WRITE_METHODS = {'POST', 'PUT', 'DELETE'}
CENT = Decimal('0.01')
MAX_PRICE = Decimal('99999.99')

app = FastAPI(docs_url='/docs', redoc_url=None, openapi_url='/openapi.json')
lock = threading.Lock()
state = {}


def openapi_spec():
    if not app.openapi_schema:
        with open(SPEC_PATH, encoding='utf-8') as f:
            app.openapi_schema = yaml.safe_load(f)
    return app.openapi_schema


app.openapi = openapi_spec


class ApiError(Exception):
    def __init__(self, status, code, detail, headers=None):
        super().__init__(detail)
        self.status, self.code, self.detail, self.headers = status, code, detail, headers


def error_response(status, code, detail, headers=None):
    return JSONResponse({'error': code, 'detail': detail}, status_code=status, headers=headers)


def invalid(detail):
    return ApiError(422, 'validation', detail)


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def parse_price(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise invalid('price is required and must be a number or a numeric string')
    try:
        price = Decimal(str(value).strip())
    except InvalidOperation:
        price = Decimal('NaN')
    if not price.is_finite():
        raise invalid('price must be numeric')
    if price <= 0 or price > MAX_PRICE:
        raise invalid('price must be greater than 0 and at most 99999.99')
    if price != price.quantize(CENT):
        raise invalid('price must have at most 2 decimals')
    return str(price.quantize(CENT))


def create_listing(account, sku, title, price):
    listing_id = f"L{state['next_id']}"
    state['next_id'] += 1
    created = now()
    listing = {'listing_id': listing_id, 'account': account, 'sku': sku, 'title': title, 'price': price,
               'created_at': created, 'updated_at': created}
    state['listings'][listing_id] = listing
    return dict(listing)


def load_config(config=None):
    if config is None:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    failures = config.get('failures') or {}
    state.clear()
    state.update(config=config, failures=failures, listings={}, next_id=1, requests={}, writes={}, calls=[])
    state['accounts'] = {str(k): {'token': v['token'], 'expired': bool(v.get('expired'))} for k, v in config['accounts'].items()}
    state['transient_left'] = [rule.get('times', 1) for rule in failures.get('transient_rules', [])]
    state['timeout_left'] = [rule.get('times', 1) for rule in failures.get('accept_then_timeout', [])]
    for seed in config.get('seed_listings', []):
        create_listing(int(seed['account']), seed.get('sku'), seed['title'], parse_price(seed['price']))


def hits(counters, account, every):
    counters[account] = counters.get(account, 0) + 1
    return bool(every) and counters[account] % every == 0


def take(rules, left, method, account, sku):
    for i, rule in enumerate(rules):
        same_account = int(rule.get('account', account)) == account
        if left[i] > 0 and rule.get('method', method).upper() == method and same_account and rule.get('sku', sku) == sku:
            left[i] -= 1
            return True
    return False


def authenticate(account_key, header):
    account = state['accounts'].get(account_key)
    if account is None:
        raise ApiError(404, 'account_not_found', f'account {account_key} does not exist')
    scheme, _, token = header.partition(' ')
    if scheme.lower() != 'bearer' or token.strip() != account['token']:
        raise ApiError(401, 'invalid_token', 'missing or invalid bearer token')
    if account['expired']:
        raise ApiError(401, 'token_expired', 'the token has expired; renew it in the portal')
    return int(account_key)


def validate(body, method):
    if not isinstance(body, dict):
        raise invalid('body must be a JSON object')
    if method == 'POST' and (not isinstance(body.get('sku'), str) or not body['sku'].strip()):
        raise invalid('sku is required and must be a non-empty string')
    title = body.get('title')
    if not isinstance(title, str) or not title.strip():
        raise invalid('title is required and must be a non-empty string')
    max_length = state['config'].get('max_title_length', 80)
    if len(title) > max_length:
        raise invalid(f'title must be at most {max_length} characters')
    return title, parse_price(body.get('price'))


def list_page(account, raw_page):
    if not raw_page.isdigit() or int(raw_page) < 1:
        raise invalid('page must be an integer >= 1')
    page, size = int(raw_page), state['config'].get('page_size', 50)
    items = [dict(item) for item in state['listings'].values() if item['account'] == account]
    next_page = page + 1 if page * size < len(items) else None
    return {'items': items[(page - 1) * size:page * size], 'page': page, 'next_page': next_page}


def process(request, method, account_key, listing, sku, body):
    account = authenticate(account_key, request.headers.get('authorization', ''))
    failures = state['failures']
    if hits(state['requests'], account, failures.get('rate_limit_every')):
        raise ApiError(429, 'rate_limited', 'too many requests; retry after the Retry-After seconds', {'Retry-After': '1'})
    if method in WRITE_METHODS and (
        hits(state['writes'], account, failures.get('transient_every'))
        or take(failures.get('transient_rules', []), state['transient_left'], method, account, sku)
    ):
        raise ApiError(503, 'unavailable', 'the portal is temporarily unavailable; retry later')
    if method == 'GET':
        return 200, list_page(account, request.query_params.get('page', '1'))
    if method != 'DELETE':
        title, price = validate(body, method)
    if method == 'POST':
        status, content = 201, create_listing(account, body['sku'], title, price)
    elif listing is None:
        raise ApiError(404, 'listing_not_found', 'listing not found in this account')
    elif method == 'PUT':
        listing.update(title=title, price=price, updated_at=now())
        status, content = 200, dict(listing)
    else:
        del state['listings'][listing['listing_id']]
        status, content = 204, None
    if take(failures.get('accept_then_timeout', []), state['timeout_left'], method, account, sku):
        raise ApiError(504, 'gateway_timeout', 'the portal did not respond in time')
    return status, content


async def handle(request, method, account_key, listing_id=None):
    raw = await request.body()
    try:
        body = json.loads(raw) if raw.strip() else None
    except ValueError:
        body = None
    delay_ms = state['failures'].get('delay_ms')
    if delay_ms:
        await asyncio.sleep(delay_ms / 1000)
    with lock:
        listing = state['listings'].get(listing_id)
        if listing is not None and str(listing['account']) != account_key:
            listing = None
        if method == 'POST':
            sku = body.get('sku') if isinstance(body, dict) else None
        else:
            sku = listing['sku'] if listing is not None else None
        try:
            status, content = process(request, method, account_key, listing, sku, body)
            response = Response(status_code=status) if content is None else JSONResponse(content, status_code=status)
        except ApiError as e:
            response = error_response(e.status, e.code, e.detail, e.headers)
        account = int(account_key) if account_key.isdigit() else account_key
        state['calls'].append({'seq': len(state['calls']) + 1, 'method': method, 'path': request.url.path,
                               'account': account, 'sku': sku, 'status': response.status_code})
        return response


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    return error_response(exc.status_code, 'not_found' if exc.status_code == 404 else 'http_error', str(exc.detail))


@app.get('/accounts/{account}/listings')
async def get_listings(request: Request, account: str):
    return await handle(request, 'GET', account)


@app.post('/accounts/{account}/listings')
async def post_listing(request: Request, account: str):
    return await handle(request, 'POST', account)


@app.put('/accounts/{account}/listings/{listing_id}')
async def put_listing(request: Request, account: str, listing_id: str):
    return await handle(request, 'PUT', account, listing_id)


@app.delete('/accounts/{account}/listings/{listing_id}')
async def delete_listing(request: Request, account: str, listing_id: str):
    return await handle(request, 'DELETE', account, listing_id)


@app.get('/_admin/state')
async def admin_state():
    with lock:
        accounts = {key: {'expired': value['expired']} for key, value in state['accounts'].items()}
        return JSONResponse({'accounts': accounts, 'listings': [dict(item) for item in state['listings'].values()]})


@app.get('/_admin/calls')
async def admin_calls():
    with lock:
        return JSONResponse([dict(call) for call in state['calls']])


@app.post('/_admin/reset')
async def admin_reset(request: Request):
    raw = await request.body()
    with lock:
        load_config(json.loads(raw) if raw.strip() else None)
        return JSONResponse({'status': 'ok', 'listings': len(state['listings'])})


def set_expired(account_key, expired):
    with lock:
        if account_key not in state['accounts']:
            return error_response(404, 'account_not_found', f'account {account_key} does not exist')
        state['accounts'][account_key]['expired'] = expired
        return JSONResponse({'account': int(account_key), 'expired': expired})


@app.post('/_admin/accounts/{account}/renew')
async def admin_renew(account: str):
    return set_expired(account, False)


@app.post('/_admin/accounts/{account}/expire')
async def admin_expire(account: str):
    return set_expired(account, True)


load_config()
