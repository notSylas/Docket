import hashlib
import json
from email.message import EmailMessage, Message
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from docket.core.db.models import Chunk, EvidenceUnit, EvidenceVersion
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.infra.parsing.filecheck import InvalidFileError, check_file_signature
from docket.infra.parsing.mail_family import MailParser, MailLimitError, chunk_mail
from docket.infra.parsing.recipes import FAMILY_MAIL, mail_recipe, recipe_family
from docket.interfaces.cli.context import AppContext
from docket.interfaces.cli.quiet import ignore_notices


def message(body='Reserve: 420 rupees.\n> Quoted reply\n'):
    m = EmailMessage()
    m['From'], m['To'] = 'ada@example.test', 'reader@example.test'
    m['Subject'], m['Message-ID'] = 'नमस्ते', '<duplicate@example.test>'
    m.set_content(body)
    return m


def mailbox_bytes(messages):
    return b''.join(b'From ada@example.test Mon Oct 12 10:00:00 2026\n' + m.as_bytes() + b'\n' for m in messages)


def parse(tmp_path, data, suffix='.eml', **kw):
    p = tmp_path / ('mail' + suffix)
    p.write_bytes(data)
    result = MailParser(**kw).parse('s', p)
    assert p.read_bytes() == data
    return result


def test_duplicate_ids_offsets_and_original_hashes(tmp_path):
    raw = mailbox_bytes([message('One'), message('Two')])
    messages = parse(tmp_path, raw, '.mbox').messages
    a, b = [m.locator for m in messages]
    assert a['message_id'] == b['message_id']
    assert a['byte_offset'] == 0 < b['byte_offset']
    assert a['message_sha256'] != b['message_sha256']
    for m in messages:
        loc = m.locator
        assert hashlib.sha256(raw[loc['byte_offset']:loc['byte_end']]).hexdigest() == loc['message_sha256']


def test_alternative_unicode_and_reply(tmp_path):
    m = message()
    m.add_alternative('<p>Wrong alternative</p>', subtype='html')
    unit = parse(tmp_path, m.as_bytes()).messages[0]
    assert '> Quoted reply' in unit.text and 'Wrong alternative' not in unit.text
    assert unit.locator['headers']['subject'] == 'नमस्ते'
    assert unit.locator['body_parts'][0]['mime_part'] == '1.1'
    assert not unit.locator['lossy']


@pytest.mark.parametrize('charset,body,expected,lossy', [('iso-8859-1', b'caf\xe9', 'café', False), ('not-a-charset', 'नमस्ते'.encode(), 'नमस्ते', True), ('utf-8', b'broken\xff', 'broken', True)])
def test_charset_and_encoded_subject(tmp_path, charset, body, expected, lossy):
    data = b'Subject: =?utf-8?b?4KS44KWC4KSa4KSo4KS+?=\nContent-Type: text/plain; charset=' + charset.encode() + b'\n\n' + body
    unit = parse(tmp_path, data).messages[0]
    assert expected in unit.text and unit.locator['lossy'] is lossy
    assert unit.locator['headers']['subject'] == 'सूचना'


def test_attachments_never_decoded_or_descended(tmp_path, monkeypatch):
    m = message()
    m.add_attachment(b'ATTACHMENT_CANARY', maintype='application', subtype='octet-stream', filename='secret.bin')
    m.add_attachment(message('NESTED_CANARY'))
    original = Message.get_payload
    def checked(self, *args, **kw):
        if kw.get('decode') and self.get_content_disposition() == 'attachment':
            pytest.fail('decoded attachment')
        return original(self, *args, **kw)
    monkeypatch.setattr(Message, 'get_payload', checked)
    unit = parse(tmp_path, m.as_bytes()).messages[0]
    assert len(unit.locator['attachments']) == 2 and unit.locator['attachments'][0]['filename'] == 'secret.bin'
    assert 'CANARY' not in unit.text and len(list(tmp_path.iterdir())) == 1


