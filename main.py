from flask import Flask, request, abort
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, PushMessageRequest, TextMessage, QuickReply, QuickReplyItem, MessageAction, PostbackAction
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent, JoinEvent, FollowEvent
import os
import psycopg2
from datetime import datetime, timedelta
import threading
import time

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
    cur.execute('''
        CREATE TABLE IF NOT EXISTS bath_schedule (
            id SERIAL PRIMARY KEY,
            notify_time TIME
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
            weekday_map = {0:'月',1:'火',2:'水',3:'木',4:'金',5:'土',6:'日'}
            today = weekday_map[now.weekday()]

            cur.execute('SELECT meal_type, meal_time, remind_minutes, group_id FROM meal_times')
            for meal_type, meal_time, remind_minutes, group_id in cur.fetchall():
                meal_dt = datetime.combine(now.date(), meal_time)
                remind_dt = meal_dt - timedelta(minutes=remind_minutes)
                diff = abs((now - remind_dt).total_seconds())
                if diff < 60 and group_id:
                    with ApiClient(configuration) as api_client:
                        MessagingApi(api_client).push_message(PushMessageRequest(
                            to=group_id,
                            messages=[TextMessage(text=f'🍚 {meal_type}ごはんリマインド\n⏰ {meal_time.strftime("%H:%M")} まであと{remind_minutes}分')]
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

            cur.execute('SELECT notify_time FROM bath_schedule LIMIT 1')
            row = cur.fetchone()
            if row:
                notify_dt = datetime.combine(now.date(), row[0])
                diff = abs((now - notify_dt).total_seconds())
                if diff < 60 and GROUP_ID:
                    with ApiClient(configuration) as api_client:
                        MessagingApi(api_client).push_message(PushMessageRequest(
                            to=GROUP_ID,
                            messages=[TextMessage(text='🛁 お風呂洗ってありますか？')]
                        ))

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
    if GROUP_ID:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(
                to=GROUP_ID,
                messages=[TextMessage(text=text)]
            ))

@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data
    params = dict(item.split('=') for item in data.split('&'))
    action = params.get('action', '')
    value = params.get('value', '')
    context = params.get('context', '')
    user_id = event.source.user_id

    with ApiClient(configuration) as api_client:

        if action == 'ごはん':
            user_state.pop(user_id, None)
            reply = TextMessage(text='どの時間帯を設定しますか？', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='🌅 朝', data='action=ごはん選択&value=朝')),
                QuickReplyItem(action=PostbackAction(label='☀️ 昼', data='action=ごはん選択&value=昼')),
                QuickReplyItem(action=PostbackAction(label='🌙 夜', data='action=ごはん選択&value=夜')),
                QuickReplyItem(action=PostbackAction(label='🔔 できました！', data='action=ごはんできた')),
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
            reply = TextMessage(text=f'✅ {meal_type}ごはん {hour:02d}:{minute:02d} を登録しました！\n2時間前にリマインドします。', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='🌅 朝も設定', data='action=ごはん選択&value=朝')),
                QuickReplyItem(action=PostbackAction(label='☀️ 昼も設定', data='action=ごはん選択&value=昼')),
                QuickReplyItem(action=PostbackAction(label='🌙 夜も設定', data='action=ごはん選択&value=夜')),
                QuickReplyItem(action=PostbackAction(label='✅ 終わり', data='action=完了')),
            ]))

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
            reply = TextMessage(text='何時台に確認しますか？', quick_reply=make_hour_qr([19,20,21,22,23], 'bath'))

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
                QuickReplyItem(action=PostbackAction(label='📤 時間を共有する', data='action=帰宅共有メニュー')),
                QuickReplyItem(action=PostbackAction(label='📥 時間を確認する', data='action=帰宅確認')),
            ]))

        elif action == '帰宅共有メニュー':
            reply = TextMessage(text='帰宅・出発どちらを共有しますか？', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='🏠 帰宅', data='action=帰宅種類&value=帰宅')),
                QuickReplyItem(action=PostbackAction(label='🚃 出発', data='action=帰宅種類&value=出発')),
            ]))

        elif action == '帰宅の種類':
            user_state[user_id] = {'action': 'share_time_hour', 'kind': value}
            reply = TextMessage(text=f'{value}は何時台ですか？', quick_reply=make_hour_qr(EVENING_HOURS, 'share'))

        elif action == '時' and context == 'share':
            user_state[user_id]['hour'] = int(value)
            user_state[user_id]['action'] = 'share_time_minute'
            reply = TextMessage(text=f'{value}時何分ですか？', quick_reply=make_minute_qr('share'))

        elif action == '分' and context == 'share':
            hour = user_state[user_id]['hour']
            minute = int(value)
            user_state[user_id]['time'] = f'{hour:02d}:{minute:02d}'
            user_state[user_id]['action'] = 'share_meal'
            reply = TextMessage(text='ご飯はどうしますか？', quick_reply=QuickReply(items=[
                QuickReplyItem(action=PostbackAction(label='🏠 家で食べる', data='action=ごはん状況&value=家で食べる🏠')),
                QuickReplyItem(action=PostbackAction(label='🍴 外で食べる', data='action=ごはん状況&value=外で食べる🍴')),
                QuickReplyItem(action=PostbackAction(label='❓ 未定', data='action=ごはん状況&value=未定❓')),
            ]))

        elif action == 'ごはん状況':
            kind = user_state[user_id]['kind']
            time_str = user_state[user_id]['time']
            try:
                name = MessagingApi(api_client).get_profile(user_id).display_name
            except:
                name = 'だれか'
            push_group(f'🚃 {name} {time_str}{kind}予定 / {value}')
            user_state.pop(user_id, None)
            reply = TextMessage(text='家族グループに送りました☑️')

        elif action == '帰宅確認':
            push_group('🚃 今日の帰宅・出発時間を教えてください！\nまめBotの個別チャットで「出発・帰宅」から共有してください。')
            reply = TextMessage(text='家族グループに確認メッセージを送りました☑️')

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
                trash_type = user_state[user_id]['trash_type']
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

        elif action == 'ゴミ曜日完了✅':
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

        send_reply(api_client, event.reply_token, reply)

@handler.add(FollowEvent)
def handle_follow(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=
                    'こんにちは！まめBotです🫘\n\n'
                    '家族の日常をもっとスムーズにするお手伝いをします。\n\n'
                    '【できること】\n'
                    '🍚 ごはんの時間をリマインド\n'
                    '🚃 出発・帰宅時間を家族に共有\n'
                    '🛁 お風呂の状況をお知らせ\n'
                    '🗑️ ゴミの日を通知\n\n'
                    '下のメニューから使ってみてください！'
                )]
            )
        )

@handler.add(JoinEvent)
def handle_join(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        bot_info = line_bot_api.get_bot_info()
        friend_url = f'https://line.me/R/ti/p/@{bot_info.basic_id}'
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=
                    'まめBotがグループに参加しました🫘\n\n'
                    '家族の日常をもっとスムーズにするお手伝いをします。\n\n'
                    '【できること】\n'
                    '🍚 ごはんの時間リマインド\n'
                    '🚃 出発・帰宅時間の共有\n'
                    '🛁 お風呂の状況お知らせ\n'
                    '🗑️ ゴミの日通知\n\n'
                    'まめBotに話しかけるには、\n'
                    '個別チャットで友達追加してください！\n'
                    f'↓\n{friend_url}\n\n'
                    '設定はまめBotとの個別チャットから\nメニューを使ってできます😊'
                )]
            )
        )

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text
    user_id = event.source.user_id

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

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
        else:
            reply = TextMessage(text='メニューから選んでください。')

        line_bot_api.reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[reply])
        )

def setup_rich_menu():
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            
            from linebot.v3.messaging.models import (
                RichMenuRequest, RichMenuSize, RichMenuArea, RichMenuBounds
            )
            
            rich_menu = RichMenuRequest(
                size=RichMenuSize(width=2500, height=1686),
                selected=True,
                name='まめBotメニュー',
                chat_bar_text='メニュー',
                areas=[
                    RichMenuArea(
                        bounds=RichMenuBounds(x=0, y=0, width=1250, height=843),
                        action=PostbackAction(label='ごはん', data='action=ごはん')
                    ),
                    RichMenuArea(
                        bounds=RichMenuBounds(x=1250, y=0, width=1250, height=843),
                        action=PostbackAction(label='出発・帰宅', data='action=出発・帰宅')
                    ),
                    RichMenuArea(
                        bounds=RichMenuBounds(x=0, y=843, width=1250, height=843),
                        action=PostbackAction(label='お風呂', data='action=お風呂')
                    ),
                    RichMenuArea(
                        bounds=RichMenuBounds(x=1250, y=843, width=1250, height=843),
                        action=PostbackAction(label='ゴミの日', data='action=ゴミの日')
                    ),
                ]
            )
            rich_menu_id = line_bot_api.create_rich_menu(rich_menu).rich_menu_id
            print(f'Rich menu created: {rich_menu_id}')
            return rich_menu_id
    except Exception as e:
        print(f'Rich menu setup error: {e}')
        return None

with app.app_context():
    init_db()
    t = threading.Thread(target=reminder_loop, daemon=True)
    t.start()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
