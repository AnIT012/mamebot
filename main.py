from flask import Flask, request, abort
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, PushMessageRequest, TextMessage, QuickReply, QuickReplyItem, MessageAction, PostbackAction
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent, JoinEvent, FollowEvent, LeaveEvent
import os
import psycopg2
import random
from datetime import datetime, timedelta, timezone, date
import threading
import time
import requests as http_requests

app = Flask(__name__)

@app.route("/", methods=['GET', 'HEAD'])
def health_check():
    return "OK", 200

configuration = Configuration(access_token=os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
DATABASE_URL = os.environ.get('DATABASE_URL')

JST = timezone(timedelta(hours=9))

def get_jst_date():
    return datetime.now(JST).date()

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS trash_schedule (
            id SERIAL PRIMARY KEY,
            group_id TEXT,
            trash_type VARCHAR(50),
            weekdays TEXT,
            week_type TEXT DEFAULT 'every'
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS bath_schedule (
            id SERIAL PRIMARY KEY,
            group_id TEXT UNIQUE,
            notify_time TIME
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            id SERIAL PRIMARY KEY,
            group_id TEXT UNIQUE
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS members (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            display_name TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS daily_schedule (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            user_name TEXT,
            depart_time TEXT,
            arrive_time TEXT,
            meal_status TEXT,
            created_date DATE,
            UNIQUE (user_id, created_date)
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS bath_done (
            id SERIAL PRIMARY KEY,
            group_id TEXT,
            done_date DATE,
            UNIQUE (group_id, done_date)
        )
    ''')
    conn.commit()

    try:
        cur.execute('ALTER TABLE groups ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT FALSE')
        conn.commit()
    except:
        conn.rollback()
    try:
        cur.execute('ALTER TABLE groups ADD COLUMN IF NOT EXISTS invite_code TEXT UNIQUE')
        conn.commit()
    except:
        conn.rollback()
    try:
        cur.execute('ALTER TABLE members ADD COLUMN IF NOT EXISTS group_id TEXT')
        conn.commit()
    except:
        conn.rollback()
    try:
        cur.execute('ALTER TABLE members DROP CONSTRAINT IF EXISTS members_user_id_key')
        conn.commit()
    except:
        conn.rollback()
    try:
        cur.execute('ALTER TABLE members ADD CONSTRAINT members_user_group_unique UNIQUE (user_id, group_id)')
        conn.commit()
    except:
        conn.rollback()
    try:
        cur.execute('ALTER TABLE daily_schedule ADD CONSTRAINT unique_daily_user UNIQUE (user_id, created_date)')
        conn.commit()
    except:
        conn.rollback()
    try:
        cur.execute('ALTER TABLE trash_schedule ADD COLUMN IF NOT EXISTS group_id TEXT')
        conn.commit()
    except:
        conn.rollback()
    try:
        cur.execute('ALTER TABLE trash_schedule ADD COLUMN IF NOT EXISTS week_type TEXT DEFAULT \'every\'')
        conn.commit()
    except:
        conn.rollback()
    try:
        cur.execute('ALTER TABLE bath_schedule ADD COLUMN IF NOT EXISTS group_id TEXT')
        conn.commit()
    except:
        conn.rollback()
    try:
        cur.execute('ALTER TABLE bath_done ADD COLUMN IF NOT EXISTS group_id TEXT')
        conn.commit()
    except:
        conn.rollback()

    cur.close()
    conn.close()

def get_group_ids():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT group_id FROM groups WHERE active = TRUE')
        rows = cur.fetchall()
        cur.close()
        conn.close()
        ids = [row[0] for row in rows]
        if not ids:
            fallback = os.environ.get('LINE_GROUP_ID')
            if fallback:
                ids = [fallback]
        return ids
    except:
        fallback = os.environ.get('LINE_GROUP_ID')
        return [fallback] if fallback else []

def get_active_gid():
    ids = get_group_ids()
    return ids[0] if ids else None

def get_user_group(user_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT group_id FROM members WHERE user_id = %s LIMIT 1', (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row else None
    except:
        return None

def push_members(text, group_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT user_id FROM members WHERE group_id = %s', (group_id,))
        member_ids = cur.fetchall()
        cur.close()
        conn.close()
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")}'
        }
        for (mid,) in member_ids:
            http_requests.post(
                'https://api.line.me/v2/bot/message/push',
                headers=headers,
                json={'to': mid, 'messages': [{'type': 'text', 'text': text}]}
            )
    except Exception as e:
        print(f'Push members error: {e}')

def push_to_group(group_id, text):
    if not group_id:
        return
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")}'
    }
    data = {
        'to': group_id,
        'messages': [{
            'type': 'textV2',
            'text': '{mention}\n' + text,
            'substitution': {
                'mention': {
                    'type': 'mention',
                    'mentionee': {'type': 'all'}
                }
            }
        }]
    }
    http_requests.post('https://api.line.me/v2/bot/message/push', headers=headers, json=data)

def push_group(text):
    for gid in get_group_ids():
        push_to_group(gid, text)

def send_dinner_summary(group_id):
    today = get_jst_date()
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        SELECT user_name, meal_status FROM daily_schedule
        WHERE created_date = %s
        AND user_id IN (SELECT user_id FROM members WHERE group_id = %s)
        ORDER BY id
    ''', (today, group_id))
    responses = cur.fetchall()
    cur.execute('''
        SELECT display_name FROM members
        WHERE group_id = %s
        AND user_id NOT IN (
            SELECT user_id FROM daily_schedule
            WHERE created_date = %s AND meal_status IS NOT NULL
        )
    ''', (group_id, today))
    unanswered = cur.fetchall()
    cur.close()
    conn.close()
    summary = '🍚 夕食まとめ'
    for r_name, r_meal in responses:
        if r_meal:
            summary += f'\n{r_name}: {r_meal}'
    for (u_name,) in unanswered:
        summary += f'\n{u_name}: 未回答'
    push_to_group(group_id, summary)

def is_nth_week(date_obj, nth_weeks):
    week_of_month = (date_obj.day - 1) // 7 + 1
    return week_of_month in nth_weeks

def reminder_loop():
    while True:
        try:
            conn = get_db()
            cur = conn.cursor()
            now = datetime.now(JST).replace(tzinfo=None)
            today_date = get_jst_date()
            weekday_map = {0:'月',1:'火',2:'水',3:'木',4:'金',5:'土',6:'日'}
            today = weekday_map[now.weekday()]
            yesterday = weekday_map[(now.weekday() - 1) % 7]
            yesterday_date = today_date - timedelta(days=1)

            cur.execute('SELECT group_id, trash_type, weekdays, week_type FROM trash_schedule')
            for group_id, trash_type, weekdays, week_type in cur.fetchall():
                if not group_id:
                    continue
                # 当日朝7時
                if today in weekdays:
                    should_notify = False
                    if week_type == 'every':
                        should_notify = True
                    elif week_type == 'odd':
                        should_notify = is_nth_week(today_date, [1, 3])
                    elif week_type == 'even':
                        should_notify = is_nth_week(today_date, [2, 4])
                    if should_notify:
                        notify_dt = datetime.combine(now.date(), datetime.strptime('07:00', '%H:%M').time())
                        diff = abs((now - notify_dt).total_seconds())
                        if diff < 90:
                            push_to_group(group_id, f'⏰ リマインド\n🗑️ 今日は{trash_type}の日です！忘れずに！')
                # 前日21時
                if yesterday in weekdays:
                    should_notify = False
                    if week_type == 'every':
                        should_notify = True
                    elif week_type == 'odd':
                        should_notify = is_nth_week(yesterday_date, [1, 3])
                    elif week_type == 'even':
                        should_notify = is_nth_week(yesterday_date, [2, 4])
                    if should_notify:
                        notify_dt = datetime.combine(now.date(), datetime.strptime('21:00', '%H:%M').time())
                        diff = abs((now - notify_dt).total_seconds())
                        if diff < 90:
                            push_to_group(group_id, f'⏰ リマインド\n🗑️ 明日は{trash_type}の日です！準備を忘れずに！')

            cur.execute('SELECT group_id, notify_time FROM bath_schedule')
            for group_id, notify_time in cur.fetchall():
                if not group_id:
                    continue
                notify_dt = datetime.combine(now.date(), notify_time)
                diff = abs((now - notify_dt).total_seconds())
                if diff < 90:
                    cur2 = conn.cursor()
                    cur2.execute('SELECT id FROM bath_done WHERE group_id = %s AND done_date = %s', (group_id, today_date))
                    done = cur2.fetchone()
                    cur2.close()
                    if not done:
                        push_to_group(group_id, '⏰ リマインド\n🛁 まだお風呂が洗われていません！')

            cur.close()
            conn.close()
        except Exception as e:
            print(f'Reminder error: {e}')
        time.sleep(60)

user_state = {}

def make_hour_qr(hours, context):
    return QuickReply(items=[
        QuickReplyItem(action=PostbackAction(label=f'{h}時', data=f'action=時&value={h}&context={context}'))
        for h in hours
    ])

def make_minute_qr(context):
    return QuickReply(items=[
        QuickReplyItem(action=PostbackAction(label=f'{m:02d}分', data=f'action=分&value={m}&context={context}'))
        for m in [0,5,10,15,20,25,30,35,40,45,50,55]
    ])

AM_HOURS = [6,7,8,9,10,11]
PM_HOURS = [12,13,14,15,16,17,18,19,20,21,22,23]

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

def send_reply(api_client, reply_token, reply):
    MessagingApi(api_client).reply_message(
        ReplyMessageRequest(reply_token=reply_token, messages=[reply])
    )

def process_action(action, value, context, user_id, api_client, reply_token):
    today = get_jst_date()
    user_group = get_user_group(user_id)

    if action == 'ごはん':
        user_state.pop(user_id, None)
        reply = TextMessage(text='何をしますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='🍽️ ご飯どうする？', data='action=ご飯どうする')),
            QuickReplyItem(action=PostbackAction(label='🔔 できました！', data='action=ごはんできた')),
        ]))

    elif action == 'ご飯どうする':
        reply = TextMessage(text='今日の夕食はどうしますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='🏠 家で食べる', data='action=夕食登録&value=家で食べる🏠')),
            QuickReplyItem(action=PostbackAction(label='🍴 外で食べる', data='action=夕食登録&value=外で食べる🍴')),
            QuickReplyItem(action=PostbackAction(label='❓ 未定', data='action=夕食登録&value=未定❓')),
        ]))

    elif action == '夕食登録':
        try:
            name = MessagingApi(api_client).get_profile(user_id).display_name
        except:
            name = 'だれか'
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            '''INSERT INTO daily_schedule (user_id, user_name, meal_status, created_date)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (user_id, created_date) DO UPDATE SET
               meal_status=EXCLUDED.meal_status, user_name=EXCLUDED.user_name''',
            (user_id, name, value, today)
        )
        conn.commit()
        cur.close()
        conn.close()
        if user_group:
            send_dinner_summary(user_group)
        reply = TextMessage(text='回答を送りました！家族グループに一覧を送信しました☑️')

    elif action == 'ごはんできた':
        try:
            name = MessagingApi(api_client).get_profile(user_id).display_name
        except:
            name = 'だれか'
        if user_group:
            push_to_group(user_group, f'🍚 {name}がご飯を作りました！みんな集まってください！')
        reply = TextMessage(text='家族グループに送りました！')

    elif action == 'お風呂':
        user_state.pop(user_id, None)
        try:
            name = MessagingApi(api_client).get_profile(user_id).display_name
        except:
            name = 'あなた'
        user_state[user_id] = {'name': name}
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT notify_time FROM bath_schedule WHERE group_id = %s LIMIT 1', (user_group,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            current_time = row[0].strftime('%H:%M')
            reply = TextMessage(text=f'お風呂の状況を教えてください！\n\n現在のお風呂未洗い通知時間: {current_time}', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='✅ 洗った', data='action=お風呂状況&value=洗いました🚿')),
                QuickReplyItem(action=PostbackAction(label='🛁 洗って入れた', data='action=お風呂状況&value=洗ってお湯を入れました🛁')),
                QuickReplyItem(action=PostbackAction(label='📢 お願いする', data='action=お風呂お願い')),
                QuickReplyItem(action=PostbackAction(label='✏️ 通知時間を変更', data='action=お風呂時間変更確認')),
            ]))
        else:
            reply = TextMessage(text='お風呂の状況を教えてください！', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='✅ 洗った', data='action=お風呂状況&value=洗いました🚿')),
                QuickReplyItem(action=PostbackAction(label='🛁 洗って入れた', data='action=お風呂状況&value=洗ってお湯を入れました🛁')),
                QuickReplyItem(action=PostbackAction(label='📢 お願いする', data='action=お風呂お願い')),
                QuickReplyItem(action=PostbackAction(label='⏰ 通知時間を設定', data='action=お風呂時間設定')),
            ]))

    elif action == 'お風呂状況':
        name = user_state.get(user_id, {}).get('name', 'だれか')
        if user_group:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('INSERT INTO bath_done (group_id, done_date) VALUES (%s, %s) ON CONFLICT DO NOTHING', (user_group, today))
            conn.commit()
            cur.close()
            conn.close()
            push_to_group(user_group, f'🛁 {name}がお風呂を{value}')
        user_state.pop(user_id, None)
        reply = TextMessage(text='家族グループに送りました☑️')

    elif action == 'お風呂お願い':
        if user_group:
            push_to_group(user_group, '🛁 お風呂を洗ってください！')
        reply = TextMessage(text='家族グループにお願いしました☑️')

    elif action == 'お風呂時間変更確認':
        reply = TextMessage(text='通知時間を変更しますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='✏️ 変更する', data='action=お風呂時間設定')),
            QuickReplyItem(action=PostbackAction(label='キャンセル', data='action=お風呂')),
        ]))

    elif action == 'お風呂時間設定':
        user_state[user_id] = {'action': 'set_bath_hour'}
        reply = TextMessage(text='何時台に通知しますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='午前', data='action=お風呂時間帯&value=am')),
            QuickReplyItem(action=PostbackAction(label='午後', data='action=お風呂時間帯&value=pm')),
        ]))

    elif action == 'お風呂時間帯':
        hours = AM_HOURS if value == 'am' else PM_HOURS
        reply = TextMessage(text='何時ですか？', quick_reply=make_hour_qr(hours, 'bath'))

    elif action == '時' and context == 'bath':
        user_state[user_id]['hour'] = int(value)
        user_state[user_id]['action'] = 'set_bath_minute'
        reply = TextMessage(text=f'{value}時何分ですか？', quick_reply=make_minute_qr('bath'))

    elif action == '分' and context == 'bath':
        hour = user_state[user_id].get('hour')
        minute = int(value)
        notify_time = f'{hour:02d}:{minute:02d}'
        try:
            name = MessagingApi(api_client).get_profile(user_id).display_name
        except:
            name = 'だれか'
        if user_group:
            conn = get_db()
            cur = conn.cursor()
            cur.execute('INSERT INTO bath_schedule (group_id, notify_time) VALUES (%s, %s) ON CONFLICT (group_id) DO UPDATE SET notify_time = %s', (user_group, notify_time, notify_time))
            conn.commit()
            cur.close()
            conn.close()
            push_to_group(user_group, f'⚙️ 設定が更新されました\n🛁 お風呂未洗い通知時間: {notify_time}\n（{name}が設定）')
        user_state.pop(user_id, None)
        reply = TextMessage(text=f'✅ 毎日{notify_time}にお風呂が洗われていなければ通知します！')

    elif action == '出発・帰宅':
        user_state.pop(user_id, None)
        reply = TextMessage(text='どうしますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='📤 時間を共有する', data='action=帰宅共有開始')),
            QuickReplyItem(action=PostbackAction(label='📋 今日の状況を確認', data='action=帰宅確認')),
        ]))

    elif action == '帰宅共有開始':
        user_state[user_id] = {'action': 'share_type', 'depart': None, 'arrive': None}
        reply = TextMessage(text='どの時間を共有しますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='🚶 出発のみ', data='action=共有タイプ&value=depart')),
            QuickReplyItem(action=PostbackAction(label='🏠 帰宅のみ', data='action=共有タイプ&value=arrive')),
            QuickReplyItem(action=PostbackAction(label='両方', data='action=共有タイプ&value=both')),
        ]))

    elif action == '共有タイプ':
        user_state[user_id]['share_type'] = value
        if value == 'arrive':
            user_state[user_id]['action'] = 'share_arrive_ampm'
            reply = TextMessage(text='帰宅は午前・午後どちら？', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='午前', data='action=帰宅時間帯&value=am')),
                QuickReplyItem(action=PostbackAction(label='午後', data='action=帰宅時間帯&value=pm')),
            ]))
        else:
            user_state[user_id]['action'] = 'share_depart_ampm'
            reply = TextMessage(text='出発は午前・午後どちら？', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='午前', data='action=出発時間帯&value=am')),
                QuickReplyItem(action=PostbackAction(label='午後', data='action=出発時間帯&value=pm')),
            ]))

    elif action == '出発時間帯':
        hours = AM_HOURS if value == 'am' else PM_HOURS
        user_state[user_id]['action'] = 'share_depart_hour'
        reply = TextMessage(text='出発は何時ですか？', quick_reply=make_hour_qr(hours, 'depart'))

    elif action == '時' and context == 'depart':
        user_state[user_id]['depart_hour'] = int(value)
        reply = TextMessage(text=f'{value}時何分ですか？', quick_reply=make_minute_qr('depart'))

    elif action == '分' and context == 'depart':
        hour = user_state[user_id]['depart_hour']
        minute = int(value)
        user_state[user_id]['depart'] = f'{hour:02d}:{minute:02d}'
        share_type = user_state[user_id].get('share_type')
        if share_type == 'both':
            user_state[user_id]['action'] = 'share_arrive_ampm'
            reply = TextMessage(text='帰宅時間は入力しますか？', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='午前', data='action=帰宅時間帯&value=am')),
                QuickReplyItem(action=PostbackAction(label='午後', data='action=帰宅時間帯&value=pm')),
                QuickReplyItem(action=PostbackAction(label='スキップ', data='action=帰宅スキップ')),
            ]))
        else:
            user_state[user_id]['action'] = 'share_meal'
            reply = TextMessage(text='ご飯はどうしますか？', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='🏠 家で食べる', data='action=ごはん状況&value=家で食べる🏠')),
                QuickReplyItem(action=PostbackAction(label='🍴 外で食べる', data='action=ごはん状況&value=外で食べる🍴')),
                QuickReplyItem(action=PostbackAction(label='❓ 未定', data='action=ごはん状況&value=未定❓')),
            ]))

    elif action == '帰宅時間帯':
        hours = AM_HOURS if value == 'am' else PM_HOURS
        user_state[user_id]['action'] = 'share_arrive_hour'
        reply = TextMessage(text='帰宅は何時ですか？', quick_reply=make_hour_qr(hours, 'arrive'))

    elif action == '時' and context == 'arrive':
        user_state[user_id]['arrive_hour'] = int(value)
        reply = TextMessage(text=f'{value}時何分ですか？', quick_reply=make_minute_qr('arrive'))

    elif action == '分' and context == 'arrive':
        hour = user_state[user_id]['arrive_hour']
        minute = int(value)
        user_state[user_id]['arrive'] = f'{hour:02d}:{minute:02d}'
        user_state[user_id]['action'] = 'share_meal'
        reply = TextMessage(text='ご飯はどうしますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='🏠 家で食べる', data='action=ごはん状況&value=家で食べる🏠')),
            QuickReplyItem(action=PostbackAction(label='🍴 外で食べる', data='action=ごはん状況&value=外で食べる🍴')),
            QuickReplyItem(action=PostbackAction(label='❓ 未定', data='action=ごはん状況&value=未定❓')),
        ]))

    elif action == '帰宅スキップ':
        user_state[user_id]['arrive'] = None
        user_state[user_id]['action'] = 'share_meal'
        reply = TextMessage(text='ご飯はどうしますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='🏠 家で食べる', data='action=ごはん状況&value=家で食べる🏠')),
            QuickReplyItem(action=PostbackAction(label='🍴 外で食べる', data='action=ごはん状況&value=外で食べる🍴')),
            QuickReplyItem(action=PostbackAction(label='❓ 未定', data='action=ごはん状況&value=未定❓')),
        ]))

    elif action == 'ごはん状況':
        depart = user_state[user_id].get('depart')
        arrive = user_state[user_id].get('arrive')
        try:
            name = MessagingApi(api_client).get_profile(user_id).display_name
        except:
            name = 'だれか'
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            '''INSERT INTO daily_schedule (user_id, user_name, depart_time, arrive_time, meal_status, created_date)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (user_id, created_date) DO UPDATE SET
               depart_time=EXCLUDED.depart_time, arrive_time=EXCLUDED.arrive_time, meal_status=EXCLUDED.meal_status''',
            (user_id, name, depart, arrive, value, today)
        )
        if user_group:
            cur.execute('''
                SELECT user_name, depart_time, arrive_time, meal_status FROM daily_schedule
                WHERE created_date = %s AND user_id IN (SELECT user_id FROM members WHERE group_id = %s)
                ORDER BY id
            ''', (today, user_group))
        else:
            cur.execute('SELECT user_name, depart_time, arrive_time, meal_status FROM daily_schedule WHERE created_date = %s ORDER BY id', (today,))
        rows = cur.fetchall()
        conn.commit()
        cur.close()
        conn.close()
        if len(rows) == 1:
            parts = [f'🚃 {name}']
            if depart:
                parts.append(f'出発 {depart}')
            if arrive:
                parts.append(f'帰宅 {arrive}')
            parts.append(value)
            if user_group:
                push_to_group(user_group, ' / '.join(parts))
        else:
            summary = '🚃 本日の帰宅・出発まとめ'
            for r_name, r_depart, r_arrive, r_meal in rows:
                line_parts = [r_name]
                if r_depart:
                    line_parts.append(f'出発 {r_depart}')
                if r_arrive:
                    line_parts.append(f'帰宅 {r_arrive}')
                if r_meal:
                    line_parts.append(r_meal)
                summary += f'\n{" / ".join(line_parts)}'
            if user_group:
                push_to_group(user_group, summary)
        user_state.pop(user_id, None)
        reply = TextMessage(text='家族グループに送りました☑️')

    elif action == '帰宅確認':
        conn = get_db()
        cur = conn.cursor()
        if user_group:
            cur.execute('''
                SELECT user_name, depart_time, arrive_time, meal_status FROM daily_schedule
                WHERE created_date = %s AND user_id IN (SELECT user_id FROM members WHERE group_id = %s)
                ORDER BY id
            ''', (today, user_group))
        else:
            cur.execute('SELECT user_name, depart_time, arrive_time, meal_status FROM daily_schedule WHERE created_date = %s ORDER BY id', (today,))
        answered = cur.fetchall()
        if user_group:
            cur.execute('''
                SELECT display_name FROM members WHERE group_id = %s
                AND user_id NOT IN (SELECT user_id FROM daily_schedule WHERE created_date = %s)
            ''', (user_group, today))
        else:
            cur.execute('SELECT display_name FROM members WHERE user_id NOT IN (SELECT user_id FROM daily_schedule WHERE created_date = %s)', (today,))
        unanswered = cur.fetchall()
        cur.close()
        conn.close()

        status_text = f'📋 今日の状況（{today.month}/{today.day}）'
        for r_name, r_depart, r_arrive, r_meal in answered:
            line_parts = [r_name]
            if r_depart:
                line_parts.append(f'出発 {r_depart}')
            if r_arrive:
                line_parts.append(f'帰宅 {r_arrive}')
            if r_meal:
                line_parts.append(r_meal)
            status_text += f'\n{" / ".join(line_parts)}'
        for (u_name,) in unanswered:
            status_text += f'\n{u_name}: 未回答'

        if unanswered:
            reply = TextMessage(text=status_text, quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='📣 全員に入力を促す', data='action=帰宅確認今すぐ')),
            ]))
        else:
            reply = TextMessage(text=status_text)

    elif action == '帰宅確認今すぐ':
        if user_group:
            push_members('🚃 帰宅・出発時間の確認です！\nメニューの「出発・帰宅」から時間を共有してください😊', user_group)
            push_to_group(user_group, '📣 帰宅・出発時間の入力を全員にお願いしました！')
        reply = TextMessage(text='グループと全員の個別チャットに送りました☑️')

    elif action == 'ゴミの日':
        user_state.pop(user_id, None)
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT trash_type, weekdays, week_type FROM trash_schedule WHERE group_id = %s', (user_group,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if rows:
            def week_type_label(wt):
                if wt == 'odd': return '（第1・3週）'
                if wt == 'even': return '（第2・4週）'
                return ''
            schedule_text = '\n'.join([f'・{t}: {w}曜日{week_type_label(wt)}' for t, w, wt in rows])
            reply = TextMessage(text=f'現在のゴミ出しスケジュール📅\n{schedule_text}\n\n前日21時と当日朝7時に通知します。', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='➕ 追加', data='action=ゴミ登録')),
                QuickReplyItem(action=PostbackAction(label='✏️ 変更・削除', data='action=ゴミ変更選択')),
            ]))
        else:
            reply = TextMessage(text='ゴミ出しスケジュールが未設定です。', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='➕ 登録する', data='action=ゴミ登録')),
            ]))

    elif action == 'ゴミ変更選択':
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT trash_type, weekdays FROM trash_schedule WHERE group_id = %s', (user_group,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        items = [
            QuickReplyItem(action=PostbackAction(label=f'✏️ {t}', data=f'action=ゴミ変更&value={t}'))
            for t, w in rows
        ] + [
            QuickReplyItem(action=PostbackAction(label=f'🗑️ {t}を削除', data=f'action=ゴミ削除&value={t}'))
            for t, w in rows
        ]
        reply = TextMessage(text='変更・削除するゴミの種類を選んでください。', quick_reply=QuickReply(items=items[:13]))

    elif action == 'ゴミ変更':
        user_state[user_id] = {'action': 'set_trash_days', 'trash_type': value, 'days': ''}
        reply = TextMessage(text=f'「{value}」の新しい収集曜日を選んでください。', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='月', data='action=ゴミ曜日&value=月')),
            QuickReplyItem(action=PostbackAction(label='火', data='action=ゴミ曜日&value=火')),
            QuickReplyItem(action=PostbackAction(label='水', data='action=ゴミ曜日&value=水')),
            QuickReplyItem(action=PostbackAction(label='木', data='action=ゴミ曜日&value=木')),
            QuickReplyItem(action=PostbackAction(label='金', data='action=ゴミ曜日&value=金')),
            QuickReplyItem(action=PostbackAction(label='土', data='action=ゴミ曜日&value=土')),
            QuickReplyItem(action=PostbackAction(label='日', data='action=ゴミ曜日&value=日')),
        ]))

    elif action == 'ゴミ削除':
        conn = get_db()
        cur = conn.cursor()
        cur.execute('DELETE FROM trash_schedule WHERE trash_type=%s AND group_id=%s', (value, user_group))
        conn.commit()
        cur.close()
        conn.close()
        try:
            name = MessagingApi(api_client).get_profile(user_id).display_name
        except:
            name = 'だれか'
        if user_group:
            push_to_group(user_group, f'⚙️ 設定が更新されました\n🗑️ {value}のスケジュールを削除しました\n（{name}が操作）')
        reply = TextMessage(text=f'✅ {value}のスケジュールを削除しました。')

    elif action == 'ゴミ登録':
        user_state[user_id] = {'action': 'set_trash_days'}
        reply = TextMessage(text='ゴミの種類を選んでください🗑️', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='燃えるゴミ', data='action=ゴミ種類&value=燃えるゴミ')),
            QuickReplyItem(action=PostbackAction(label='燃えないゴミ', data='action=ゴミ種類&value=燃えないゴミ')),
            QuickReplyItem(action=PostbackAction(label='資源ゴミ', data='action=ゴミ種類&value=資源ゴミ')),
            QuickReplyItem(action=PostbackAction(label='ペットボトル', data='action=ゴミ種類&value=ペットボトル')),
            QuickReplyItem(action=PostbackAction(label='びん', data='action=ゴミ種類&value=びん')),
            QuickReplyItem(action=PostbackAction(label='かん', data='action=ゴミ種類&value=かん')),
            QuickReplyItem(action=PostbackAction(label='粗大ゴミ', data='action=ゴミ種類&value=粗大ゴミ')),
            QuickReplyItem(action=PostbackAction(label='➕ その他', data='action=ゴミ種類その他')),
        ]))

    elif action == 'ゴミ種類':
        user_state[user_id] = {'action': 'set_trash_days', 'trash_type': value, 'days': ''}
        reply = TextMessage(text=f'「{value}」の収集曜日を選んでください。', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='月', data='action=ゴミ曜日&value=月')),
            QuickReplyItem(action=PostbackAction(label='火', data='action=ゴミ曜日&value=火')),
            QuickReplyItem(action=PostbackAction(label='水', data='action=ゴミ曜日&value=水')),
            QuickReplyItem(action=PostbackAction(label='木', data='action=ゴミ曜日&value=木')),
            QuickReplyItem(action=PostbackAction(label='金', data='action=ゴミ曜日&value=金')),
            QuickReplyItem(action=PostbackAction(label='土', data='action=ゴミ曜日&value=土')),
            QuickReplyItem(action=PostbackAction(label='日', data='action=ゴミ曜日&value=日')),
        ]))

    elif action == 'ゴミ種類その他':
        user_state[user_id] = {'action': 'set_trash_type_custom'}
        reply = TextMessage(text='ゴミの種類を入力してください。\n例: 古紙')

    elif action == 'ゴミ曜日':
        if user_id in user_state and user_state[user_id].get('action') == 'set_trash_days':
            current_days = user_state[user_id].get('days', '')
            if value not in current_days:
                current_days += value
            user_state[user_id]['days'] = current_days
            reply = TextMessage(text=f'選択中: {current_days}曜日\n他にもありますか？', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='月', data='action=ゴミ曜日&value=月')),
                QuickReplyItem(action=PostbackAction(label='火', data='action=ゴミ曜日&value=火')),
                QuickReplyItem(action=PostbackAction(label='水', data='action=ゴミ曜日&value=水')),
                QuickReplyItem(action=PostbackAction(label='木', data='action=ゴミ曜日&value=木')),
                QuickReplyItem(action=PostbackAction(label='金', data='action=ゴミ曜日&value=金')),
                QuickReplyItem(action=PostbackAction(label='土', data='action=ゴミ曜日&value=土')),
                QuickReplyItem(action=PostbackAction(label='日', data='action=ゴミ曜日&value=日')),
                QuickReplyItem(action=PostbackAction(label='✅ 次へ', data='action=ゴミ週タイプ選択')),
            ]))
        else:
            reply = TextMessage(text='「ゴミの日」から最初からやり直してください🙇‍♂️')

    elif action == 'ゴミ週タイプ選択':
        reply = TextMessage(text='収集頻度を選んでください。', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='毎週', data='action=ゴミ曜日完了&value=every')),
            QuickReplyItem(action=PostbackAction(label='第1・3週', data='action=ゴミ曜日完了&value=odd')),
            QuickReplyItem(action=PostbackAction(label='第2・4週', data='action=ゴミ曜日完了&value=even')),
        ]))

    elif action == 'ゴミ曜日完了':
        if user_id in user_state and user_state[user_id].get('action') == 'set_trash_days':
            trash_type = user_state[user_id]['trash_type']
            days = user_state[user_id].get('days', '')
            week_type = value if value in ['every', 'odd', 'even'] else 'every'
            try:
                name = MessagingApi(api_client).get_profile(user_id).display_name
            except:
                name = 'だれか'
            conn = get_db()
            cur = conn.cursor()
            cur.execute('DELETE FROM trash_schedule WHERE trash_type=%s AND group_id=%s', (trash_type, user_group))
            cur.execute('INSERT INTO trash_schedule (group_id, trash_type, weekdays, week_type) VALUES (%s, %s, %s, %s)', (user_group, trash_type, days, week_type))
            conn.commit()
            cur.close()
            conn.close()
            week_label = {'every': '毎週', 'odd': '第1・3週', 'even': '第2・4週'}.get(week_type, '毎週')
            if user_group:
                push_to_group(user_group, f'⚙️ 設定が更新されました\n🗑️ {trash_type}: {days}曜日（{week_label}）\n（{name}が設定）')
            user_state.pop(user_id, None)
            reply = TextMessage(text=f'✅ {trash_type}を{days}曜日（{week_label}）に登録しました！\n前日21時と当日朝7時に通知します🗑️', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='➕ 続けて登録', data='action=ゴミ登録')),
            ]))
        else:
            reply = TextMessage(text='「ゴミの日」から最初からやり直してください。')

    elif action == '完了':
        reply = TextMessage(text='設定が完了しました！✅')

    else:
        reply = TextMessage(text='メニューから選んでください。')

    send_reply(api_client, reply_token, reply)


