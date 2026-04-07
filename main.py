from flask import Flask, request, abort
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage, QuickReply, QuickReplyItem, MessageAction
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent
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
    conn.commit()
    cur.close()
    conn.close()

def reminder_loop():
    while True:
        try:
            conn = get_db()
            cur = conn.cursor()
            now = datetime.now()
            cur.execute('SELECT meal_type, meal_time, remind_minutes, group_id FROM meal_times')
            rows = cur.fetchall()
            for meal_type, meal_time, remind_minutes, group_id in rows:
                meal_dt = datetime.combine(now.date(), meal_time)
                remind_dt = meal_dt - timedelta(minutes=remind_minutes)
                diff = abs((now - remind_dt).total_seconds())
                if diff < 60 and group_id:
                    with ApiClient(configuration) as api_client:
                        line_bot_api = MessagingApi(api_client)
                        from linebot.v3.messaging import PushMessageRequest
                        line_bot_api.push_message(PushMessageRequest(
                            to=group_id,
                            messages=[TextMessage(text=f'🍚 {meal_type}ごはんの時間まであと{remind_minutes}分です！')]
                        ))
            cur.close()
            conn.close()
        except Exception as e:
            print(f'Reminder error: {e}')
        time.sleep(60)

user_state = {}

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text
    user_id = event.source.user_id

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # ごはんメニュー
        if text == 'ごはん':
            user_state.pop(user_id, None)
            quick_reply = QuickReply(items=[
                QuickReplyItem(action=MessageAction(label='🌅 朝', text='ごはん_朝')),
                QuickReplyItem(action=MessageAction(label='☀️ 昼', text='ごはん_昼')),
                QuickReplyItem(action=MessageAction(label='🌙 夜', text='ごはん_夜')),
                QuickReplyItem(action=MessageAction(label='🔔 ご飯できました！', text='ごはんできた')),
            ])
            reply = TextMessage(text='どの時間帯を設定しますか？', quick_reply=quick_reply)

        elif text in ['ごはん_朝', 'ごはん_昼', 'ごはん_夜']:
            mapping = {'ごはん_朝': '朝', 'ごはん_昼': '昼', 'ごはん_夜': '夜'}
            meal = mapping[text]
            user_state[user_id] = {'action': 'set_meal', 'meal_type': meal}
            reply = TextMessage(text=f'{meal}ごはんの時間を入力してください！🍚\n例: 19:00')

        elif user_id in user_state and user_state[user_id].get('action') == 'set_meal':
            time_pattern = re.compile(r'^(\d{1,2}):(\d{2})$')
            match = time_pattern.match(text)
            if match:
                hour, minute = int(match.group(1)), int(match.group(2))
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    meal_type = user_state[user_id]['meal_type']
                    group_id = user_state[user_id].get('group_id', '')
                    conn = get_db()
                    cur = conn.cursor()
                    cur.execute(
                        'INSERT INTO meal_times (meal_type, meal_time, remind_minutes, group_id) VALUES (%s, %s, %s, %s)',
                        (meal_type, f'{hour:02d}:{minute:02d}', 120, group_id)
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
                else:
                    reply = TextMessage(text='正しい時間を入力してください！🙇\n例: 19:00')
            else:
                reply = TextMessage(text='時間の形式が正しくないです！😭\n例: 19:00')

        elif text == 'ごはん設定完了':
            reply = TextMessage(text='ごはんの設定が完了しました！🍚')

        elif text == 'ごはんできた':
            reply = TextMessage(text='🍚 ご飯ができました！みんな集まってください！')

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
