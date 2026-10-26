from datetime import datetime, timezone
import pytest
from work_copilot.store import Store, ConflictError
from work_copilot.demo import seed_demo


def source(**kw):
    return dict(id='sym/demo/1', revision='1', provider='symphony', title='Release question', body='Is Atlas deployed?', room_id='room1', room_name='Delivery', projects=[{'id':'atlas','name':'Atlas','workstream':'Engineering'},{'id':'beacon','name':'Beacon','workstream':'Commercial'}], obligations=[{'intent_key':'deployment-status','title':'Confirm deployment status','project_id':'atlas','owner':None}], **kw)


def test_restart_dedupe_mapping_and_uncertain_ownership(tmp_path):
    path=tmp_path/'store.db'; store=Store(path)
    assert store.ingest([source()],checkpoint='1')['inserted']==1
    assert store.ingest([source()],checkpoint='1')['unchanged']==1
    snap=Store(path).snapshot()
    assert len(snap['obligations'])==1
    assert snap['obligations'][0]['owner'] is None
    assert snap['obligations'][0]['status']=='candidate'
    assert Store(path).project('atlas')['rooms']==[{'id':'room1','name':'Delivery'}]
    assert len(Store(path).project('beacon')['sources'])==1


def test_review_revision_and_draft_staleness(tmp_path):
    store=Store(tmp_path/'s.db'); store.ingest([source()]); ob=store.snapshot()['obligations'][0]
    reviewed=store.review(ob['id'],1,{'owner':'me','status':'open'})
    with pytest.raises(ConflictError): store.review(ob['id'],1,{'status':'resolved'})
    d=store.draft(ob['id']); assert not d['stale']; assert 'not verified' in d['text']
    revised=source(); revised['revision']='2'; revised['body']='PR merged; no deployment evidence.'
    store.ingest([revised]); assert store.snapshot()['drafts'][0]['stale']
    assert store.snapshot()['obligations'][0]['owner']=='me'
    with pytest.raises(ConflictError): store.save_draft(d['id'],d['revision'],'edited stale')


def test_deleted_evidence_does_not_resolve_and_plan_anchored(tmp_path):
    store=Store(tmp_path/'s.db'); store.ingest([source()]); first=store.snapshot()['today']
    item=source(); item['id']='sym/demo/2'; item['obligations'][0]['intent_key']='new-question'; item['obligations'][0]['due_at']='2020-01-01'
    store.ingest([item]); assert store.snapshot()['today']['priorities']==first['priorities']
    assert store.snapshot()['today']['new_urgency']
    deleted=source(); deleted.update(revision='2',deleted=True)
    store.ingest([deleted]); ob=next(x for x in store.snapshot()['obligations'] if x['intent_key']=='deployment-status')
    assert ob['freshness']=='missing'; assert ob['status']!='resolved'


def test_canonical_intent_and_rollback(tmp_path):
    store=Store(tmp_path/'s.db'); a=source(); b=source(); b['id']='meeting/demo/2'; store.ingest([a,b]); assert len(store.snapshot()['obligations'])==1
    assert len(store.snapshot()['obligations'][0]['evidence'])==2
    with pytest.raises(ValueError): store.ingest([dict(a,id='third'),{'id':'bad'}],checkpoint='bad')
    assert len(store.snapshot()['recent'])==2
    assert store.snapshot()['connectors'][0]['checkpoint'] is None


def test_synthetic_auth_gap_recovery_and_demo_idempotent(tmp_path):
    store=Store(tmp_path/'s.db', now=lambda:datetime(2026,10,26,tzinfo=timezone.utc))
    seed_demo(store); before=store.snapshot(); seed_demo(store); assert len(store.snapshot()['obligations'])==len(before['obligations'])
    store.set_connector('demo','auth_expired','Fictional expiry'); assert store.snapshot()['connectors'][0]['status']=='auth_expired'
    store.set_connector('demo','catching_up'); seed_demo(store); assert store.snapshot()['connectors'][0]['status']=='healthy'
    assert store.snapshot()['week']['start']=='2026-10-26'


def test_question_states_are_separate_and_invalid_mutations_rejected(tmp_path):
    store=Store(tmp_path/'s.db'); store.ingest([source()]); ob=store.snapshot()['obligations'][0]
    result=store.review(ob['id'],1,{'question_state':'sent'}); assert result['status']=='candidate'
    with pytest.raises(ValueError): store.review(ob['id'],result['revision'],{'status':'deployed'})
    with pytest.raises(ValueError): store.review(ob['id'],result['revision'],{'owner':'me','unexpected':'x'})


def test_full_sources_and_historical_replay(tmp_path):
    store=Store(tmp_path/'s.db'); a=source(); store.ingest([a]); newer=dict(a,revision='2',body='new'); store.ingest([newer]); store.ingest([a])
    assert store.list_sources('symphony')[0]['body']=='new'
    assert store.list_sources('vault')==[]


