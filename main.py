from flask import Flask, request, abort
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, PushMessageRequest, TextMessage, QuickReply, QuickReplyItem, MessageAction, PostbackAction
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent
import os
import psycopg2
from datetime import datetime, timedelta
import threading
import time
import re

app = Flask(__name__)

configuration = Configuration(access_token=os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
DATABASE_URL = os.environ.get('DATABASE_URL')
GROUP_ID = os.environ.get('LINE_GROUP_ID')

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
    conn.commit()
    cur.close()
    conn.close()

def reminder_loop():
    while True:
        try:
            conn = get_db()
            cur = conn.cursor()
            now = datetime.now()
            weekday = now.weekday()
            weekday_map = {0:'月',1:'火',2:'水',3:'木',4:'金',5:'土',6:'日'}
            today = weekday_map[weekday]

            cur.execute('SELECT meal_type, meal_time, remind_minutes, group_id FROM meal_times')
            for meal_type, meal_time, remind_minutes, group_id in cur.fetchall():
                meal_dt = datetime.combine(now.date(), meal_time)
                remind_dt = meal_dt - timedelta(minutes=remind_minutes)
                diff = abs((now - remind_dt).total_seconds())
                if diff < 60 and group_id:
                    with ApiClient(configuration) as api_client:
                        MessagingApi(api_client).push_message(PushMessageRequest(
                            to=group_id,
                            messages=[TextMessage(text=f'🍚 {meal_type}ごはんの時間まであと{remind_minutes}分です！')]
                        ))

            cur.execute('SELECT trash_type, weekdays, notify_time FROM trash_schedule')
            for trash_type, weekdays, notify_time in cur.fetchall():
                if today in weekdays:
                    notify_dt = datetime.combine(now.date(), notify_time)
                    diff = abs((now - notify_dt).total_seconds())
                    if diff < 60 and GROUP_ID:
                        with ApiClient(configuration) as api_client:
                            MessagingApi(api_client).push_message(PushMessageRequest(
                                to=GROUP_ID,
                                messages=[TextMessage(text=f'🗑️ 今日は{trash_type}の日です！忘れずに！')]
                            ))
            cur.close()
            conn.close()
        except Exception as e:
            print(f'Reminder error: {e}')
        time.sleep(60)

user_state = {}

MINUTES_QR = QuickReply(items=[
    QuickReplyItem(action=MessageAction(label='00分', text='分_00')),
    QuickReplyItem(action=MessageAction(label='05分', text='分_05')),
    QuickReplyItem(action=MessageAction(label='10分', text='分_10')),
    QuickReplyItem(action=MessageAction(label='15分', text='分_15')),
    QuickReplyItem(action=MessageAction(label='20分', text='分_20')),
    QuickReplyItem(action=MessageAction(label='25分', text='分_25')),
    QuickReplyItem(action=MessageAction(label='30分', text='分_30')),
    QuickReplyItem(action=MessageAction(label='35分', text='分_35')),
    QuickReplyItem(action=MessageAction(label='40分', text='分_40')),
    QuickReplyItem(action=MessageAction(label='45分', text='分_45')),
    QuickReplyItem(action=MessageAction(label='50分', text='分_50')),
    QuickReplyItem(action=MessageAction(label='55分', text='分_55')),
])

def make_hour_qr(hours):
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label=f'{h}時', text=f'時_{h}'))
        for h in hours
    ])

