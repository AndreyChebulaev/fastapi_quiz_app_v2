from fastapi import FastAPI, Request, Form, File, UploadFile, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import pandas as pd
import os
from pathlib import Path
from utils import parse_answers, format_answers, process_excel_file, save_excel_file, create_new_excel_file
import json
import shutil
import sqlite3
import secrets
import sys
import importlib
import re
from session_manager import get_user_from_session as get_main_session_user
import ast

app = FastAPI(title="Excel Questions Editor")

IMPORTED_FILES_DIR = Path("uploaded_files_imports")
SAVED_FILES_DIR = Path("uploaded_files")

# Создаем директории
IMPORTED_FILES_DIR.mkdir(exist_ok=True)
SAVED_FILES_DIR.mkdir(exist_ok=True)


def normalize_excel_filename(filename: str) -> str:
    safe_filename = os.path.basename((filename or "").strip())
    if not safe_filename:
        raise HTTPException(status_code=400, detail="Не указано имя файла")
    if not safe_filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Разрешены только Excel файлы")
    return safe_filename


def get_imported_file_path(filename: str) -> Path:
    return IMPORTED_FILES_DIR / normalize_excel_filename(filename)


def get_saved_file_path(filename: str) -> Path:
    return SAVED_FILES_DIR / normalize_excel_filename(filename)

def get_user_from_session(request: Request) -> str:
    """Получает пользователя из сессии (совместимо с основным приложением)"""
    session_token = request.cookies.get("session_token")
    if session_token:
        return get_main_session_user(session_token)
    return None

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
            "password": user[7],
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

# Настройка шаблонов
templates = Jinja2Templates(directory="templates")


def init_test_settings_table():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS test_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT UNIQUE NOT NULL,
            time_limit_minutes INTEGER,
            available_until TEXT,
            max_attempts INTEGER,
            allowed_students TEXT,
            allowed_groups TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Миграция для старых БД
    cursor.execute("PRAGMA table_info(test_settings)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if "allowed_students" not in existing_columns:
        cursor.execute("ALTER TABLE test_settings ADD COLUMN allowed_students TEXT")
    if "allowed_groups" not in existing_columns:
        cursor.execute("ALTER TABLE test_settings ADD COLUMN allowed_groups TEXT")
    if "max_attempts" not in existing_columns:
        cursor.execute("ALTER TABLE test_settings ADD COLUMN max_attempts INTEGER")
    conn.commit()
    conn.close()


def get_test_settings(filename: str):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT time_limit_minutes, available_until, allowed_students, allowed_groups, max_attempts FROM test_settings WHERE filename = ?",
        (filename,)
    )
    row = cursor.fetchone()
    conn.close()

    time_limit_minutes = ""
    available_until = ""
    allowed_students = ""
    allowed_groups = ""
    max_attempts = ""
    if row:
        if row[0] is not None:
            try:
                parsed = int(row[0])
                if parsed > 0:
                    time_limit_minutes = str(parsed)
            except (TypeError, ValueError):
                time_limit_minutes = ""
        if row[1]:
            available_until = str(row[1]).strip()
        if len(row) > 2 and row[2]:
            allowed_students = str(row[2]).strip()
        if len(row) > 3 and row[3]:
            allowed_groups = str(row[3]).strip()
        if len(row) > 4 and row[4] is not None:
            try:
                parsed_attempts = int(row[4])
                if parsed_attempts > 0:
                    max_attempts = str(parsed_attempts)
            except (TypeError, ValueError):
                max_attempts = ""

    return {
        "time_limit_minutes": time_limit_minutes,
        "available_until": available_until,
        "allowed_students": allowed_students,
        "allowed_groups": allowed_groups,
        "max_attempts": max_attempts
    }


def parse_access_list(value: str) -> list[str]:
    if not value:
        return []
    parts = re.split(r"[,\n;]+", str(value))
    return [p.strip() for p in parts if p.strip()]


