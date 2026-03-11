# fastapi_quiz_app/main.py
from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import pandas as pd
from sentence_transformers import SentenceTransformer, util
import os
import re
import torch
from typing import List, Dict, Optional, Any
import glob
import sqlite3
import hashlib
import secrets
from fastapi.middleware.cors import CORSMiddleware
import json
import time

from datetime import datetime, timedelta
import main2
import app
from session_manager import create_session, active_sessions
from app import app as app_v2

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
app.mount("/v2", app_v2)
app.mount("/main2", main2.app)

# Глобальные данные
MODEL_NAME = 'all-MiniLM-L6-v2'
THRESHOLD = 0.833

# region agent log helper
DEBUG_LOG_PATH = r"c:\Users\Andrey\Desktop\fastapi_quiz_app_v2\.cursor\debug.log"


def debug_log(run_id: str, hypothesis_id: str, location: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
    """Append a single NDJSON debug log line for this debug session."""
    try:
        payload = {
            "id": f"log_{int(time.time() * 1000)}",
            "timestamp": int(time.time() * 1000),
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
        }
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        # Инструментация не должна ломать приложение
        pass


# endregion

# region agent log H1 – SentenceTransformer loading
try:
    model = SentenceTransformer(MODEL_NAME)
    debug_log(
        run_id="post_fix_1",
        hypothesis_id="H1",
        location="main.py:33",
        message="SentenceTransformer model loaded successfully",
        data={"model_name": MODEL_NAME},
    )
except Exception as e:
    # Подтверждённая проблема: нет доступа к huggingface.co, не роняем всё приложение
    model = None
    debug_log(
        run_id="post_fix_1",
        hypothesis_id="H1",
        location="main.py:33",
        message="SentenceTransformer model load failed (graceful fallback)",
        data={"model_name": MODEL_NAME, "error": str(e)},
    )
# endregion
questions = []
reference_answers = []
user_answers = []
all_embeddings = []
quiz_sessions = {}

# Папка для хранения загруженных файлов
UPLOAD_DIR = "uploaded_files"
os.makedirs(UPLOAD_DIR, exist_ok=True)



def hash_password(password: str) -> str:
    """Хеширует пароль с использованием SHA-256 и соли"""
    salt = "quiz_system_salt"  # В реальном приложении используйте уникальную соль для каждого пользователя
    return hashlib.sha256((password + salt).encode()).hexdigest()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверяет пароль"""
    return hash_password(plain_password) == hashed_password

def get_user_from_session(request: Request) -> Optional[str]:
    """Получает пользователя из сессии"""
    session_token = request.cookies.get("session_token")
    if session_token:
        from session_manager import get_user_from_session as get_session_user
        return get_session_user(session_token)
    return None

def login_required(request: Request):
    """Декоратор для проверки аутентификации"""
    user = get_user_from_session(request)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    return user

# Инициализация базы данных с новой структурой
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_type TEXT NOT NULL,
            last_name TEXT NOT NULL,
            first_name TEXT NOT NULL,
            middle_name TEXT,
            group_name TEXT,
            login TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS test_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT UNIQUE NOT NULL,
            time_limit_minutes INTEGER,
            available_until TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Создаем тестового пользователя если его нет
    cursor.execute("SELECT COUNT(*) FROM users WHERE login = 'admin'")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO users (user_type, last_name, first_name, middle_name, group_name, login, password) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("admin", "Иванов", "Иван", "Иванович", "Администраторы", "admin", hash_password("admin123"))
        )
        print("Создан тестовый пользователь: admin / admin123")
    
    conn.commit()
    conn.close()

init_db()


def get_session_token(request: Request) -> Optional[str]:
    return request.cookies.get("session_token")


def parse_available_until(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed_date = datetime.strptime(value, "%Y-%m-%d")
        return parsed_date + timedelta(days=1) - timedelta(seconds=1)
    except ValueError:
        return None


def format_date_ru(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return value


def format_datetime_ru(value: datetime) -> str:
    return value.strftime("%d.%m.%Y %H:%M:%S")


def format_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "--"
    safe_seconds = max(0, int(seconds))
    hours = safe_seconds // 3600
    minutes = (safe_seconds % 3600) // 60
    secs = safe_seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def get_test_settings(filename: str) -> Dict[str, Any]:
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT time_limit_minutes, available_until FROM test_settings WHERE filename = ?",
        (filename,)
    )
    row = cursor.fetchone()
    conn.close()

    time_limit_minutes = None
    available_until = None
    if row:
        if row[0] is not None:
            try:
                parsed_limit = int(row[0])
                time_limit_minutes = parsed_limit if parsed_limit > 0 else None
            except (TypeError, ValueError):
                time_limit_minutes = None
        if row[1]:
            available_until = str(row[1]).strip()

    return {
        "time_limit_minutes": time_limit_minutes,
        "available_until": available_until
    }


def get_quiz_restriction_status(request: Request) -> Dict[str, Any]:
    session_token = get_session_token(request)
    if not session_token:
        return {"error": None, "remaining_seconds": None, "timed_out": False, "deadline_expired": False}

    quiz_state = quiz_sessions.get(session_token)
    if not quiz_state:
        return {"error": None, "remaining_seconds": None, "timed_out": False, "deadline_expired": False}

    now = datetime.now()
    timed_out = False
    deadline_expired = False
    remaining_seconds = None

    available_until_dt = parse_available_until(quiz_state.get("available_until"))
    if available_until_dt and now > available_until_dt:
        deadline_expired = True

    time_limit_minutes = quiz_state.get("time_limit_minutes")
    started_at = quiz_state.get("started_at")
    if time_limit_minutes and started_at:
        elapsed = (now - started_at).total_seconds()
        remaining_seconds = max(0, int(time_limit_minutes * 60 - elapsed))
        if elapsed >= time_limit_minutes * 60:
            timed_out = True

    if timed_out:
        return {
            "error": "Время прохождения теста истекло.",
            "remaining_seconds": 0,
            "timed_out": True,
            "deadline_expired": deadline_expired
        }

    if deadline_expired:
        return {
            "error": "Дедлайн выполнения теста уже прошел.",
            "remaining_seconds": remaining_seconds,
            "timed_out": timed_out,
            "deadline_expired": True
        }

    return {
        "error": None,
        "remaining_seconds": remaining_seconds,
        "timed_out": False,
        "deadline_expired": False
    }


def render_select_with_error(request: Request, error_message: str):
    files = get_uploaded_files()
    context = get_template_context(request)
    context.update({
        "request": request,
        "files": files,
        "error": error_message
    })
    return templates.TemplateResponse("select.html", context)

def parse_quoted_strings(s):
    """Парсит строку с ответами в кавычках, разделенных запятыми"""
    return [m.group(1) for m in re.finditer(r'"([^"]*)"', s)]

def get_uploaded_files():
    """Получает список загруженных файлов"""
    files = []
    for file_path in glob.glob(os.path.join(UPLOAD_DIR, "*.xlsx")):
        filename = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        settings = get_test_settings(filename)
        available_until_dt = parse_available_until(settings.get("available_until"))
        is_available = True
        unavailable_reason = ""
        if available_until_dt and datetime.now() > available_until_dt:
            is_available = False
            unavailable_reason = "Дедлайн уже прошел"
        files.append({
            "name": filename,
            "size": file_size,
            "path": file_path,
            "time_limit_minutes": settings.get("time_limit_minutes"),
            "available_until": settings.get("available_until"),
            "available_until_display": format_date_ru(settings.get("available_until")),
            "is_available": is_available,
            "unavailable_reason": unavailable_reason
        })
    return files

def get_user_by_login(login: str):
    """Получает пользователя из базы данных по логину"""
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, user_type, last_name, first_name, middle_name, group_name, login, password, created_at 
        FROM users WHERE login = ?
    """, (login,))
    user = cursor.fetchone()
    conn.close()
    
    if user:
        return {
            "id": user[0],
            "user_type": user[1],
            "last_name": user[2],
            "first_name": user[3],
            "middle_name": user[4],
            "group_name": user[5],
            "login": user[6],
            "password": user[7],  # Внимание: хранится в открытом виде!
            "created_at": user[8]
        }
    return None

