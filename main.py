import json
import logging
import os
import random
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import psycopg2
import psycopg2.errors
import requests as http_requests
from flask import Flask, abort, request
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PostbackAction,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import (
    FollowEvent,
    JoinEvent,
    LeaveEvent,
    MessageEvent,
    PostbackEvent,
    TextMessageContent,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger('mamebot')

app = Flask(__name__)

LINE_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DATABASE_URL = os.environ.get('DATABASE_URL')

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

JST = timezone(timedelta(hours=9))


def get_jst_date():
    return datetime.now(JST).date()


@app.route("/", methods=['GET', 'HEAD'])
def health_check():
    return "OK", 200


# ============================================================
# DB
# ============================================================

@contextmanager
def db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with db() as conn:
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
                group_id TEXT UNIQUE,
                invite_code TEXT UNIQUE
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS members (
                id SERIAL PRIMARY KEY,
                user_id TEXT,
                display_name TEXT,
                group_id TEXT,
                UNIQUE (user_id, group_id)
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
        cur.execute('''
            CREATE TABLE IF NOT EXISTS reminder_sent (
                id SERIAL PRIMARY KEY,
                group_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                target_date DATE NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (group_id, kind, target_date)
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_state (
                user_id TEXT PRIMARY KEY,
                state JSONB NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS summary_schedule (
                id SERIAL PRIMARY KEY,
                group_id TEXT NOT NULL,
                summary_type TEXT NOT NULL,
                notify_time TIME NOT NULL,
                UNIQUE (group_id, summary_type)
            )
        ''')
        conn.commit()

        # 既存グループに「家族まとめ」のデフォルト時刻を初期化（重複時は無視）
        cur.execute('''
            INSERT INTO summary_schedule (group_id, summary_type, notify_time)
            SELECT group_id, 'daily', TIME '16:00' FROM groups
            ON CONFLICT (group_id, summary_type) DO NOTHING
        ''')
        conn.commit()

        # Idempotent migrations — ADD COLUMN/DROP CONSTRAINT IF [NOT] EXISTS は安全に再実行可
        cur.execute('ALTER TABLE groups ADD COLUMN IF NOT EXISTS invite_code TEXT UNIQUE')
        cur.execute('ALTER TABLE members ADD COLUMN IF NOT EXISTS group_id TEXT')
        cur.execute('ALTER TABLE members DROP CONSTRAINT IF EXISTS members_user_id_key')
        cur.execute('ALTER TABLE trash_schedule ADD COLUMN IF NOT EXISTS group_id TEXT')
        cur.execute("ALTER TABLE trash_schedule ADD COLUMN IF NOT EXISTS week_type TEXT DEFAULT 'every'")
        cur.execute('ALTER TABLE bath_schedule ADD COLUMN IF NOT EXISTS group_id TEXT')
        cur.execute('ALTER TABLE bath_done ADD COLUMN IF NOT EXISTS group_id TEXT')
        conn.commit()

        # ADD CONSTRAINT は IF NOT EXISTS 不可。既存時の DuplicateObject だけ握る。
        for ddl in [
            'ALTER TABLE members ADD CONSTRAINT members_user_group_unique UNIQUE (user_id, group_id)',
            'ALTER TABLE daily_schedule ADD CONSTRAINT unique_daily_user UNIQUE (user_id, created_date)',
            'ALTER TABLE bath_schedule ADD CONSTRAINT bath_schedule_group_id_unique UNIQUE (group_id)',
        ]:
            try:
                cur.execute(ddl)
                conn.commit()
            except psycopg2.errors.DuplicateObject:
                conn.rollback()
            except psycopg2.errors.DuplicateTable:
                conn.rollback()
        cur.close()


# ============================================================
# user_state (DB-backed)
# ============================================================

def get_state(user_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute('SELECT state FROM user_state WHERE user_id = %s', (user_id,))
        row = cur.fetchone()
        cur.close()
    return row[0] if row else None


def set_state(user_id, state):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            '''INSERT INTO user_state (user_id, state, updated_at) VALUES (%s, %s, NOW())
               ON CONFLICT (user_id) DO UPDATE SET state = EXCLUDED.state, updated_at = NOW()''',
            (user_id, json.dumps(state, ensure_ascii=False)),
        )
        conn.commit()
        cur.close()


def update_state(user_id, **patch):
    current = get_state(user_id) or {}
    current.update(patch)
    set_state(user_id, current)


def clear_state(user_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute('DELETE FROM user_state WHERE user_id = %s', (user_id,))
        conn.commit()
        cur.close()


# ============================================================
# Helpers
# ============================================================

def get_user_group(user_id):
    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                'SELECT group_id FROM members WHERE user_id = %s AND group_id IS NOT NULL ORDER BY id DESC LIMIT 1',
                (user_id,),
            )
            row = cur.fetchone()
            cur.close()
        return row[0] if row else None
    except Exception as e:
        logger.warning(f'get_user_group failed: {e}')
        return None


def get_display_name(api_client, user_id, default='だれか'):
    try:
        return MessagingApi(api_client).get_profile(user_id).display_name
    except Exception as e:
        logger.warning(f'get_profile failed for {user_id}: {e}')
        return default


def push_to_group(group_id, text):
    if not group_id:
        return
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {LINE_ACCESS_TOKEN}',
    }
    data = {
        'to': group_id,
        'messages': [{
            'type': 'textV2',
            'text': '{mention}\n' + text,
            'substitution': {
                'mention': {
                    'type': 'mention',
                    'mentionee': {'type': 'all'},
                },
            },
        }],
    }
    try:
        res = http_requests.post(
            'https://api.line.me/v2/bot/message/push',
            headers=headers,
            json=data,
            timeout=10,
        )
        logger.info(f'push_to_group status={res.status_code} group={group_id}')
        if res.status_code >= 400:
            logger.warning(f'push_to_group body={res.text}')
    except Exception as e:
        logger.warning(f'push_to_group failed: {e}')


def push_members(text, group_id):
    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute('SELECT user_id FROM members WHERE group_id = %s', (group_id,))
            member_ids = cur.fetchall()
            cur.close()
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {LINE_ACCESS_TOKEN}',
        }
        for (mid,) in member_ids:
            try:
                http_requests.post(
                    'https://api.line.me/v2/bot/message/push',
                    headers=headers,
                    json={'to': mid, 'messages': [{'type': 'text', 'text': text}]},
                    timeout=10,
                )
            except Exception as e:
                logger.warning(f'push to member {mid} failed: {e}')
    except Exception as e:
        logger.warning(f'push_members failed: {e}')