@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data
    params = dict(item.split('=') for item in data.split('&'))
    action = params.get('action', '')
    value = params.get('value', '')
    context = params.get('context', '')
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        process_action(action, value, context, user_id, api_client, event.reply_token)


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text
    user_id = event.source.user_id

    if hasattr(event.source, 'group_id'):
        return

    with ApiClient(configuration) as api_client:
        if user_id in user_state and user_state[user_id].get('action') == 'set_trash_type_custom':
            trash_type = text
            user_state[user_id] = {'action': 'set_trash_days', 'trash_type': trash_type, 'days': ''}
            reply = TextMessage(text=f'「{trash_type}」の収集曜日を選んでください。', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='月', data='action=ゴミ曜日&value=月')),
                QuickReplyItem(action=PostbackAction(label='火', data='action=ゴミ曜日&value=火')),
                QuickReplyItem(action=PostbackAction(label='水', data='action=ゴミ曜日&value=水')),
                QuickReplyItem(action=PostbackAction(label='木', data='action=ゴミ曜日&value=木')),
                QuickReplyItem(action=PostbackAction(label='金', data='action=ゴミ曜日&value=金')),
                QuickReplyItem(action=PostbackAction(label='土', data='action=ゴミ曜日&value=土')),
                QuickReplyItem(action=PostbackAction(label='日', data='action=ゴミ曜日&value=日')),
            ]))
            send_reply(api_client, event.reply_token, reply)

        elif text in ['ごはん', 'お風呂', '出発・帰宅', 'ゴミの日']:
            process_action(text, '', '', user_id, api_client, event.reply_token)

        elif text == '使い方':
            reply = TextMessage(text=
                '📖 まめBot 使い方\n\n'
                '🍚 ごはん\n夕食の予定を家族に共有したり、ごはんができたら一斉通知もできます。\n\n'
                '🚃 出発・帰宅\n今日の出発・帰宅時間とご飯の有無を家族に共有できます。今日の状況確認や全員への入力依頼もできます。\n\n'
                '🛁 お風呂\nお風呂を洗ったか家族に報告・お願いができます。設定した時間までに洗われていなければ自動通知します。\n\n'
                '🗑️ ゴミの日\nゴミの種類と収集曜日を登録すると前日21時と当日朝7時に自動通知されます。第1・3週や第2・4週の設定も可能です。\n\n'
                '💡 設定した内容はすべて家族グループに通知されます。'
            )
            send_reply(api_client, event.reply_token, reply)

        else:
            if text.isdigit() and len(text) == 6:
                conn = get_db()
                cur = conn.cursor()
                cur.execute('SELECT group_id FROM groups WHERE invite_code = %s AND active = TRUE', (text,))
                row = cur.fetchone()
                if row:
                    group_id = row[0]
                    try:
                        profile = MessagingApi(api_client).get_profile(user_id)
                        name = profile.display_name
                    except:
                        name = 'だれか'
                    cur.execute(
                        'INSERT INTO members (user_id, display_name, group_id) VALUES (%s, %s, %s) ON CONFLICT (user_id, group_id) DO UPDATE SET display_name=%s',
                        (user_id, name, group_id, name)
                    )
                    conn.commit()
                    cur.close()
                    conn.close()
                    reply = TextMessage(text=(
                        '✅ 登録完了！グループと紐付けました😊\n\n'
                        '📖 さっそく使ってみましょう！\n'
                        '🍚 ごはん: 夕食の予定を共有\n'
                        '🚃 出発・帰宅: 今日の時間を共有\n'
                        '🛁 お風呂: 洗った報告やお願い\n'
                        '🗑️ ゴミの日: 収集日の通知設定\n\n'
                        '画面下のメニューから選んでみてください👇'
                    ))
                else:
                    cur.close()
                    conn.close()
                    reply = TextMessage(text='コードが見つかりませんでした。グループに表示されたコードを確認してください。')
                send_reply(api_client, event.reply_token, reply)
            else:
                send_reply(api_client, event.reply_token, TextMessage(text='メニューから選んでください。\nグループの登録コード（6桁）をお持ちの方はそのまま入力してください。'))


