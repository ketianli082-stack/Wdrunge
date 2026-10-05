"""Credentials a user pastes into a conversation (a connection URL with a password) go
to the service connection settings instead of the team: the message keeps the address
with the password hidden, so no model prompt, task or file ever holds it."""
from __future__ import annotations

import re
from urllib.parse import unquote

from app.resources.kinds import TYPES

SCHEMES = {'mysql': 'mysql', 'mariadb': 'mariadb', 'postgres': 'postgresql', 'postgresql': 'postgresql', 'redis': 'redis',
           'rediss': 'redis', 'mongodb': 'mongodb', 'amqp': 'rabbitmq', 'amqps': 'rabbitmq'}
CREDENTIAL_URL = re.compile(
    r'''\b(?P<scheme>mysql|mariadb|postgres(?:ql)?|rediss?|mongodb|amqps?)(?:\+\w+)?://'''
    r'''(?P<user>[^:@/\s'"`]*):(?P<password>[^@/\s'"`]+)@(?P<host>\[[^\]\s]+\]|[^:/\s?#'"`]+)'''
    r'''(?::(?P<port>\d{1,5}))?(?:/(?P<database>[^\s?#'"`/]*))?''')


def find(text: str) -> list[dict]:
    found = []
    for match in CREDENTIAL_URL.finditer(text or ''):
        kind = SCHEMES[match['scheme']]
        port = int(match['port']) if match['port'] else TYPES[kind]['port']
        if not 0 < port < 65536 or match['password'] in {'***', '<password>', 'password', '****'}:
            continue
        found.append({'type': kind, 'host': match['host'].strip('[]'), 'port': port, 'username': unquote(match['user']),
                      'password': unquote(match['password']), 'database': unquote(match['database'] or ''),
                      'span': match.span('password')})
    return found


async def capture(resources, project_id: str, text: str) -> tuple[str, list[str]]:
    """Save pasted credentials as connections of this project; return the message with
    the passwords hidden and the names of the connections saved."""
    items = find(text)
    if not items or resources is None:
        return text, []
    from app.resources.service import ConnectionInput
    saved = []
    rows = await resources._rows()
    for item in items:
        existing = next((row for row in rows if row['data']['type'] == item['type'] and row['data']['host'] == item['host']
                         and row['data'].get('port') == item['port'] and row['data'].get('username') == item['username']), None)
        if existing:
            data = existing['data']
            body = ConnectionInput(name=existing['name'], label=data.get('label', ''), type=data['type'], host=data['host'],
                                   port=data.get('port'), username=data.get('username', ''), password=item['password'],
                                   database=data.get('database', ''), scheme=data.get('scheme', ''), env_prefix=data.get('env_prefix', ''),
                                   provision=data.get('provision', False), projects=data.get('projects'),
                                   extra=[{'key': e['key'], 'secret': e['secret'], 'value': None if e['secret'] else e.get('value', '')}
                                          for e in data.get('extra', [])], note=data.get('note', ''))
            result = await resources.save(body, existing['id'])
        else:
            taken = {row['name'] for row in rows}
            name, counter = item['type'], 2
            while name in taken:
                name, counter = f"{item['type']}-{counter}", counter + 1
            # A URL naming a database means "use this one"; without one the platform
            # creates the project's own database where it can.
            result = await resources.save(ConnectionInput(
                name=name, type=item['type'], host=item['host'], port=item['port'], username=item['username'],
                password=item['password'], database=item['database'], provision=not item['database'], projects=[project_id],
                label=f"{TYPES[item['type']]['label']}（来自对话）", note='从对话中粘贴的连接自动保存'))
        rows = await resources._rows()
        saved.append(result['name'])
    masked = text
    for item in sorted(items, key=lambda item: item['span'][0], reverse=True):
        start, end = item['span']
        masked = masked[:start] + '***' + masked[end:]
    names = '、'.join(dict.fromkeys(saved))
    masked += (f'\n\n（Wdrunge：已把连接 {names} 的密码保存到「服务连接」设置，并在消息中隐去。'
               '团队通过平台提供的环境变量使用它，不需要知道密码。）')
    return masked, list(dict.fromkeys(saved))
