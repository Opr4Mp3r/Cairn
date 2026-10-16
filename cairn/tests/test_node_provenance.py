"""Graph authorship is operational data, never another research premise."""
from __future__ import annotations

import json
import sqlite3

import pytest
import yaml

from cairn.server import db
from cairn.server.models import AuditRecordRequest

from test_audit_api import client
from test_audit_workflow import api
from test_audit_review import audit, assessment, claim, completion, finish, node_review, review_body


def generic(client):
    response = client.post('/projects', json={'title': 'trace', 'origin': 'source', 'goal': 'answer'})
    assert response.status_code == 201, response.text
    return response.json()['project']['id']


def intent(client, pid, *, creator='planner', harness=None):
    response = client.post(f'/projects/{pid}/intents', json={
        'from': ['origin'], 'description': 'investigate', 'creator': creator, 'harness': harness,
    })
    assert response.status_code == 201, response.text
    return response.json()['id']


def saved_config(pid, config):
    with db.get_conn() as conn:
        conn.execute('INSERT INTO audit_submissions '
                     '(id,run_dir,revision,fingerprint,status,prepared_json,execution_json,project_id,created_at,updated_at) '
                     "VALUES ('submission','/unused',1,'fixed','running','{}',? ,?,'now','now')",
                     (json.dumps(config), pid))


def test_new_fact_uses_executor_and_intent_keeps_distinct_creator(client):
    pid = generic(client)
    iid = intent(client, pid, harness='grok')
    claimed = client.post(f'/projects/{pid}/intents/{iid}/heartbeat', json={'worker': 'explorer', 'harness': 'codex'})
    assert claimed.status_code == 200
    before = client.get(f'/projects/{pid}/provenance').json()
    assert before['intents'][iid] == {
        'kind': 'research', 'creator': {'worker': 'planner', 'harness': 'grok'},
        'executor': {'worker': 'explorer', 'harness': 'codex'},
    }
    response = client.post(f'/projects/{pid}/intents/{iid}/conclude', json={
        'worker': 'explorer', 'description': 'observed result', 'harness': 'codex',
    })
    assert response.status_code == 200, response.text
    fid = response.json()['fact']['id']
    source = client.get(f'/projects/{pid}/provenance').json()
    assert source['facts'][fid] == {
        'kind': 'research', 'producer': {'worker': 'explorer', 'harness': 'codex'}, 'intent_id': iid,
    }
    for anchor in ('origin', 'goal'):
        assert source['facts'][anchor] == {'kind': 'input', 'producer': None, 'intent_id': None}
    graph = client.get(f'/projects/{pid}').json()
    exported = yaml.safe_load(client.get(f'/projects/{pid}/export?format=yaml').text)
    assert 'harness' not in json.dumps(graph)
    assert 'harness' not in json.dumps(exported)


def test_release_and_reclaim_never_inherits_previous_executor_harness(client):
    pid = generic(client)
    iid = intent(client, pid)
    assert client.post(f'/projects/{pid}/intents/{iid}/heartbeat', json={'worker': 'one', 'harness': 'pi'}).status_code == 200
    assert client.post(f'/projects/{pid}/intents/{iid}/release', json={'worker': 'one'}).status_code == 200
    assert client.get(f'/projects/{pid}/provenance').json()['intents'][iid]['executor'] is None
    assert client.post(f'/projects/{pid}/intents/{iid}/heartbeat', json={'worker': 'two'}).status_code == 200
    assert client.get(f'/projects/{pid}/provenance').json()['intents'][iid]['executor'] == {'worker': 'two', 'harness': None}


def test_expired_claim_clears_harness_before_another_executor(client):
    pid = generic(client)
    iid = intent(client, pid)
    client.post(f'/projects/{pid}/intents/{iid}/heartbeat', json={'worker': 'one', 'harness': 'pi'})
    with db.get_conn() as conn:
        conn.execute("UPDATE intents SET last_heartbeat_at='2000-01-01T00:00:00Z' WHERE project_id=? AND id=?", (pid, iid))
    assert client.post(f'/projects/{pid}/intents/{iid}/heartbeat', json={'worker': 'two'}).status_code == 200
    assert client.get(f'/projects/{pid}/provenance').json()['intents'][iid]['executor']['harness'] is None