def send_daily_summary(group_id):
    """1日1回、家族全員の出発・帰宅・夕食予定をまとめて送る。"""
    today = get_jst_date()
    with db() as conn:
        cur = conn.cursor()
        cur.execute('''
            SELECT user_name, depart_time, arrive_time, meal_status FROM daily_schedule
            WHERE created_date = %s
            AND user_id IN (SELECT user_id FROM members WHERE group_id = %s)
            ORDER BY id
        ''', (today, group_id))
        answered = cur.fetchall()
        cur.execute('''
            SELECT display_name FROM members WHERE group_id = %s
            AND user_id NOT IN (SELECT user_id FROM daily_schedule WHERE created_date = %s)
        ''', (group_id, today))
        unanswered = cur.fetchall()
        cur.close()
    # 家族メンバーがそもそも未登録なら何もしない
    if not answered and not unanswered:
        return
    summary = f'🏠 今日の家族まとめ（{today.month}/{today.day}）'
    for r_name, r_depart, r_arrive, r_meal in answered:
        line_parts = [r_name]
        if r_depart:
            line_parts.append(f'出発 {r_depart}')
        if r_arrive:
            line_parts.append(f'帰宅 {r_arrive}')
        if r_meal:
            line_parts.append(f'夕食 {r_meal}')
        if len(line_parts) == 1:
            line_parts.append('未回答')
        summary += f'\n{" / ".join(line_parts)}'
    for (u_name,) in unanswered:
        summary += f'\n{u_name}: 未回答'
    push_to_group(group_id, summary)


def is_nth_week(date_obj, nth_weeks):
    week_of_month = (date_obj.day - 1) // 7 + 1
    return week_of_month in nth_weeks


def get_summary_time_label(group_id, summary_type):
    """まとめ通知の現在時刻を 'HH:MM' or '未設定' で返す。"""
    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                'SELECT notify_time FROM summary_schedule WHERE group_id = %s AND summary_type = %s',
                (group_id, summary_type),
            )
            row = cur.fetchone()
            cur.close()
        return row[0].strftime('%H:%M') if row else '未設定'
    except Exception as e:
        logger.warning(f'get_summary_time_label failed: {e}')
        return '未設定'


def mark_reminder(group_id, kind, target_date):
    """同一 group/kind/date は1回だけ通知する。新規登録に成功した時 True。"""
    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                '''INSERT INTO reminder_sent (group_id, kind, target_date) VALUES (%s, %s, %s)
                   ON CONFLICT (group_id, kind, target_date) DO NOTHING RETURNING id''',
                (group_id, kind, target_date),
            )
            row = cur.fetchone()
            conn.commit()
            cur.close()
        return row is not None
    except Exception as e:
        logger.warning(f'mark_reminder failed: {e}')
        return False


# ============================================================
# Reminder loop
# ============================================================

