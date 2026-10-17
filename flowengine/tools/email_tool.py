"""Email: send one message per record over SMTP.

Every message is built and checked first: addresses, header safety, templates and attachment
files. Nothing is sent if any record fails. Then all messages go out in one SMTP session. Sending
needs the run's network and remote-write permissions, because the messages carry record data to
an outside service.

Credentials come from a saved SMTP connection (toolbar → Web & API). The password is used only after
TLS is established, by implicit TLS on smtps:// or by STARTTLS. The one exception is a localhost
server. A server without authentication can also be set directly in the tool, with encryption
None, SSL/TLS or STARTTLS.
"""
from __future__ import annotations

import mimetypes
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid
from pathlib import Path

from ..errors import ConfigError, DataError, EngineError
from ..modeling import missing
from ..plugin import Anchor, Tool, ToolManifest
from ..table import Table
from ..types import Field, FieldType, Schema, format_value
from ._common import require_fields

TEXT = 2**30 - 1
MAX_ATTACHMENT = 25 * 1024 * 1024
_ADDRESS = re.compile(r'^[^@\s<>,;"]+@[^@\s<>,;"]+\.[^@\s<>,;"]+$')
_PLACEHOLDER = re.compile(r'\[([^\[\]\r\n]+)\]')
_LOOPBACK = ('localhost', '127.0.0.1', '::1')
_DEFAULT_PORT = {'none': 25, 'ssl': 465, 'starttls': 587}
_OPT_TEXT = {'type': ['string', 'null'], 'default': None}
_OPT_FIELD = {'type': ['string', 'null'], 'default': None}


def _fill(template, record):
    """Replace [Field] with the record's value; unknown names stay as written."""
    return _PLACEHOLDER.sub(lambda m: ('' if missing(record[m.group(1)]) else format_value(record[m.group(1)]))
                            if m.group(1) in record else m.group(0), template)


def _value(cfg, key, record):
    field = cfg.get(f'{key}_field')
    if field:
        v = record[field]
        return '' if missing(v) else format_value(v)
    return _fill(cfg.get(key) or '', record)


def _addresses(text, what, number, required):
    if '\r' in text or '\n' in text:
        raise DataError(f'Record {number}: {what} contains a line break')
    pairs = getaddresses([text.replace(';', ',')])
    out = []
    for name, addr in pairs:
        if not addr and not name:
            continue
        if not _ADDRESS.match(addr):
            raise DataError(f'Record {number}: {what} address {addr or name!r} is not a valid email address')
        out.append((name, addr))
    if required and not out:
        raise DataError(f'Record {number}: no {what} address')
    return out


def _header(name, addr):
    from email.headerregistry import Address
    return str(Address(display_name=name, addr_spec=addr))