def test_historical_snapshot_maps_exact_names_and_never_returns_environment(client):
    pid = generic(client)
    iid = intent(client, pid, creator='old-planner')
    result = client.post(f'/projects/{pid}/intents/{iid}/conclude', json={'worker': 'old-explorer', 'description': 'found'})
    fid = result.json()['fact']['id']
    saved_config(pid, {'workers': [
        {'name': 'old-planner', 'type': 'grok', 'env': {'TOKEN': 'never-expose-this-secret'}},
        {'name': 'old-explorer', 'type': 'minimax'},
    ], 'common_env': {'API_KEY': 'also-secret'}})
    response = client.get(f'/projects/{pid}/provenance')
    assert response.json()['facts'][fid]['producer'] == {'worker': 'old-explorer', 'harness': 'minimax'}
    assert response.json()['intents'][iid]['creator']['harness'] == 'grok'
    assert 'secret' not in response.text and 'env' not in response.text and 'API_KEY' not in response.text
    with db.get_conn() as conn:
        conn.execute("UPDATE intents SET worker_harness='codex' WHERE project_id=? AND id=?", (pid, iid))
    assert client.get(f'/projects/{pid}/provenance').json()['facts'][fid]['producer']['harness'] == 'codex'


def test_snapshot_role_pool_fallback_is_exact_not_name_guessing(client):
    pid = generic(client)
    iid = intent(client, pid, creator='pool-reason-grok')
    other = intent(client, pid, creator='pool-reason-codex')
    saved_config(pid, {'role_pools': {'reason': {'grok': 1}}})
    data = client.get(f'/projects/{pid}/provenance').json()['intents']
    assert data[iid]['creator']['harness'] == 'grok'
    assert data[other]['creator']['harness'] is None


def test_without_snapshot_harness_looking_actor_is_unknown(client):
    pid = generic(client)
    iid = intent(client, pid, creator='pool-reason-codex')
    assert client.get(f'/projects/{pid}/provenance').json()['intents'][iid]['creator']['harness'] is None
    assert client.get('/projects/missing/provenance').status_code == 404


def test_ambiguous_snapshot_actor_does_not_pick_arbitrary_harness(client):
    pid = generic(client)
    iid = intent(client, pid)
    saved_config(pid, {'workers': [{'name': 'planner', 'type': 'codex'}, {'name': 'planner', 'type': 'pi'}]})
    assert client.get(f'/projects/{pid}/provenance').json()['intents'][iid]['creator']['harness'] is None


def test_generic_completion_and_external_feedback_keep_different_origins(client):
    pid = generic(client)
    response = client.post(f'/projects/{pid}/complete', json={
        'from': ['origin'], 'description': 'done', 'worker': 'reasoner', 'harness': 'pi',
    })
    assert response.status_code == 200, response.text
    edge = response.json()['id']
    data = client.get(f'/projects/{pid}/provenance').json()
    assert data['intents'][edge]['kind'] == 'completion'
    assert data['intents'][edge]['executor'] == {'worker': 'reasoner', 'harness': 'pi'}
    assert data['facts']['goal']['producer'] is None
    reopened = client.post(f'/projects/{pid}/reopen', json={'description': 'additional feedback', 'creator': 'human'})
    assert reopened.status_code == 200, reopened.text
    fid = reopened.json()['fact']['id']
    saved_config(pid, {'workers': [{'name': 'human', 'type': 'codex'}]})
    feedback = client.get(f'/projects/{pid}/provenance').json()['facts'][fid]
    assert feedback['kind'] == 'feedback'
    assert feedback['producer'] == {'worker': 'human', 'harness': None}


def test_imported_seed_is_not_authored_by_its_reviewer(audit):
    cairn, http, _config, pid, _repo = audit
    saved_config(pid, {'workers': [{'name': 'audit-controller', 'type': 'codex'}]})
    before = http.get(f'/projects/{pid}/provenance').json()
    assert before['facts']['f001']['kind'] == 'import'
    assert before['facts']['f001']['producer'] == {'worker': 'audit-controller', 'harness': None}
    record = node_review(audit).model_copy(update={'worker': 'independent-reviewer', 'harness': 'pi'})
    assert cairn.record_audit_review(pid, record).ok
    assert http.get(f'/projects/{pid}/provenance').json()['facts']['f001'] == before['facts']['f001']