def get_user_full_info(login: str):
    """Получает полную информацию о пользователе для отображения"""
    user = get_user_by_login(login)
    if user:
        # Формируем полное имя
        full_name = f"{user['last_name']} {user['first_name']}"
        if user['middle_name']:
            full_name += f" {user['middle_name']}"
        
        return {
            "login": user['login'],
            "full_name": full_name,
            "user_type": user['user_type'],
            "group_name": user['group_name'],
            "created_at": user['created_at']
        }
    return None


def get_user_permissions(user_type: str):
    """Возвращает доступные права пользователя"""
    permissions = {
        'can_view_tests': True,  # Все могут видеть тесты
        'can_take_tests': True,  # Все могут проходить тесты
        'can_upload_files': user_type in ['teacher', 'admin'],  # Только преподаватели и админы могут загружать файлы
        'can_delete_files': user_type in ['teacher', 'admin'],  # Только преподаватели и админы могут удалять файлы
        'can_manage_users': user_type == 'admin',  # Только админы могут управлять пользователями
        'can_edit_tests': user_type in ['teacher', 'admin'],  # Только преподаватели и админы могут редактировать тесты
    }
    return permissions

def get_template_context(request: Request):
    """Возвращает контекст для шаблонов с информацией о пользователе"""
    login = get_user_from_session(request)
    if login:
        user_info = get_user_full_info(login)
        user_permissions = get_user_permissions(user_info['user_type'])
        return {
            "user_info": user_info,
            "user_permissions": user_permissions
        }
    return {
        "user_info": None,
        "user_permissions": None
    }

