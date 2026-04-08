from flask import Flask, request, abort
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, PushMessageRequest, TextMessage, QuickReply, QuickReplyItem, MessageAction, PostbackAction
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent, JoinEvent, FollowEvent, LeaveEvent
import os
import psycopg2
from datetime import datetime, timedelta, timezone
import threading
import time
import requests as http_requests

app = Flask(__name__)

configuration = Configuration(access_token=os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS meal_times (
            id SERIAL PRIMARY KEY,
            meal_type VARCHAR(10),
            meal_time TIME,
            remind_minutes INTEGER DEFAULT 120,
            group_id TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS trash_schedule (
            id SERIAL PRIMARY KEY,
            trash_type VARCHAR(50),
            weekdays TEXT,
            notify_time TIME DEFAULT '07:00'
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS bath_schedule (
            id SERIAL PRIMARY KEY,
            notify_time TIME
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS depart_check_schedule (
            id SERIAL PRIMARY KEY,
            notify_time TIME
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS dinner_schedule (
            id SERIAL PRIMARY KEY,
            notify_time TIME
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            id SERIAL PRIMARY KEY,
            group_id TEXT UNIQUE,
            active BOOLEAN DEFAULT FALSE
        )
    ''')
    try:
        cur.execute('ALTER TABLE groups ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT FALSE')
    except:
        pass
    cur.execute('''
        CREATE TABLE IF NOT EXISTS members (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            display_name TEXT,
            group_id TEXT,
            UNIQUE (user_id, group_id)
        )
    ''')
    try:
        cur.execute('ALTER TABLE members ADD COLUMN IF NOT EXISTS group_id TEXT')
        cur.execute('ALTER TABLE members DROP CONSTRAINT IF EXISTS members_user_id_key')
        cur.execute('ALTER TABLE members ADD CONSTRAINT members_user_group_unique UNIQUE (user_id, group_id)')
    except:
        pass
    cur.execute('''
        CREATE TABLE IF NOT EXISTS daily_schedule (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            user_name TEXT,
            depart_time TEXT,
            arrive_time TEXT,
            meal_status TEXT,
            created_date DATE DEFAULT CURRENT_DATE,
            UNIQUE (user_id, created_date)
        )
    ''')
    try:
        cur.execute('ALTER TABLE daily_schedule ADD CONSTRAINT unique_daily_user UNIQUE (user_id, created_date)')
    except:
        pass
    conn.commit()
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

def push_members(text):
    try:
        group_ids = get_group_ids()
        active_gid = group_ids[0] if group_ids else None
        if not active_gid:
            return
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT user_id FROM members WHERE group_id = %s', (active_gid,))
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

def reminder_loop():
    while True:
        try:
            conn = get_db()
            cur = conn.cursor()
            JST = timezone(timedelta(hours=9))
            now = datetime.now(JST).replace(tzinfo=None)
            weekday_map = {0:'月',1:'火',2:'水',3:'木',4:'金',5:'土',6:'日'}
            today = weekday_map[now.weekday()]

            cur.execute('SELECT meal_type, meal_time, remind_minutes, group_id FROM meal_times')
            for meal_type, meal_time, remind_minutes, group_id in cur.fetchall():
                meal_dt = datetime.combine(now.date(), meal_time)
                remind_dt = meal_dt - timedelta(minutes=remind_minutes)
                diff = abs((now - remind_dt).total_seconds())
                if diff < 90 and group_id:
                    push_group(f'🍚 {meal_type}ごはんリマインド\n⏰ {meal_time.strftime("%H:%M")} まであと{remind_minutes}分')

            cur.execute('SELECT trash_type, weekdays, notify_time FROM trash_schedule')
            for trash_type, weekdays, notify_time in cur.fetchall():
                if today in weekdays:
                    notify_dt = datetime.combine(now.date(), notify_time)
                    diff = abs((now - notify_dt).total_seconds())
                    if diff < 90:
                        push_group(f'🗑️ 今日は{trash_type}の日です！忘れずに！')

            cur.execute('SELECT notify_time FROM bath_schedule LIMIT 1')
            row = cur.fetchone()
            if row:
                notify_dt = datetime.combine(now.date(), row[0])
                diff = abs((now - notify_dt).total_seconds())
                if diff < 90:
                    push_group('🛁 お風呂洗ってありますか？')

            cur.execute('SELECT notify_time FROM depart_check_schedule LIMIT 1')
            row = cur.fetchone()
            if row:
                notify_dt = datetime.combine(now.date(), row[0])
                diff = abs((now - notify_dt).total_seconds())
                if diff < 90:
                    push_members('🚃 帰宅・出発時間の確認です！\nメニューの「出発・帰宅」から時間を共有してください😊')

            cur.execute('SELECT notify_time FROM dinner_schedule LIMIT 1')
            row = cur.fetchone()
            if row:
                notify_dt = datetime.combine(now.date(), row[0])
                diff = abs((now - notify_dt).total_seconds())
                if diff < 90:
                    push_members('🍚 今日の夕食はどうしますか？\nメニューの「ごはん」→「ご飯どうする？」から教えてください😊')

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

MORNING_HOURS = [6,7,8,9,10]
LUNCH_HOURS = [11,12,13,14]
EVENING_HOURS = [17,18,19,20,21,22]
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

def push_group(text):
    group_ids = get_group_ids()
    for gid in group_ids:
        if gid:
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")}'
            }
            data = {
                'to': gid,
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
            http_requests.post(
                'https://api.line.me/v2/bot/message/push',
                headers=headers,
                json=data
            )

def send_dinner_summary():
    group_ids = get_group_ids()
    active_gid = group_ids[0] if group_ids else None
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_name, meal_status FROM daily_schedule WHERE created_date = CURRENT_DATE ORDER BY id')
    responses = cur.fetchall()
    cur.execute('''
        SELECT display_name FROM members
        WHERE group_id = %s
        AND user_id NOT IN (
            SELECT user_id FROM daily_schedule
            WHERE created_date = CURRENT_DATE AND meal_status IS NOT NULL
        )
    ''', (active_gid,))
    unanswered = cur.fetchall()
    cur.close()
    conn.close()
    summary = '🍚 夕食まとめ'
    for r_name, r_meal in responses:
        if r_meal:
            summary += f'\n{r_name}: {r_meal}'
    for (u_name,) in unanswered:
        summary += f'\n{u_name}: 未回答'
    push_group(summary)

def process_action(action, value, context, user_id, api_client, reply_token):

    if action == 'ごはん':
        user_state.pop(user_id, None)
        reply = TextMessage(text='何をしますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='🕐 時間を設定', data='action=ごはん時間設定')),
            QuickReplyItem(action=PostbackAction(label='🍽️ ご飯どうする？', data='action=ご飯どうする')),
            QuickReplyItem(action=PostbackAction(label='🔔 できました！', data='action=ごはんできた')),
        ]))

    elif action == 'ごはん時間設定':
        reply = TextMessage(text='どの時間帯を設定しますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='🌅 朝', data='action=ごはん選択&value=朝')),
            QuickReplyItem(action=PostbackAction(label='☀️ 昼', data='action=ごはん選択&value=昼')),
            QuickReplyItem(action=PostbackAction(label='🌙 夜', data='action=ごはん選択&value=夜')),
        ]))

    elif action == 'ご飯どうする':
        reply = TextMessage(text='今日の夕食はどうしますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='🏠 家で食べる', data='action=夕食登録&value=家で食べる🏠')),
            QuickReplyItem(action=PostbackAction(label='🍴 外で食べる', data='action=夕食登録&value=外で食べる🍴')),
            QuickReplyItem(action=PostbackAction(label='❓ 未定', data='action=夕食登録&value=未定❓')),
            QuickReplyItem(action=PostbackAction(label='⏰ 確認時間を設定', data='action=夕食送信設定')),
        ]))

    elif action == 'ごはん選択':
        meal_type = value
        user_state[user_id] = {'action': 'set_meal_hour', 'meal_type': meal_type}
        hours = MORNING_HOURS if meal_type == '朝' else LUNCH_HOURS if meal_type == '昼' else EVENING_HOURS
        reply = TextMessage(text=f'{meal_type}ごはんは何時台ですか？', quick_reply=make_hour_qr(hours, 'meal'))

    elif action == '時' and context == 'meal':
        user_state[user_id]['hour'] = int(value)
        user_state[user_id]['action'] = 'set_meal_minute'
        reply = TextMessage(text=f'{value}時何分ですか？', quick_reply=make_minute_qr('meal'))

    elif action == '分' and context == 'meal':
        hour = user_state[user_id]['hour']
        minute = int(value)
        meal_type = user_state[user_id]['meal_type']
        user_state[user_id]['meal_time'] = f'{hour:02d}:{minute:02d}'
        user_state[user_id]['action'] = 'set_remind_minutes'
        reply = TextMessage(
            text=f'{meal_type}ごはん {hour:02d}:{minute:02d} を設定しました！\n何分前にリマインドしますか？',
            quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='30分前', data='action=リマインド設定&value=30')),
                QuickReplyItem(action=PostbackAction(label='1時間前', data='action=リマインド設定&value=60')),
                QuickReplyItem(action=PostbackAction(label='2時間前（デフォルト）', data='action=リマインド設定&value=120')),
                QuickReplyItem(action=PostbackAction(label='3時間前', data='action=リマインド設定&value=180')),
            ])
        )

    elif action == 'リマインド設定':
        remind_minutes = int(value)
        meal_type = user_state[user_id]['meal_type']
        meal_time = user_state[user_id]['meal_time']
        group_ids = get_group_ids()
        gid = group_ids[0] if group_ids else ''
        conn = get_db()
        cur = conn.cursor()
        cur.execute('DELETE FROM meal_times WHERE meal_type=%s', (meal_type,))
        cur.execute(
            'INSERT INTO meal_times (meal_type, meal_time, remind_minutes, group_id) VALUES (%s, %s, %s, %s)',
            (meal_type, meal_time, remind_minutes, gid)
        )
        conn.commit()
        cur.close()
        conn.close()
        push_group(f'🍚 {meal_type}ごはんの予定\n⏰ {meal_time}')
        user_state.pop(user_id, None)
        reply = TextMessage(
            text=f'✅ 登録完了！{meal_time}の{remind_minutes}分前にリマインドします。\nグループに通知しました！',
            quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='🌅 朝も設定', data='action=ごはん選択&value=朝')),
                QuickReplyItem(action=PostbackAction(label='☀️ 昼も設定', data='action=ごはん選択&value=昼')),
                QuickReplyItem(action=PostbackAction(label='🌙 夜も設定', data='action=ごはん選択&value=夜')),
                QuickReplyItem(action=PostbackAction(label='✅ 終わり', data='action=完了')),
            ])
        )

    elif action == '夕食登録':
        try:
            name = MessagingApi(api_client).get_profile(user_id).display_name
        except:
            name = 'だれか'
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            '''INSERT INTO daily_schedule (user_id, user_name, meal_status, created_date)
               VALUES (%s, %s, %s, CURRENT_DATE)
               ON CONFLICT (user_id, created_date) DO UPDATE SET
               meal_status=EXCLUDED.meal_status, user_name=EXCLUDED.user_name''',
            (user_id, name, value)
        )
        conn.commit()
        cur.close()
        conn.close()
        send_dinner_summary()
        reply = TextMessage(text='回答を送りました！家族グループに一覧を送信しました☑️')

    elif action == '夕食送信設定':
        user_state[user_id] = {'action': 'set_dinner_ampm'}
        reply = TextMessage(text='何時に送信しますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='午前', data='action=夕食時間帯&value=am')),
            QuickReplyItem(action=PostbackAction(label='午後', data='action=夕食時間帯&value=pm')),
        ]))

    elif action == '夕食時間帯':
        hours = AM_HOURS if value == 'am' else PM_HOURS
        user_state[user_id]['action'] = 'set_dinner_hour'
        reply = TextMessage(text='何時ですか？', quick_reply=make_hour_qr(hours, 'dinner'))

    elif action == '時' and context == 'dinner':
        user_state[user_id]['hour'] = int(value)
        user_state[user_id]['action'] = 'set_dinner_minute'
        reply = TextMessage(text=f'{value}時何分ですか？', quick_reply=make_minute_qr('dinner'))

    elif action == '分' and context == 'dinner':
        hour = user_state[user_id].get('hour')
        minute = int(value)
        conn = get_db()
        cur = conn.cursor()
        cur.execute('DELETE FROM dinner_schedule')
        cur.execute('INSERT INTO dinner_schedule (notify_time) VALUES (%s)', (f'{hour:02d}:{minute:02d}',))
        conn.commit()
        cur.close()
        conn.close()
        user_state.pop(user_id, None)
        reply = TextMessage(text=f'✅ 毎日{hour:02d}:{minute:02d}に夕食確認を送ります！')

    elif action == 'ごはんできた':
        push_group('🍚 ご飯ができました！みんな集まってください！')
        reply = TextMessage(text='家族グループに送りました！')

    elif action == 'お風呂':
        user_state.pop(user_id, None)
        try:
            name = MessagingApi(api_client).get_profile(user_id).display_name
        except:
            name = 'あなた'
        user_state[user_id] = {'name': name}
        reply = TextMessage(text='お風呂の状況を教えてください！', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='✅ 洗った', data='action=お風呂状況&value=洗いました🚿')),
            QuickReplyItem(action=PostbackAction(label='❌ 洗ってない', data='action=お風呂状況&value=まだ洗っていません💦')),
            QuickReplyItem(action=PostbackAction(label='🛁 洗って入れた', data='action=お風呂状況&value=洗ってお湯を入れました🛁')),
            QuickReplyItem(action=PostbackAction(label='📢 お願いする', data='action=お風呂お願い')),
            QuickReplyItem(action=PostbackAction(label='⏰ リマインド時間を設定', data='action=お風呂時間設定')),
        ]))

    elif action == 'お風呂状況':
        name = user_state.get(user_id, {}).get('name', 'だれか')
        push_group(f'🛁 {name}がお風呂を{value}')
        user_state.pop(user_id, None)
        reply = TextMessage(text='家族グループに送りました☑️')

    elif action == 'お風呂お願い':
        push_group('🛁 お風呂を洗ってください！')
        reply = TextMessage(text='家族グループにお願いしました☑️')

    elif action == 'お風呂時間設定':
        user_state[user_id] = {'action': 'set_bath_hour'}
        reply = TextMessage(text='何時台に確認しますか？', quick_reply=QuickReply(items=[
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
        conn = get_db()
        cur = conn.cursor()
        cur.execute('DELETE FROM bath_schedule')
        cur.execute('INSERT INTO bath_schedule (notify_time) VALUES (%s)', (f'{hour:02d}:{minute:02d}',))
        conn.commit()
        cur.close()
        conn.close()
        user_state.pop(user_id, None)
        reply = TextMessage(text=f'✅ 毎日{hour:02d}:{minute:02d}にお風呂の確認を送ります！')

    elif action == '出発・帰宅':
        user_state.pop(user_id, None)
        reply = TextMessage(text='共有しますか？確認しますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='📤 時間を共有する', data='action=帰宅共有開始')),
            QuickReplyItem(action=PostbackAction(label='📥 時間を確認する', data='action=帰宅確認')),
        ]))

    elif action == '帰宅共有開始':
        user_state[user_id] = {'action': 'share_depart_ampm', 'depart': None, 'arrive': None}
        reply = TextMessage(text='出発時間は？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='午前', data='action=出発時間帯&value=am')),
            QuickReplyItem(action=PostbackAction(label='午後', data='action=出発時間帯&value=pm')),
            QuickReplyItem(action=PostbackAction(label='スキップ', data='action=出発スキップ')),
        ]))

    elif action == '出発時間帯':
        hours = AM_HOURS if value == 'am' else PM_HOURS
        user_state[user_id]['action'] = 'share_depart_hour'
        reply = TextMessage(text='出発は何時ですか？', quick_reply=make_hour_qr(hours, 'depart'))

    elif action == '時' and context == 'depart':
        user_state[user_id]['depart_hour'] = int(value)
        user_state[user_id]['action'] = 'share_depart_minute'
        reply = TextMessage(text=f'{value}時何分ですか？', quick_reply=make_minute_qr('depart'))

    elif action == '分' and context == 'depart':
        hour = user_state[user_id]['depart_hour']
        minute = int(value)
        user_state[user_id]['depart'] = f'{hour:02d}:{minute:02d}'
        user_state[user_id]['action'] = 'share_arrive_ampm'
        reply = TextMessage(text='帰宅時間は？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='午前', data='action=帰宅時間帯&value=am')),
            QuickReplyItem(action=PostbackAction(label='午後', data='action=帰宅時間帯&value=pm')),
            QuickReplyItem(action=PostbackAction(label='スキップ', data='action=帰宅スキップ')),
        ]))

    elif action == '出発スキップ':
        user_state[user_id]['depart'] = None
        user_state[user_id]['action'] = 'share_arrive_ampm'
        reply = TextMessage(text='帰宅時間は？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='午前', data='action=帰宅時間帯&value=am')),
            QuickReplyItem(action=PostbackAction(label='午後', data='action=帰宅時間帯&value=pm')),
            QuickReplyItem(action=PostbackAction(label='スキップ', data='action=帰宅スキップ')),
        ]))

    elif action == '帰宅時間帯':
        hours = AM_HOURS if value == 'am' else PM_HOURS
        user_state[user_id]['action'] = 'share_arrive_hour'
        reply = TextMessage(text='帰宅は何時ですか？', quick_reply=make_hour_qr(hours, 'arrive'))

    elif action == '時' and context == 'arrive':
        user_state[user_id]['arrive_hour'] = int(value)
        user_state[user_id]['action'] = 'share_arrive_minute'
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
               VALUES (%s, %s, %s, %s, %s, CURRENT_DATE)
               ON CONFLICT (user_id, created_date) DO UPDATE SET
               depart_time=EXCLUDED.depart_time, arrive_time=EXCLUDED.arrive_time, meal_status=EXCLUDED.meal_status''',
            (user_id, name, depart, arrive, value)
        )
        cur.execute('SELECT user_name, depart_time, arrive_time, meal_status FROM daily_schedule WHERE created_date = CURRENT_DATE ORDER BY id')
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
            push_group(' / '.join(parts))
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
            push_group(summary)
        user_state.pop(user_id, None)
        reply = TextMessage(text='家族グループに送りました☑️')

    elif action == '帰宅確認':
        reply = TextMessage(text='どうしますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='📤 今すぐ送信', data='action=帰宅確認今すぐ')),
            QuickReplyItem(action=PostbackAction(label='⏰ 毎日自動送信を設定', data='action=帰宅確認時間設定')),
        ]))

    elif action == '帰宅確認今すぐ':
        push_members('🚃 帰宅・出発時間の確認です！\nメニューの「出発・帰宅」から時間を共有してください😊')
        group_ids = get_group_ids()
        active_gid = group_ids[0] if group_ids else None
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM members WHERE group_id = %s', (active_gid,))
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        reply = TextMessage(text=f'全員({count}人)に確認メッセージを送りました☑️')

    elif action == '帰宅確認時間設定':
        user_state[user_id] = {'action': 'set_depart_check_ampm'}
        reply = TextMessage(text='何時に自動送信しますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='午前', data='action=帰宅確認時間帯&value=am')),
            QuickReplyItem(action=PostbackAction(label='午後', data='action=帰宅確認時間帯&value=pm')),
        ]))

    elif action == '帰宅確認時間帯':
        hours = AM_HOURS if value == 'am' else PM_HOURS
        user_state[user_id]['action'] = 'set_depart_check_hour'
        reply = TextMessage(text='何時ですか？', quick_reply=make_hour_qr(hours, 'depart_check'))

    elif action == '時' and context == 'depart_check':
        user_state[user_id]['hour'] = int(value)
        user_state[user_id]['action'] = 'set_depart_check_minute'
        reply = TextMessage(text=f'{value}時何分ですか？', quick_reply=make_minute_qr('depart_check'))

    elif action == '分' and context == 'depart_check':
        hour = user_state[user_id].get('hour')
        minute = int(value)
        conn = get_db()
        cur = conn.cursor()
        cur.execute('DELETE FROM depart_check_schedule')
        cur.execute('INSERT INTO depart_check_schedule (notify_time) VALUES (%s)', (f'{hour:02d}:{minute:02d}',))
        conn.commit()
        cur.close()
        conn.close()
        user_state.pop(user_id, None)
        reply = TextMessage(text=f'✅ 毎日{hour:02d}:{minute:02d}に帰宅確認を送ります！')

    elif action == 'ゴミの日':
        user_state.pop(user_id, None)
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT trash_type, weekdays FROM trash_schedule')
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if rows:
            schedule_text = '\n'.join([f'・{t}: {w}曜日' for t, w in rows])
            reply = TextMessage(text=f'現在のゴミ出しスケジュール📅\n{schedule_text}', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='➕ 追加・変更', data='action=ゴミ登録')),
            ]))
        else:
            reply = TextMessage(text='ゴミ出しスケジュールが未設定です。', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='➕ 登録する', data='action=ゴミ登録')),
            ]))

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
                QuickReplyItem(action=PostbackAction(label='✅ 完了', data='action=ゴミ曜日完了')),
            ]))
        else:
            reply = TextMessage(text='「ゴミの日」から最初からやり直してください🙇‍♂️')

    elif action == 'ゴミ曜日完了':
        if user_id in user_state and user_state[user_id].get('action') == 'set_trash_days':
            trash_type = user_state[user_id]['trash_type']
            days = user_state[user_id].get('days', '')
            conn = get_db()
            cur = conn.cursor()
            cur.execute('DELETE FROM trash_schedule WHERE trash_type=%s', (trash_type,))
            cur.execute('INSERT INTO trash_schedule (trash_type, weekdays) VALUES (%s, %s)', (trash_type, days))
            conn.commit()
            cur.close()
            conn.close()
            user_state.pop(user_id, None)
            reply = TextMessage(text=f'✅ {trash_type}を{days}曜日に登録しました！\n毎朝7時に通知します🗑️', quick_reply=QuickReply(items=[
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

    # グループからのメッセージは無視
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
                '🍚 ごはん\n朝・昼・夜のごはん時間を登録してリマインドを設定できます。夕食の予定を家族に共有したり、ごはんができたら一斉通知もできます。\n\n'
                '🚃 出発・帰宅\n今日の出発・帰宅時間とご飯の有無を家族に共有できます。確認メッセージを全員に送ることもできます。\n\n'
                '🛁 お風呂\nお風呂を洗ったか家族に報告・お願いができます。毎日決まった時間にお風呂確認を自動送信する設定も可能です。\n\n'
                '🗑️ ゴミの日\nゴミの種類と収集曜日を登録すると毎朝7時に自動通知されます。\n\n'
                '💡 設定した内容はすべて家族グループに通知されます。'
            )
            send_reply(api_client, event.reply_token, reply)

        else:
            send_reply(api_client, event.reply_token, TextMessage(text='メニューから選んでください。'))


@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            profile = line_bot_api.get_profile(user_id)
            name = profile.display_name
            group_ids = get_group_ids()
            active_gid = group_ids[0] if group_ids else None
            conn = get_db()
            cur = conn.cursor()
            if active_gid:
                cur.execute(
                    'INSERT INTO members (user_id, display_name, group_id) VALUES (%s, %s, %s) ON CONFLICT (user_id, group_id) DO UPDATE SET display_name=%s',
                    (user_id, name, active_gid, name)
                )
            else:
                cur.execute(
                    'INSERT INTO members (user_id, display_name, group_id) VALUES (%s, %s, NULL) ON CONFLICT DO NOTHING',
                    (user_id, name)
                )
            conn.commit()
            cur.close()
            conn.close()
            print(f'Member registered: {name}')
        except Exception as e:
            print(f'Member registration error: {e}')

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=
                    'こんにちは！まめBotです🫘\n\n'
                    '家族の日常をもっとスムーズにするお手伝いをします。\n\n'
                    '下のメニューから使ってみてください！\n'
                    '使い方を見るには「使い方」と送ってください📖'
                )]
            )
        )


@handler.add(JoinEvent)
def handle_join(event):
    group_id = event.source.group_id
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('UPDATE groups SET active = FALSE')
        cur.execute('INSERT INTO groups (group_id, active) VALUES (%s, TRUE) ON CONFLICT (group_id) DO UPDATE SET active = TRUE', (group_id,))
        conn.commit()
        cur.close()
        conn.close()
        print(f'Group registered: {group_id}')
    except Exception as e:
        print(f'Group registration error: {e}')

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        bot_info = line_bot_api.get_bot_info()
        basic_id = bot_info.basic_id.lstrip('@')
        friend_url = f'https://line.me/R/ti/p/@{basic_id}'
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=
                    'まめBotがグループに参加しました🫘\n\n'
                    '家族の日常をもっとスムーズにするお手伝いをします。\n\n'
                    '【手順】\n'
                    '① 下のリンクからまめBotを個別で友達追加\n'
                    '② 個別チャットのメニューから操作\n'
                    '③ 設定内容がこのグループに届きます\n\n'
                    '友達追加はこちら↓\n'
                    f'{friend_url}'
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
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)