@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            profile = line_bot_api.get_profile(user_id)
            name = profile.display_name
            print(f'Member followed: {name}')
        except Exception as e:
            print(f'Follow error: {e}')

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=
                    'こんにちは！まめBotです🫘\n\n'
                    '家族の日常をもっとスムーズにするお手伝いをします。\n\n'
                    '【プライバシーポリシー】\n'
                    '・収集情報: LINEユーザーID・表示名・入力内容\n'
                    '・利用目的: グループ内での情報共有機能の提供\n'
                    '・第三者提供: 一切行いません\n'
                    '・お問い合わせ: https://github.com/annonymousIT/mamebot/issues\n\n'
                    'グループにまめBotを招待すると登録コード（6桁）が発行されます。\n'
                    'そのコードをここに入力するとグループと紐付けられます！\n\n'
                    '下のメニューから使ってみてください😊'
                )]
            )
        )


@handler.add(JoinEvent)
def handle_join(event):
    group_id = event.source.group_id
    invite_code = str(random.randint(100000, 999999))
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('UPDATE groups SET active = FALSE')
        cur.execute(
            'INSERT INTO groups (group_id, active, invite_code) VALUES (%s, TRUE, %s) ON CONFLICT (group_id) DO UPDATE SET active = TRUE, invite_code = %s',
            (group_id, invite_code, invite_code)
        )
        cur.execute('UPDATE members SET group_id = %s WHERE group_id IS NULL', (group_id,))
        conn.commit()
        cur.close()
        conn.close()
        print(f'Group registered: {group_id}, code: {invite_code}')
    except Exception as e:
        print(f'Group registration error: {e}')

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=
                    'まめBotがグループに参加しました🫘\n\n'
                    '家族の日常をもっとスムーズにするお手伝いをします。\n\n'
                    '【登録手順】\n'
                    '① 下のリンクからまめBotを友達追加👇\n'
                    'https://line.me/R/ti/p/@240fwfwn\n\n'
                    '② まめBotとの「個別チャット」で下のコードを送信\n\n'
                    f'🔑 登録コード: 【{invite_code}】\n\n'
                    '③ 登録完了！メニューから使えます😊'
                )]
            )
        )


@handler.add(LeaveEvent)
def handle_leave(event):
    group_id = event.source.group_id
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('DELETE FROM groups WHERE group_id = %s', (group_id,))
        conn.commit()
        cur.close()
        conn.close()
        print(f'Group removed: {group_id}')
    except Exception as e:
        print(f'Group removal error: {e}')


with app.app_context():
    init_db()
    t = threading.Thread(target=reminder_loop, daemon=True)
    t.start()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)