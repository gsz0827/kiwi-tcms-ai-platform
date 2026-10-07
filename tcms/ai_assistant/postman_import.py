"""Bounded, offline Postman JSON request conversion; never execute uploaded code."""
import json
import re
from urllib.parse import parse_qsl, urlsplit
from .api_validation import SENSITIVE, VARIABLE, validate_case

MAX_BYTES = 2 * 1024 * 1024
SCHEMAS = {f'https://{host}/{prefix}collection/v{version}.0/collection.json'
           for host, prefix in [('schema.getpostman.com', 'json/'), ('schema.postman.com', 'json/'),
                                ('schema.getpostman.com', ''), ('schema.postman.com', '')]
           for version in ('2.0', '2.1')}


def strict_json(raw):
    def pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise ValueError('JSON 含重复字段，无法可靠转换。')
            output[key] = value
        return output
    def invalid(_):
        raise ValueError('JSON 不支持 NaN 或 Infinity。')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def no_literal_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if SENSITIVE.search(str(key)) and item not in ('', None):
                if not isinstance(item, str) or not re.fullmatch(
                    r'(?:Bearer\s+)?\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}', item, re.I
                ):
                    raise ValueError('含明文凭据；请改为变量引用，并在环境中配置认证。')
            no_literal_secrets(item)
    elif isinstance(value, list):
        for item in value:
            no_literal_secrets(item)


def key_values(items, label):
    if not isinstance(items, list):
        raise ValueError(label + '格式不正确。')
    values = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(label + '格式不正确。')
        if item.get('disabled'):
            continue
        key, value = item.get('key'), item.get('value', '')
        if not isinstance(key, str) or not key or not isinstance(value, str):
            raise ValueError(label + '名称和值必须为文本。')
        comparison = key.lower() if label == '请求头' else key
        if any((existing.lower() if label == '请求头' else existing) == comparison for existing in values):
            raise ValueError(label + '包含重复名称，需先人工整理。')
        values[key] = value
    no_literal_secrets(values)
    return values


def convert_request(request, name):
    if not isinstance(request, dict):
        raise ValueError('仅支持结构化 HTTP 请求。')
    raw = request.get('url')
    url = raw if isinstance(raw, dict) else {}
    raw = url.get('raw') if isinstance(raw, dict) else raw
    if not isinstance(raw, str) or len(raw) > 4000:
        raise ValueError('请求地址缺少有效 raw 文本。')
    if '#' in raw or '\\' in raw or any(ord(c) < 32 for c in raw):
        raise ValueError('请求地址包含片段、控制字符或反斜线。')
    warnings = []
    match = re.match(r'^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}(?=/|\?|$)', raw)
    if match:
        raw = raw[match.end():] or '/'
        warnings.append('服务地址由执行环境提供；不导入源地址变量值。')
    elif raw.startswith(('http://', 'https://')):
        parsed = urlsplit(raw)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('不支持含账号或无效主机的请求地址。')
        raw = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
        warnings.append('源服务地址不导入；执行时使用所选环境。')
    elif not raw.startswith('/'):
        raise ValueError('地址须为 http/https、{{服务地址变量}} 或相对路径。')
    path, _, query = raw.partition('?')
    if url.get('variable'):
        raise ValueError('URL 路径参数需先改为 {{变量名}} 形式。')
    if 'query' in url:
        query_values = key_values(url['query'], '查询参数')
    else:
        query_values = key_values([dict(key=k, value=v) for k, v in parse_qsl(query, keep_blank_values=True)], '查询参数')
    headers = key_values(request.get('header', []), '请求头')
    if request.get('auth') and request['auth'].get('type') != 'noauth':
        warnings.append('Postman 认证配置不导入，请在环境中设置认证请求头。')
    body = request.get('body') or {}
    if not isinstance(body, dict):
        raise ValueError('请求体格式不正确。')
    send_body = bool(body.get('mode') and not body.get('disabled'))
    payload = {}
    if send_body:
        if body.get('mode') != 'raw':
            raise ValueError('当前仅支持 JSON 请求体；表单、文件、GraphQL 请人工配置。')
        try:
            payload = strict_json(body.get('raw', ''))
        except (TypeError, ValueError, RecursionError):
            raise ValueError('raw 请求体不是有效 JSON。') from None
    data = dict(name=name, method=request.get('method', 'GET'), path=path, query=query_values,
                headers=headers, body=payload, send_body=send_body,
                expected_status=200, assertions=[], extracts={}, max_elapsed_ms=0)
    no_literal_secrets(data)
    unresolved = re.findall(r'\{\{.*?\}\}', json.dumps(data, ensure_ascii=False))
    if any(not VARIABLE.fullmatch(value) for value in unresolved):
        raise ValueError('动态变量或变量名暂不支持，请改为 {{字母数字下划线}} 形式。')
    if request.get('protocolProfileBehavior'):
        warnings.append('Postman 协议行为不导入，请人工确认。')
    validate_case(data)
    return data, warnings