class Email(Tool):
    manifest = ToolManifest(
        tool_id='core.email', version='1.0.0', display_name='Email', catalog_ids=('reporting:email',),
        inputs=(Anchor('input', 'Input', description='One email is sent per record'),),
        outputs=(Anchor('output', 'Output', description='The records with EmailStatus and MessageID'),),
        side_effects='network', deterministic=False,
        description='Send one email per record over SMTP. From, To, Cc, Bcc, Subject and Body take fixed text '
                    'with [Field] placeholders, or a field. Lists are separated by commas or semicolons. The '
                    'body is plain text or HTML. Attachments come from fixed paths or a path field (several '
                    'paths separated by semicolons, at most 25 MB each). All messages are checked before any is '
                    'sent. Use a saved SMTP connection for a server that needs a login. Needs permission for '
                    'network requests and remote writes.',
        config_schema={'type': 'object', 'additionalProperties': False, 'properties': {
            'enabled': {'type': 'boolean', 'default': True},
            'connection_id': {'type': ['string', 'null'], 'default': None, 'x-flowbench-connection': 'smtp',
                              'description': 'Saved SMTP connection (Web & API in the toolbar) for servers that need a login'},
            'smtp_host': _OPT_TEXT, 'port': {'type': ['integer', 'null'], 'minimum': 1, 'maximum': 65535, 'default': None},
            'encryption': {'enum': ['none', 'ssl', 'starttls'], 'default': 'starttls'},
            'from': _OPT_TEXT, 'from_field': _OPT_FIELD, 'to': _OPT_TEXT, 'to_field': _OPT_FIELD,
            'cc': _OPT_TEXT, 'cc_field': _OPT_FIELD, 'bcc': _OPT_TEXT, 'bcc_field': _OPT_FIELD,
            'subject': {'type': 'string', 'default': ''}, 'subject_field': _OPT_FIELD,
            'body': {'type': 'string', 'default': ''}, 'body_field': _OPT_FIELD,
            'body_format': {'enum': ['text', 'html'], 'default': 'text'},
            'attachments': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}, 'default': []},
            'attachment_field': _OPT_FIELD,
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 600, 'default': 60}}})

    _FIELD_KEYS = ('from_field', 'to_field', 'cc_field', 'bcc_field', 'subject_field', 'body_field', 'attachment_field')

    def _out_schema(self, schema):
        return Schema([*schema, Field('EmailStatus', FieldType.V_WSTRING, 16), Field('MessageID', FieldType.V_WSTRING, 256)])

    def infer(self, ctx, cfg, inputs):
        require_fields(inputs['input'], [cfg[k] for k in self._FIELD_KEYS if cfg[k]])
        if not cfg['connection_id'] and not cfg['smtp_host']:
            raise ConfigError('Choose a saved SMTP connection or enter the SMTP server')
        if not cfg['from'] and not cfg['from_field']:
            raise ConfigError('Set the From address (or a From field)')
        if not cfg['to'] and not cfg['to_field']:
            raise ConfigError('Set the To address (or a To field)')
        return {'output': self._out_schema(inputs['input'])}

    def _messages(self, ctx, cfg, table):
        out = []
        for i, record in enumerate(table.to_dicts(), 1):
            ctx.check_cancelled()
            sender = _addresses(_value(cfg, 'from', record), 'From', i, True)
            if len(sender) != 1:
                raise DataError(f'Record {i}: From must be one address')
            to = _addresses(_value(cfg, 'to', record), 'To', i, True)
            cc = _addresses(_value(cfg, 'cc', record), 'Cc', i, False)
            bcc = _addresses(_value(cfg, 'bcc', record), 'Bcc', i, False)
            subject = _value(cfg, 'subject', record)
            if '\r' in subject or '\n' in subject:
                raise DataError(f'Record {i}: the subject contains a line break')
            msg = EmailMessage()
            msg['From'] = _header(*sender[0])
            msg['To'] = ', '.join(_header(*a) for a in to)
            if cc:
                msg['Cc'] = ', '.join(_header(*a) for a in cc)
            msg['Subject'] = subject
            domain = sender[0][1].rsplit('@', 1)[1]
            msg['Message-ID'] = make_msgid(domain=domain)
            body = _value(cfg, 'body', record)
            if cfg['body_format'] == 'html':
                msg.set_content(re.sub(r'<[^>]+>', '', body))
                msg.add_alternative(body, subtype='html')
            else:
                msg.set_content(body)
            paths = list(cfg['attachments'])
            if cfg['attachment_field'] and not missing(record[cfg['attachment_field']]):
                paths += [p.strip() for p in str(record[cfg['attachment_field']]).split(';') if p.strip()]
            for raw in paths:
                path = Path(_fill(raw, record))
                path = path if path.is_absolute() else Path(ctx.base_dir) / path
                if not path.is_file():
                    raise DataError(f'Record {i}: attachment {str(path)!r} was not found')
                size = path.stat().st_size
                if size > MAX_ATTACHMENT:
                    raise DataError(f'Record {i}: attachment {path.name!r} is {size / 1048576:.1f} MB (limit 25 MB)')
                kind, _ = mimetypes.guess_type(path.name)
                main, sub = (kind or 'application/octet-stream').split('/', 1)
                msg.add_attachment(path.read_bytes(), maintype=main, subtype=sub, filename=path.name)
            recipients = [a for _, a in to + cc + bcc]
            out.append((msg, sender[0][1], recipients))
        return out

    def _server(self, cfg):
        if cfg['connection_id']:
            from ..service_connections import ServiceConnectionStore
            store = ServiceConnectionStore()
            conn = store.get(cfg['connection_id'], kind='smtp')
            host, port, encryption = conn.smtp_server()
            return host, port, encryption, (conn.username, store.secret(conn))
        encryption = cfg['encryption']
        return cfg['smtp_host'], cfg['port'] or _DEFAULT_PORT[encryption], encryption, None

    def run(self, ctx, cfg, inputs):
        table = inputs['input']
        self.infer(ctx, cfg, {'input': table.schema})
        messages = self._messages(ctx, cfg, table)
        schema = self._out_schema(table.schema)
        if not cfg['enabled']:
            ctx.info('Email is disabled; no messages were sent')
            return {'output': Table.from_rows(schema, [(*r, 'disabled', None) for r in table.rows()])}
        if not messages:
            return {'output': Table.from_rows(schema, [])}
        if not (ctx.limits.get('allow_network') and ctx.limits.get('allow_remote_writes')):
            raise EngineError('Email sends record data to a mail server and is disabled for this run. Allow network '
                              'requests and remote writes (CLI --allow-network --allow-remote-writes, or confirm in the '
                              'desktop app).', code='permission-denied', node_id=ctx.node_id)
        host, port, encryption, login = self._server(cfg)
        local = host in _LOOPBACK
        context = ssl.create_default_context()
        try:
            if encryption == 'ssl':
                smtp = smtplib.SMTP_SSL(host, port, timeout=cfg['timeout_seconds'], context=context)
            else:
                smtp = smtplib.SMTP(host, port, timeout=cfg['timeout_seconds'])
            with smtp:
                smtp.ehlo()
                if encryption == 'starttls':
                    if smtp.has_extn('starttls'):
                        smtp.starttls(context=context)
                        smtp.ehlo()
                    elif not local:
                        raise DataError(f'{host} does not offer STARTTLS; the connection would not be encrypted')
                if login:
                    if encryption == 'none' and not local:
                        raise DataError('A password is only sent over an encrypted connection')
                    smtp.login(*login)
                ids = []
                for msg, sender, recipients in messages:
                    ctx.check_cancelled()
                    smtp.send_message(msg, from_addr=sender, to_addrs=recipients)
                    ids.append(msg['Message-ID'])
        except smtplib.SMTPAuthenticationError:
            raise DataError('The mail server rejected the username or password') from None
        except smtplib.SMTPDataError as e:
            if e.smtp_code == 552:
                raise DataError('The mail server rejected a message as too large; remove large attachments') from None
            raise DataError(f'The mail server rejected a message: {e.smtp_code} {e.smtp_error!r}') from None
        except (smtplib.SMTPException, OSError) as e:
            raise DataError(f'Could not send email through {host}:{port}: {e}') from None
        ctx.info(f'Sent {len(ids)} email(s) through {host}:{port}')
        return {'output': Table.from_rows(schema, [(*r, 'sent', m) for r, m in zip(table.rows(), ids)])}


TOOLS = [Email]
