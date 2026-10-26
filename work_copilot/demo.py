"""Fictional demonstration fixtures. This is not a live provider connector."""
from datetime import timedelta


def seed_demo(store):
    """Replay-safe synthetic multi-room evidence anchored to first demo date."""
    existing=store._connector('demo')
    base=existing.get('demo_date') or store.now().date().isoformat()
    now=store.now()
    project=lambda id,name,ws: {'id':id,'name':name,'workstream':ws}
    atlas=lambda ws: project('atlas','Atlas launch',ws)
    beacon=lambda ws: project('beacon','Beacon onboarding',ws)
    records=[
        {'id':'demo/meeting/atlas/missing','revision':'1','provider':'meeting-demo','title':'Awaiting pilot workshop transcript','body':'Workshop transcript is not available in the observed sources. Waiting for organizer to share it; discussion and decisions are unknown.','projects':[atlas('Product')],'obligations':[{'intent_key':'request-missing-transcript','title':'Request the missing workshop transcript','project_id':'atlas','owner':'me','status':'waiting'}]},
        {'id':'demo/transcript/atlas/recap','revision':'1','provider':'transcript-demo','title':'Delivery recap — same release question','body':'Maya’s release-readiness question from the delivery room is still pending. This is the same request, not a new task. Risk: no production deployment evidence has been supplied.','projects':[atlas('Engineering')],'obligations':[{'intent_key':'release-readiness','title':'Reply to Maya on Atlas release readiness','project_id':'atlas','owner':'me','status':'open','due_at':base,'commitment':True}]},
        {'id':'demo/symphony/delivery/101','revision':'1','provider':'symphony-demo','title':'Release readiness request','body':'Maya asks: can you confirm Atlas release readiness? Engineering reports the change is merged; production deployment is not confirmed.','room_id':'delivery','room_name':'Delivery / shared launch room','projects':[atlas('Engineering'),beacon('Product')],'obligations':[{'intent_key':'release-readiness','title':'Reply to Maya on Atlas release readiness','project_id':'atlas','owner':'me','status':'open','due_at':base,'commitment':True}]},
        {'id':'demo/github/atlas/pr-42','revision':'1','provider':'github-demo','title':'Atlas PR #42 merged','body':'PR #42 merged into main. Unit checks passed. There is no deployment receipt in this source.','projects':[atlas('Engineering')],'obligations':[]},
        {'id':'demo/symphony/commercial/102','revision':'1','provider':'symphony-demo','title':'Commercial scope question','body':'Leo asks for the agreed pilot scope. Pricing approval is still with Finance; no approval date was promised.','room_id':'commercial','room_name':'Atlas commercial','projects':[atlas('Commercial')],'obligations':[{'intent_key':'pilot-scope','title':'Prepare a grounded pilot scope reply','project_id':'atlas','owner':'me','status':'waiting','question_state':'sent'}]},
        {'id':'demo/confluence/atlas/brief','revision':'3','provider':'confluence-demo','title':'Pilot scope decision · revision 3','body':'The pilot covers two teams and read-only reporting. Automated external messaging is excluded. Pricing remains undecided.','projects':[atlas('Product'),atlas('Commercial')]},
        {'id':'demo/transcript/weekly/9','revision':'1','provider':'transcript-demo','title':'Weekly product sync','body':'Someone should confirm who owns the Beacon checklist. This discussion does not assign it to you. Atlas pilot scope remains two teams.','room_id':'delivery','room_name':'Delivery / shared launch room','projects':[beacon('Product'),atlas('Product')],'obligations':[{'intent_key':'checklist-owner','title':'Review who owns the Beacon checklist','project_id':'beacon','owner':None,'status':'candidate'}]},
        {'id':'demo/jira/beacon/27','revision':'2','provider':'jira-demo','title':'BEACON-27 · onboarding review','body':'Onboarding checklist is in review. Priya owns the engineering review; the product acceptance owner remains unconfirmed.','projects':[beacon('Engineering')],'obligations':[{'intent_key':'engineering-review','title':'Waiting for Priya’s engineering review','project_id':'beacon','owner':'person:priya','status':'waiting'}]},
        {'id':'demo/transcript/aliases/10','revision':'1','provider':'transcript-demo','title':'Ambiguous Phoenix reference','body':'“Phoenix needs a follow-up.” Phoenix is an unresolved nickname, not a known project ID.','projects':[],'obligations':[{'intent_key':'resolve-alias:phoenix','title':'Map the ambiguous “Phoenix” project reference','owner':None,'status':'candidate'}]},
    ]
    for record in records:
        record['observed_at']=base+'T09:00:00+00:00'
        record['url']='https://example.invalid/'+record['id']
    result=store.ingest(records,connector='demo',checkpoint='fixture-v1')
    with store._lock, store.db:
        status=store._connector('demo'); status.update(demo_date=base,capabilities=['fictional_fixture','replay','simulated_auth_gap'],limitations='Fictional data only. No network access, real ACL enforcement or live integration.')
        store._put_connector(status)
    return result