def test_overseer_hint_has_actual_harness_without_changing_research_author(audit):
    cairn, http, _config, pid, _repo = audit
    target = next(i for i in cairn.get_project(pid).intents if i.to is None)
    before = http.get(f'/projects/{pid}/provenance').json()
    context = cairn.get_review_input(pid, 'intent', target.id)
    body = AuditRecordRequest(
        record_key='direction', object_type='intent', object_id=target.id, input_digest=context['input_digest'],
        phase='overseer', session='independent', worker='direction-reader', harness='minimax',
        attempt_status='success', decision='approved', hint='preserve the original scope',
    )
    assert cairn.record_audit_review(pid, body).ok
    data = http.get(f'/projects/{pid}/provenance').json()
    assert data['facts'] == before['facts'] and data['intents'] == before['intents']
    assert next(iter(data['hints'].values()))['producer'] == {'worker': 'direction-reader', 'harness': 'minimax'}
    revision = cairn.get_audit(pid).audit_revision
    assert cairn.record_audit_review(pid, body).ok
    assert len(http.get(f'/projects/{pid}/provenance').json()['hints']) == 1
    assert cairn.get_audit(pid).audit_revision == revision


@pytest.mark.parametrize('decision', ['approved', 'rejected'])
def test_final_review_records_its_own_output_not_fact_authorship(audit, decision):
    cairn, http, _config, pid, _repo = audit
    before = http.get(f'/projects/{pid}/provenance').json()
    request = claim(audit, completion(audit)['id'])
    body = review_body(audit, request, verdict=assessment(decision=decision))
    body['record'].update(worker='final-worker', harness='codex')
    result = finish(audit, request, body)
    assert result.status_code == 200, result.text
    data = http.get(f'/projects/{pid}/provenance').json()
    assert data['facts'] == before['facts']
    if decision == 'approved':
        edge = next(i for i in data['intents'].values() if i['kind'] == 'completion')
        assert edge['executor'] == {'worker': 'final-worker', 'harness': 'codex'}
    else:
        assert next(iter(data['hints'].values()))['producer'] == {'worker': 'final-worker', 'harness': 'codex'}


def test_old_reviewer_alias_cannot_be_confused_with_a_configured_worker(audit):
    cairn, http, _config, pid, _repo = audit
    saved_config(pid, {'workers': [{'name': 'overseer', 'type': 'codex'}]})
    assert cairn.record_audit_review(pid, node_review(audit, hint='legacy direction')).ok
    hint = next(iter(http.get(f'/projects/{pid}/provenance').json()['hints'].values()))
    assert hint['producer'] == {'worker': 'overseer', 'harness': None}


def test_hint_can_have_explicit_harness_without_changing_its_public_content(client):
    pid = generic(client)
    response = client.post(f'/projects/{pid}/hints', json={
        'content': 'a hint', 'creator': 'operator', 'worker': 'selected-agent', 'harness': 'pi',
    })
    assert response.status_code == 201
    assert response.json()['creator'] == 'operator' and 'harness' not in response.json()
    assert client.get(f'/projects/{pid}/provenance').json()['hints'][response.json()['id']]['producer'] == {
        'worker': 'selected-agent', 'harness': 'pi',
    }


