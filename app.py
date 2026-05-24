import os
import json
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sock import Sock

app = Flask(__name__)
app.config['SECRET_KEY'] = 'splotch_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///splotch.db'
db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = None 

sock = Sock(app)

canvas_history = {} 
undone_history = {} # ДОБАВЛЕНО: Хранилище отмененных действий
room_occupancy = {} 
chat_history = {}   
clients = {} 
active_rooms = {}

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)

class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(50), nullable=False)

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash('Этот логин уже занят. Попробуйте другой!')
            return redirect(url_for('register'))
            
        hashed_password = generate_password_hash(password)
        new_user = User(username=username, password=hashed_password)
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash('Неправильный логин или пароль')
            return redirect(url_for('login'))
            
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html', username=current_user.username)


def emit(ws, event, data):
    try:
        ws.send(json.dumps({'event': event, 'data': data}))
    except Exception:
        pass

def broadcast(room_name, event, data, exclude_ws=None):
    if room_name in active_rooms:
        message = json.dumps({'event': event, 'data': data})
        for ws in list(active_rooms[room_name]):
            if ws != exclude_ws:
                try:
                    ws.send(message)
                except Exception:
                    pass

def manual_join_room(ws, room_name):
    clients[ws]['room'] = room_name
    if room_name not in active_rooms:
        active_rooms[room_name] = set()
    active_rooms[room_name].add(ws)

def manual_leave_room(ws, room_name, disconnect=False, to_main=False, new_room=None):
    if room_name in active_rooms and ws in active_rooms[room_name]:
        active_rooms[room_name].remove(ws)
        if not active_rooms[room_name]:
            del active_rooms[room_name]
            
    username = clients[ws]['user']
    
    if room_name in room_occupancy:
        room_occupancy[room_name] -= 1
        if room_occupancy[room_name] <= 0 and room_name != 'main':
            with app.app_context():
                room_to_del = Room.query.filter_by(name=room_name).first()
                if room_to_del:
                    db.session.delete(room_to_del)
                    db.session.commit()
            if room_name in canvas_history:
                del canvas_history[room_name] 
            if room_name in undone_history:
                del undone_history[room_name]
            del room_occupancy[room_name]
            if room_name in chat_history:
                del chat_history[room_name]

    if disconnect:
        broadcast(room_name, 'chat_message', {'user': 'Система', 'text': f'Пользователь {username} покинул игру'})
    elif to_main:
        broadcast(room_name, 'chat_message', {'user': 'Система', 'text': f'Пользователь {username} перешел в общий зал'})
    elif new_room:
        broadcast(room_name, 'chat_message', {'user': 'Система', 'text': f'Пользователь {username} перешел в комнату {new_room}'})

# Функция очистки истории отмен пользователя при новом действии
def clear_user_redo_history(room, username):
    if room in undone_history:
        undone_history[room] = [block for block in undone_history[room] if block['items'][0].get('user') != username]