def reminder_loop():
    weekday_map = {0: '月', 1: '火', 2: '水', 3: '木', 4: '金', 5: '土', 6: '日'}
    while True:
        try:
            now = datetime.now(JST).replace(tzinfo=None)
            today_date = get_jst_date()
            today = weekday_map[now.weekday()]
            tomorrow = weekday_map[(now.weekday() + 1) % 7]
            tomorrow_date = today_date + timedelta(days=1)

            with db() as conn:
                cur = conn.cursor()
                cur.execute('''
                    SELECT group_id, trash_type, weekdays, week_type FROM trash_schedule
                    WHERE group_id IN (SELECT group_id FROM groups)
                ''')
                trash_rows = cur.fetchall()
                cur.execute('''
                    SELECT group_id, notify_time FROM bath_schedule
                    WHERE group_id IN (SELECT group_id FROM groups)
                ''')
                bath_rows = cur.fetchall()

                for group_id, trash_type, weekdays, week_type in trash_rows:
                    if not group_id:
                        continue
                    # 当日朝7時：今日が収集日のとき「今日は◯◯の日」を通知
                    if today in weekdays and _should_notify_week(week_type, today_date):
                        notify_dt = datetime.combine(now.date(), datetime.strptime('07:00', '%H:%M').time())
                        if abs((now - notify_dt).total_seconds()) < 90:
                            if mark_reminder(group_id, f'trash_today:{trash_type}', today_date):
                                push_to_group(group_id, f'🗑️ 今日は{trash_type}の日です〜\n忘れずに〜🫘')
                    # 前日21時：明日が収集日のとき「明日は◯◯の日」を通知
                    if tomorrow in weekdays and _should_notify_week(week_type, tomorrow_date):
                        notify_dt = datetime.combine(now.date(), datetime.strptime('21:00', '%H:%M').time())
                        if abs((now - notify_dt).total_seconds()) < 90:
                            if mark_reminder(group_id, f'trash_tomorrow:{trash_type}', tomorrow_date):
                                push_to_group(group_id, f'🗑️ 明日は{trash_type}の日です〜\n準備よろしくおねがいします🫘')

                for group_id, notify_time in bath_rows:
                    if not group_id:
                        continue
                    notify_dt = datetime.combine(now.date(), notify_time)
                    if abs((now - notify_dt).total_seconds()) < 90:
                        cur.execute(
                            'SELECT id FROM bath_done WHERE group_id = %s AND done_date = %s',
                            (group_id, today_date),
                        )
                        done = cur.fetchone()
                        if not done and mark_reminder(group_id, 'bath_unwashed', today_date):
                            push_to_group(group_id, '🛁 そろそろお風呂…まだ洗われてないみたい🫘')

                cur.execute('''
                    SELECT group_id, notify_time FROM summary_schedule
                    WHERE summary_type = 'daily'
                    AND group_id IN (SELECT group_id FROM groups)
                ''')
                summary_rows = cur.fetchall()
                for group_id, notify_time in summary_rows:
                    if not group_id:
                        continue
                    notify_dt = datetime.combine(now.date(), notify_time)
                    if abs((now - notify_dt).total_seconds()) < 90:
                        if mark_reminder(group_id, 'summary:daily', today_date):
                            send_daily_summary(group_id)
                cur.close()
        except Exception as e:
            logger.warning(f'Reminder loop error: {e}')
        time.sleep(60)


def _should_notify_week(week_type, date_obj):
    if week_type == 'every':
        return True
    if week_type == 'odd':
        return is_nth_week(date_obj, [1, 3])
    if week_type == 'even':
        return is_nth_week(date_obj, [2, 4])
    if week_type == 'first':
        return is_nth_week(date_obj, [1])
    if week_type == 'second':
        return is_nth_week(date_obj, [2])
    if week_type == 'third':
        return is_nth_week(date_obj, [3])
    if week_type == 'fourth':
        return is_nth_week(date_obj, [4])
    return False


# ============================================================
# Quick Reply factories
# ============================================================

AM_HOURS = [6, 7, 8, 9, 10, 11]
PM_HOURS = [12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]
WEEKDAY_ITEMS = [
    QuickReplyItem(action=PostbackAction(label=w, data=f'action=ゴミ曜日&value={w}'))
    for w in ['月', '火', '水', '木', '金', '土', '日']
]


def make_hour_qr(hours, context):
    return QuickReply(items=[
        QuickReplyItem(action=PostbackAction(label=f'{h}時', data=f'action=時&value={h}&context={context}'))
        for h in hours
    ])


def make_minute_qr(context):
    return QuickReply(items=[
        QuickReplyItem(action=PostbackAction(label=f'{m:02d}分', data=f'action=分&value={m}&context={context}'))
        for m in [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]
    ])


# ============================================================
# Webhook
# ============================================================

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    if not signature:
        logger.warning('Missing X-Line-Signature header')
        abort(400)
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.warning('Invalid signature')
        abort(400)
    return 'OK'


def send_reply(api_client, reply_token, reply):
    MessagingApi(api_client).reply_message(
        ReplyMessageRequest(reply_token=reply_token, messages=[reply])
    )


def not_registered_reply():
    return TextMessage(text='グループへの登録が必要です。\nグループに表示された登録コード（6桁）をここに入力してください。')


# ============================================================
# Action processor
# ============================================================