def test_mapping_review_updates_canonical_key(tmp_path):
    store=Store(tmp_path/'s.db'); a=source(); a['obligations'][0]['project_id']=None; store.ingest([a])
    ob=store.snapshot()['obligations'][0]; store.review(ob['id'],1,{'project_id':'atlas'})
    b=source(); b['id']='new-source'; store.ingest([b]); assert len(store.snapshot()['obligations'])==1


def test_missing_transcript_draft_does_not_claim_access(tmp_path):
    store=Store(tmp_path/'s.db'); seed_demo(store)
    ob=next(o for o in store.snapshot()['obligations'] if o['intent_key']=='request-missing-transcript')
    draft=store.draft(ob['id'])
    assert 'Could you share' in draft['text']
    assert ob['status']=='waiting'


def test_revision_collision_is_rejected_but_observation_replay_allowed(tmp_path):
    store=Store(tmp_path/'s.db'); a=source(); store.ingest([a]); store.ingest([a])
    with pytest.raises(ValueError): store.ingest([dict(a,body='different payload')])


def test_timestamp_order_uses_instants_not_lexical_offsets(tmp_path):
    store=Store(tmp_path/'s.db'); a=source(observed_at='2026-10-26T10:00:00+02:00'); store.ingest([a]); b=dict(a,revision='2',observed_at='2026-10-26T09:00:00+00:00',body='newer')
    store.ingest([b]); assert store.list_sources()[0]['body']=='newer'


def test_draft_cannot_be_fresh_during_connector_gap(tmp_path):
    store=Store(tmp_path/'s.db'); store.ingest([source()],connector='demo'); ob=store.snapshot()['obligations'][0]
    store.set_connector('demo','auth_expired'); draft=store.draft(ob['id'])
    assert draft['stale']; assert store.snapshot()['obligations'][0]['freshness']=='coverage_gap'
    with pytest.raises(ConflictError): store.save_draft(draft['id'],draft['revision'],'reviewed')


def test_source_change_requests_review_without_overwriting_confirmation(tmp_path):
    store=Store(tmp_path/'s.db'); a=source(); store.ingest([a]); ob=store.snapshot()['obligations'][0]; store.review(ob['id'],1,{'owner':'me','status':'open'})
    b=source(); b['revision']='2'; b['obligations'][0]['owner']='someone-else'; store.ingest([b]); ob=store.snapshot()['obligations'][0]
    assert ob['owner']=='me'; assert ob['review_reason']; assert any(x['id']==ob['id'] for x in store.snapshot()['review'])


def test_source_mapping_preserves_revision_and_requires_latest_token(tmp_path):
    store=Store(tmp_path/'s.db'); a=source(); a['projects']=[]; a['obligations']=[]; store.ingest([a]); mapped=store.map_source(a['id'],'1',[{'id':'atlas','name':'Atlas','workstream':'Product'}])
    assert mapped['revision']=='1'; assert mapped['mapping_revision']!='1'
    assert store.project('atlas')['sources'][0]['id']==a['id']
    with pytest.raises(ConflictError): store.map_source(a['id'],'1',[])
    store.ingest([a]); assert store.list_sources()[0]['projects'][0]['id']=='atlas'


def test_obligation_mapping_projects_untagged_evidence(tmp_path):
    store=Store(tmp_path/'s.db'); a=source(); store.ingest([a]); b=source(); b.update(id='untagged',projects=[]); b['obligations'][0]['project_id']=None; b['obligations'][0]['intent_key']='unknown'; store.ingest([b]); ob=next(o for o in store.snapshot()['obligations'] if o['intent_key']=='unknown')
    store.review(ob['id'],1,{'project_id':'atlas'}); assert 'untagged' in [s['id'] for s in store.project('atlas')['sources']]


def test_draft_ages_without_new_ingestion(tmp_path):
    from datetime import timedelta
    now=[datetime(2026,10,26,tzinfo=timezone.utc)]
    store=Store(tmp_path/'s.db',now=lambda:now[0]); store.ingest([source()]); ob=store.snapshot()['obligations'][0]; d=store.draft(ob['id']); now[0]+=timedelta(days=8)
    assert store.snapshot()['drafts'][0]['stale']
    with pytest.raises(ConflictError): store.save_draft(d['id'],d['revision'],'old')


def test_mapping_new_evidence_invalidates_existing_project_draft(tmp_path):
    store=Store(tmp_path/'s.db'); store.ingest([source()]); ob=store.snapshot()['obligations'][0]; d=store.draft(ob['id'])
    b=source(); b.update(id='unmapped',projects=[],obligations=[]); store.ingest([b]); assert not store.snapshot()['drafts'][0]['stale']
    store.map_source('unmapped','1',[{'id':'atlas','name':'Atlas'}]); assert store.snapshot()['drafts'][0]['stale']