def test_hostile_html_offline(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'socket', lambda *a, **kw: pytest.fail('network'))
    m = EmailMessage()
    m['Subject'] = 'HTML'
    m.set_content('<h1>Fact</h1><p>Keep <a href="http://invalid">link text</a></p><!--CANARY--><script>CANARY</script><img src="http://invalid"><iframe>CANARY</iframe>', subtype='html')
    text = parse(tmp_path, m.as_bytes()).messages[0].text
    assert 'Fact\nKeep link text' in text and 'CANARY' not in text


def test_5000_message_bound(tmp_path, monkeypatch):
    import docket.infra.parsing.mail_family as mail
    calls = []
    original = mail._message
    def counted(*a, **kw):
        calls.append(1)
        return original(*a, **kw)
    monkeypatch.setattr(mail, '_message', counted)
    parsed = parse(tmp_path, mailbox_bytes([message()] * 5000), '.mbox', max_messages=3)
    assert len(calls) == len(parsed.messages) == 3
    assert 'Partial mailbox' in parsed.notice and '3 of 5000' in parsed.notice


def test_bounds(tmp_path):
    p = tmp_path / 'mail.mbox'
    p.write_bytes(b'x' * 101)
    with pytest.raises(MailLimitError, match='smaller copy'):
        MailParser(max_bytes=100).parse('s', p)
    with pytest.raises(ValueError, match='positive'):
        MailParser(max_messages=0)


def test_one_unit_per_message_and_exact_locators(tmp_path):
    class Counter:
        def count(self, text): return len(text)
    parsed = parse(tmp_path, mailbox_bytes([message(), message('line 1\nline 2')]), '.mbox')
    units, chunks = chunk_mail(parsed, counter=Counter(), max_tokens=19)
    assert len(units) == 2 < len(chunks)
    for c in chunks:
        unit = units[c.evidence_unit_index]
        piece = next(p for p in json.loads(unit.locator_json)['chunks'] if p['ordinal'] == c.ordinal)
        assert unit.text[piece['char_start']:piece['char_end']] == c.text
        assert len(c.text) <= 19 and all(1 <= p['line_start'] <= p['line_end'] for p in piece['body_parts'])


@pytest.mark.parametrize('suffix', ['.eml', '.mbox'])
def test_disguised_binary(tmp_path, suffix):
    p = tmp_path / ('binary' + suffix)
    p.write_bytes(b'PK\x03\x04binary')
    with pytest.raises(InvalidFileError): check_file_signature(p)


def test_pipeline_roundtrip_rechunk_partial_and_original(tmp_path):
    ctx = AppContext.for_testing(data_dir=tmp_path / 'data', gateway=FakeInferenceGateway(), parser=SimpleNamespace(parser_name='unused', parser_version='1'))
    ctx.settings.mail_max_messages = 1
    folder = tmp_path / 'docs'
    folder.mkdir()
    path = folder / 'mail.mbox'
    raw = mailbox_bytes([message(), message('Second')])
    path.write_bytes(raw)
    src = ctx.source_manager.register_source(folder)
    try:
        result = ctx.pipeline.run_ingestion_for_source(src.id)
        assert result.files_failed == 0
        assert any('Partial mailbox' in n for n in ignore_notices(result, None, src.id))
        with ctx.session_factory() as db:
            version = db.execute(select(EvidenceVersion)).scalar_one()
            units = db.execute(select(EvidenceUnit)).scalars().all()
            chunks = db.execute(select(Chunk)).scalars().all()
        assert len(units) == 1 and ctx.store.get(version.content_hash) == raw == path.read_bytes()
        for c in chunks:
            evidence = ctx.resolver.resolve(c.id)
            assert evidence.text == c.text and 'Message 1' in evidence.location and 'partial mailbox' in evidence.location
        assert recipe_family(str(path)) == FAMILY_MAIL
        ctx.pipeline._recipes[FAMILY_MAIL] = mail_recipe(ctx.settings, 'stdlib-mail', '2')
        candidates, _ = ctx.pipeline._classify_versions(src.id)
        result = ctx.pipeline.rechunk_version(src.id, candidates[0])
        assert result.status == 'ingested' and result.notice
    finally: ctx.engine.dispose()
