'use strict';
let state = null;
let token = null;
let currentRoute = '';
const main = document.querySelector('#main');
const dialog = document.querySelector('#draft-dialog');
const $ = (tag, className, text) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
};
const append = (parent, ...children) => { children.filter(Boolean).forEach(child => parent.append(child)); return parent; };
const button = (text, action, className = 'btn') => { const b = $('button', className, text); b.type = 'button'; b.addEventListener('click', () => run(action, b)); return b; };
const tag = (text, kind = '') => $('span', `tag ${kind}`, text);
const projectName = id => (state.projects || []).find(p => p.id === id)?.name || 'Unmapped project';
const readable = value => typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value || 'unknown').replaceAll('_', ' ');
const date = value => {
  if (!value) return 'No date promised';
  const d = new Date(value.length === 10 ? `${value}T12:00:00` : value);
  return Number.isNaN(d.valueOf()) ? value : d.toLocaleDateString(undefined, {month:'short', day:'numeric'});
};
function notice(message, error = false) {
  const box = document.querySelector('#notice'); box.textContent = message; box.className = error ? 'error' : ''; box.hidden = false;
}
async function run(action, control) {
  if (control) control.disabled = true;
  try { await action(); } catch (error) { notice(error.message, true); }
  finally { if (control) control.disabled = false; }
}
async function request(path, method = 'GET', body) {
  if (method !== 'GET' && !token) token = (await request('/api/session')).token;
  const response = await fetch(path, {method, headers: method === 'GET' ? {} : {'Content-Type':'application/json', 'X-Copilot-Token':token}, ...(body !== undefined ? {body:JSON.stringify(body)} : {})});
  if (response.status === 409) throw new Error('This record changed since you opened it. Refresh and review the latest evidence before saving. Your change was not applied.');
  if (!response.ok) { let detail; try { detail = (await response.json()).detail; } catch (_) {} throw new Error(typeof detail === 'string' ? detail : `Request failed (${response.status}). Nothing was sent.`); }
  return response.json();
}
function safeLink(url, label) {
  try { const parsed = new URL(url); if (!['http:', 'https:'].includes(parsed.protocol)) return $('span', '', label); }
  catch (_) { return $('span', '', label); }
  const link = $('a', '', label); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; return link;
}
function heading(eyebrow, title, description) {
  return append($('div','page-heading'), $('div','eyebrow',eyebrow), $('h1','',title), $('p','lede',description));
}
function sectionTitle(text, count, suffix) {
  const h = $('h2','',text); if (count !== undefined) h.append($('span','',count));
  return append($('div','section-title'), h, suffix ? $('small','',suffix) : null);
}
function empty(text) { return $('div','empty',text); }
function evidence(sources, open = false) {
  const box = $('details','evidence'); box.open = open;
  box.append($('summary','',`Evidence · ${(sources || []).length} source${sources?.length === 1 ? '' : 's'}`));
  for (const source of sources || []) {
    const item = $('div','evidence-item');
    append(item, $('b','',source.title || source.id), $('div','source-meta',`${readable(source.provider)} · revision ${source.revision ?? '?'} · observed ${date(source.observed_at)}${source.deleted ? ' · DELETED' : ''}`));
    if (source.body) item.append($('pre','source-body',source.body));
    if (source.url) item.append(safeLink(source.url, 'Open original ↗'));
    else item.append($('span','source-meta',source.id));
    if (!source.deleted) item.append(button('Map to projects', () => openSourceMap(source), 'text-button'));
    box.append(item);
  }
  return box;
}
function openSourceMap(source) {
  const content = document.querySelector('#draft-content'); content.replaceChildren();
  const title = $('h2','','Link source to projects'); title.id = 'draft-title';
  append(content,append($('div','dialog-heading'),title,button('Close',() => dialog.close(),'btn small')),$('p','lede',source.title || source.id),$('p','subtle-note','Choose every relevant project. Shared rooms and notes can support multiple projects. This changes local mappings only, never the original note.'));
  const selections = [];
  const existing = source.projects || [];
  state.projects.forEach(p => {
    const row = $('div','mapping-row'); const label = $('label'); const check = $('input'); check.type = 'checkbox';
    const prior = existing.filter(x => (typeof x === 'string' ? x : x.id) === p.id); check.checked = prior.length > 0;
    append(label,check,$('span','',p.name));
    const streamNames = [...new Set(['general','commercial','engineering','product',...prior.map(x=>x.workstream || 'general')])];
    const stream = selectControl('Workstreams (Ctrl/Cmd for multiple)',streamNames.map(x=>[x,x]),''); stream.select.multiple = true; stream.select.size = 4;
    [...stream.select.options].forEach(option => { option.selected = prior.some(x => (x.workstream || 'general') === option.value) || (!prior.length && option.value === 'general'); });
    append(row,label,stream.label);content.append(row);selections.push({project:p,check,stream:stream.select});
  });
  if (!state.projects.length) append(content,empty('No projects yet. Create one before mapping this source.'),button('Create project',openProjectCreate,'btn primary'));
  else content.append(button('Save project links', async () => {
    const receipt = await request('/api/sources/map','POST',{source_id:source.id,revision:String(source.mapping_revision || source.revision),projects:selections.filter(x=>x.check.checked).flatMap(x=>[...x.stream.selectedOptions].map(option=>({id:x.project.id,name:x.project.name,workstream:option.value})))});
    dialog.close(); await refresh(); notice(receipt.projection_warning || 'Source-to-project links saved. Original content preserved.');
  },'btn primary'));
  if (!dialog.open) dialog.showModal();
}
function obligationCard(item, reviewMode = false) {
  const card = $('article',`card ${item.owner === 'me' && item.status === 'open' ? 'emphasis' : ''}`);
  const meta = append($('div','card-meta'), $('span','project-label',projectName(item.project_id)), tag(readable(item.status), item.status === 'candidate' ? 'warning' : 'neutral'));
  if (item.commitment) meta.append(tag('Commitment'));
  append(card, meta, $('h3','',item.title));
  if (item.review_reason) card.append($('p','draft-warning',readable(item.review_reason)));
  const owner = item.owner === 'me' ? 'You own this' : item.owner ? `Owner: ${item.owner}` : 'Ownership needs review';
  append(card, $('p','',`${owner} · ${item.due_at ? `Due ${date(item.due_at)}` : 'No deadline stated'}`), $('div','freshness',`Question: ${readable(item.question_state)} · Evidence: ${readable(item.freshness)}`), item.follow_up_at ? $('p','follow-up',`Follow up ${date(item.follow_up_at)} · reminder, not a deadline`) : null);
  const actions = $('div','card-actions');
  actions.append(button('Prepare reply ↗', () => openDraft(item), 'btn primary small'));
  if (!reviewMode) actions.append(button('Review', () => { const form = card.querySelector('.review-form'); if (form) form.remove(); else card.append(reviewForm(item)); }, 'btn small'));
  append(card, actions, evidence(item.evidence));
  if (reviewMode) card.append(reviewForm(item));
  return card;
}
function selectControl(labelText, values, current) {
  const label = $('label','',labelText); const select = $('select');
  values.forEach(([value, text]) => { const option = $('option','',text); option.value = value; option.selected = value === current; select.append(option); });
  label.append(select); return {label, select};
}
function reviewForm(item) {
  const form = $('div','review-form');
  const row = $('div','form-row');
  const status = selectControl('Obligation', [['candidate','Candidate'],['open','Open'],['waiting','Waiting'],['resolved','Resolved'],['dismissed','Dismissed']], item.status);
  const owner = selectControl('Owner', [['','Unresolved'],['me','Me'],...(item.owner && item.owner !== 'me' ? [[item.owner,item.owner]] : [])], item.owner || '');
  const project = selectControl('Project', [['','Unmapped'],...state.projects.map(p => [p.id,p.name])], item.project_id || '');
  const question = selectControl('Question state (manual record)', [['pending','Pending'],['sent','Sent elsewhere'],['answered','Answered']], item.question_state || 'pending');
  append(row,status.label,owner.label,project.label,question.label);
  append(form,row,$('p','subtle-note','Recording a review does not send a reply. Only mark “Sent elsewhere” after you have actually sent it in the original tool.'),button('Save review', async () => {
    await request(`/api/obligations/${encodeURIComponent(item.id)}/review`,'POST',{revision:item.revision,changes:{status:status.select.value,owner:owner.select.value || null,project_id:project.select.value || null,question_state:question.select.value}});
    notice('Review saved locally. Nothing is sent.'); await refresh();
  },'btn small'));
  return form;
}
function itemList(items, reviewMode = false, emptyText = 'Nothing here within the observed sources. Check Sources & coverage for gaps.') {
  const list = $('div','stack'); if (!items.length) list.append(empty(emptyText));
  items.forEach(item => list.append(obligationCard(item,reviewMode))); return list;
}
function today() {
  const active = state.obligations.filter(o => !['resolved','dismissed'].includes(o.status));
  const owed = active.filter(o => o.owner === 'me' && o.status === 'open');
  const waiting = active.filter(o => o.status === 'waiting');
  const review = state.review || active.filter(o => o.status === 'candidate');
  const day = state.today?.date || new Date().toISOString().slice(0,10);
  append(main,heading(new Date(`${day}T12:00:00`).toLocaleDateString(undefined,{weekday:'long',month:'long',day:'numeric'}),'A clearer working day.','The context is gathered. Here’s what needs your attention — and what can wait.'));
  const strip = $('div','summary-strip');
  [[owed.length,'NEEDS YOUR RESPONSE'],[waiting.length,'WAITING ON OTHERS'],[review.length,'READY FOR REVIEW'],[state.projects.length,'PROJECTS IN CONTEXT']].forEach(([n,label]) => strip.append(append($('div','stat'),$('b','',String(n).padStart(2,'0')),$('span','',label))));
  main.append(strip);
  if (!state.projects.length) main.append(onboarding());
  const captureBar = $('div','capture-bar'); append(captureBar,$('span','','A meeting, an untagged update, a decision worth keeping.'),append($('div','card-actions'),button('Track waiting / action',openTrack,'btn small'),button('＋ Capture a note', openCapture, 'btn small'))); main.append(captureBar);
  const columns = $('div','columns'); const left = $('div'); const right = $('aside');
  append(left,sectionTitle('Needs you',owed.length,'Evidence-backed, not inbox-driven'),itemList(owed));
  if (waiting.length) append(left,append($('div','section-gap'),sectionTitle('Waiting',waiting.length),itemList(waiting)));
  const plan = $('section','card plan aside-block');
  append(plan,$('div','eyebrow','YOUR ANCHORED PLAN'),$('h2','','Today, in focus'),$('p','subtle-note','Suggested priorities, held steady for the day. New urgency is surfaced separately.'));
  const priorities = (state.today?.priorities || []).map(id => state.obligations.find(o => o.id === (id.id || id))).filter(Boolean);
  priorities.forEach((item,i) => plan.append(append($('div','plan-item'),$('span','plan-number',i+1),append($('div'),$('h3','',item.title),$('p','',`${projectName(item.project_id)} · ${item.commitment ? 'Explicit commitment' : 'Suggested focus'}`)))));
  if (!priorities.length) plan.append($('p','subtle-note','No priorities yet. Add explicit obligations through a selected note or JSON import.'));
  right.append(plan);
  const urgent = (state.today?.new_urgency || []).map(id => state.obligations.find(o => o.id === (id.id || id))).filter(Boolean);
  if (urgent.length) { const urgentBox = $('section','aside-block'); append(urgentBox,sectionTitle('New urgency',urgent.length),itemList(urgent)); right.append(urgentBox); }
  const change = $('section','aside-block'); change.append(sectionTitle('Recently changed'));
  (state.recent || []).slice(0,3).forEach(s => change.append(append($('article','card quiet-card'),append($('div','card-meta'),tag(readable(s.provider),'neutral'),$('span','',date(s.observed_at))),$('h3','',s.title || s.id),$('div','freshness',`Source revision ${s.revision ?? '?'}${s.deleted ? ' · deleted' : ''}`))));
  if (!state.recent?.length) change.append(empty('No source changes captured yet.'));
  right.append(change);
  append(right,append($('div','card quiet-card'),$('div','eyebrow','A LITTLE LESS GUESSWORK'),$('p','','Replies start from evidence. Unknowns stay unknown. You decide what leaves your computer.')));
  append(columns,left,right); main.append(columns);
  main.append($('p','subtle-note',state.coverage_note || 'Only selected, successfully captured sources are represented.'));
}
function week() {
  append(main,heading('DIRECTION, NOT JUST ACTIVITY','This week','Keep explicit commitments separate from suggested next steps. Dates are only shown when supplied by a source.'));
  const w = state.week || {}; main.append($('p','lede',`${date(w.start)} — ${date(w.end)}`));
  const cols = $('div','columns section-gap');
  append(cols,append($('section'),sectionTitle('Explicit commitments',(w.commitments || []).length),itemList(w.commitments || [])),append($('section'),sectionTitle('Suggested next steps',(w.suggestions || []).length),$('p','lede','Suggestions are not promises or automatically assigned work.'),itemList(w.suggestions || [])));
  main.append(cols);
  main.append($('p','subtle-note',`${(w.undated || []).length} undated items and ${(w.backlog || []).length} items outside this week remain in Today/attention; they are not commitments for this week.`));
}
function projects() {
  append(main,heading('ONE PROJECT, ALL THE CONTEXT','Projects','Commercial, engineering, and product workstreams — connected across rooms without losing the original evidence.'));
  main.append(button('＋ Create project',openProjectCreate,'btn primary'));
  if (!state.projects.length) main.append(onboarding());
  const grid = $('div','grid-projects section-gap');
  state.projects.forEach(p => { const card = $('article','card project-card'); append(card,$('div','eyebrow','PROJECT CONTEXT'),$('h2','',p.name),$('p','',`${state.obligations.filter(o => o.project_id === p.id && !['resolved','dismissed'].includes(o.status)).length} open obligations · Stable ID: ${p.id}`)); const link = $('a','text-button','Open project →'); link.href = `#project/${encodeURIComponent(p.id)}`; card.append(link); grid.append(card); });
  if (state.projects.length) main.append(grid);
}
async function projectPage(id) {
  const p = await request(`/api/projects/${encodeURIComponent(id)}`);
  if (currentRoute !== `project/${encodeURIComponent(id)}`) return;
  main.replaceChildren(); append(main,heading('PROJECT KNOWLEDGE',p.name,'A linked view of the original sources. This synthesis is not a substitute for the authoritative systems.'));
  const tags = $('div','tags'); (p.workstreams || []).forEach(w => tags.append(tag(typeof w === 'string' ? w : w.name))); main.append(tags);
  const rooms = $('section','card section-gap'); append(rooms,sectionTitle('Connected rooms',(p.rooms || []).length),$('p','','A room can belong to more than one project. Links are explicit, not inferred ownership.'));
  const roomTags = $('div','tags'); (p.rooms || []).forEach(r => roomTags.append(tag(r.name || r.id,'neutral'))); rooms.append(roomTags); main.append(rooms);
  const cols = $('div','columns section-gap'); append(cols,append($('section'),sectionTitle('Obligations',(p.obligations || []).length),itemList(p.obligations || [])),append($('section'),sectionTitle('Source knowledge',(p.sources || []).length),evidence(p.sources,true))); main.append(cols);
}
function reviews() {
  append(main,heading('HUMAN JUDGMENT GOES HERE','Review queue','An untagged request is a candidate, not proof that you own it. Resolve project and ownership ambiguity before treating it as a commitment.'),itemList(state.review || [],true,'No unresolved candidates in the observed sources.'));
}
function sources() {
  append(main,heading('WHAT WE KNOW — AND WHAT WE DON’T','Sources & coverage','Only deliberately selected data is captured. Laptop sleep pauses collection. Reconnect and successful catch-up do not prove remote ACL coverage.'));
  if (state.refresh_needed?.needed) main.append($('p','subtle-note',`Source verification suggested: ${state.refresh_needed.reason}. Local activity is a claim, not proof that stakeholders were informed.`));
  const list = $('div','stack');
  (state.connectors || []).forEach(c => { const card = $('article','card connector-row'); const details = $('div'); append(details,$('h3','',c.name || c.id || c.connector),$('p','',c.error || c.coverage_note || 'Read-only local collection. See checkpoint and last successful capture below.'),$('div','source-meta',`Last success: ${c.last_success || c.last_success_at || 'Not recorded'} · Checkpoint: ${typeof c.checkpoint === 'object' ? JSON.stringify(c.checkpoint) : c.checkpoint ?? 'None'}`)); append(card,details,tag(readable(c.status),['error','auth_error','expired'].includes(c.status) ? 'warning' : 'neutral')); list.append(card); });
  if (!state.connectors?.length) list.append(empty('No sources connected. Use the CLI to capture an explicitly selected vault or import normalized JSON.'));
  main.append(list);
  const sourceReview = $('section','section-gap'); append(sourceReview,sectionTitle('Recent source knowledge',(state.recent || []).length),$('p','lede','Inspect original context and map untagged notes or shared rooms to the relevant projects.'),evidence(state.recent || [],true)); main.append(sourceReview);
  const truth = $('section','card section-gap'); append(truth,sectionTitle('Integration boundaries'),$('p','','Available now: selected Markdown vault capture, normalized JSON import, and a fictional demo connector. Shared context is available to your preferred agent via the official MCP SDK.'),$('p','','Provider-specific Symphony, Jira, GitHub, Confluence and transcript mappings are not built in. A generic read-only MCP adapter requires explicit OAuth setup, an approved tool allowlist and a normalized response contract. Demo labels describe fictional evidence, not connected accounts.'),$('p','','No paid model calls, remote sends, or background login services. The app does not encrypt your local disk. Select only work data your organization permits you to process.')); main.append(truth);
}
function onboarding() {
  const card = $('section','card onboarding');
  append(card,$('div','eyebrow','START WITH YOUR REAL WORK'),$('h2','','First-run checklist'),$('p','lede','A small setup, then your context begins to connect. Nothing is imported from work accounts automatically.'));
  const steps = $('ol');
  const vault = $('li'); append(vault,$('b','',state.runtime?.vault_selected ? 'Selected vault is ready. ' : 'Select a work vault. '),$('span','',state.runtime?.vault_selected ? 'Only Markdown inside your selected folder is captured; originals remain yours.' : 'Restart with --vault /path/to/approved/work-vault. Choose a folder your organization permits you to process.')); steps.append(vault);
  steps.append(append($('li'),$('b','','Create your first project. '),$('span','','Choose a stable ID, then optional aliases and room IDs. One room may belong to several projects.')));
  steps.append(append($('li'),$('b','','Capture a note or transcript. '),$('span','','Choose a project and workstream, or leave it unmapped for review. Commercial, engineering and product are tracks within a project.')));
  steps.append(append($('li'),$('b','','Inspect, then connect. '),$('span','','Read the original evidence in Search or Projects. Run “work-copilot doctor” to check local setup; configure remote MCP only with approved access.')));
  append(card,steps,append($('div','card-actions'),button('Create project',openProjectCreate,'btn primary'),button('Capture a note',openCapture,'btn')));return card;
}
function openProjectCreate() {
  const content = document.querySelector('#draft-content'); content.replaceChildren();
  const title = $('h2','','Create project'); title.id = 'draft-title';
  append(content,append($('div','dialog-heading'),title,button('Close',()=>dialog.close(),'btn small')),$('p','lede','Stable project IDs connect notes, rooms and obligations. Aliases are hints for review, not permission to silently assign ambiguous evidence.'));
  const id = field('Stable project ID'); id.input.placeholder = 'e.g. atlas'; id.input.maxLength = 100;
  const name = field('Project name'); name.input.placeholder = 'e.g. Atlas launch'; name.input.maxLength = 200;
  const aliases = field('Aliases (optional, comma-separated)'); aliases.input.placeholder = 'Atlas pilot, AL';
  const rooms = field('Room IDs (optional, comma-separated)'); rooms.input.placeholder = 'delivery-room, commercial-room';
  append(content,id.label,name.label,aliases.label,rooms.label,$('p','subtle-note','Room IDs should be the exact stable identifiers in your imported sources. You can link multiple rooms and share a room across projects. Workstreams are assigned when capturing or mapping source evidence.'),button('Create project',async()=>{
    if (!id.input.value.trim() || !name.input.value.trim()) throw new Error('Enter a stable ID and project name.');
    const split = value => [...new Set(value.split(',').map(x=>x.trim()).filter(Boolean))];
    await request('/api/projects','POST',{id:id.input.value.trim(),name:name.input.value.trim(),aliases:split(aliases.input.value),rooms:split(rooms.input.value).map(value=>({id:value,name:value}))});
    dialog.close(); await refresh(); notice('Project created locally. Capture or map evidence to begin its knowledge view.');
  },'btn primary'));
  if (!dialog.open) dialog.showModal();
}
function field(labelText, type = 'text', value = '') {
  const label = $('label','field-label',labelText); const input = $('input'); input.type = type; input.value = value; label.append(input); return {label,input};
}
function searchPage() {
  append(main,heading('FIND THE ORIGINAL CONTEXT','Search your knowledge','Ranked lexical search across captured source text — not semantic search or an AI-generated answer. Filter before sharing context with your chosen agent.'));
  const form = $('form','card search-form');
  const query = field('Words or phrase'); query.input.placeholder = 'e.g. pilot scope or deployment'; query.input.maxLength = 300;
  const project = selectControl('Project',[['','All projects'],...state.projects.map(p=>[p.id,p.name])],'');
  const workstream = field('Workstream'); workstream.input.placeholder = 'e.g. Engineering';
  const provider = field('Provider'); provider.input.placeholder = 'e.g. vault';
  const since = field('Observed since','date'); const until = field('Observed until','date');
  const filters = append($('div','form-row'),project.label,workstream.label,provider.label,since.label,until.label);
  const submit = $('button','btn primary','Search context'); submit.type = 'submit';
  append(form,query.label,filters,submit); main.append(form);
  const resultBox = $('section','section-gap'); main.append(resultBox);
  form.addEventListener('submit',event=>{event.preventDefault(); run(async()=>{
    const params = new URLSearchParams();
    const values = {q:query.input.value.trim(),project_id:project.select.value,workstream:workstream.input.value.trim(),provider:provider.input.value.trim(),since:since.input.value ? new Date(`${since.input.value}T00:00:00`).toISOString() : '',until:until.input.value ? new Date(`${until.input.value}T23:59:59.999`).toISOString() : ''};
    Object.entries(values).forEach(([key,value])=>{if(value) params.set(key,value);});
    const result = await request(`/api/search?${params}`); const sources = Array.isArray(result) ? result : result.results || [];
    resultBox.replaceChildren(); resultBox.append(sectionTitle('Matching sources',sources.length,'Ranked lexical · inspect original evidence'));
    if (!sources.length) resultBox.append(empty('No matching captured sources. Try fewer filters or check source coverage.'));
    for(const source of sources){const card=$('article','card search-result');append(card,$('h3','',source.title || source.id),$('p','source-body',source.snippet || source.body || ''),evidence([source]));resultBox.append(card);}
  },submit);});
}
function openTrack() {
  const content = document.querySelector('#draft-content'); content.replaceChildren();
  const title = $('h2','','Track waiting / action'); title.id = 'draft-title';
  append(content,append($('div','dialog-heading'),title,button('Close',()=>dialog.close(),'btn small')),$('p','lede','Explicit local intake, not inferred ownership. A follow-up reminder is separate from a promised due date.'));
  const name = field('What needs attention?'); name.input.maxLength = 300;
  const intent = field('Stable intent key'); intent.input.placeholder = 'e.g. atlas-pilot-scope'; intent.input.maxLength = 200;
  const project = selectControl('Project',[['','Unmapped — review later'],...state.projects.map(p=>[p.id,p.name])],'');
  const status = selectControl('State',[['waiting','Waiting'],['open','Open action'],['candidate','Candidate to review']],'waiting');
  const owner = selectControl('Owner',[['me','Me'],['','Unresolved']],'me');
  const followup = field('Follow up at (your local time, optional)','datetime-local');
  const bodyLabel = $('label','source-meta','Optional supporting context'); bodyLabel.htmlFor = 'track-evidence'; const body = $('textarea','draft-editor'); body.id = 'track-evidence'; body.rows = 3;
  append(content,name.label,intent.label,append($('div','form-row'),project.label,status.label,owner.label),followup.label,bodyLabel,body,button('Track locally',async()=>{
    if(!name.input.value.trim() || !intent.input.value.trim()) throw new Error('Add a title and a stable intent key. Reuse the key only for the same request.');
    await request('/api/obligations','POST',{payload:{title:name.input.value.trim(),intent_key:intent.input.value.trim(),project_id:project.select.value || null,status:status.select.value,owner:owner.select.value || null,follow_up_at:followup.input.value ? new Date(followup.input.value).toISOString() : null,body:body.value}});
    dialog.close(); await refresh(); notice('Tracked locally. No message sent and no deadline promised.');
  },'btn primary'));
  dialog.showModal();
}
function openCapture() {
  const content = document.querySelector('#draft-content'); content.replaceChildren();
  const title = $('h2','','Capture working context'); title.id = 'draft-title';
  append(content,append($('div','dialog-heading'),title,button('Close',() => dialog.close(),'btn small')),$('p','lede','Preserve a note or transcript in your selected vault. Structured intake can propose obligations; plain text remains searchable source context.'));
  if (!state.runtime?.vault_selected) {
    append(content,$('p','draft-warning','No vault selected. Restart with --vault /path/to/approved/work-vault to enable capture. Your existing notes are preserved.'));
  } else {
    const nameLabel = $('label','source-meta','Title'); nameLabel.htmlFor = 'capture-title';
    const name = $('input'); name.id = 'capture-title'; name.placeholder = 'Meeting notes or project update'; name.maxLength = 200; name.className = 'capture-title';
    const project = selectControl('Project (optional)',[['','Unmapped — review later'],...state.projects.map(p=>[p.id,p.name])],'');
    const workstream = selectControl('Workstream',[['general','General'],['commercial','Commercial'],['engineering','Engineering'],['product','Product']],'general');
    const bodyLabel = $('label','source-meta','Note or transcript'); bodyLabel.htmlFor = 'capture-body';
    const body = $('textarea','draft-editor'); body.id = 'capture-body'; body.placeholder = 'Paste the original context. Do not include data you are not permitted to process.';
    append(content,nameLabel,name,append($('div','form-row'),project.label,workstream.label),bodyLabel,body,button('Save to selected vault',async () => {
      if (!name.value.trim() || !body.value.trim()) throw new Error('Add a title and note before saving.');
      await request('/api/inbox','POST',{title:name.value,body:body.value,project_id:project.select.value || null,workstream:workstream.select.value}); dialog.close(); await refresh(); notice('Original note saved in your selected vault and captured locally.');
    },'btn primary'));
  }
  dialog.showModal();
}
async function openDraft(item) {
  const draft = await request(`/api/obligations/${encodeURIComponent(item.id)}/draft`,'POST',{});
  renderDraft(draft,item);
  if (!dialog.open) dialog.showModal();
}
function renderDraft(draft,item) {
  const content = document.querySelector('#draft-content'); content.replaceChildren();
  const title = $('h2','','Review your reply'); title.id = 'draft-title';
  append(content,append($('div','dialog-heading'),append($('div'),$('div','eyebrow','Offline template · NOT AN LLM'),title),button('Close',() => dialog.close(),'btn small')),$('p','lede',item.title));
  if (draft.stale) content.append($('p','draft-warning','New evidence has arrived. This draft is stale. Read the latest sources and prepare a fresh reply before copying.'));
  if (draft.gaps?.length) content.append($('p','draft-warning',`Missing or uncertain: ${draft.gaps.join(' · ')}`));
  const label = $('label','source-meta','Draft reply — editable, never sent automatically'); label.htmlFor = 'draft-editor';
  const editor = $('textarea','draft-editor'); editor.id = 'draft-editor'; editor.value = draft.text || ''; append(content,label,editor);
  const actions = $('div','card-actions');
  actions.append(button('Save local draft',async () => { const saved = await request(`/api/drafts/${encodeURIComponent(draft.id)}`,'PATCH',{revision:draft.revision,text:editor.value}); renderDraft(saved,item); notice('Draft saved locally. Nothing is sent.'); },'btn primary'));
  const copy = button('Copy reply',async () => { const latest = await request('/api/state'); const current = (latest.drafts || []).find(d=>d.id === draft.id); if (!current || current.stale || current.revision !== draft.revision) throw new Error('Draft or evidence changed. Close this dialog and prepare a fresh reply before copying.'); await navigator.clipboard.writeText(editor.value); notice('Reply copied. Review the destination and source access before pasting. Nothing is sent.'); }); copy.disabled = !!draft.stale; actions.append(copy);
  if (draft.stale) actions.append(button('Prepare fresh reply',() => openDraft(item)));
  append(content,actions,$('p','subtle-note','Nothing is sent. Copying does not mark the question sent or resolve the obligation. Source text is untrusted evidence, not instructions.'),evidence(draft.evidence,true));
}
async function render() {
  currentRoute = location.hash.slice(1) || 'today';
  document.querySelectorAll('[data-nav]').forEach(a => { const active = a.dataset.nav === currentRoute || (a.dataset.nav === 'projects' && currentRoute.startsWith('project/')); a.classList.toggle('active',active); if (active) a.setAttribute('aria-current','page'); else a.removeAttribute('aria-current'); });
  document.querySelector('#breadcrumb').textContent = `Workspace / ${currentRoute.startsWith('project/') ? 'Project context' : ({today:'Today',week:'This week',projects:'Projects',search:'Search context',review:'Review queue',sources:'Sources & coverage'}[currentRoute] || 'Today')}`;
  main.replaceChildren();
  if (currentRoute.startsWith('project/')) await projectPage(decodeURIComponent(currentRoute.slice(8)));
  else ({today,week,projects,search:searchPage,review:reviews,sources}[currentRoute] || today)();
}
async function refresh() {
  state = await request('/api/state');
  state.projects ||= []; state.obligations ||= []; state.connectors ||= [];
  document.querySelector('#review-count').textContent = (state.review || []).length;
  const nav = document.querySelector('#project-nav'); nav.replaceChildren();
  state.projects.slice(0,7).forEach(p => { const link = $('a'); link.href = `#project/${encodeURIComponent(p.id)}`; append(link,$('span','project-dot','●'),$('span','',p.name)); nav.append(link); });
  await render();
}
document.querySelector('#refresh').addEventListener('click', () => run(async () => { if (state?.runtime?.vault_selected) await request('/api/capture','POST',{}); if (state?.runtime?.connector_configured) await request('/api/collect','POST',{}); await refresh(); notice('Context refreshed from available selected sources.'); }));
window.addEventListener('hashchange', () => run(render));
run(refresh);