@sock.route('/ws')
def handle_ws(ws):
    if not current_user.is_authenticated:
        return
        
    username = current_user.username
    clients[ws] = {'user': username, 'room': None}
    
    try:
        while True:
            raw_data = ws.receive()
            if not raw_data:
                break
                
            msg = json.loads(raw_data)
            event = msg.get('event')
            data = msg.get('data', {})
            
            if event == 'join_main':
                manual_join_room(ws, 'main')
                room_occupancy['main'] = room_occupancy.get('main', 0) + 1
                
                emit(ws, 'canvas_state', canvas_history.get('main', []))
                emit(ws, 'chat_state', chat_history.get('main', []))
                
                broadcast('main', 'chat_message', {'user': 'Система', 'text': f'Пользователь {username} вошел в общий зал'}, exclude_ws=ws)
                emit(ws, 'chat_message', {'user': 'Система', 'text': 'Вы вошли в общий зал'})

            elif event == 'create_room':
                room_name = data['room'].strip()
                password = data['password'].strip()
                
                if Room.query.filter_by(name=room_name).first():
                    emit(ws, 'error', {'msg': 'Комната с таким именем уже существует!'})
                    continue

                new_room = Room(name=room_name, password=password)
                db.session.add(new_room)
                db.session.commit()
                
                broadcast('main', 'chat_message', {'user': 'Система', 'text': f'Пользователь {username} создал комнату "{room_name}"'})
                
                manual_join_room(ws, room_name)
                room_occupancy[room_name] = 1
                emit(ws, 'room_joined', {'room': room_name})
                
                emit(ws, 'canvas_state', [])
                emit(ws, 'chat_state', [])
                emit(ws, 'chat_message', {'user': 'Система', 'text': f'Вы вошли в комнату {room_name}'})

            elif event == 'join_private':
                room_name = data['room'].strip()
                password = data['password'].strip()
                
                room = Room.query.filter_by(name=room_name).first()
                if room and room.password == password:
                    manual_join_room(ws, room_name)
                    room_occupancy[room_name] = room_occupancy.get(room_name, 0) + 1
                    emit(ws, 'room_joined', {'room': room_name})
                    
                    emit(ws, 'canvas_state', canvas_history.get(room_name, []))
                    emit(ws, 'chat_state', chat_history.get(room_name, [])) 
                    
                    broadcast(room_name, 'chat_message', {'user': 'Система', 'text': f'Пользователь {username} зашел в комнату'}, exclude_ws=ws)
                    emit(ws, 'chat_message', {'user': 'Система', 'text': f'Вы вошли в комнату {room_name}'})
                else:
                    emit(ws, 'error', {'msg': 'Неверное имя комнаты или пароль!'})

            elif event == 'leave_room_event':
                room_name = data['room']
                manual_leave_room(ws, room_name, to_main=data.get('to_main'), new_room=data.get('new_room'))

            elif event == 'draw_line':
                room = data['room']
                data['user'] = username
                clear_user_redo_history(room, username) # Очищаем Redo при рисовании
                if room not in canvas_history:
                    canvas_history[room] = []
                canvas_history[room].append(data)
                broadcast(room, 'draw_line', data, exclude_ws=ws)

            elif event == 'draw_shape':
                room = data['room']
                data['user'] = username
                clear_user_redo_history(room, username) # Очищаем Redo при рисовании
                if room not in canvas_history:
                    canvas_history[room] = []
                canvas_history[room].append(data)
                broadcast(room, 'draw_shape', data, exclude_ws=ws)

            elif event == 'draw_image':
                room = data['room']
                data['user'] = username
                clear_user_redo_history(room, username) # Очищаем Redo
                if room not in canvas_history:
                    canvas_history[room] = []
                canvas_history[room].append({'type': 'image', 'image_data': data['image_data'], 'x': data.get('x', 0), 'y': data.get('y', 0), 'user': username, 'action_id': data.get('action_id')})
                broadcast(room, 'draw_image', data, exclude_ws=ws)

            elif event == 'clear_canvas':
                room = data['room']
                canvas_history[room] = [] 
                undone_history[room] = []
                broadcast(room, 'clear_canvas', {})

            elif event == 'undo_action':
                room = data['room']
                if room in canvas_history:
                    last_action_id = None
                    # Ищем action_id самого последнего действия текущего пользователя
                    for i in range(len(canvas_history[room]) - 1, -1, -1):
                        if canvas_history[room][i].get('user') == username:
                            last_action_id = canvas_history[room][i].get('action_id')
                            if last_action_id:
                                break
                    
                    if last_action_id:
                        # Разделяем на то, что останется, и то, что отменяется
                        to_keep = []
                        to_undo = []
                        for item in canvas_history[room]:
                            if item.get('action_id') == last_action_id:
                                to_undo.append(item)
                            else:
                                to_keep.append(item)
                                
                        canvas_history[room] = to_keep
                        
                        # Сохраняем в историю отмен
                        if room not in undone_history:
                            undone_history[room] = []
                        undone_history[room].append({'action_id': last_action_id, 'items': to_undo})
                        
                        broadcast(room, 'canvas_state', canvas_history[room])

            # Обработка возврата отмененного действия
            elif event == 'redo_action':
                room = data['room']
                if room in undone_history and undone_history[room]:
                    last_undone_idx = None
                    # Ищем последнее отмененное действие текущего пользователя
                    for i in range(len(undone_history[room]) - 1, -1, -1):
                        if undone_history[room][i]['items'][0].get('user') == username:
                            last_undone_idx = i
                            break
                            
                    if last_undone_idx is not None:
                        # Возвращаем действие на холст
                        action_block = undone_history[room].pop(last_undone_idx)
                        canvas_history[room].extend(action_block['items'])
                        broadcast(room, 'canvas_state', canvas_history[room])


            elif event == 'chat_message':
                room = data['room']
                msg_data = {'user': username, 'text': data['text']}
                if room not in chat_history:
                    chat_history[room] = []
                chat_history[room].append(msg_data)
                broadcast(room, 'chat_message', msg_data)

    except Exception as e:
        pass
    finally:
        user_info = clients.get(ws)
        if user_info and user_info['room']:
            manual_leave_room(ws, user_info['room'], disconnect=True)
        if ws in clients:
            del clients[ws]


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000, debug=True)