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
from typing import List, Dict, Optional
import glob
import sqlite3
import hashlib
import secrets
from fastapi.middleware.cors import CORSMiddleware

from datetime import datetime
import main2
import app
from session_manager import create_session, active_sessions


app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
app.mount("/v2", app.app)
app.mount("/main2", main2.app)

# Глобальные данные
MODEL_NAME = 'all-MiniLM-L6-v2'
THRESHOLD = 0.833
model = SentenceTransformer(MODEL_NAME)
questions = []
reference_answers = []
user_answers = []
all_embeddings = []

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

def parse_quoted_strings(s):
    """Парсит строку с ответами в кавычках, разделенных запятыми"""
    return [m.group(1) for m in re.finditer(r'"([^"]*)"', s)]

def get_uploaded_files():
    """Получает список загруженных файлов"""
    files = []
    for file_path in glob.glob(os.path.join(UPLOAD_DIR, "*.xlsx")):
        filename = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        files.append({
            "name": filename,
            "size": file_size,
            "path": file_path
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

@app.get("/", response_class=HTMLResponse)
def login_page(request: Request):
    """Страница входа"""
    # Если пользователь уже авторизован, перенаправляем на выбор файла
    if get_user_from_session(request):
        return RedirectResponse(url="/select", status_code=303)
    
    return templates.TemplateResponse("login.html", {"request": request})

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
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": "Неверный логин или пароль"
            })
        
        # Проверяем пароль (внимание: пароль хранится в открытом виде!)
        if password != user["password"]:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": "Неверный логин или пароль"
            })
        
        # Создаем сессию и устанавливаем cookie
        from session_manager import create_session
        session_token = create_session(username)
        response = RedirectResponse(url="/select", status_code=303)
        response.set_cookie(key="session_token", value=session_token, httponly=True)
        return response
        
    except Exception as e:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": f"Ошибка входа: {str(e)}"
        })

@app.get("/select", response_class=HTMLResponse)
def select_file_page(request: Request):
    """Страница выбора файла с сервера"""
    login = get_user_from_session(request)
    if not login:
        return RedirectResponse(url="/", status_code=303)
    
    user_info = get_user_full_info(login)
    user_permissions = get_user_permissions(user_info['user_type'])
    files = get_uploaded_files()
    
    return templates.TemplateResponse("select.html", {
        "request": request,
        "files": files,
        "user_info": user_info,
        "user_permissions": user_permissions
    })

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
        return templates.TemplateResponse("select.html", {
            "request": request,
            "files": files,
            "user_info": user_info,
            "user_permissions": user_permissions,
            "error": "У вас нет прав для загрузки файлов"
        })
    
    try:
        # Сохраняем файл в папку uploaded_files
        file_path = os.path.join(UPLOAD_DIR, str(file.filename))
        with open(file_path, "wb") as f:
            f.write(await file.read())
        
        # Возвращаем на страницу выбора файлов с сообщением об успехе
        files = get_uploaded_files()
        return templates.TemplateResponse("select.html", {
            "request": request,
            "files": files,
            "user_info": user_info,
            "user_permissions": user_permissions,
            "message": f"Файл {file.filename} успешно загружен!"
        })
        
    except Exception as e:
        files = get_uploaded_files()
        return templates.TemplateResponse("select.html", {
            "request": request,
            "files": files,
            "user_info": user_info,
            "user_permissions": user_permissions,
            "error": f"Ошибка загрузки файла: {str(e)}"
        })

async def load_quiz_data(request: Request, file_path: str):
    """Загружает данные викторины из файла и начинает тест"""
    global questions, reference_answers, user_answers, all_embeddings
    
    try:
        df = pd.read_excel(file_path, engine='openpyxl', usecols=[0,1], header=None, names=['q','a'])
        
        questions = df['q'].astype(str).tolist()
        reference_answers = [parse_quoted_strings(answers_str) for answers_str in df['a'].astype(str)]
        
        # Создаем эмбеддинги для всех эталонных ответов
        all_embeddings = []
        for answers_list in reference_answers:
            embeddings = model.encode(answers_list, convert_to_tensor=True)
            all_embeddings.append(embeddings)
        
        user_answers = []
        
        # Перенаправляем на первый вопрос
        return RedirectResponse(url="/quiz?idx=0", status_code=303)
        
    except Exception as e:
        files = get_uploaded_files()
        user = get_user_from_session(request)
        return templates.TemplateResponse("select.html", {
            "request": request,
            "files": files,
            "username": user,
            "error": f"Ошибка загрузки теста: {str(e)}"
        })

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
        return templates.TemplateResponse("select.html", {
            "request": request,
            "files": files,
            "username": user,
            "error": f"Файл {filename} не найден на сервере"
        })
    
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
    
    return templates.TemplateResponse("admin_users.html", {
        "request": request,
        "users": users,
        "user_info": user_info,
        "user_permissions": user_permissions
    })

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
    
    if idx >= len(questions):
        return RedirectResponse(url="/final_results", status_code=303)
    
    current_answer = user_answers[idx] if idx < len(user_answers) else ""
    
    return templates.TemplateResponse("quiz.html", {
        "request": request,
        "question": questions[idx],
        "idx": idx,
        "current_answer": current_answer,
        "total_questions": len(questions),
        "questions": questions,
        "user_info": user_info,
        "user_permissions": user_permissions
    })

@app.post("/answer")
async def save_answer(request: Request, idx: int = Form(...), user_answer: str = Form(...)):
    user = get_user_from_session(request)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    
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
    
    if len(user_answers) < len(questions):
        for i in range(len(questions)):
            if i >= len(user_answers) or not user_answers[i].strip():
                return templates.TemplateResponse("complete_all.html", {
                    "request": request,
                    "unanswered_index": i,
                    "total_questions": len(questions),
                    "answered_count": sum(1 for ans in user_answers if ans and ans.strip()),
                    "user_info": user_info,
                    "user_permissions": user_permissions
                })
    
    user_embeddings = model.encode(user_answers, convert_to_tensor=True)
    results = []
    total_correct = 0
    
    for i, (user_emb, user_answer) in enumerate(zip(user_embeddings, user_answers)):
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
    
    return templates.TemplateResponse("final_results.html", {
        "request": request,
        "results": results,
        "total_correct": total_correct,
        "total_questions": total_questions,
        "percentage": f"{percentage:.1f}",
        "threshold": THRESHOLD,
        "user_info": user_info,
        "user_permissions": user_permissions
    })

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