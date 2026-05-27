import json # библиотека для работы с json - кодирование декодирование
from flask import Flask, render_template, request, redirect, url_for, flash # для html шаблонов, доступ к данным HTTP-запроса, url
from flask_sqlalchemy import SQLAlchemy # для базы данных
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user # компоненты для пользовательских сеансов: авторизация, завершение сессии, пр.
from werkzeug.security import generate_password_hash, check_password_hash # для хэширования паролей 
from flask_sock import Sock # для работы с веб сокет

app = Flask(__name__) # создание экземпляра приложения
app.config['SECRET_KEY'] = 'splotch_secret_key' # строка используемая для защиты от подделки
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///splotch.db' # место для создания бд
db = SQLAlchemy(app) # инициализация бд

login_manager = LoginManager(app) # создание менеджера логина - управляет авторизацией
login_manager.login_view = 'login' # маршрут логина
login_manager.login_message = None # отключение стандартного сообщения

sock = Sock(app) # подключение поддержки веб сокет

canvas_history = {} # история рисования для каждой комнаты
undone_history = {} # список отмененных действий
room_occupancy = {} # список количества участников в каждой комнате
chat_history = {} # история чата для комнаты
clients = {} # список соединний. ключ - объект вебсокет, значение - инф о пользователе и его комнате
active_rooms = {} # список активных комнат: ключ - имя комнаты, значение - множество участников

# модель пользователя для бд, наследуется от UserMixin
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True) # автоматически генерируется ключ
    username = db.Column(db.String(50), unique=True, nullable=False) 
    password = db.Column(db.String(255), nullable=False)

# модель комнаты
class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(50), nullable=False)

# загрузка пользователя из бд по айди
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# маршрут для регистрации пользователя
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash('Этот логин уже занят. Попробуйте другой!') # функция для уведомления
            return redirect(url_for('register'))
            
        hashed_password = generate_password_hash(password)
        new_user = User(username=username, password=hashed_password)
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('login'))
    return render_template('register.html')

# маршрут для входа в систему
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

# маршрут для выхода из системы
@app.route('/logout')
@login_required # только для авторизованных пользователей
def logout():
    logout_user() #завершаем сессию
    return redirect(url_for('login'))

# маршрут открытия главной страницы
@app.route('/')
@login_required
def index():
    return render_template('index.html', username=current_user.username)

# отправка события одному клиенту
def emit(ws, event, data):
    try:
        ws.send(json.dumps({'event': event, 'data': data}))
    except Exception:
        pass

# отправка всем участникам комнаты (кроме исключенного)
def broadcast(room_name, event, data, exclude_ws=None):
    if room_name in active_rooms:
        message = json.dumps({'event': event, 'data': data}) # метод сериализации
        for ws in list(active_rooms[room_name]): # перебираем веб сокет соединения
            if ws != exclude_ws:
                try:
                    ws.send(message)
                except Exception:
                    pass

# присоединение веб сокета к комнате
def manual_join_room(ws, room_name):
    clients[ws]['room'] = room_name # сохраняем имя комнаты для клиента
    if room_name not in active_rooms: # если комната новая создаем запись
        active_rooms[room_name] = set()
    active_rooms[room_name].add(ws) # добавляем веб сокет в множество участников

# выход из комнаты
def manual_leave_room(ws, room_name, disconnect=False, to_main=False):
    # если комната существует и пользователь там есть удаляем его
    if room_name in active_rooms and ws in active_rooms[room_name]:
        active_rooms[room_name].remove(ws)
        if not active_rooms[room_name]: # если комната теперь пустая удаляем из списка
            del active_rooms[room_name]
            
    username = clients[ws]['user'] # чтобы отправить остальным сообщение
    
    if room_name in room_occupancy: # уменьшаем счетчик и если это не мэйн удаляем из бд и списков
        room_occupancy[room_name] -= 1
        if room_occupancy[room_name] <= 0 and room_name != 'main':
            with app.app_context(): 
                room_to_del = Room.query.filter_by(name=room_name).first()
                if room_to_del:
                    db.session.delete(room_to_del) # удаляем запись
                    db.session.commit()
            # удаляем всю инфу комнаты
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

# очистка истории отмен пользователя при новом действии
def clear_user_redo_history(room, username):
    if room in undone_history:
        undone_history[room] = [block for block in undone_history[room] if block['items'][0].get('user') != username]