@app.get("/", response_class=HTMLResponse)
def login_page(request: Request):
    """Страница входа"""
    # Если пользователь уже авторизован, перенаправляем на выбор файла
    if get_user_from_session(request):
        return RedirectResponse(url="/select", status_code=303)
    
    context = get_template_context(request)
    context["request"] = request
    return templates.TemplateResponse("login.html", context)

@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),  # Изменили на username
    password: str = Form(...)
):
    """Обработка входа"""
    try:
        user = get_user_by_login(username)  # Передаем username как логин
        if not user:
            context = get_template_context(request)
            context.update({
                "request": request,
                "error": "Неверный логин или пароль"
            })
            return templates.TemplateResponse("login.html", context)
        
        # Проверяем пароль (внимание: пароль хранится в открытом виде!)
        if password != user["password"]:
            context = get_template_context(request)
            context.update({
                "request": request,
                "error": "Неверный логин или пароль"
            })
            return templates.TemplateResponse("login.html", context)
        
        # Создаем сессию и устанавливаем cookie
        from session_manager import create_session
        session_token = create_session(username)
        response = RedirectResponse(url="/select", status_code=303)
        response.set_cookie(key="session_token", value=session_token, httponly=True)
        return response
        
    except Exception as e:
        context = get_template_context(request)
        context.update({
            "request": request,
            "error": f"Ошибка входа: {str(e)}"
        })
        return templates.TemplateResponse("login.html", context)

@app.get("/select", response_class=HTMLResponse)
def select_file_page(request: Request):
    """Страница выбора файла с сервера"""
    login = get_user_from_session(request)
    if not login:
        return RedirectResponse(url="/", status_code=303)
    
    user_info = get_user_full_info(login)
    user_permissions = get_user_permissions(user_info['user_type'])
    files = get_uploaded_files()
    
    context = get_template_context(request)
    context.update({
        "request": request,
        "files": files
    })
    
    return templates.TemplateResponse("select.html", context)

