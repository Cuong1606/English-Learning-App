#!/usr/bin/env python3
"""Regression tests for the app's FSRS-6 scheduling and Daily Study queue.
Reference math/state transitions follow open-spaced-repetition/py-fsrs Scheduler
(default 21 parameters, 1m/10m learning, 10m relearning, fuzzing disabled).
"""
from __future__ import annotations
import importlib.util
import math
import random
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("english_server", ROOT / "server.py")
sv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sv)

W = sv.FSRS_W
DECAY = -W[20]
FACTOR = 0.9 ** (1 / DECAY) - 1
LEARN = (60, 600)
RELEARN = (600,)


def clamp_d(d): return min(max(float(d), 1.0), 10.0)
def clamp_s(s): return max(float(s), 0.001)
def initial_s(r): return clamp_s(W[r-1])
def initial_d(r, clamp=True):
    d = W[4] - math.exp(W[5]*(r-1)) + 1
    return clamp_d(d) if clamp else d

def short_s(s, r):
    inc = math.exp(W[17]*(r-3+W[18])) * (s ** -W[19])
    if r in (2,3,4): inc=max(inc,1.0)
    return clamp_s(s*inc)

def next_d(d,r):
    linear=(10-d)*(-(W[6]*(r-3)))/9
    return clamp_d(W[7]*initial_d(4,False)+(1-W[7])*(d+linear))

