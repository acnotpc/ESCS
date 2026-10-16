"""Explicit Andy-to-Control-Room messages through the existing user webhook."""
from urllib.parse import urlsplit
import re


async def verify_control_room(settings, call):
    """Check the existing Andy-to-Control-Room route without sending."""
    settings.validate()
    url = urlsplit(settings.rest_url)
    if (url.scheme != 'https' or url.hostname != 'escs.bitrix24.com' or url.port not in (None,443)
            or url.username or url.password or url.query or url.fragment
            or not re.fullmatch(r'/rest/1/[A-Za-z0-9_-]+/?', url.path)):
        raise ValueError('Existing Andy ESCS webhook required')
    recent = await call('im.recent.list', {'SKIP_OPENLINES': 'Y'})
    rows = recent.get('items', recent) if isinstance(recent, dict) else recent
    matches = [r for r in rows if isinstance(r, dict)
               and str(r.get('id') or r.get('dialogId') or r.get('dialog_id')) == '1037'] if isinstance(rows, list) else []
    if (len(matches) != 1 or matches[0].get('type') != 'user'
            or (matches[0].get('title') or matches[0].get('name')) != 'Control Room Team'):
        raise ValueError('Existing Control Room Team conversation requires verification')
async def send_control_room(settings, call, message):
    body = message.strip()
    if not body or len(body) > 12000:
        raise ValueError('Message must contain 1-12000 characters')
    await verify_control_room(settings, call)
    # Personal dialog IDs are user IDs, not their underlying chat IDs.
    result = await call('im.message.add', {'DIALOG_ID': '1037', 'MESSAGE': body,
                                        'SYSTEM': 'N', 'URL_PREVIEW': 'N'})
    if isinstance(result, bool) or not str(result).isdigit() or int(result) <= 0:
        raise RuntimeError('Send outcome requires reconciliation; do not retry')
    return {'sent': True, 'dialogId': '1037', 'messageId': int(result), 'authorId': 1}