@app.post("/upload", response_class=HTMLResponse)
async def upload_file(request: Request, file: UploadFile = File(...)):
    """Загрузка нового файла на сервер"""
    # Проверяем авторизацию
    user_login = get_user_from_session(request)
    if not user_login:
        return RedirectResponse(url="/", status_code=303)
    
    # Проверяем права доступа
    user_info = get_user_full_info(user_login)
    user_permissions = get_user_permissions(user_info['user_type'])
    
    if not user_permissions['can_upload_files']:
        files = get_uploaded_files()
        context = get_template_context(request)
        context.update({
            "request": request,
            "files": files,
            "error": "У вас нет прав для загрузки файлов"
        })
        return templates.TemplateResponse("select.html", context)
    
    try:
        # Сохраняем файл в папку uploaded_files
        file_path = os.path.join(UPLOAD_DIR, str(file.filename))
        with open(file_path, "wb") as f:
            f.write(await file.read())
        
        # Возвращаем на страницу выбора файлов с сообщением об успехе
        files = get_uploaded_files()
        context = get_template_context(request)
        context.update({
            "request": request,
            "files": files,
            "message": f"Файл {file.filename} успешно загружен!"
        })
        return templates.TemplateResponse("select.html", context)
        
    except Exception as e:
        files = get_uploaded_files()
        context = get_template_context(request)
        context.update({
            "request": request,
            "files": files,
            "error": f"Ошибка загрузки файла: {str(e)}"
        })
        return templates.TemplateResponse("select.html", context)

async def load_quiz_data(request: Request, file_path: str):
    """Загружает данные викторины из файла и начинает тест"""
    global questions, reference_answers, user_answers, all_embeddings
    
    try:
        # region agent log H2 – загрузка теста и наличие модели
        debug_log(
            run_id="post_fix_1",
            hypothesis_id="H2",
            location="main.py:312",
            message="load_quiz_data called",
            data={"file_path": file_path, "model_is_none": model is None},
        )
        # endregion

        filename = os.path.basename(file_path)
        settings = get_test_settings(filename)
        available_until_dt = parse_available_until(settings.get("available_until"))
        if available_until_dt and datetime.now() > available_until_dt:
            return render_select_with_error(request, "Нельзя начать тест: дедлайн выполнения уже прошел.")

        if model is None:
            # Модель не загружена (например, нет интернета для HuggingFace) — даём понятную ошибку пользователю
            files = get_uploaded_files()
            context = get_template_context(request)
            context.update({
                "request": request,
                "files": files,
                "error": "Модель проверки ответов не загружена (нет доступа к HuggingFace). "
                         "Обратитесь к администратору: требуется интернет или локальный кэш модели."
            })
            return templates.TemplateResponse("select.html", context)

        df = pd.read_excel(file_path, engine='openpyxl', usecols=[0,1], header=None, names=['q','a'])
        
        questions = df['q'].astype(str).tolist()
        reference_answers = [parse_quoted_strings(answers_str) for answers_str in df['a'].astype(str)]
        
        # Создаем эмбеддинги для всех эталонных ответов
        all_embeddings = []
        for answers_list in reference_answers:
            embeddings = model.encode(answers_list, convert_to_tensor=True)
            all_embeddings.append(embeddings)
        
        user_answers = []

        session_token = get_session_token(request)
        if session_token:
            quiz_sessions[session_token] = {
                "filename": filename,
                "started_at": datetime.now(),
                "time_limit_minutes": settings.get("time_limit_minutes"),
                "available_until": settings.get("available_until")
            }
        
        # Перенаправляем на первый вопрос
        return RedirectResponse(url="/quiz?idx=0", status_code=303)
        
    except Exception as e:
        files = get_uploaded_files()
        context = get_template_context(request)
        context.update({
            "request": request,
            "files": files,
            "error": f"Ошибка загрузки теста: {str(e)}"
        })
        return templates.TemplateResponse("select.html", context)

