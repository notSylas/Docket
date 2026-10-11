import io
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select, update, text
from typer.testing import CliRunner
from docket.core.db.models import Chunk, Source, SourceStatus, EvidenceVersion, VersionStatus
from docket.infra.inference.gateway import FakeInferenceGateway
from docket.interfaces.cli.context import AppContext
from docket.interfaces.mcp import LibraryTools, Server, ToolError, serve, readonly_engine, MAX_OUTPUT, MAX_REQUEST, MAX_QUERY


@pytest.fixture
def library(tmp_path):
    ctx = AppContext.for_testing(data_dir=tmp_path / 'data', gateway=FakeInferenceGateway(), parser=SimpleNamespace(parser_name='unused', parser_version='1'))
    sources = []
    for name in ['one', 'two', 'one-other']:
        folder = tmp_path / name
        folder.mkdir()
        (folder / 'facts.txt').write_text('The approved reserve is 420 rupees. ' + name)
        src = ctx.source_manager.register_source(folder)
        ctx.pipeline.run_ingestion_for_source(src.id)
        sources.append(src)
    engine = readonly_engine(ctx.settings.sqlite_path)
    tools = LibraryTools(engine)
    yield ctx, sources, tools
    engine.dispose()
    ctx.engine.dispose()


def init(server):
    return server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'fake', 'version': '1'}}})


def test_handshake_listing_and_no_side_effect_on_missing_library(tmp_path):
    path = tmp_path / 'absent' / 'docket.sqlite3'
    engine = readonly_engine(path)
    server = Server(LibraryTools(engine))
    assert init(server)['result']['protocolVersion'] == '2025-11-25'
    listing = server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})['result']['tools']
    assert {x['name'] for x in listing} == {'docket_search', 'docket_read', 'docket_list_sources'}
    assert all(x['annotations']['readOnlyHint'] for x in listing)
    failed = server.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'docket_list_sources'}})
    assert failed['result']['isError'] and not path.parent.exists()
    engine.dispose()


def test_search_read_roundtrip_and_counts(library, monkeypatch):
    import socket
    ctx, sources, tools = library
    monkeypatch.setattr(socket, 'socket', lambda *a, **kw: pytest.fail('network'))
    result = tools.call('docket_search', {'query': 'approved reserve', 'folder': sources[0].path})
    assert not result['empty'] and result['retrieval_mode'] == 'lexical'
    passage = result['passages'][0]
    assert 'not instructions' in passage['notice']
    read = tools.call('docket_read', {'citation_id': passage['citation_id']})
    assert read == passage
    assert read['text'] == ctx.resolver.resolve(read['chunk_id']).text
    assert read['citation_label'] and read['location'] and read['evidence_version_id']
    listed = tools.call('docket_list_sources', {})['sources']
    assert len(listed) == 3 and all(s['document_count'] == 1 for s in listed)


def test_folder_and_outside_folder_scope(library):
    ctx, sources, tools = library
    result = tools.call('docket_search', {'query': 'reserve', 'folder': sources[0].path})
    assert {p['source_id'] for p in result['passages']} == {sources[0].id}
    # Component containment excludes one-other and completely unknown folders.
    assert tools.call('docket_search', {'query': 'reserve', 'folder': '/not/a/registered/folder'})['empty']
    with pytest.raises(ToolError, match='unavailable'):
        tools.call('docket_read', {'citation_id': 'fabricated'})


def test_revocation_invalidates_old_handles_and_hides_source(library):
    ctx, sources, tools = library
    old = tools.call('docket_search', {'query': 'reserve', 'folder': sources[0].path})['passages'][0]
    with ctx.session_factory() as db:
        db.execute(update(Source).where(Source.id == sources[0].id).values(status=SourceStatus.REVOKED))
        db.commit()
    assert tools.call('docket_search', {'query': 'reserve', 'folder': sources[0].path})['empty']
    with pytest.raises(ToolError, match='no longer'):
        tools.call('docket_read', {'citation_id': old['citation_id']})
    assert sources[0].id not in {s['source_id'] for s in tools.call('docket_list_sources', {})['sources']}