def R(s,last,ts):
    days=max(0,int((ts-last)//86400))
    return (1+FACTOR*days/s)**DECAY

def next_s(d,s,retr,rating):
    if rating==1:
        long=W[11]*(d**-W[12])*(((s+1)**W[13])-1)*math.exp((1-retr)*W[14])
        short=s/math.exp(W[17]*W[18])
        return clamp_s(min(long,short))
    hard=W[15] if rating==2 else 1.0
    easy=W[16] if rating==4 else 1.0
    return clamp_s(s*(1+math.exp(W[8])*(11-d)*(s**-W[9])*(math.exp((1-retr)*W[10])-1)*hard*easy))

def interval(s,ret=.9):
    ret=min(max(float(ret),.70),.99)
    return min(max(round((s/FACTOR)*((ret**(1/DECAY))-1)),1),36500)

def ref_schedule(existing,rating,ret=.9,ts=1_700_000_000.0):
    if existing:
        state=int(existing['state']); step=existing['step']; s=existing['stability']; d=existing['difficulty']; last=existing['last_review_ts']; reviews=int(existing.get('review_count',0)); lapses=int(existing.get('lapse_count',0)); intro=float(existing['introduced_at_ts'])
    else:
        state=1; step=0; s=None; d=None; last=None; reviews=0; lapses=0; intro=ts
    before=state
    days=None if last is None else int((ts-float(last))//86400)
    if state==1:
        step=0 if step is None else int(step)
        if s is None or d is None: s=initial_s(rating); d=initial_d(rating)
        elif days is not None and days<1: s=short_s(s,rating); d=next_d(d,rating)
        else: s=next_s(d,s,R(s,last,ts),rating); d=next_d(d,rating)
        if rating==1: step=0; due=ts+LEARN[0]
        elif rating==2:
            due=ts+(LEARN[0]+LEARN[1])/2 if step==0 else ts+LEARN[min(step,len(LEARN)-1)]
        elif rating==3:
            if step+1==len(LEARN): state=2; step=None; due=ts+interval(s,ret)*86400
            else: step+=1; due=ts+LEARN[step]
        else: state=2; step=None; due=ts+interval(s,ret)*86400
    elif state==2:
        if s is None or d is None: s=initial_s(rating); d=initial_d(rating)
        elif days is not None and days<1: s=short_s(s,rating)
        else: s=next_s(d,s,R(s,last,ts),rating)
        d=next_d(d,rating)
        if rating==1: lapses+=1; state=3; step=0; due=ts+RELEARN[0]
        else: due=ts+interval(s,ret)*86400
    elif state==3:
        step=0 if step is None else int(step)
        if s is None or d is None: s=initial_s(rating); d=initial_d(rating)
        elif days is not None and days<1: s=short_s(s,rating); d=next_d(d,rating)
        else: s=next_s(d,s,R(s,last,ts),rating); d=next_d(d,rating)
        if rating==1: step=0; due=ts+RELEARN[0]
        elif rating==2: due=ts+RELEARN[0]*1.5
        else: state=2; step=None; due=ts+interval(s,ret)*86400
    else: raise AssertionError(state)
    return {'state':state,'step':step,'stability':float(s),'difficulty':float(d),'due_ts':float(due),'last_review_ts':ts,'introduced_at_ts':intro,'review_count':reviews+1,'lapse_count':lapses,'last_rating':rating,'state_before':before}


def close(a,b,tol=1e-10):
    return abs(float(a)-float(b)) <= tol*max(1,abs(float(a)),abs(float(b)))

def assert_same(a,b):
    for k in ('state','step','review_count','lapse_count','last_rating','state_before'):
        assert a[k]==b[k], (k,a[k],b[k])
    for k in ('stability','difficulty','due_ts','last_review_ts','introduced_at_ts'):
        assert close(a[k],b[k]), (k,a[k],b[k])


def test_first_steps():
    t=1_700_000_000.0
    expected={1:(1,0,60),2:(1,0,330),3:(1,1,600)}
    for r,(state,step,sec) in expected.items():
        c=sv.schedule_fsrs(None,r,.9,t)
        assert (c['state'],c['step'])==(state,step)
        assert c['due_ts']-t==sec
    e=sv.schedule_fsrs(None,4,.9,t)
    assert e['state']==2 and e['step'] is None and e['due_ts']>t+86400


def test_reference_random_sequences():
    rng=random.Random(20260809)
    for _ in range(1000):
        ours=None; ref=None; ts=1_700_000_000.0
        for _step in range(rng.randint(1,30)):
            # includes same-day, on-time-ish, early and overdue reviews
            ts += rng.choice([30,60,300,600,3600,6*3600,86400,2*86400,7*86400,30*86400])
            rating=rng.randint(1,4)
            ret=rng.choice([.85,.90,.93,.95])
            ours=sv.schedule_fsrs(ours,rating,ret,ts)
            ref=ref_schedule(ref,rating,ret,ts)
            assert_same(ours,ref)
            assert 1<=ours['difficulty']<=10
            assert ours['stability']>=.001
            assert ours['due_ts']>ts


def test_retention_direction():
    # At the same stability, higher desired retention must never lengthen interval.
    for s in (.1,.5,1,2,5,10,30,100,1000):
        iv=[sv.next_interval_days(s,r) for r in (.85,.90,.93,.95)]
        assert iv==sorted(iv,reverse=True), (s,iv)


def test_lapse_relearning():
    t=1_700_000_000.0
    c=sv.schedule_fsrs(None,4,.9,t)  # Review
    t2=c['due_ts']
    c2=sv.schedule_fsrs(c,1,.9,t2)
    assert c2['state']==3 and c2['step']==0
    assert c2['lapse_count']==1
    assert c2['due_ts']-t2==600
    c3=sv.schedule_fsrs(c2,2,.9,t2+600)
    assert c3['state']==3 and c3['due_ts']-(t2+600)==900
    c4=sv.schedule_fsrs(c3,3,.9,c3['due_ts'])
    assert c4['state']==2 and c4['step'] is None


def test_daily_queue_and_global_card():
    tmp=Path(tempfile.mkdtemp(prefix='english_srs_test_'))
    old=(sv.USER_DIR,sv.USER_DB,sv.USER_AUDIO)
    try:
        sv.USER_DIR=tmp/'user_data'; sv.USER_DB=sv.USER_DIR/'learning.sqlite'; sv.USER_AUDIO=tmp/'user_audio'
        with sv.content_conn() as c:
            topics,cols=sv.ordered_collections(c)
            core=next(x for x in cols if not x['is_vocabulary'])
        with sv.user_conn() as u:
            sv.setting_set(u,'active_collection_key',f"core:{core['id']}")
            sv.setting_set(u,'new_per_day','3')
            u.commit()
        d=sv.daily_session()
        assert d['dueCount']==0 and d['newCount']<=3
        assert len({x['item_key'] for x in d['items']})==len(d['items'])
        assert d['items'], 'source collection unexpectedly empty'
        key=d['items'][0]['item_key']
        # One review creates exactly one global SRS card for that sentence.
        sv.apply_review(key,3,'test')
        sv.apply_review(key,3,'test')
        with sv.user_conn() as u:
            assert u.execute('SELECT COUNT(*) FROM fsrs_cards WHERE item_key=?',(key,)).fetchone()[0]==1
            assert u.execute('SELECT COUNT(*) FROM review_log WHERE item_key=?',(key,)).fetchone()[0]==2
            # Force this card due now and ensure due-only mode returns it.
            u.execute('UPDATE fsrs_cards SET due_ts=? WHERE item_key=?',(sv.now_ts()-1,key)); u.commit()
        d2=sv.daily_session('0')
        assert d2['dueCount']>=1
        assert key in {x['item_key'] for x in d2['items']}
    finally:
        sv.USER_DIR,sv.USER_DB,sv.USER_AUDIO=old
        shutil.rmtree(tmp,ignore_errors=True)


def test_daily_source_switch_preserves_progress_and_deleted_my_island_falls_back():
    tmp,old=_temp_user_env('english_daily_source_test_')
    try:
        with sv.content_conn() as c:
            _topics,collections=sv.ordered_collections(c)
            learn=next(x for x in collections if not x['is_vocabulary'])
            vocabulary=next(x for x in collections if x['is_vocabulary'])
            course=next(x for x in sv.get_courses(c) if x['key']=='english_by_topic')['collections'][0]
        sources=[f"core:{learn['id']}",f"core:{vocabulary['id']}",f"core:{course['id']}"]
        with sv.user_conn() as u:
            u.execute("INSERT INTO collection_progress(collection_key,last_index,updated_at_ts) VALUES(?,?,?)",(sources[0],7,sv.now_ts()))
            u.commit()
        for key in sources:
            assert sv.set_active_source(key)['collectionKey']==key
            boot=sv.get_bootstrap()
            assert boot['activeSource']['collectionKey']==key
        with sv.user_conn() as u:
            assert u.execute("SELECT last_index FROM collection_progress WHERE collection_key=?",(sources[0],)).fetchone()[0]==7

        island=sv.create_my_island('Temporary Daily Source','fallback test')
        my_key=f"my:{island['id']}"
        sv.set_active_source(my_key)
        assert sv.get_bootstrap()['activeSource']['collectionKey']==my_key
        sv.delete_my_island(island['id'])
        boot=sv.get_bootstrap()
        assert boot['activeSource'] is not None
        assert boot['activeSource']['collectionKey']==sv.USER_SETTING_DEFAULTS['active_collection_key']

        with sv.user_conn() as u:
            sv.setting_set(u,'active_collection_key','my:999999')
            u.commit()
        session=sv.daily_session()
        assert session['activeSource'] is not None
        with sv.user_conn() as u:
            assert sv.setting_get(u,'active_collection_key')==sv.USER_SETTING_DEFAULTS['active_collection_key']
    finally:
        _restore_user_env(tmp,old)


def test_course_catalog_is_uniform_and_data_driven():
    assert [x['key'] for x in sv.COURSE_CATALOG]==['essential4000','common_phrases','english_by_topic']
    with sv.content_conn() as c:
        courses=sv.get_courses(c)
    assert [x['key'] for x in courses]==['essential4000','common_phrases','english_by_topic']
    for course in courses:
        assert {'key','name','description','layout','sentence_count','audio_available','audio_missing','collections','sections'}<=set(course)
        assert course['sentence_count']==sum(int(x['sentence_count']) for x in course['collections'])
    essential,phrases,topic=courses
    assert len(essential['sections'])==6
    assert len(essential['collections'])==180 and essential['sentence_count']==3600
    assert len(phrases['collections'])==1 and phrases['sentence_count']==852
    assert len(topic['collections'])==30 and topic['sentence_count']==990


def test_auto_delay_setting_validation():
    tmp=Path(tempfile.mkdtemp(prefix='english_setting_test_'))
    old=(sv.USER_DIR,sv.USER_DB,sv.USER_AUDIO)
    try:
        sv.USER_DIR=tmp/'user_data'; sv.USER_DB=sv.USER_DIR/'learning.sqlite'; sv.USER_AUDIO=tmp/'user_audio'
        sv.user_conn().close()
        assert float(sv.update_setting('list_auto_delay',2.5)['value'])==2.5
        assert float(sv.update_setting('list_auto_delay',-5)['value'])==0
        assert float(sv.update_setting('list_auto_delay',99)['value'])==10
    finally:
        sv.USER_DIR,sv.USER_DB,sv.USER_AUDIO=old
        shutil.rmtree(tmp,ignore_errors=True)


def test_user_conn_creates_missing_parent_directories():
    tmp=Path(tempfile.mkdtemp(prefix='english_fresh_install_test_'))
    old=(sv.USER_DIR,sv.USER_DB,sv.USER_AUDIO)
    try:
        base=tmp/'missing'/'EnglishLocal'
        sv.USER_DIR=base/'user_data'; sv.USER_DB=sv.USER_DIR/'learning.sqlite'; sv.USER_AUDIO=base/'user_audio'
        sv.user_conn().close()
        assert sv.USER_DB.is_file()
        assert sv.USER_AUDIO.is_dir()
    finally:
        sv.USER_DIR,sv.USER_DB,sv.USER_AUDIO=old
        shutil.rmtree(tmp,ignore_errors=True)



def _temp_user_env(prefix):
    tmp=Path(tempfile.mkdtemp(prefix=prefix))
    old=(sv.USER_DIR,sv.USER_DB,sv.USER_AUDIO)
    sv.USER_DIR=tmp/'user_data'; sv.USER_DB=sv.USER_DIR/'learning.sqlite'; sv.USER_AUDIO=tmp/'user_audio'
    sv.user_conn().close()
    return tmp,old


def _restore_user_env(tmp,old):
    sv.USER_DIR,sv.USER_DB,sv.USER_AUDIO=old
    shutil.rmtree(tmp,ignore_errors=True)


def _first_core_keys(count=3):
    with sv.content_conn() as c:
        topics,cols=sv.ordered_collections(c)
        core=next(x for x in cols if not x['is_vocabulary'])
        keys=[]
        for r in c.execute('SELECT content_id FROM content_membership WHERE island_id=? ORDER BY order_index,sentence_id',(core['id'],)):
            k=sv.item_key_for_content(r[0],c)
            if k not in keys: keys.append(k)
            if len(keys)>=count: break
    return core,keys


def test_review_now_and_reset_preserve_memory_and_history():
    tmp,old=_temp_user_env('english_srs_manage_test_')
    try:
        _,keys=_first_core_keys(1); key=keys[0]
        sv.apply_review(key,4,'test')
        with sv.user_conn() as u:
            before=dict(u.execute('SELECT * FROM fsrs_cards WHERE item_key=?',(key,)).fetchone())
            logs_before=u.execute('SELECT COUNT(*) FROM review_log WHERE item_key=?',(key,)).fetchone()[0]
        r=sv.manage_srs('review_now',item_key=key)
        assert r['affected']==1
        with sv.user_conn() as u:
            after=dict(u.execute('SELECT * FROM fsrs_cards WHERE item_key=?',(key,)).fetchone())
            logs_after=u.execute('SELECT COUNT(*) FROM review_log WHERE item_key=?',(key,)).fetchone()[0]
        for field in ('state','step','stability','difficulty','last_review_ts','introduced_at_ts','review_count','lapse_count','last_rating'):
            assert after[field]==before[field], (field,before[field],after[field])
        assert after['due_ts']<=sv.now_ts()
        assert logs_after==logs_before
        sv.manage_srs('suspend',item_key=key)
        reset=sv.manage_srs('reset',item_key=key)
        assert reset['affected']==1 and reset['history_preserved']
        with sv.user_conn() as u:
            assert u.execute('SELECT 1 FROM fsrs_cards WHERE item_key=?',(key,)).fetchone() is None
            assert u.execute('SELECT COUNT(*) FROM review_log WHERE item_key=?',(key,)).fetchone()[0]==logs_before
            assert u.execute('SELECT 1 FROM suspended_items WHERE item_key=?',(key,)).fetchone() is not None
        info=sv.get_srs_info(item_key=key)
        assert info['state']=='New' and info['suspended'] is True and info['history_count']==logs_before
    finally:
        _restore_user_env(tmp,old)


def test_suspend_resume_excluded_from_daily_for_new_and_due_cards():
    tmp,old=_temp_user_env('english_suspend_test_')
    try:
        core,keys=_first_core_keys(4)
        with sv.user_conn() as u:
            sv.setting_set(u,'active_collection_key',f"core:{core['id']}")
            sv.setting_set(u,'new_per_day','3'); u.commit()
        key=keys[0]
        sv.manage_srs('suspend',item_key=key)
        d=sv.daily_session()
        assert key not in {x['item_key'] for x in d['items']}
        assert d['newCount']<=3
        sv.manage_srs('resume',item_key=key)
        d2=sv.daily_session()
        assert key in {x['item_key'] for x in d2['items']}
        sv.apply_review(key,4,'test')
        with sv.user_conn() as u:
            u.execute('UPDATE fsrs_cards SET due_ts=? WHERE item_key=?',(sv.now_ts()-5,key)); u.commit()
        sv.manage_srs('suspend',item_key=key)
        d3=sv.daily_session('0')
        assert key not in {x['item_key'] for x in d3['items']}
        sv.manage_srs('resume',item_key=key)
        d4=sv.daily_session('0')
        assert key in {x['item_key'] for x in d4['items']}
    finally:
        _restore_user_env(tmp,old)


def test_bulk_collection_uses_unique_canonical_keys():
    tmp,old=_temp_user_env('english_bulk_srs_test_')
    try:
        ck='core:230'  # known source collection with repeated canonical memberships
        info=sv.get_srs_info(collection_key_value=ck)
        assert info['total']==293, info
        r=sv.manage_srs('suspend',collection_key_value=ck)
        assert r['affected']==info['total']
        info2=sv.get_srs_info(collection_key_value=ck)
        assert info2['counts']['suspended']==info['total']
        assert sv.manage_srs('suspend',collection_key_value=ck)['affected']==0
        assert sv.manage_srs('resume',collection_key_value=ck)['affected']==info['total']
        with sv.content_conn() as c:
            first=sv.item_key_for_content(c.execute('SELECT content_id FROM content_membership WHERE island_id=230 ORDER BY order_index LIMIT 1').fetchone()[0],c)
        sv.apply_review(first,4,'test')
        reset=sv.manage_srs('reset',collection_key_value=ck)
        assert reset['affected']==1
        with sv.user_conn() as u:
            assert u.execute('SELECT COUNT(*) FROM review_log WHERE item_key=?',(first,)).fetchone()[0]==1
    finally:
        _restore_user_env(tmp,old)


def test_retention_reschedule_off_on_and_learning_untouched():
    tmp,old=_temp_user_env('english_retention_reschedule_test_')
    try:
        _,keys=_first_core_keys(3)
        review1,review2,learning=keys
        sv.apply_review(review1,4,'test'); sv.apply_review(review2,4,'test'); sv.apply_review(learning,1,'test')
        with sv.user_conn() as u:
            due_before={r['item_key']:r['due_ts'] for r in u.execute('SELECT item_key,due_ts FROM fsrs_cards')}
        off=sv.update_setting('desired_retention','0.95')
        assert off['rescheduled']==0
        with sv.user_conn() as u:
            due_off={r['item_key']:r['due_ts'] for r in u.execute('SELECT item_key,due_ts FROM fsrs_cards')}
        assert due_off==due_before
        sv.update_setting('reschedule_on_retention_change','1')
        on=sv.update_setting('desired_retention','0.85')
        assert on['rescheduled']==2, on
        with sv.user_conn() as u:
            due_on={r['item_key']:r['due_ts'] for r in u.execute('SELECT item_key,due_ts FROM fsrs_cards')}
        assert due_on[review1]!=due_off[review1]
        assert due_on[review2]!=due_off[review2]
        assert due_on[learning]==due_off[learning]
    finally:
        _restore_user_env(tmp,old)



def test_course_and_book_bulk_scope():
    tmp,old=_temp_user_env('english_course_srs_test_')
    try:
        course=sv.get_srs_info(group_key='course:essential4000')
        book=sv.get_srs_info(group_key='book:essential4000:1')
        assert course['total']==3600
        assert book['total']==600
        r=sv.manage_srs('suspend',group_key='book:essential4000:1')
        assert r['affected']==600
        course2=sv.get_srs_info(group_key='course:essential4000')
        assert course2['counts']['suspended']==600
        r2=sv.manage_srs('resume',group_key='course:essential4000')
        assert r2['affected']==600
    finally:
        _restore_user_env(tmp,old)


def test_english_by_topic_content_runtime_saved_and_srs():
    expected_units={800:("U1 · Family",20),814:("U15 · Directions",36),829:("U30 · Wedding",39)}
    expected_aliases={
        30156:20086,
        30290:1012,
        30318:24018,
        30484:1687,
        30672:2903,
    }
    with sv.content_conn() as c:
        courses={course['key']:course for course in sv.get_courses(c)}
        topic=courses['english_by_topic']
        assert len(topic['units'])==30
        assert topic['sentence_count']==990
        assert topic['audio_available']==990 and topic['audio_missing']==0
        assert c.execute("SELECT COUNT(*) FROM content_membership WHERE source_group='course:english_by_topic'").fetchone()[0]==990
        assert c.execute("SELECT COUNT(*) FROM content_audio WHERE audio_path LIKE 'english_by_topic/%'").fetchone()[0]==990
        assert c.execute("SELECT COUNT(*) FROM sentence_content WHERE content_id BETWEEN 30001 AND 30990 AND instr(en_us,'/')>0").fetchone()[0]==135
        aliases={r[0]:r[1] for r in c.execute("SELECT content_id,canonical_content_id FROM srs_alias WHERE content_id BETWEEN 30001 AND 30990")}
        assert aliases==expected_aliases
        assert c.execute("SELECT COUNT(*) FROM sentence_content s LEFT JOIN srs_alias a ON a.content_id=s.content_id WHERE s.content_id BETWEEN 30001 AND 30990 AND a.content_id IS NULL").fetchone()[0]==985

    tmp,old=_temp_user_env('english_by_topic_runtime_test_')
    try:
        opened={}
        for collection_id,(name,count) in expected_units.items():
            collection=sv.get_core_collection(collection_id)
            opened[collection_id]=collection
            assert collection['collection']['name']==name
            assert len(collection['items'])==count
            assert all(item['en_us'] and item['vi_vn'] for item in collection['items'])
            assert all(str(item['audio']).startswith('/course-audio/english_by_topic/') for item in collection['items'])

        # Learn, Shadowing, and Active Recall consume this same complete item payload.
        first=opened[800]['items'][0]
        assert {'en_us','vi_vn','audio','item_key'}<=set(first)
        saved=sv.bookmark_item(first['item_key'],True)
        assert saved['saved'] is True
        saved_items=sv.get_saved_items()
        assert len(saved_items)==1 and saved_items[0]['item_key']==first['item_key']
        review=sv.apply_review(first['item_key'],3,'active_recall')
        assert review['ok'] and review['item_key']==first['item_key']
        refreshed=sv.get_core_collection(800)
        assert refreshed['items'][0]['saved'] is True
        assert refreshed['items'][0]['srs'] is not None

        unit=sv.get_srs_info(collection_key_value='core:814')
        course=sv.get_srs_info(group_key='course:english_by_topic')
        assert unit['total']==36 and course['total']==990
        assert sv.manage_srs('suspend',collection_key_value='core:814')['affected']==36
        assert sv.get_srs_info(group_key='course:english_by_topic')['counts']['suspended']==36
        assert sv.manage_srs('resume',group_key='course:english_by_topic')['affected']==36
        assert sv.manage_srs('suspend',group_key='course:english_by_topic')['affected']==990
        assert sv.manage_srs('resume',group_key='course:english_by_topic')['affected']==990
    finally:
        _restore_user_env(tmp,old)


def test_english_by_topic_import_rolls_back_atomically():
    importer_spec=importlib.util.spec_from_file_location('english_by_topic_importer',ROOT/'scripts'/'import_english_by_topic.py')
    importer=importlib.util.module_from_spec(importer_spec)
    importer_spec.loader.exec_module(importer)
    tmp=Path(tempfile.mkdtemp(prefix='english_by_topic_rollback_test_'))
    database=tmp/'content.sqlite'
    try:
        with sv.content_conn() as source:
            source_rows=source.execute(
                """SELECT co.name,co.description,m.order_index,s.en_us,s.vi_vn,ca.audio_path
                   FROM content_membership m JOIN collections co ON co.id=m.island_id
                   JOIN sentence_content s ON s.content_id=m.content_id
                   JOIN content_audio ca ON ca.content_id=m.content_id
                   WHERE co.source_group='course:english_by_topic'
                   ORDER BY co.id,m.order_index,m.sentence_id"""
            ).fetchall()
        rows=[];topics_by_unit={}
        for row in source_rows:
            unit,topic_en=str(row['name']).split(' · ',1)
            item={
                'course':'English by Topic','unit':unit,'topic_en':topic_en,
                'topic_vi':str(row['description']),'index':str(row['order_index']),
                'audio_file':Path(str(row['audio_path'])).name,
                'english':str(row['en_us']),'vietnamese':str(row['vi_vn']),
            }
            rows.append(item)
            topics_by_unit.setdefault(unit,{
                'unit':unit,'topic_en':topic_en,'topic_vi':str(row['description']),'sentence_count':'0'
            })
            topics_by_unit[unit]['sentence_count']=str(int(topics_by_unit[unit]['sentence_count'])+1)
        topics=list(topics_by_unit.values())
        assert len(rows)==990 and len(topics)==30
        shutil.copy2(sv.CONTENT_DB,database)
        with sqlite3.connect(database) as c:
            c.execute('DELETE FROM srs_alias WHERE content_id BETWEEN 30001 AND 30990')
            c.execute("DELETE FROM content_audio WHERE audio_path LIKE 'english_by_topic/%'")
            c.execute("DELETE FROM content_membership WHERE source_group='course:english_by_topic'")
            c.execute('DELETE FROM sentence_content WHERE content_id BETWEEN 30001 AND 30990')
            c.execute("DELETE FROM collections WHERE source_group='course:english_by_topic'")
            c.execute("CREATE TRIGGER force_topic_import_failure BEFORE INSERT ON sentence_content WHEN NEW.content_id=30500 BEGIN SELECT RAISE(ABORT,'forced rollback test'); END")
        try:
            importer.import_database(database,rows,topics)
            raise AssertionError('forced import failure did not occur')
        except sqlite3.IntegrityError as exc:
            assert 'forced rollback test' in str(exc)
        with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as c:
            assert c.execute("SELECT COUNT(*) FROM collections WHERE source_group='course:english_by_topic'").fetchone()[0]==0
            assert c.execute("SELECT COUNT(*) FROM content_membership WHERE source_group='course:english_by_topic'").fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM sentence_content WHERE content_id BETWEEN 30001 AND 30990').fetchone()[0]==0
            assert c.execute("SELECT COUNT(*) FROM content_audio WHERE audio_path LIKE 'english_by_topic/%'").fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM srs_alias WHERE content_id BETWEEN 30001 AND 30990').fetchone()[0]==0
            assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    finally:
        shutil.rmtree(tmp,ignore_errors=True)

def test_user_db_migration_adds_srs_management_without_losing_existing_settings():
    tmp=Path(tempfile.mkdtemp(prefix='english_migration_test_'))
    old=(sv.USER_DIR,sv.USER_DB,sv.USER_AUDIO)
    try:
        sv.USER_DIR=tmp/'user_data'; sv.USER_DB=sv.USER_DIR/'learning.sqlite'; sv.USER_AUDIO=tmp/'user_audio'
        sv.USER_DIR.mkdir(parents=True)
        con=sqlite3.connect(sv.USER_DB)
        con.execute('CREATE TABLE app_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        con.execute("INSERT INTO app_settings(key,value) VALUES('existing_marker','keep-me')")
        con.execute('CREATE TABLE custom_sentences(id INTEGER PRIMARY KEY AUTOINCREMENT,en_us TEXT NOT NULL,vi_vn TEXT NOT NULL DEFAULT \'\',usage_note TEXT NOT NULL DEFAULT \'\',literal_note TEXT NOT NULL DEFAULT \'\',audio_file TEXT,created_at_ts REAL NOT NULL,updated_at_ts REAL NOT NULL)')
        con.commit(); con.close()
        with sv.user_conn() as u:
            assert sv.setting_get(u,'existing_marker')=='keep-me'
            assert sv.setting_get(u,'reschedule_on_retention_change')=='0'
            assert u.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='suspended_items'").fetchone()
            cols={r[1] for r in u.execute('PRAGMA table_info(custom_sentences)')}
            assert {'audio_key','audio_expected','note'}<=cols
    finally:
        sv.USER_DIR,sv.USER_DB,sv.USER_AUDIO=old
        shutil.rmtree(tmp,ignore_errors=True)

def run_all():
    tests=[test_first_steps,test_reference_random_sequences,test_retention_direction,test_lapse_relearning,test_daily_queue_and_global_card,test_daily_source_switch_preserves_progress_and_deleted_my_island_falls_back,test_course_catalog_is_uniform_and_data_driven,test_auto_delay_setting_validation,test_user_conn_creates_missing_parent_directories,test_review_now_and_reset_preserve_memory_and_history,test_suspend_resume_excluded_from_daily_for_new_and_due_cards,test_bulk_collection_uses_unique_canonical_keys,test_retention_reschedule_off_on_and_learning_untouched,test_course_and_book_bulk_scope,test_english_by_topic_content_runtime_saved_and_srs,test_english_by_topic_import_rolls_back_atomically,test_user_db_migration_adds_srs_management_without_losing_existing_settings]
    for fn in tests:
        fn(); print('PASS',fn.__name__)
    print(f'PASS ALL: {len(tests)} test groups')

if __name__=='__main__': run_all()