def normalize_access_value(value: str) -> str:
    parts = parse_access_list(value)
    return ", ".join(parts)


def get_access_students() -> list[dict[str, str]]:
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute("""
        SELECT last_name, first_name, middle_name, login, group_name
        FROM users
        WHERE user_type = 'student'
        ORDER BY last_name, first_name, login
    """)
    rows = cursor.fetchall()
    conn.close()

    students = []
    for last_name, first_name, middle_name, login, group_name in rows:
        if not login:
            continue
        full_name = f"{last_name or ''} {first_name or ''}".strip()
        if middle_name:
            full_name = f"{full_name} {middle_name}".strip()
        label = full_name if full_name else "Без имени"
        if group_name:
            label = f"{label} — {group_name}"
        students.append({
            "value": login,
            "label": label
        })
    return students


def get_access_groups() -> list[str]:
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT group_name
        FROM users
        WHERE group_name IS NOT NULL AND TRIM(group_name) <> ''
          AND user_type != 'admin'
        ORDER BY group_name
    """)
    groups = [row[0] for row in cursor.fetchall() if row[0]]
    conn.close()
    return groups


def build_access_context(test_settings: dict) -> dict:
    return {
        "access_students": get_access_students(),
        "access_groups": get_access_groups(),
        "allowed_students_list": parse_access_list(test_settings.get("allowed_students", "")),
        "allowed_groups_list": parse_access_list(test_settings.get("allowed_groups", "")),
    }


def upsert_test_settings(
    filename: str,
    time_limit_minutes: str,
    max_attempts: str,
    available_until: str,
    allowed_students: str,
    allowed_groups: str
):
    normalized_limit = None
    limit_value = (time_limit_minutes or "").strip()
    if limit_value:
        parsed_limit = int(limit_value)
        if parsed_limit <= 0:
            raise ValueError("Лимит времени должен быть больше 0.")
        normalized_limit = parsed_limit

    normalized_max_attempts = None
    max_attempts_value = (max_attempts or "").strip()
    if max_attempts_value:
        parsed_attempts = int(max_attempts_value)
        if parsed_attempts <= 0:
            raise ValueError("Лимит попыток должен быть больше 0.")
        normalized_max_attempts = parsed_attempts

    normalized_date = (available_until or "").strip() or None
    if normalized_date:
        try:
            pd.to_datetime(normalized_date, format="%Y-%m-%d", errors="raise")
        except Exception:
            raise ValueError("Некорректная дата дедлайна. Используйте формат YYYY-MM-DD.")

    normalized_students = normalize_access_value(allowed_students)
    normalized_groups = normalize_access_value(allowed_groups)
    stored_students = normalized_students if normalized_students else None
    stored_groups = normalized_groups if normalized_groups else None

    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute(
        '''
        INSERT INTO test_settings (filename, time_limit_minutes, max_attempts, available_until, allowed_students, allowed_groups, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(filename) DO UPDATE SET
            time_limit_minutes=excluded.time_limit_minutes,
            max_attempts=excluded.max_attempts,
            available_until=excluded.available_until,
            allowed_students=excluded.allowed_students,
            allowed_groups=excluded.allowed_groups,
            updated_at=CURRENT_TIMESTAMP
        ''',
        (filename, normalized_limit, normalized_max_attempts, normalized_date, stored_students, stored_groups)
    )
    conn.commit()
    conn.close()


init_test_settings_table()

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    # Проверяем авторизацию
    user_login = get_user_from_session(request)
    if not user_login:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/", status_code=303)  # Redirect to main app login
    
    # Проверяем права доступа
    user_info = get_user_full_info(user_login)
    user_permissions = get_user_permissions(user_info['user_type'])
    
    if not user_permissions['can_edit_tests']:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/select", status_code=303)
    
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/editor/create-new", status_code=303)
    
    return templates.TemplateResponse("upload.html", {
        "request": request,
        "user_info": user_info,
        "user_permissions": user_permissions
    })

@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    # Проверяем авторизацию
    user_login = get_user_from_session(request)
    if not user_login:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    
    # Проверяем права доступа
    user_info = get_user_full_info(user_login)
    user_permissions = get_user_permissions(user_info['user_type'])
    
    if not user_permissions['can_edit_tests']:
        raise HTTPException(status_code=403, detail="У вас нет прав для редактирования тестов")
    
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Только Excel файлы разрешены")
    
    # Сохраняем файл
    safe_filename = normalize_excel_filename(file.filename)
    file_path = get_imported_file_path(safe_filename)
    with open(file_path, "wb") as f:
        f.write(await file.read())
    
    # Обрабатываем файл
    try:
        original_data = process_excel_file(str(file_path))
        questions_data = []
        
        # Обрабатываем все строки как данные (без заголовков)
        for index, row in enumerate(original_data):
            if row and len(row) >= 2 and pd.notna(row[0]) and str(row[0]).strip():
                question = str(row[0])
                answers = parse_answers(row[1]) if pd.notna(row[1]) else [""]
                questions_data.append({
                    "index": index,
                    "question": question,
                    "answers": answers
                })
        
        test_settings = get_test_settings(safe_filename)
        context = {
            "request": request,
            "filename": safe_filename,
            "questions": questions_data,
            "user_info": user_info,
            "user_permissions": user_permissions,
            "original_data": json.dumps(original_data, ensure_ascii=False) if original_data else "[]",
            "test_settings": test_settings
        }
        context.update(build_access_context(test_settings))
        return templates.TemplateResponse("edit.html", context)
    
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка обработки файла: {str(e)}")

@app.post("/save")
async def save_file(
    request: Request,
    filename: str = Form(...),
    original_data: str = Form("[]"),
    questions: list[str] = Form(...),
    answers: list[str] = Form(...)
):
    # Проверяем авторизацию
    user_login = get_user_from_session(request)
    if not user_login:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    
    # Проверяем права доступа
    user_info = get_user_full_info(user_login)
    user_permissions = get_user_permissions(user_info['user_type'])
    
    if not user_permissions['can_edit_tests']:
        raise HTTPException(status_code=403, detail="У вас нет прав для редактирования тестов")
    
    try:
        # Определяем, это новый файл или редактирование существующего
        is_new_file = filename == "new_file.xlsx" or not original_data or original_data == "[]"
        
        if is_new_file:
            # СОЗДАНИЕ НОВОГО ФАЙЛА - БЕЗ ЗАГОЛОВКОВ
            output_filename = f"new_questions.xlsx"
            output_path = f"uploaded_files/{output_filename}"
            
            # Подготавливаем данные для нового файла
            new_data = []
            for i in range(len(questions)):
                question_text = questions[i]
                # Парсим ответы из формы и правильно форматируем
                answers_list = [answer.strip() for answer in answers[i].split('","') if answer.strip()]
                # Убираем лишние кавычки
                clean_answers = [answer.strip('"') for answer in answers_list if answer.strip('"')]
                formatted_answers = format_answers(clean_answers)
                
                new_data.append({
                    "question": question_text,
                    "answers": formatted_answers
                })
            
            # Создаем новый файл БЕЗ заголовков
            create_new_excel_file(output_path, new_data)
            
            # Подготавливаем данные для отображения
            display_data = []
            for item in new_data:
                display_data.append({
                    "Question": item["question"],
                    "Answers": item["answers"]
                })
            
        else:
            # РЕДАКТИРОВАНИЕ СУЩЕСТВУЮЩЕГО ФАЙЛА
            # Полностью заменяем содержимое файла с тем же именем
            output_filename = filename
            output_path = f"uploaded_files/{output_filename}"
            
            # Создаем новые данные
            new_data = []
            for i in range(len(questions)):
                question_text = questions[i]
                # Парсим ответы из формы и правильно форматируем
                answers_list = [answer.strip() for answer in answers[i].split('","') if answer.strip()]
                # Убираем лишние кавычки
                clean_answers = [answer.strip('"') for answer in answers_list if answer.strip('"')]
                formatted_answers = format_answers(clean_answers)
                
                new_data.append([question_text, formatted_answers])
            
            # Сохраняем полностью новый файл (не копируем старый)
            save_excel_file(output_path, new_data)
            
            # Подготавливаем данные для отображения
            display_data = []
            for row in new_data:
                display_data.append({
                    "Question": row[0] if len(row) > 0 else "",
                    "Answers": row[1] if len(row) > 1 else ""
                })
        
        return RedirectResponse(url="/select", status_code=303)
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка сохранения: {str(e)}")

@app.get("/download/{filename}")
async def download_file(filename: str, request: Request = None):
    if request:
        # Проверяем авторизацию
        user_login = get_user_from_session(request)
        if not user_login:
            raise HTTPException(status_code=401, detail="Требуется авторизация")
        
        # Проверяем права доступа
        user_info = get_user_full_info(user_login)
        user_permissions = get_user_permissions(user_info['user_type'])
        
        if not user_permissions['can_edit_tests']:
            raise HTTPException(status_code=403, detail="У вас нет прав для скачивания файлов")
    
    safe_filename = normalize_excel_filename(filename)
    file_path = get_saved_file_path(safe_filename)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    
    return FileResponse(
        path=str(file_path),
        filename=safe_filename,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@app.get("/create-new")
async def create_new(request: Request):
    # Проверяем авторизацию
    user_login = get_user_from_session(request)
    if not user_login:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/", status_code=303)  # Redirect to main app login
    
    # Проверяем права доступа
    user_info = get_user_full_info(user_login)
    user_permissions = get_user_permissions(user_info['user_type'])
    
    if not user_permissions['can_edit_tests']:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/select", status_code=303)
    
    test_settings = {
        "time_limit_minutes": "",
        "available_until": "",
        "allowed_students": "",
        "allowed_groups": "",
        "max_attempts": ""
    }
    context = {
        "request": request,
        "filename": "new_file.xlsx",
        "questions": [{"index": 0, "question": "", "answers": [""]}],
        "user_info": user_info,
        "user_permissions": user_permissions,
        "original_data": "[]",
        "test_settings": test_settings
    }
    context.update(build_access_context(test_settings))
    return templates.TemplateResponse("edit.html", context)



@app.get("/edit/{filename}")
async def edit(filename: str, request: Request):
    user_login = get_user_from_session(request)
    if not user_login:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/", status_code=303)

    # Проверяем права доступа
    user_info = get_user_full_info(user_login)
    user_permissions = get_user_permissions(user_info['user_type'])
    
    if not user_permissions['can_edit_tests']:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/select", status_code=303)
    
    # Путь к директории с файлами
    safe_filename = normalize_excel_filename(filename)
    file_path = get_saved_file_path(safe_filename)
    
    if not file_path.exists():
        from fastapi.responses import JSONResponse
        return JSONResponse(
            content={"error": f"Файл {filename} не найден на сервере"},
            status_code=404
        )
    
    # Читаем Excel файл
    try:
        # Читаем Excel файл
        df = pd.read_excel(file_path, header=None)
        
        # Преобразуем данные в нужный формат
        questions_data = []
        
        for index, row in df.iterrows():
            question_text = str(row.iloc[0]) if len(row) > 0 and pd.notna(row.iloc[0]) else ""
            
            # Обрабатываем ответы (второй столбец)
            answers = []
            if len(row) > 1 and pd.notna(row.iloc[1]):
                answers_str = str(row.iloc[1])
                try:
                    # Используем функцию из utils
                    answers = parse_answers(answers_str)
                except:
                    # Если не получается, разбиваем по запятым
                    answers = [a.strip().strip('"').strip("'") for a in answers_str.split(',') if a.strip()]
            
            questions_data.append({
                "question": question_text,
                "answers": answers
            })
        
        # Рендерим шаблон
        from fastapi.templating import Jinja2Templates
        templates = Jinja2Templates(directory="templates")
        
        test_settings = get_test_settings(safe_filename)
        context = {
            "request": request,
            "filename": safe_filename,
            "questions": questions_data,
            "original_data": "excel_file",
            "user_info": user_info,
            "user_permissions": user_permissions,
            "test_settings": test_settings
        }
        context.update(build_access_context(test_settings))
        return templates.TemplateResponse("edit.html", context)
        
    except Exception as e:
        import traceback
        return JSONResponse(
            content={"error": f"Ошибка чтения файла: {str(e)}\n{traceback.format_exc()}"},
            status_code=500
        )

def parse_answers_string(answers_str: str) -> list:
    """Парсит строку с ответами в кавычках"""
    # Убираем лишние пробелы
    answers_str = answers_str.strip()
    
    # Если строка уже похожа на список Python
    if answers_str.startswith('[') and answers_str.endswith(']'):
        try:
            return ast.literal_eval(answers_str)
        except:
            pass
    
    # Разбиваем по кавычкам
    answers = []
    in_quote = False
    current_answer = ""
    
    for char in answers_str:
        if char == '"':
            if in_quote:
                # Закрывающая кавычка
                if current_answer:
                    answers.append(current_answer)
                current_answer = ""
            in_quote = not in_quote
        elif char == ',' and not in_quote:
            # Запятая вне кавычек - разделитель
            if current_answer:
                answers.append(current_answer.strip())
                current_answer = ""
        else:
            if in_quote or char not in [' ', '\t', '\n']:
                current_answer += char
    
    # Добавляем последний ответ
    if current_answer.strip():
        answers.append(current_answer.strip())
    
    return answers

@app.post("/save_edit")
async def save_edit(
    request: Request,
    filename: str = Form(...),
    questions: list[str] = Form(...),
    answers: list[str] = Form(...),
    time_limit_minutes: str = Form(""),
    max_attempts: str = Form(""),
    available_until: str = Form(""),
    allowed_students: str = Form(""),
    allowed_groups: str = Form("")
):
    user_login = get_user_from_session(request)
    if not user_login:
        return JSONResponse(
            content={"error": "Не авторизован"},
            status_code=401
        )
    
    # Проверяем права
    user_info = get_user_full_info(user_login)
    user_permissions = get_user_permissions(user_info['user_type'])
    
    if not user_permissions['can_edit_tests']:
        return JSONResponse(
            content={"error": "Нет прав на редактирование"},
            status_code=403
        )
    
    # Формируем DataFrame для Excel
    data = []
    for i, question in enumerate(questions):
        if i < len(answers):
            try:
                # Парсим JSON массив ответов
                answers_list = json.loads(answers[i])
                # Форматируем ответы в строку с кавычками
                answers_str = ','.join([f'"{answer}"' for answer in answers_list])
            except:
                answers_str = answers[i]
        else:
            answers_str = ""
        
        data.append([question, answers_str])
    
    # Сохраняем в Excel файл в правильной директории
    safe_filename = normalize_excel_filename(filename)
    file_path = get_saved_file_path(safe_filename)

    try:
        # Используем нашу утилиту для сохранения без заголовков
        save_excel_file(str(file_path), data)
        upsert_test_settings(safe_filename, time_limit_minutes, max_attempts, available_until, allowed_students, allowed_groups)

        # Возвращаем JSON ответ для асинхронного запроса
        return JSONResponse(
            content={
                "success": True,
                "message": f"Файл {filename} успешно сохранен",
                "filename": safe_filename
            }
        )
    except Exception as e:
        import traceback
        return JSONResponse(
            content={"error": f"Ошибка сохранения: {str(e)}\n{traceback.format_exc()}"},
            status_code=500
        )