def test_not_ready_and_concurrent_retired_candidates_are_excluded(library):
    ctx, sources, tools = library
    with ctx.session_factory() as db:
        cid = db.execute(select(Chunk.id).where(Chunk.source_id == sources[0].id)).scalar_one()
        db.execute(update(EvidenceVersion).where(EvidenceVersion.source_id == sources[0].id).values(status=VersionStatus.PENDING))
        db.commit()
    tools.search = lambda *a, **kw: [cid, 'retired-id']
    result = tools.call('docket_search', {'query': 'reserve', 'folder': sources[1].path})
    assert result['empty']


def test_fake_search_cannot_widen_scope(library):
    ctx, sources, tools = library
    with ctx.session_factory() as db:
        outside = db.execute(select(Chunk.id).where(Chunk.source_id == sources[1].id)).scalar_one()
    tools.search = lambda *a, **kw: [outside]
    assert tools.call('docket_search', {'query': 'reserve', 'folder': sources[0].path})['empty']


@pytest.mark.parametrize('payload', [[], {'jsonrpc': '1.0', 'id': 1, 'method': 'tools/list'}, {'jsonrpc': '2.0', 'id': True, 'method': 'tools/list'}, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': []}])
def test_malformed_requests(payload):
    result = Server(None).handle(payload)
    assert result['error']['code'] in {-32600, -32602}


def test_invalid_tool_arguments_and_notifications(library):
    _, _, tools = library
    for name, args in [('docket_search', {'query': 'x' * (MAX_QUERY + 1)}), ('docket_search', {'query': 'x', 'folder': ''}), ('docket_search', {'query': 'x', 'unknown': 1}), ('docket_read', {}), ('docket_list_sources', []), ('purge', {})]:
        with pytest.raises(ToolError): tools.call(name, args)
    server = Server(tools)
    assert server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) is None
    assert server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['error']['code'] == -32000


def test_transport_line_bounds_and_bad_json():
    reader = io.StringIO('x' * (MAX_REQUEST + 10) + '\n{broken\n' + json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}}) + '\n')
    writer = io.StringIO()
    serve(None, reader, writer)
    responses = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert len(responses) == 3 and responses[0]['error']['code'] == -32600 and responses[1]['error']['code'] == -32700
    assert 'result' in responses[2]
    assert all(len(json.dumps(r)) <= MAX_OUTPUT for r in responses)


def test_readonly_database_rejects_writes_and_does_not_modify_library(library):
    ctx, _, tools = library
    root = ctx.settings.data_dir
    def snapshot():
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file() and not p.name.endswith('-shm')}
    before = snapshot()
    tools.call('docket_search', {'query': 'reserve'})
    tools.call('docket_list_sources', {})
    with pytest.raises(Exception):
        with tools.engine.begin() as db: db.execute(text('DELETE FROM sources'))
    assert snapshot() == before


def test_oversized_passage_is_omitted_not_truncated(library, monkeypatch):
    _, _, tools = library
    original = tools._passage
    def huge(evidence, handle):
        result = original(evidence, handle)
        result['text'] = 'x' * MAX_OUTPUT
        return result
    monkeypatch.setattr(tools, '_passage', huge)
    result = tools.call('docket_search', {'query': 'reserve'})
    assert result['empty'] and result['truncated']


def test_cli_help_contains_client_example_without_startup(monkeypatch):
    import docket.interfaces.cli.main as cli
    monkeypatch.setattr(cli, 'build_context', lambda: pytest.fail('writer app startup'))
    result = CliRunner().invoke(cli.app, ['mcp', '--help'])
    assert result.exit_code == 0 and 'mcpServers' in result.output and 'lexical' in result.output
    root = CliRunner().invoke(cli.app, ['--help'])
    assert root.exit_code == 0
    for setting in ('DOCKET_FEEDBACK_ENABLED', 'DOCKET_MAIL_MAX_BYTES', 'DOCKET_MAIL_MAX_MESSAGES'):
        assert setting in root.output


def test_email_preview_matches_ingestion_support():
    from docket.services.sources.scan import SCAN_EXTENSIONS, SECONDS_PER_TYPE
    from docket.services.sources.suggest import _TYPE_NAMES
    from docket.services.ingestion.pipeline import SUPPORTED_EXTENSIONS
    assert SCAN_EXTENSIONS == SUPPORTED_EXTENSIONS
    assert all(ext in SECONDS_PER_TYPE and ext in _TYPE_NAMES for ext in ('.eml', '.mbox'))