def process_action(action, value, context, user_id, api_client, reply_token):
    today = get_jst_date()
    user_group = get_user_group(user_id)

    # ========== ごはん ==========
    if action == 'ごはん':
        clear_state(user_id)
        if not user_group:
            send_reply(api_client, reply_token, not_registered_reply())
            return
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
        name = get_display_name(api_client, user_id)
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                '''INSERT INTO daily_schedule (user_id, user_name, meal_status, created_date)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (user_id, created_date) DO UPDATE SET
                   meal_status=EXCLUDED.meal_status, user_name=EXCLUDED.user_name''',
                (user_id, name, value, today),
            )
            conn.commit()
            cur.close()
        reply = TextMessage(text=f'☑️ 夕食の予定を登録しました\n・{value}\n\n家族まとめは設定時刻に家族グループへ送ります🫘')

    elif action == 'ごはんできた':
        name = get_display_name(api_client, user_id)
        if user_group:
            push_to_group(user_group, f'🍚 {name}がごはん作ってくれたよ〜\nみんな集合〜！')
        reply = TextMessage(text='以下の内容を家族グループに送りました☑️\n・ごはんができました！')

    # ========== お風呂 ==========
    elif action == 'お風呂':
        clear_state(user_id)
        if not user_group:
            send_reply(api_client, reply_token, not_registered_reply())
            return
        name = get_display_name(api_client, user_id, default='あなた')
        set_state(user_id, {'name': name})
        with db() as conn:
            cur = conn.cursor()
            cur.execute('SELECT notify_time FROM bath_schedule WHERE group_id = %s LIMIT 1', (user_group,))
            row = cur.fetchone()
            cur.close()
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
        name = (get_state(user_id) or {}).get('name', 'だれか')
        if user_group:
            with db() as conn:
                cur = conn.cursor()
                cur.execute(
                    'INSERT INTO bath_done (group_id, done_date) VALUES (%s, %s) ON CONFLICT DO NOTHING',
                    (user_group, today),
                )
                conn.commit()
                cur.close()
            push_to_group(user_group, f'🛁 {name}がお風呂を{value}')
        clear_state(user_id)
        reply = TextMessage(text=f'以下の内容を家族グループに送りました☑️\n・お風呂を{value}')

    elif action == 'お風呂お願い':
        if user_group:
            push_to_group(user_group, '🛁 お風呂を洗ってください！')
        reply = TextMessage(text='以下の内容を家族グループに送りました☑️\n・お風呂をお願いしました')

    elif action == 'お風呂時間変更確認':
        reply = TextMessage(text='通知時間を変更しますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='✏️ 変更する', data='action=お風呂時間設定')),
            QuickReplyItem(action=PostbackAction(label='キャンセル', data='action=お風呂')),
        ]))

    elif action == 'お風呂時間設定':
        set_state(user_id, {'action': 'set_bath_hour'})
        reply = TextMessage(text='通知する時間帯を選んでください。', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='午前', data='action=お風呂時間帯&value=am')),
            QuickReplyItem(action=PostbackAction(label='午後', data='action=お風呂時間帯&value=pm')),
        ]))

    elif action == 'お風呂時間帯':
        hours = AM_HOURS if value == 'am' else PM_HOURS
        update_state(user_id, action='set_bath_minute')
        reply = TextMessage(text='何時ですか？', quick_reply=make_hour_qr(hours, 'bath'))

    elif action == '時' and context == 'bath':
        update_state(user_id, hour=int(value))
        reply = TextMessage(text='何分ですか？', quick_reply=make_minute_qr('bath'))

    # ========== 家族まとめ時刻設定 ==========
    elif action == '家族まとめ時刻設定':
        set_state(user_id, {'action': 'set_summary_time'})
        reply = TextMessage(text='時間帯を選んでください。', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='午前', data='action=まとめ時間帯&value=am&context=summary_daily')),
            QuickReplyItem(action=PostbackAction(label='午後', data='action=まとめ時間帯&value=pm&context=summary_daily')),
        ]))

    elif action == 'まとめ時間帯' and context == 'summary_daily':
        hours = AM_HOURS if value == 'am' else PM_HOURS
        reply = TextMessage(text='何時ですか？', quick_reply=make_hour_qr(hours, context))

    elif action == '時' and context == 'summary_daily':
        update_state(user_id, hour=int(value))
        reply = TextMessage(text='何分ですか？', quick_reply=make_minute_qr(context))

    elif action == '分' and context == 'summary_daily':
        state = get_state(user_id) or {}
        hour = state.get('hour')
        if hour is None:
            send_reply(api_client, reply_token, TextMessage(text='メニューから最初からやり直してください。'))
            return
        minute = int(value)
        notify_time = f'{hour:02d}:{minute:02d}'
        if user_group:
            with db() as conn:
                cur = conn.cursor()
                cur.execute(
                    '''INSERT INTO summary_schedule (group_id, summary_type, notify_time)
                       VALUES (%s, 'daily', %s)
                       ON CONFLICT (group_id, summary_type) DO UPDATE SET notify_time = EXCLUDED.notify_time''',
                    (user_group, notify_time),
                )
                conn.commit()
                cur.close()
        clear_state(user_id)
        reply = TextMessage(text=f'☑️ 家族まとめ通知時刻を {notify_time} に設定しました')

    elif action == '分' and context == 'bath':
        state = get_state(user_id) or {}
        hour = state.get('hour')
        if hour is None:
            send_reply(api_client, reply_token, TextMessage(text='お風呂メニューから最初からやり直してください。'))
            return
        minute = int(value)
        notify_time = f'{hour:02d}:{minute:02d}'
        if user_group:
            with db() as conn:
                cur = conn.cursor()
                cur.execute(
                    '''INSERT INTO bath_schedule (group_id, notify_time) VALUES (%s, %s)
                       ON CONFLICT (group_id) DO UPDATE SET notify_time = %s''',
                    (user_group, notify_time, notify_time),
                )
                conn.commit()
                cur.close()
        clear_state(user_id)
        reply = TextMessage(text=f'以下の設定を保存しました☑️\n・お風呂未洗い通知: {notify_time}')

    # ========== 出発・帰宅 ==========
    elif action == '出発・帰宅':
        clear_state(user_id)
        if not user_group:
            send_reply(api_client, reply_token, not_registered_reply())
            return
        current_time = get_summary_time_label(user_group, 'daily')
        reply = TextMessage(text=f'どうしますか？\n\n家族まとめ通知: {current_time}', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='📤 時間を共有する', data='action=帰宅共有開始')),
            QuickReplyItem(action=PostbackAction(label='📋 今日の状況を確認', data='action=帰宅確認')),
            QuickReplyItem(action=PostbackAction(label='⏰ まとめ時刻を変更', data='action=家族まとめ時刻設定')),
        ]))

    elif action == '帰宅共有開始':
        set_state(user_id, {'action': 'share_type', 'depart': None, 'arrive': None})
        reply = TextMessage(text='どの時間を共有しますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='🚶 出発のみ', data='action=共有タイプ&value=depart')),
            QuickReplyItem(action=PostbackAction(label='🏠 帰宅のみ', data='action=共有タイプ&value=arrive')),
            QuickReplyItem(action=PostbackAction(label='両方', data='action=共有タイプ&value=both')),
        ]))

    elif action == '共有タイプ':
        update_state(user_id, share_type=value)
        if value == 'arrive':
            update_state(user_id, action='share_arrive_ampm')
            reply = TextMessage(text='帰宅の時間帯を選んでください。', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='午前', data='action=帰宅時間帯&value=am')),
                QuickReplyItem(action=PostbackAction(label='午後', data='action=帰宅時間帯&value=pm')),
            ]))
        else:
            update_state(user_id, action='share_depart_ampm')
            reply = TextMessage(text='出発の時間帯を選んでください。', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='午前', data='action=出発時間帯&value=am')),
                QuickReplyItem(action=PostbackAction(label='午後', data='action=出発時間帯&value=pm')),
            ]))

    elif action == '出発時間帯':
        hours = AM_HOURS if value == 'am' else PM_HOURS
        update_state(user_id, action='share_depart_hour')
        reply = TextMessage(text='出発は何時ですか？', quick_reply=make_hour_qr(hours, 'depart'))

    elif action == '時' and context == 'depart':
        update_state(user_id, depart_hour=int(value))
        reply = TextMessage(quick_reply=make_minute_qr('depart'), text='何分ですか？')

    elif action == '分' and context == 'depart':
        state = get_state(user_id) or {}
        hour = state.get('depart_hour', 0)
        minute = int(value)
        update_state(user_id, depart=f'{hour:02d}:{minute:02d}')
        share_type = state.get('share_type')
        if share_type == 'both':
            update_state(user_id, action='share_arrive_ampm')
            reply = TextMessage(text='帰宅の時間帯を選んでください。', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='午前', data='action=帰宅時間帯&value=am')),
                QuickReplyItem(action=PostbackAction(label='午後', data='action=帰宅時間帯&value=pm')),
                QuickReplyItem(action=PostbackAction(label='スキップ', data='action=帰宅スキップ')),
            ]))
        else:
            update_state(user_id, action='share_meal')
            reply = TextMessage(text='ご飯はどうしますか？', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='🏠 家で食べる', data='action=ごはん状況&value=家で食べる🏠')),
                QuickReplyItem(action=PostbackAction(label='🍴 外で食べる', data='action=ごはん状況&value=外で食べる🍴')),
                QuickReplyItem(action=PostbackAction(label='❓ 未定', data='action=ごはん状況&value=未定❓')),
            ]))

    elif action == '帰宅時間帯':
        hours = AM_HOURS if value == 'am' else PM_HOURS
        update_state(user_id, action='share_arrive_hour')
        reply = TextMessage(text='帰宅は何時ですか？', quick_reply=make_hour_qr(hours, 'arrive'))

    elif action == '時' and context == 'arrive':
        update_state(user_id, arrive_hour=int(value))
        reply = TextMessage(quick_reply=make_minute_qr('arrive'), text='何分ですか？')

    elif action == '分' and context == 'arrive':
        state = get_state(user_id) or {}
        hour = state.get('arrive_hour', 0)
        minute = int(value)
        update_state(user_id, arrive=f'{hour:02d}:{minute:02d}', action='share_meal')
        reply = TextMessage(text='ご飯はどうしますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='🏠 家で食べる', data='action=ごはん状況&value=家で食べる🏠')),
            QuickReplyItem(action=PostbackAction(label='🍴 外で食べる', data='action=ごはん状況&value=外で食べる🍴')),
            QuickReplyItem(action=PostbackAction(label='❓ 未定', data='action=ごはん状況&value=未定❓')),
        ]))

    elif action == '帰宅スキップ':
        update_state(user_id, arrive=None, action='share_meal')
        reply = TextMessage(text='ご飯はどうしますか？', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='🏠 家で食べる', data='action=ごはん状況&value=家で食べる🏠')),
            QuickReplyItem(action=PostbackAction(label='🍴 外で食べる', data='action=ごはん状況&value=外で食べる🍴')),
            QuickReplyItem(action=PostbackAction(label='❓ 未定', data='action=ごはん状況&value=未定❓')),
        ]))

    elif action == 'ごはん状況':
        state = get_state(user_id) or {}
        depart = state.get('depart')
        arrive = state.get('arrive')
        name = get_display_name(api_client, user_id)
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                '''INSERT INTO daily_schedule (user_id, user_name, depart_time, arrive_time, meal_status, created_date)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (user_id, created_date) DO UPDATE SET
                   depart_time=EXCLUDED.depart_time, arrive_time=EXCLUDED.arrive_time, meal_status=EXCLUDED.meal_status''',
                (user_id, name, depart, arrive, value, today),
            )
            conn.commit()
            cur.close()
        clear_state(user_id)
        confirm_parts = []
        if depart:
            confirm_parts.append(f'・出発 {depart}')
        if arrive:
            confirm_parts.append(f'・帰宅 {arrive}')
        confirm_parts.append(f'・ご飯 {value}')
        reply = TextMessage(text='☑️ 登録しました\n' + '\n'.join(confirm_parts) + '\n\n家族まとめは設定時刻に家族グループへ送ります🫘')

    elif action == '帰宅確認':
        with db() as conn:
            cur = conn.cursor()
            if user_group:
                cur.execute('''
                    SELECT user_name, depart_time, arrive_time, meal_status FROM daily_schedule
                    WHERE created_date = %s AND user_id IN (SELECT user_id FROM members WHERE group_id = %s)
                    ORDER BY id
                ''', (today, user_group))
            else:
                cur.execute(
                    'SELECT user_name, depart_time, arrive_time, meal_status FROM daily_schedule WHERE created_date = %s ORDER BY id',
                    (today,),
                )
            answered = cur.fetchall()
            if user_group:
                cur.execute('''
                    SELECT display_name FROM members WHERE group_id = %s
                    AND user_id NOT IN (SELECT user_id FROM daily_schedule WHERE created_date = %s)
                ''', (user_group, today))
            else:
                cur.execute(
                    'SELECT display_name FROM members WHERE user_id NOT IN (SELECT user_id FROM daily_schedule WHERE created_date = %s)',
                    (today,),
                )
            unanswered = cur.fetchall()
            cur.close()
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

    # ========== ゴミの日 ==========
    elif action == 'ゴミの日':
        clear_state(user_id)
        if not user_group:
            send_reply(api_client, reply_token, not_registered_reply())
            return
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                'SELECT trash_type, weekdays, week_type FROM trash_schedule WHERE group_id = %s',
                (user_group,),
            )
            rows = cur.fetchall()
            cur.close()
        if rows:
            def week_type_label(wt):
                labels = {
                    'odd': '（第1・3週）',
                    'even': '（第2・4週）',
                    'first': '（第1週のみ）',
                    'second': '（第2週のみ）',
                    'third': '（第3週のみ）',
                    'fourth': '（第4週のみ）',
                }
                return labels.get(wt, '')
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
        with db() as conn:
            cur = conn.cursor()
            cur.execute('SELECT trash_type, weekdays FROM trash_schedule WHERE group_id = %s', (user_group,))
            rows = cur.fetchall()
            cur.close()
        items = [
            QuickReplyItem(action=PostbackAction(label=f'✏️ {t}', data=f'action=ゴミ変更&value={t}'))
            for t, w in rows
        ] + [
            QuickReplyItem(action=PostbackAction(label=f'🗑️ {t}を削除', data=f'action=ゴミ削除&value={t}'))
            for t, w in rows
        ]
        reply = TextMessage(text='変更・削除するゴミの種類を選んでください。', quick_reply=QuickReply(items=items[:13]))

    elif action == 'ゴミ変更':
        set_state(user_id, {'action': 'set_trash_days', 'trash_type': value, 'days': ''})
        reply = TextMessage(text=f'「{value}」の新しい収集曜日を選んでください。', quick_reply=QuickReply(items=WEEKDAY_ITEMS))

    elif action == 'ゴミ削除':
        with db() as conn:
            cur = conn.cursor()
            cur.execute('DELETE FROM trash_schedule WHERE trash_type=%s AND group_id=%s', (value, user_group))
            conn.commit()
            cur.close()
        reply = TextMessage(text=f'以下の設定を保存しました☑️\n・{value}のスケジュールを削除しました')

    elif action == 'ゴミ登録':
        set_state(user_id, {'action': 'set_trash_days'})
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
        set_state(user_id, {'action': 'set_trash_days', 'trash_type': value, 'days': ''})
        reply = TextMessage(text=f'「{value}」の収集曜日を選んでください。', quick_reply=QuickReply(items=WEEKDAY_ITEMS))

    elif action == 'ゴミ種類その他':
        set_state(user_id, {'action': 'set_trash_type_custom'})
        reply = TextMessage(text='ゴミの種類を入力してください。\n例: 古紙')

    elif action == 'ゴミ曜日':
        state = get_state(user_id)
        if state and state.get('action') == 'set_trash_days':
            current_days = state.get('days', '')
            if value not in current_days:
                current_days += value
            update_state(user_id, days=current_days)
            reply = TextMessage(text=f'選択中: {current_days}曜日', quick_reply=QuickReply(items=WEEKDAY_ITEMS + [
                QuickReplyItem(action=PostbackAction(label='✅ 次へ', data='action=ゴミ週タイプ選択')),
            ]))
        else:
            reply = TextMessage(text='「ゴミの日」から最初からやり直してください🙇‍♂️')

    elif action == 'ゴミ週タイプ選択':
        reply = TextMessage(text='収集頻度を選んでください。', quick_reply=QuickReply(items=[
            QuickReplyItem(action=PostbackAction(label='毎週', data='action=ゴミ曜日完了&value=every')),
            QuickReplyItem(action=PostbackAction(label='第1・3週', data='action=ゴミ曜日完了&value=odd')),
            QuickReplyItem(action=PostbackAction(label='第2・4週', data='action=ゴミ曜日完了&value=even')),
            QuickReplyItem(action=PostbackAction(label='第1週のみ', data='action=ゴミ曜日完了&value=first')),
            QuickReplyItem(action=PostbackAction(label='第2週のみ', data='action=ゴミ曜日完了&value=second')),
            QuickReplyItem(action=PostbackAction(label='第3週のみ', data='action=ゴミ曜日完了&value=third')),
            QuickReplyItem(action=PostbackAction(label='第4週のみ', data='action=ゴミ曜日完了&value=fourth')),
        ]))

    elif action == 'ゴミ曜日完了':
        state = get_state(user_id)
        if state and state.get('action') == 'set_trash_days':
            trash_type = state['trash_type']
            days = state.get('days', '')
            week_type = value if value in ['every', 'odd', 'even', 'first', 'second', 'third', 'fourth'] else 'every'
            with db() as conn:
                cur = conn.cursor()
                cur.execute('DELETE FROM trash_schedule WHERE trash_type=%s AND group_id=%s', (trash_type, user_group))
                cur.execute(
                    'INSERT INTO trash_schedule (group_id, trash_type, weekdays, week_type) VALUES (%s, %s, %s, %s)',
                    (user_group, trash_type, days, week_type),
                )
                conn.commit()
                cur.close()
            week_label = {
                'every': '毎週',
                'odd': '第1・3週',
                'even': '第2・4週',
                'first': '第1週のみ',
                'second': '第2週のみ',
                'third': '第3週のみ',
                'fourth': '第4週のみ',
            }.get(week_type, '毎週')
            clear_state(user_id)
            reply = TextMessage(text=f'以下の設定を保存しました☑️\n・{trash_type}: {days}曜日（{week_label}）\n前日21時と当日朝7時に通知します🗑️', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='➕ 続けて登録', data='action=ゴミ登録')),
            ]))
        else:
            reply = TextMessage(text='「ゴミの日」から最初からやり直してください。')

    elif action == '完了':
        reply = TextMessage(text='設定が完了しました！✅')

    else:
        reply = TextMessage(text='メニューから選んでください。')

    send_reply(api_client, reply_token, reply)