def test_fts_search_filters_updates_deletes_and_aliases(tmp_path):
    store=Store(tmp_path/'s.db'); a=source(observed_at='2026-10-26T09:00:00+00:00'); a['body']='Production rollout blocked'; store.ingest([a])
    result=store.search('Atlas rollout',project_id='atlas',workstream='Engineering',provider='symphony',since='2026-10-25',until='2026-10-27')
    assert len(result)==1; assert result[0]['source_id']==a['id']; assert 'rollout' in result[0]['snippet']
    assert store.search('rollout',project_id='unknown')==[]
    assert store.search('rollout',since='2026-11-01')==[]
    b=dict(a,revision='2',body='Production shipped'); store.ingest([b]); assert store.search('rollout')==[]; assert store.search('shipped')
    store.ingest([dict(b,revision='3',deleted=True)]); assert store.search('shipped')==[]


def test_fts_query_operators_are_text_not_sql(tmp_path):
    store=Store(tmp_path/'s.db'); store.ingest([source()]); assert isinstance(store.search('" OR * ; DROP TABLE sources'),list)
    assert store.search('')==[]
    with pytest.raises(ValueError): store.search('atlas',limit=999)


def test_track_waiting_followup_is_not_deadline_and_dedupes(tmp_path):
    store=Store(tmp_path/'s.db',now=lambda:datetime(2026,10,26,tzinfo=timezone.utc)); store.ingest([source()])
    payload={'intent_key':'atlas-transcript-alex','title':'Ask Alex for the Atlas transcript','project_id':'atlas','waiting_on':'person:alex','follow_up_at':'2026-10-27','evidence_ids':['sym/demo/1']}
    ob=store.track_obligation(payload); assert ob['status']=='waiting'; assert ob['due_at'] is None; assert ob['follow_up_at']=='2026-10-27'; assert len(ob['evidence'])==2
    again=store.track_obligation(payload); assert again['id']==ob['id']; assert len(store.snapshot()['obligations'])==2
    updated=store.review(ob['id'],ob['revision'],{'follow_up_at':'2026-10-26'}); assert updated['follow_up_at']=='2026-10-26'
    with pytest.raises(ValueError): store.track_obligation(dict(payload,evidence_ids=['missing']))


def test_connector_metadata_and_status_aliases(tmp_path):
    store=Store(tmp_path/'s.db'); store.configure_connector('remote',['mcp_read'],'Vendor schema unverified'); store.set_connector('remote','connected')
    assert store.snapshot()['connectors'][0]['status']=='healthy'; assert store.snapshot()['connectors'][0]['capabilities']==['mcp_read']


def test_reviewed_mapping_survives_source_revisions(tmp_path):
    store=Store(tmp_path/'s.db'); a=source(); a['obligations'][0]['project_id']=None; store.ingest([a]); ob=store.snapshot()['obligations'][0]; store.review(ob['id'],1,{'project_id':'atlas'})
    store.ingest([dict(a,revision='2')]); obs=store.snapshot()['obligations']; assert len(obs)==1; assert obs[0]['project_id']=='atlas'; assert len(obs[0]['evidence'])==1


def test_draft_unions_direct_evidence_outside_project_mapping(tmp_path):
    store=Store(tmp_path/'s.db'); a=source(); a['obligations']=[]; store.ingest([a]); b=source(); b.update(id='direct',projects=[],body='Important direct request'); store.ingest([b]); ob=store.snapshot()['obligations'][0]; draft=store.draft(ob['id']); assert {s['id'] for s in draft['evidence']}=={'direct','sym/demo/1'}; assert 'Important direct request' in draft['text']


def test_missing_transcript_request_not_repeated_after_sent_or_answered(tmp_path):
    store=Store(tmp_path/'s.db'); seed_demo(store); ob=next(o for o in store.snapshot()['obligations'] if o['intent_key']=='request-missing-transcript')
    ob=store.review(ob['id'],ob['revision'],{'question_state':'sent'}); assert 'Could you share' not in store.draft(ob['id'])['text']
    ob=store.review(ob['id'],ob['revision'],{'question_state':'answered'}); assert 'Could you share' not in store.draft(ob['id'])['text']


def test_revised_source_title_is_reviewable(tmp_path):
    store=Store(tmp_path/'s.db'); a=source(); store.ingest([a]); b=source(); b['revision']='2'; b['obligations'][0]['title']='Changed question'; store.ingest([b]); assert 'title' in store.snapshot()['obligations'][0]['review_reason']