@app.post("/select", response_class=HTMLResponse)
async def select_existing_file(request: Request, filename: str = Form(...)):
    """Выбор существующего файла с сервера и начало теста"""
    # Проверяем авторизацию
    user = get_user_from_session(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    
    file_path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(file_path):
        files = get_uploaded_files()
        context = get_template_context(request)
        context.update({
            "request": request,
            "files": files,
            "error": f"Файл {filename} не найден на сервере"
        })
        return templates.TemplateResponse("select.html", context)
    
    # Загружаем данные и начинаем тест
    return await load_quiz_data(request, file_path)

@app.get("/files", response_class=JSONResponse)
def get_files_list(request: Request):
    """API для получения списка файлов"""
    user = get_user_from_session(request)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    
    files = get_uploaded_files()
    return {"files": files}

@app.post("/delete_file")
async def delete_file(request: Request, filename: str = Form(...)):
    """Удаление файла с сервера"""
    user_login = get_user_from_session(request)
    if not user_login:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    
    # Проверяем права доступа
    user_info = get_user_full_info(user_login)
    user_permissions = get_user_permissions(user_info['user_type'])
    
    if not user_permissions['can_delete_files']:
        raise HTTPException(status_code=403, detail="У вас нет прав для удаления файлов")
    
    try:
        file_path = os.path.join(UPLOAD_DIR, filename)
        if os.path.exists(file_path):
            os.remove(file_path)
            return JSONResponse({"status": "success", "message": f"Файл {filename} удален"})
        else:
            return JSONResponse({"status": "error", "message": "Файл не найден"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)})