# ============================================================
# Event handlers
# ============================================================

@handler.add(PostbackEvent)
def handle_postback(event):
    params = dict(parse_qsl(event.postback.data, keep_blank_values=True))
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
        state = get_state(user_id)
        if state and state.get('action') == 'set_trash_type_custom':
            trash_type = text
            set_state(user_id, {'action': 'set_trash_days', 'trash_type': trash_type, 'days': ''})
            reply = TextMessage(
                text=f'「{trash_type}」の収集曜日を選んでください。',
                quick_reply=QuickReply(items=WEEKDAY_ITEMS),
            )
            send_reply(api_client, event.reply_token, reply)

        elif text in ['ごはん', 'お風呂', '出発・帰宅', 'ゴミの日']:
            process_action(text, '', '', user_id, api_client, event.reply_token)

        elif text == '使い方':
            reply = TextMessage(text=
                '📖 まめBot 使い方\n\n'
                '🏠 家族まとめ通知\n夕食の予定・出発・帰宅時間を個別チャットで登録すると、設定時刻に1日1回だけまとめて家族グループへ通知します（デフォルト 16:00）。\n\n'
                '🍚 ごはん\n夕食の予定を登録（家族まとめに反映）。「できました！」は即時通知。\n\n'
                '🚃 出発・帰宅\n今日の出発・帰宅時間とご飯の有無を登録（家族まとめに反映）。「今日の状況を確認」で随時チェック・全員への入力依頼もできます。\n\n'
                '🛁 お風呂\nお風呂を洗ったか家族に報告・お願いができます。設定した時間までに洗われていなければ自動通知します。\n\n'
                '🗑️ ゴミの日\nゴミの種類と収集曜日を登録すると前日21時と当日朝7時に自動通知されます。第1・3週や第2・4週の設定も可能です。\n\n'
                '⏰ 家族まとめ通知時刻は「出発・帰宅」メニュー内の「⏰ まとめ時刻を変更」から設定できます🫘'
            )
            send_reply(api_client, event.reply_token, reply)

        elif text.isdigit() and len(text) == 6:
            with db() as conn:
                cur = conn.cursor()
                cur.execute('SELECT group_id FROM groups WHERE invite_code = %s', (text,))
                row = cur.fetchone()
                if row:
                    group_id = row[0]
                    name = get_display_name(api_client, user_id)
                    cur.execute(
                        '''INSERT INTO members (user_id, display_name, group_id) VALUES (%s, %s, %s)
                           ON CONFLICT (user_id, group_id) DO UPDATE SET display_name=%s''',
                        (user_id, name, group_id, name),
                    )
                    conn.commit()
                    cur.close()
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
                    reply = TextMessage(text='コードが見つかりませんでした。グループに表示されたコードを確認してください。')
            send_reply(api_client, event.reply_token, reply)

        else:
            send_reply(api_client, event.reply_token, TextMessage(text='メニューから選んでください。\nグループの登録コード（6桁）をお持ちの方はそのまま入力してください。'))