# обработчик вебсокет соединений
@sock.route('/ws') 
def handle_ws(ws): # проверяем авторизован ли пользователь
    if not current_user.is_authenticated:
        return
        
    username = current_user.username
    clients[ws] = {'user': username, 'room': None} # вносим в список
    
    try: # бесконечный цикл приема сообщений
        while True:
            raw_data = ws.receive() # ждем сообщения
            if not raw_data:
                break # выходим из цикла если соединение закрыто
                
            msg = json.loads(raw_data) # метод десериализации
            event = msg.get('event')
            data = msg.get('data', {})
            
            # вход в общий зал
            if event == 'join_main':
                manual_join_room(ws, 'main') # функция присоединения, см выше
                room_occupancy['main'] = room_occupancy.get('main', 0) + 1 # увеличиваем счетчик участников
                
                # отправляем пользователю историю рисования и чата
                emit(ws, 'canvas_state', canvas_history.get('main', [])) 
                emit(ws, 'chat_state', chat_history.get('main', []))
                
                # уведомляем всех о входе
                broadcast('main', 'chat_message', {'user': 'Система', 'text': f'Пользователь {username} вошел в общий зал'}, exclude_ws=ws)
                emit(ws, 'chat_message', {'user': 'Система', 'text': 'Вы вошли в общий зал'})

            # создание комнаты
            elif event == 'create_room':
                room_name = data['room'].strip() # получаем имя и пароль и удаляем пробелы по краям
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

            # вход в комнату
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

            # выход из комнаты (не принудительный)
            elif event == 'leave_room_event':
                room_name = data['room']
                manual_leave_room(ws, room_name, to_main=data.get('to_main'))

            # кисть или ластик
            elif event == 'draw_line':
                room = data['room']
                data['user'] = username # добавляем к данным имя пользователя
                clear_user_redo_history(room, username) # очищаем историю при рисовании
                if room not in canvas_history: # если истории нет создаем
                    canvas_history[room] = []
                canvas_history[room].append(data) # добавляем действие в историю
                broadcast(room, 'draw_line', data, exclude_ws=ws) # рассылаем всем

            # фигура - аналогично
            elif event == 'draw_shape':
                room = data['room']
                data['user'] = username
                clear_user_redo_history(room, username) 
                if room not in canvas_history:
                    canvas_history[room] = []
                canvas_history[room].append(data)
                broadcast(room, 'draw_shape', data, exclude_ws=ws)
            
            # загрузка - аналогично
            elif event == 'draw_image':
                room = data['room']
                data['user'] = username
                clear_user_redo_history(room, username) 
                if room not in canvas_history:
                    canvas_history[room] = []
                canvas_history[room].append({'type': 'image', 'image_data': data['image_data'], 'x': data.get('x', 0), 'y': data.get('y', 0), 'user': username, 'action_id': data.get('action_id')})
                broadcast(room, 'draw_image', data, exclude_ws=ws)
            
            # очистка
            elif event == 'clear_canvas':
                room = data['room']
                canvas_history[room] = [] 
                undone_history[room] = []
                broadcast(room, 'clear_canvas', {})

            # отмена действия
            elif event == 'undo_action':
                room = data['room']
                if room in canvas_history:
                    last_action_id = None
                    # ищем action_id последнего действия пользователя
                    for i in range(len(canvas_history[room]) - 1, -1, -1):
                        if canvas_history[room][i].get('user') == username:
                            last_action_id = canvas_history[room][i].get('action_id')
                            if last_action_id:
                                break
                    # группы чтобы можно было отменить линию или фигуру, т.к. они передаются как много небольших действий
                    if last_action_id:
                        # разделяем на то, что останется и отменяется
                        to_keep = []
                        to_undo = []
                        for item in canvas_history[room]:
                            if item.get('action_id') == last_action_id:
                                to_undo.append(item)
                            else:
                                to_keep.append(item)
                                
                        canvas_history[room] = to_keep
                        
                        # сохраняем в историю отмен
                        if room not in undone_history:
                            undone_history[room] = []
                        undone_history[room].append({'action_id': last_action_id, 'items': to_undo})
                        
                        broadcast(room, 'canvas_state', canvas_history[room])

            # возврат отмененного действия
            elif event == 'redo_action':
                room = data['room']
                if room in undone_history and undone_history[room]:
                    last_undone_idx = None
                    # ищем последнее отмененное действие текущего пользователя
                    for i in range(len(undone_history[room]) - 1, -1, -1):
                        if undone_history[room][i]['items'][0].get('user') == username:
                            last_undone_idx = i
                            break
                            
                    if last_undone_idx is not None:
                        # возвращаем действие на холст
                        action_block = undone_history[room].pop(last_undone_idx)
                        canvas_history[room].extend(action_block['items'])
                        broadcast(room, 'canvas_state', canvas_history[room])

            # сообщение
            elif event == 'chat_message':
                room = data['room']
                msg_data = {'user': username, 'text': data['text']}
                if room not in chat_history:
                    chat_history[room] = []
                chat_history[room].append(msg_data)
                broadcast(room, 'chat_message', msg_data)

    # при любой ошибке выходим из цикла
    except Exception as e:
        pass
    # в любом случае получаем информацию, если что выходим из комнаты
    finally:
        user_info = clients.get(ws)
        if user_info and user_info['room']:
            manual_leave_room(ws, user_info['room'], disconnect=True)
        if ws in clients:
            del clients[ws]
# удаляем запись, вебсокет закрывается

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000, debug=True)