# Эндпоинты для управления пользователями
@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request):
    """Страница управления пользователями"""
    login = get_user_from_session(request)
    if not login:
        return RedirectResponse(url="/", status_code=303)
    
    # Проверяем права доступа
    user_info = get_user_full_info(login)
    user_permissions = get_user_permissions(user_info['user_type'])
    
    if not user_permissions['can_manage_users']:
        # Если нет прав, перенаправляем на главную страницу тестов
        return RedirectResponse(url="/select", status_code=303)
    
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, user_type, last_name, first_name, middle_name, group_name, login, password, created_at 
        FROM users ORDER BY created_at DESC
    """)
    users_data = cursor.fetchall()
    conn.close()
    
    # Форматируем данные для отображения
    users = []
    for user in users_data:
        full_name = f"{user[2]} {user[3]}"
        if user[4]:
            full_name += f" {user[4]}"
        
        users.append({
            "id": user[0],
            "user_type": user[1],
            "full_name": full_name,
            "group_name": user[5],
            "login": user[6],
            "password": user[7],  # Пароль в открытом виде (небезопасно!)
            "created_at": user[8]
        })
    
    context = get_template_context(request)
    context.update({
        "request": request,
        "users": users
    })
    
    return templates.TemplateResponse("admin_users.html", context)

@app.post("/admin/add_user")
async def add_user(
    request: Request,
    user_type: str = Form(...),
    last_name: str = Form(...),
    first_name: str = Form(...),
    middle_name: str = Form(...),
    group_name: str = Form(...),
    login: str = Form(...),
    password: str = Form(...)
):
    """Добавление нового пользователя"""
    current_login = get_user_from_session(request)
    if not current_login:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    
    # Проверяем права доступа
    current_user_info = get_user_full_info(current_login)
    user_permissions = get_user_permissions(current_user_info['user_type'])
    
    if not user_permissions['can_manage_users']:
        raise HTTPException(status_code=403, detail="У вас нет прав для добавления пользователей")
    
    try:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO users 
            (user_type, last_name, first_name, middle_name, group_name, login, password) 
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_type, last_name, first_name, middle_name, group_name, login, password)
        )
        conn.commit()
        conn.close()
        return JSONResponse({"status": "success", "message": f"Пользователь {login} создан"})
    except sqlite3.IntegrityError:
        return JSONResponse({"status": "error", "message": "Пользователь с таким логином уже существует"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)})

@app.post("/admin/delete_user")
async def delete_user(request: Request, user_id: int = Form(...)):
    """Удаление пользователя"""
    current_login = get_user_from_session(request)
    if not current_login:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    
    # Проверяем права доступа
    current_user_info = get_user_full_info(current_login)
    user_permissions = get_user_permissions(current_user_info['user_type'])
    
    if not user_permissions['can_manage_users']:
        raise HTTPException(status_code=403, detail="У вас нет прав для удаления пользователей")
    
    try:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        conn.close()
        return JSONResponse({"status": "success", "message": "Пользователь удален"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)})

# Эндпоинты теста (требуют авторизации)
@app.get("/quiz", response_class=HTMLResponse)
def quiz_form(request: Request, idx: int = 0):
    login = get_user_from_session(request)
    if not login:
        return RedirectResponse(url="/", status_code=303)
    
    user_info = get_user_full_info(login)
    user_permissions = get_user_permissions(user_info['user_type'])

    restriction_status = get_quiz_restriction_status(request)
    if restriction_status["error"]:
        return render_select_with_error(request, restriction_status["error"])
    
    if idx >= len(questions):
        return RedirectResponse(url="/final_results", status_code=303)
    
    current_answer = user_answers[idx] if idx < len(user_answers) else ""
    
    context = get_template_context(request)
    context.update({
        "request": request,
        "question": questions[idx],
        "idx": idx,
        "current_answer": current_answer,
        "total_questions": len(questions),
        "questions": questions,
        "remaining_seconds": restriction_status["remaining_seconds"]
    })
    
    return templates.TemplateResponse("quiz.html", context)

@app.post("/answer")
async def save_answer(request: Request, idx: int = Form(...), user_answer: str = Form(...)):
    user = get_user_from_session(request)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    
    restriction_status = get_quiz_restriction_status(request)
    if restriction_status["error"]:
        raise HTTPException(status_code=403, detail=restriction_status["error"])

    global user_answers
    try:
        while len(user_answers) <= idx:
            user_answers.append("")
            
        user_answers[idx] = user_answer.strip()
        return JSONResponse({"status": "success"})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/navigate")
async def navigate_question(request: Request, current_idx: int = Form(...), direction: str = Form(...)):
    user = get_user_from_session(request)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    
    restriction_status = get_quiz_restriction_status(request)
    if restriction_status["error"]:
        raise HTTPException(status_code=403, detail=restriction_status["error"])

    try:
        if direction == "next":
            new_idx = current_idx + 1
        else:
            new_idx = current_idx - 1
        
        # Проверяем границы
        if new_idx < 0:
            new_idx = 0
        elif new_idx >= len(questions):
            # Если пытаемся перейти за последний вопрос - перенаправляем на завершение
            return RedirectResponse(url="/final_results", status_code=303)
        
        return RedirectResponse(url=f"/quiz?idx={new_idx}", status_code=303)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

async def check_test_completion(request: Request):
    """Общая логика проверки завершения теста"""
    user = get_user_from_session(request)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    
    restriction_status = get_quiz_restriction_status(request)
    if restriction_status["error"]:
        raise HTTPException(status_code=403, detail=restriction_status["error"])

    global user_answers, questions
    unanswered = []
    
    for i in range(len(questions)):
        if i >= len(user_answers) or not user_answers[i].strip():
            unanswered.append(i + 1)
    
    return {
        "completed": len(unanswered) == 0,
        "unanswered": unanswered,
        "total_questions": len(questions),
        "answered_count": sum(1 for ans in user_answers if ans and ans.strip())
    }

@app.post("/check_completion")
async def check_test_completion_post(request: Request): 
    """POST версия для проверки завершения"""
    return await check_test_completion(request)

@app.get("/check_completion")
async def check_test_completion_get(request: Request):
    """GET версия для проверки завершения"""
    return await check_test_completion(request)

@app.get("/final_results", response_class=HTMLResponse)
def show_final_results(request: Request):
    user = get_user_from_session(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    
    user_info = get_user_full_info(user)
    user_permissions = get_user_permissions(user_info['user_type'])
    global user_answers, questions

    session_token = get_session_token(request)
    quiz_state = quiz_sessions.get(session_token) if session_token else None
    now = datetime.now()
    started_at = quiz_state.get("started_at") if quiz_state else None
    finished_at = now
    if quiz_state:
        if quiz_state.get("finished_at"):
            finished_at = quiz_state["finished_at"]
        else:
            quiz_state["finished_at"] = now
            finished_at = now

    elapsed_seconds = None
    if started_at:
        elapsed_seconds = max(0, int((finished_at - started_at).total_seconds()))
    restriction_status = get_quiz_restriction_status(request)
    force_finish = restriction_status["timed_out"] or restriction_status["deadline_expired"]
    
    if len(user_answers) < len(questions) and not force_finish:
        for i in range(len(questions)):
            if i >= len(user_answers) or not user_answers[i].strip():
                context = get_template_context(request)
                context.update({
                    "request": request,
                    "unanswered_index": i,
                    "total_questions": len(questions),
                    "answered_count": sum(1 for ans in user_answers if ans and ans.strip())
                })
                return templates.TemplateResponse("complete_all.html", context)
    
    answers_for_scoring = list(user_answers)
    if len(answers_for_scoring) < len(questions):
        answers_for_scoring.extend([""] * (len(questions) - len(answers_for_scoring)))

    user_embeddings = model.encode(answers_for_scoring, convert_to_tensor=True)
    results = []
    total_correct = 0
    
    for i, (user_emb, user_answer) in enumerate(zip(user_embeddings, answers_for_scoring)):
        if not user_answer.strip():
            results.append({
                "question": questions[i],
                "user_answer": "",
                "is_correct": False,
                "score": "0.00",
                "best_reference_answer": reference_answers[i][0] if reference_answers[i] else "",
                "reference_answers": reference_answers[i],
                "max_similarity": 0.0
            })
            continue

        question_embeddings = all_embeddings[i]
        similarities = util.cos_sim(user_emb, question_embeddings)[0]
        
        max_sim_idx = int(similarities.argmax())
        max_similarity = float(similarities[max_sim_idx])
        best_reference_answer = reference_answers[i][max_sim_idx]
        
        is_correct = max_similarity >= THRESHOLD
        if is_correct:
            total_correct += 1
        
        results.append({
            "question": questions[i],
            "user_answer": user_answer,
            "is_correct": is_correct,
            "score": f"{max_similarity:.2f}",
            "best_reference_answer": best_reference_answer,
            "reference_answers": reference_answers[i],
            "max_similarity": max_similarity
        })
    
    total_questions = len(questions)
    percentage = (total_correct / total_questions) * 100 if total_questions > 0 else 0
    
    context = get_template_context(request)
    context.update({
        "request": request,
        "results": results,
        "total_correct": total_correct,
        "total_questions": total_questions,
        "percentage": f"{percentage:.1f}",
        "threshold": THRESHOLD,
        "restriction_message": restriction_status["error"] if force_finish else None,
        "completed_at_display": format_datetime_ru(finished_at),
        "elapsed_time_display": format_duration(elapsed_seconds)
    })
    
    return templates.TemplateResponse("final_results.html", context)

@app.get("/logout")
def logout():
    """Выход из системы"""
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("session_token")
    return response

@app.get("/app")
def redirect_to_editor(request: Request):
    """Перенаправление на редактор тестов"""
    user = get_user_from_session(request)
    if not user:
        return RedirectResponse(url="/", status_code=303)
    
    user_info = get_user_full_info(user)
    user_permissions = get_user_permissions(user_info['user_type'])
    
    if not user_permissions['can_edit_tests']:
        return RedirectResponse(url="/select", status_code=303)
    
    return RedirectResponse(url="/main2", status_code=307)
# Для запуска:
# uvicorn main:app --reload