@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        name = get_display_name(api_client, user_id, default='(不明)')
        logger.info(f'Member followed: {name} ({user_id})')

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=
                    'はじめまして、まめBotです🫘\n\n'
                    '家族のちょっとした連絡、ぼくがお手伝いします。\n\n'
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
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                '''INSERT INTO groups (group_id, invite_code) VALUES (%s, %s)
                   ON CONFLICT (group_id) DO UPDATE SET invite_code = %s''',
                (group_id, invite_code, invite_code),
            )
            # 家族まとめ通知のデフォルト時刻をセット
            cur.execute(
                '''INSERT INTO summary_schedule (group_id, summary_type, notify_time)
                   VALUES (%s, 'daily', TIME '16:00')
                   ON CONFLICT (group_id, summary_type) DO NOTHING''',
                (group_id,),
            )
            conn.commit()
            cur.close()
        logger.info(f'Group registered: {group_id}, code: {invite_code}')
    except Exception as e:
        logger.warning(f'Group registration error: {e}')

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=
                    'まめBotがグループに参加しました🫘\n'
                    'よろしくおねがいします〜\n\n'
                    '家族のちょっとした連絡、ぼくがお手伝いします。\n\n'
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
        with db() as conn:
            cur = conn.cursor()
            # daily_schedule は user_id ベース。グループに残ってる member 経由で当該行を削除。
            cur.execute('''
                DELETE FROM daily_schedule
                WHERE user_id IN (SELECT user_id FROM members WHERE group_id = %s)
            ''', (group_id,))
            cur.execute('DELETE FROM members WHERE group_id = %s', (group_id,))
            cur.execute('DELETE FROM trash_schedule WHERE group_id = %s', (group_id,))
            cur.execute('DELETE FROM bath_schedule WHERE group_id = %s', (group_id,))
            cur.execute('DELETE FROM bath_done WHERE group_id = %s', (group_id,))
            cur.execute('DELETE FROM reminder_sent WHERE group_id = %s', (group_id,))
            cur.execute('DELETE FROM summary_schedule WHERE group_id = %s', (group_id,))
            cur.execute('DELETE FROM groups WHERE group_id = %s', (group_id,))
            conn.commit()
            cur.close()
        logger.info(f'Group removed (cascade): {group_id}')
    except Exception as e:
        logger.warning(f'Group removal error: {e}')


with app.app_context():
    init_db()
    t = threading.Thread(target=reminder_loop, daemon=True)
    t.start()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