def test_manual_evidence_survives_source_edit(tmp_path):
    store=Store(tmp_path/'s.db'); a=source(); a['obligations']=[]; store.ingest([a]); ob=store.track_obligation({'intent_key':'manual','title':'Follow up','evidence_ids':[a['id']]}); store.ingest([dict(a,revision='2',body='Updated evidence')]); ob=store._obligation(ob['id']); assert len(ob['evidence'])==2; assert any(s['body']=='Updated evidence' for s in ob['evidence'])


def test_project_onboarding_aliases_rooms_and_search(tmp_path):
    store=Store(tmp_path/'s.db'); p=store.create_project('atlas','Atlas launch',aliases=['North Star'],rooms=[{'id':'room/shared','name':'Shared room'}]); assert p['aliases']==['North Star']; assert p['rooms'][0]['id']=='room/shared'
    a=source(); store.ingest([a]); assert store.search('North Star deployed')
    assert store.snapshot()['projects'][0]['aliases']==['North Star']
    with pytest.raises(ConflictError): store.create_project('atlas','Other')
    with pytest.raises(ValueError): store.create_project('','Bad')


def test_timezone_today_followup_and_search_day_bounds(tmp_path):
    store=Store(tmp_path/'s.db',now=lambda:datetime(2026,10,26,1,tzinfo=timezone.utc),timezone_name='America/New_York')
    initial=source(observed_at='2026-10-26T01:00:00+00:00'); initial['obligations'][0]['owner']='me'; store.ingest([initial]); today=store.snapshot()['today']; assert today['date']=='2026-10-25'
    ob=store.track_obligation({'intent_key':'followup','title':'Follow up','follow_up_at':'2026-10-26T01:00:00+00:00'})
    assert ob['id'] in store.snapshot()['today']['new_urgency']
    assert store.search('Atlas',since='2026-10-25',until='2026-10-25')
    assert not store.search('Atlas',since='2026-10-26')
    with pytest.raises(ValueError): Store(tmp_path/'bad.db',timezone_name='Not/AZone')


def test_track_ui_optional_body_is_local_context(tmp_path):
    store=Store(tmp_path/'s.db'); ob=store.track_obligation({'intent_key':'transcript','title':'Ask for transcript','body':'Alex offered to share the notes.','owner':'me','status':'waiting','waiting_on':'Alex','follow_up_at':'2026-10-27','due_at':None,'project_id':None,'question_state':'pending','commitment':False})
    assert 'Alex offered' in ob['evidence'][0]['body']
    with pytest.raises(ValueError): store.track_obligation({'intent_key':'bad','title':'bad','body':123})


def test_week_separates_undated_and_outside_window(tmp_path):
    store=Store(tmp_path/'s.db',now=lambda:datetime(2026,10,26,tzinfo=timezone.utc))
    due=store.track_obligation({'intent_key':'due','title':'This week','due_at':'2026-10-28','commitment':True})
    follow=store.track_obligation({'intent_key':'follow','title':'Follow up this week','follow_up_at':'2026-10-30'})
    future=store.track_obligation({'intent_key':'future','title':'Future','due_at':'2026-11-10','commitment':True})
    undated=store.track_obligation({'intent_key':'undated','title':'Unscheduled'})
    week=store.snapshot()['week']
    assert [o['id'] for o in week['commitments']]==[due['id']]
    assert [o['id'] for o in week['suggestions']]==[follow['id']]
    assert [o['id'] for o in week['undated']]==[undated['id']]
    assert [o['id'] for o in week['backlog']]==[future['id']]
    assert week['undated_count']==1 and week['backlog_count']==1


def test_refresh_needed_survives_restart_without_connector_mutation(tmp_path):
    store=Store(tmp_path/'s.db'); receipt=store.request_refresh('Local activity changed',source_id=None)
    assert receipt['needed']; assert Store(tmp_path/'s.db').snapshot()['refresh_needed']['reason']=='Local activity changed'
    assert store.snapshot()['connectors']==[]


def test_empty_first_run_does_not_freeze_empty_daily_plan(tmp_path):
    store=Store(tmp_path/'s.db',now=lambda:datetime(2026,10,26,tzinfo=timezone.utc))
    assert store.snapshot()['today']['priorities']==[]
    first=store.track_obligation({'intent_key':'first','title':'My first follow-up','owner':'me','follow_up_at':'2026-10-26'})
    assert store.snapshot()['today']['priorities']==[first['id']]
    second=store.track_obligation({'intent_key':'second','title':'New urgency','owner':'me','follow_up_at':'2026-10-26'})
    today=store.snapshot()['today']; assert today['priorities']==[first['id']]; assert second['id'] in today['new_urgency']


def test_unknown_ownership_is_review_not_personal_priority(tmp_path):
    store=Store(tmp_path/'s.db'); store.ingest([source()]); snapshot=store.snapshot()
    assert snapshot['review']; assert snapshot['today']['priorities']==[]