MORNING_HOURS = make_hour_qr([6,7,8,9,10])
LUNCH_HOURS = make_hour_qr([11,12,13,14])
EVENING_HOURS = make_hour_qr([17,18,19,20,21,22])

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data
    params = dict(item.split('=') for item in data.split('&'))
    action = params.get('action', '')
    fake_event = type('obj', (object,), {
        'message': type('obj', (object,), {'text': action})(),
        'source': event.source,
        'reply_token': event.reply_token
    })()
    handle_message(fake_event)

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text
    user_id = event.source.user_id

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # ========== ごはん ==========
        if text == 'ごはん':
            user_state.pop(user_id, None)
            quick_reply = QuickReply(items=[
                QuickReplyItem(action=MessageAction(label='🌅 朝', text='ごはん_朝')),
                QuickReplyItem(action=MessageAction(label='☀️ 昼', text='ごはん_昼')),
                QuickReplyItem(action=MessageAction(label='🌙 夜', text='ごはん_夜')),
                QuickReplyItem(action=MessageAction(label='🔔 できました！', text='ごはんできた')),
            ])
            reply = TextMessage(text='どの時間帯を設定しますか？', quick_reply=quick_reply)

        elif text == 'ごはん_朝':
            user_state[user_id] = {'action': 'set_meal_hour', 'meal_type': '朝'}
            reply = TextMessage(text='朝ごはんは何時台ですか？', quick_reply=MORNING_HOURS)

        elif text == 'ごはん_昼':
            user_state[user_id] = {'action': 'set_meal_hour', 'meal_type': '昼'}
            reply = TextMessage(text='昼ごはんは何時台ですか？', quick_reply=LUNCH_HOURS)

        elif text == 'ごはん_夜':
            user_state[user_id] = {'action': 'set_meal_hour', 'meal_type': '夜'}
            reply = TextMessage(text='夜ごはんは何時台ですか？', quick_reply=EVENING_HOURS)

        elif text.startswith('時_') and user_id in user_state and user_state[user_id].get('action') == 'set_meal_hour':
            hour = int(text.replace('時_', ''))
            user_state[user_id]['hour'] = hour
            user_state[user_id]['action'] = 'set_meal_minute'
            reply = TextMessage(text=f'{hour}時何分ですか？', quick_reply=MINUTES_QR)

        elif text.startswith('分_') and user_id in user_state and user_state[user_id].get('action') == 'set_meal_minute':
            minute = int(text.replace('分_', ''))
            hour = user_state[user_id]['hour']
            meal_type = user_state[user_id]['meal_type']
            conn = get_db()
            cur = conn.cursor()
            cur.execute('DELETE FROM meal_times WHERE meal_type=%s', (meal_type,))
            cur.execute(
                'INSERT INTO meal_times (meal_type, meal_time, remind_minutes, group_id) VALUES (%s, %s, %s, %s)',
                (meal_type, f'{hour:02d}:{minute:02d}', 120, GROUP_ID)
            )
            conn.commit()
            cur.close()
            conn.close()
            user_state.pop(user_id, None)
            quick_reply = QuickReply(items=[
                QuickReplyItem(action=MessageAction(label='🌅 朝も設定', text='ごはん_朝')),
                QuickReplyItem(action=MessageAction(label='☀️ 昼も設定', text='ごはん_昼')),
                QuickReplyItem(action=MessageAction(label='🌙 夜も設定', text='ごはん_夜')),
                QuickReplyItem(action=MessageAction(label='✅ 終わり', text='ごはん設定完了')),
            ])
            reply = TextMessage(
                text=f'✅ {meal_type}ごはん {hour:02d}:{minute:02d} を登録しました！\n2時間前にリマインドします。\n他の時間帯も設定しますか？',
                quick_reply=quick_reply
            )

        elif text == 'ごはん設定完了':
            reply = TextMessage(text='ごはんの設定が完了しました！🍚')

        elif text == 'ごはんできた':
            if GROUP_ID:
                with ApiClient(configuration) as api_client2:
                    MessagingApi(api_client2).push_message(PushMessageRequest(
                        to=GROUP_ID,
                        messages=[TextMessage(text='🍚 ご飯ができました！みんな集まってください！')]
                    ))
            reply = TextMessage(text='家族グループに送りました！')

        # ========== お風呂 ==========
        elif text == 'お風呂':
            user_state.pop(user_id, None)
            try:
                profile = line_bot_api.get_profile(user_id)
                name = profile.display_name
            except:
                name = 'あなた'
            user_state[user_id] = {'name': name}
            quick_reply = QuickReply(items=[
                QuickReplyItem(action=MessageAction(label='✅ 洗った', text='お風呂_洗った')),
                QuickReplyItem(action=MessageAction(label='❌ 洗ってない', text='お風呂_洗ってない')),
                QuickReplyItem(action=MessageAction(label='🛁 洗って入れた', text='お風呂_洗って入れた')),
            ])
            reply = TextMessage(text='お風呂の状況を教えてください！', quick_reply=quick_reply)

        elif text in ['お風呂_洗った', 'お風呂_洗ってない', 'お風呂_洗って入れた']:
            status_map = {
                'お風呂_洗った': '洗いました🚿',
                'お風呂_洗ってない': 'まだ洗っていません💦',
                'お風呂_洗って入れた': '洗ってお湯を入れました🛁'
            }
            status = status_map[text]
            name = user_state.get(user_id, {}).get('name', 'だれか')
            if GROUP_ID:
                with ApiClient(configuration) as api_client2:
                    MessagingApi(api_client2).push_message(PushMessageRequest(
                        to=GROUP_ID,
                        messages=[TextMessage(text=f'🛁 {name}がお風呂を{status}')]
                    ))
            user_state.pop(user_id, None)
            reply = TextMessage(text='家族グループに送りました！')

        # ========== 出発・帰宅 ==========
        elif text == '出発・帰宅':
            user_state.pop(user_id, None)
            quick_reply = QuickReply(items=[
                QuickReplyItem(action=MessageAction(label='📤 共有する', text='帰宅_共有')),
                QuickReplyItem(action=MessageAction(label='📥 確認する', text='帰宅_確認')),
            ])
            reply = TextMessage(text='共有しますか？確認しますか？', quick_reply=quick_reply)

        elif text == '帰宅_共有':
            quick_reply = QuickReply(items=[
                QuickReplyItem(action=MessageAction(label='🏠 帰宅', text='帰宅_共有_帰宅')),
                QuickReplyItem(action=MessageAction(label='🚃 出発', text='帰宅_共有_出発')),
            ])
            reply = TextMessage(text='帰宅・出発どちらを共有しますか？', quick_reply=quick_reply)

        elif text in ['帰宅_共有_帰宅', '帰宅_共有_出発']:
            kind = '帰宅' if text == '帰宅_共有_帰宅' else '出発'
            user_state[user_id] = {'action': 'share_time_hour', 'kind': kind}
            reply = TextMessage(text=f'{kind}は何時台ですか？', quick_reply=EVENING_HOURS)

        elif text.startswith('時_') and user_id in user_state and user_state[user_id].get('action') == 'share_time_hour':
            hour = int(text.replace('時_', ''))
            user_state[user_id]['hour'] = hour
            user_state[user_id]['action'] = 'share_time_minute'
            reply = TextMessage(text=f'{hour}時何分ですか？', quick_reply=MINUTES_QR)

        elif text.startswith('分_') and user_id in user_state and user_state[user_id].get('action') == 'share_time_minute':
            minute = int(text.replace('分_', ''))
            hour = user_state[user_id]['hour']
            user_state[user_id]['time'] = f'{hour:02d}:{minute:02d}'
            user_state[user_id]['action'] = 'share_meal'
            quick_reply = QuickReply(items=[
                QuickReplyItem(action=MessageAction(label='🏠 家で食べる', text='ごはん状況_家')),
                QuickReplyItem(action=MessageAction(label='🍴 外で食べる', text='ごはん状況_外')),
                QuickReplyItem(action=MessageAction(label='❓ 未定', text='ごはん状況_未定')),
            ])
            reply = TextMessage(text='ご飯はどうしますか？', quick_reply=quick_reply)

        elif text in ['ごはん状況_家', 'ごはん状況_外', 'ごはん状況_未定'] and user_id in user_state and user_state[user_id].get('action') == 'share_meal':
            meal_map = {
                'ごはん状況_家': '家で食べる🏠',
                'ごはん状況_外': '外で食べる🍴',
                'ごはん状況_未定': '未定❓'
            }
            meal_status = meal_map[text]
            kind = user_state[user_id]['kind']
            time_str = user_state[user_id]['time']
            try:
                profile = line_bot_api.get_profile(user_id)
                name = profile.display_name
            except:
                name = 'だれか'
            if GROUP_ID:
                with ApiClient(configuration) as api_client2:
                    MessagingApi(api_client2).push_message(PushMessageRequest(
                        to=GROUP_ID,
                        messages=[TextMessage(text=f'🚃 {name} {time_str}{kind}予定 / {meal_status}')]
                    ))
            user_state.pop(user_id, None)
            reply = TextMessage(text='家族グループに送りました！')

        elif text == '帰宅_確認':
            if GROUP_ID:
                with ApiClient(configuration) as api_client2:
                    MessagingApi(api_client2).push_message(PushMessageRequest(
                        to=GROUP_ID,
                        messages=[TextMessage(text='🚃 今日の帰宅・出発時間を教えてください！\nまめBotの個別チャットで「出発・帰宅」から共有してください。')]
                    ))
            reply = TextMessage(text='家族グループに確認メッセージを送りました！')

        # ========== ゴミの日 ==========
        elif text == 'ゴミの日':
            user_state.pop(user_id, None)
            conn = get_db()
            cur = conn.cursor()
            cur.execute('SELECT trash_type, weekdays FROM trash_schedule')
            rows = cur.fetchall()
            cur.close()
            conn.close()
            if rows:
                schedule_text = '\n'.join([f'・{t}: {w}曜日' for t, w in rows])
                reply = TextMessage(text=f'現在のゴミ出しスケジュール📅\n{schedule_text}\n\n変更する場合は「ゴミ登録」と送ってください。')
            else:
                reply = TextMessage(text='ゴミ出しスケジュールが未設定です。\n「ゴミ登録」と送って設定してください。')

        elif text == 'ゴミ登録':
            user_state[user_id] = {'action': 'set_trash_type'}
            reply = TextMessage(text='ゴミの種類を入力してください。\n例: 燃えるゴミ')

        elif user_id in user_state and user_state[user_id].get('action') == 'set_trash_type':
            user_state[user_id] = {'action': 'set_trash_days', 'trash_type': text}
            quick_reply = QuickReply(items=[
                QuickReplyItem(action=MessageAction(label='月', text='ゴミ曜日_月')),
                QuickReplyItem(action=MessageAction(label='火', text='ゴミ曜日_火')),
                QuickReplyItem(action=MessageAction(label='水', text='ゴミ曜日_水')),
                QuickReplyItem(action=MessageAction(label='木', text='ゴミ曜日_木')),
                QuickReplyItem(action=MessageAction(label='金', text='ゴミ曜日_金')),
                QuickReplyItem(action=MessageAction(label='土', text='ゴミ曜日_土')),
                QuickReplyItem(action=MessageAction(label='日', text='ゴミ曜日_日')),
            ])
            reply = TextMessage(text=f'「{text}」の収集曜日を選んでください。', quick_reply=quick_reply)

        elif text.startswith('ゴミ曜日_'):
            day = text.replace('ゴミ曜日_', '')
            if user_id in user_state and user_state[user_id].get('action') == 'set_trash_days':
                current_days = user_state[user_id].get('days', '')
                if day not in current_days:
                    current_days += day
                user_state[user_id]['days'] = current_days
                quick_reply = QuickReply(items=[
                    QuickReplyItem(action=MessageAction(label='月', text='ゴミ曜日_月')),
                    QuickReplyItem(action=MessageAction(label='火', text='ゴミ曜日_火')),
                    QuickReplyItem(action=MessageAction(label='水', text='ゴミ曜日_水')),
                    QuickReplyItem(action=MessageAction(label='木', text='ゴミ曜日_木')),
                    QuickReplyItem(action=MessageAction(label='金', text='ゴミ曜日_金')),
                    QuickReplyItem(action=MessageAction(label='土', text='ゴミ曜日_土')),
                    QuickReplyItem(action=MessageAction(label='日', text='ゴミ曜日_日')),
                    QuickReplyItem(action=MessageAction(label='✅ 完了', text='ゴミ曜日完了')),
                ])
                reply = TextMessage(text=f'選択中: {current_days}曜日\n他にもありますか？', quick_reply=quick_reply)
            else:
                reply = TextMessage(text='「ゴミ登録」と送って最初からやり直してください。')

        elif text == 'ゴミ曜日完了':
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
                reply = TextMessage(text=f'✅ {trash_type}を{days}曜日に登録しました！\n毎朝7時に通知します🗑️')
            else:
                reply = TextMessage(text='「ゴミ登録」と送って最初からやり直してください。')

        else:
            reply = TextMessage(text=text)

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[reply]
            )
        )

with app.app_context():
    init_db()
    t = threading.Thread(target=reminder_loop, daemon=True)
    t.start()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