def test_database_upgrade_preserves_existing_graph_and_is_repeatable(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.db'
    original = db.SCHEMA.replace('    creator_harness TEXT,\n', '').replace('    worker_harness TEXT,\n', '')
    original = original.replace('    worker TEXT,\n    harness TEXT,\n', '')
    with sqlite3.connect(path) as conn:
        conn.executescript(original)
        conn.execute("INSERT INTO projects (id,title,created_at) VALUES ('old','old','now')")
        conn.execute("INSERT INTO facts VALUES ('f001','old','saved result')")
        conn.execute("INSERT INTO intents (id,project_id,to_fact_id,description,creator,worker,created_at,concluded_at) VALUES ('i001','old','f001','work','planner','agent','then','now')")
        conn.execute("INSERT INTO hints VALUES ('h001','old','saved hint','operator','now')")
    monkeypatch.setattr(db, '_db_path', None)
    db.configure(path)
    with db.get_conn() as conn:
        db._ensure_provenance_columns(conn)
        assert dict(conn.execute('SELECT * FROM facts').fetchone()) == {'id': 'f001', 'project_id': 'old', 'description': 'saved result'}
        edge = dict(conn.execute('SELECT * FROM intents').fetchone())
        assert edge['worker'] == 'agent' and edge['to_fact_id'] == 'f001'
        assert edge['creator_harness'] is None and edge['worker_harness'] is None
        hint = dict(conn.execute('SELECT * FROM hints').fetchone())
        assert hint['content'] == 'saved hint' and hint['harness'] is None and hint['worker'] is None


def test_initial_open_intent_can_produce_research_with_an_actual_harness(audit):
    cairn, http, _config, pid, _repo = audit
    saved_config(pid, {'workers': [{'name': 'audit-controller', 'type': 'codex'}]})
    target = next(i for i in cairn.get_project(pid).intents if i.to is None)
    response = http.post(f'/projects/{pid}/intents/{target.id}/conclude', json={
        'worker': 'first-explorer', 'harness': 'minimax', 'description': 'new research result',
    })
    assert response.status_code == 200, response.text
    data = http.get(f'/projects/{pid}/provenance').json()
    assert data['intents'][target.id]['creator'] == {'worker': 'audit-controller', 'harness': None}
    assert data['intents'][target.id]['executor'] == {'worker': 'first-explorer', 'harness': 'minimax'}
    assert data['facts'][response.json()['fact']['id']] == {
        'kind': 'research', 'producer': {'worker': 'first-explorer', 'harness': 'minimax'}, 'intent_id': target.id,
    }


def test_claimed_creation_and_legacy_heartbeat_keep_known_harness(client):
    pid = generic(client)
    response = client.post(f'/projects/{pid}/intents', json={
        'from': ['origin'], 'description': 'bootstrap work', 'creator': 'same-worker',
        'worker': 'same-worker', 'harness': 'pi',
    })
    assert response.status_code == 201, response.text
    iid = response.json()['id']
    assert client.post(f'/projects/{pid}/intents/{iid}/heartbeat', json={'worker': 'same-worker'}).status_code == 200
    result = client.post(f'/projects/{pid}/intents/{iid}/conclude', json={'worker': 'same-worker', 'description': 'done'})
    assert result.status_code == 200, result.text
    data = client.get(f'/projects/{pid}/provenance').json()
    assert data['intents'][iid]['creator'] == data['intents'][iid]['executor'] == {'worker': 'same-worker', 'harness': 'pi'}
    assert data['facts'][result.json()['fact']['id']]['producer']['harness'] == 'pi'


def test_old_final_record_remains_unknown_even_if_alias_matches_a_worker(audit):
    _cairn, http, _config, pid, _repo = audit
    saved_config(pid, {'workers': [{'name': 'final-reviewer', 'type': 'pi'}]})
    request = claim(audit, completion(audit)['id'])
    assert finish(audit, request, review_body(audit, request)).status_code == 200
    source = http.get(f'/projects/{pid}/provenance').json()
    edge = next(item for item in source['intents'].values() if item['kind'] == 'completion')
    assert edge['executor'] == {'worker': 'final-reviewer', 'harness': None}


def test_provenance_neither_mutates_revisions_nor_changes_review_inputs(audit):
    cairn, http, _config, pid, _repo = audit
    target = next(i for i in cairn.get_project(pid).intents if i.to is None)
    before = cairn.get_review_input(pid, 'intent', target.id)
    revision = cairn.get_audit(pid).audit_revision
    http.post(f'/projects/{pid}/intents/{target.id}/heartbeat', json={'worker': 'observer', 'harness': 'codex'})
    assert http.get(f'/projects/{pid}/provenance').status_code == 200
    assert cairn.get_review_input(pid, 'intent', target.id) == before
    assert cairn.get_audit(pid).audit_revision == revision


def test_research_description_does_not_override_explicit_model_authorship(client):
    pid = generic(client)
    created = client.post(f'/projects/{pid}/intents', json={
        'from': ['origin'], 'description': 'external_feedback', 'creator': 'agent', 'harness': 'codex',
    })
    iid = created.json()['id']
    result = client.post(f'/projects/{pid}/intents/{iid}/conclude', json={
        'worker': 'agent', 'harness': 'codex', 'description': 'a model result',
    })
    assert result.status_code == 200, result.text
    data = client.get(f'/projects/{pid}/provenance').json()
    assert data['facts'][result.json()['fact']['id']]['kind'] == 'research'
    assert data['intents'][iid]['executor']['harness'] == 'codex'