def parse_collection(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError('集合文件不能超过 2 MB。')
    try:
        document = strict_json(raw.decode('utf-8-sig'))
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError('请上传有效 UTF-8 JSON 集合，不支持重复字段或过深嵌套。') from None
    if not isinstance(document, dict) or not isinstance(document.get('info'), dict):
        raise ValueError('缺少 Postman 集合 info。')
    info = document['info']
    if info.get('schema') not in SCHEMAS:
        raise ValueError('目前支持 Postman Collection v2 / v2.1 JSON；不支持 v3 YAML 或环境文件。')
    title = info.get('name')
    if not isinstance(title, str) or not title.strip() or len(title) > 120:
        raise ValueError('集合名称须为 1～120 字符。')
    rows, node_count = [], 0
    def walk(items, folders, inherited_warnings):
        nonlocal node_count
        if not isinstance(items, list) or len(folders) > 10:
            raise ValueError('目录格式不正确或超过 10 层。')
        for item in items:
            node_count += 1
            if node_count > 1000 or len(rows) >= 200:
                raise ValueError('一次最多导入 200 个请求、1000 个目录节点。')
            if not isinstance(item, dict):
                raise ValueError('集合条目格式不正确。')
            name = item.get('name', '')
            warnings = list(inherited_warnings)
            if item.get('event'):
                warnings.append('前置/测试 JavaScript 未转换，需人工重建断言与变量提取。')
            if item.get('auth') and item['auth'].get('type') != 'noauth':
                warnings.append('目录认证不导入，请在环境中配置。')
            if item.get('variable'):
                warnings.append('目录变量值不导入，请在环境中配置。')
            if 'item' in item:
                if not isinstance(name, str) or not name.strip() or len(name) > 120:
                    raise ValueError('目录名称须为 1～120 字符。')
                walk(item['item'], folders + [name], warnings)
                continue
            row = dict(index=len(rows), name=name if isinstance(name, str) else '', folders=folders,
                       warnings=warnings, error='', configuration=None, method='', path='')
            try:
                if not row['name'].strip() or len(row['name']) > 200:
                    raise ValueError('请求名称须为 1～200 字符。')
                data, notices = convert_request(item.get('request'), row['name'])
                row.update(configuration=data, method=data['method'], path=data['path'],
                           warnings=list(dict.fromkeys(warnings + notices)))
            except ValueError as exc:
                row['error'] = str(exc)
            except (TypeError, AttributeError):
                row['error'] = '请求结构不支持，请检查格式。'
            rows.append(row)
    common = ['导入后须复核预期状态码、断言和变量提取；不会自动执行。']
    if document.get('event'):
        common.append('集合前置/测试 JavaScript 未转换。')
    if document.get('auth') and document['auth'].get('type') != 'noauth':
        common.append('集合认证不导入，请在环境中配置。')
    if document.get('variable'):
        common.append('集合变量值不导入，请在环境中配置。')
    walk(document.get('item'), [title], common)
    if not rows:
        raise ValueError('集合中没有 HTTP 请求。')
    return dict(title=title, rows=rows)
