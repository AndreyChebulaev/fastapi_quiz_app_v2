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
from session_manager import get_user_from_session as get_main_session_user
import ast

app = FastAPI(title="Excel Questions Editor")

# Создаем директории
Path("uploaded_filesd_filesd_files").mkdir(exist_ok=True)

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

# Создаем директории
Path("uploaded_filesd_filesd_files").mkdir(exist_ok=True)

# Настройка шаблонов
templates = Jinja2Templates(directory="templates")

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
    return RedirectResponse(url="/main2/create-new", status_code=303)
    
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
    file_path = f"uploaded_filesd_filesd_files/{file.filename}"
    with open(file_path, "wb") as f:
        f.write(await file.read())
    
    # Обрабатываем файл
    try:
        original_data = process_excel_file(file_path)
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
        
        return templates.TemplateResponse("edit.html", {
            "request": request,
            "filename": file.filename,
            "questions": questions_data,
            "user_info": user_info,
            "user_permissions": user_permissions,
            "original_data": json.dumps(original_data, ensure_ascii=False) if original_data else "[]"
        })
    
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
            # Полностью заменяем содержимое файла
            output_filename = f"edited_{filename}"
            output_path = f"uploaded_filesd_filesd_files/{output_filename}"
            
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
        
        return templates.TemplateResponse("view.html", {
            "request": request,
            "filename": output_filename,
            "data": display_data,
            "user_info": user_info,
            "user_permissions": user_permissions
        })
    
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
    
    file_path = f"uploaded_filesd_filesd_files/{filename}"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Файл не найден")
    
    return FileResponse(
        path=file_path,
        filename=filename,
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
    
    return templates.TemplateResponse("edit.html", {
        "request": request,
        "filename": "new_file.xlsx",
        "questions": [{"index": 0, "question": "", "answers": [""]}],
        "user_info": user_info,
        "user_permissions": user_permissions,
        "original_data": "[]"
    })



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
    FILES_DIR = Path("uploaded_filesd_filesd_files")  # или ваш путь
    
    file_path = FILES_DIR / filename
    
    if not file_path.exists():
        from fastapi.responses import JSONResponse
        return JSONResponse(
            content={"error": f"Файл {filename} не найден на сервере"},
            status_code=404
        )
    
    # Читаем Excel файл
    try:
        # Читаем Excel файл
        df = pd.read_excel(file_path)
        
        # Преобразуем данные в нужный формат
        questions_data = []
        
        for index, row in df.iterrows():
            question_text = str(row.iloc[0]) if pd.notna(row.iloc[0]) else ""
            
            # Обрабатываем ответы (второй столбец)
            answers = []
            if len(row) > 1 and pd.notna(row.iloc[1]):
                answers_str = str(row.iloc[1])
                try:
                    # Пытаемся распарсить как список в кавычках
                    # Пример: "ответ1","ответ2","ответ3"
                    answers = parse_answers_string(answers_str)
                except:
                    # Если не получается, разбиваем по запятым
                    answers = [a.strip().strip('"').strip("'") for a in answers_str.split(',')]
            
            questions_data.append({
                "question": question_text,
                "answers": answers
            })
        
        # Рендерим шаблон
        from fastapi.templating import Jinja2Templates
        templates = Jinja2Templates(directory="templates")
        
        return templates.TemplateResponse(
            "edit.html",
            {
                "request": request,
                "filename": filename,
                "questions": questions_data,
                "original_data": "excel_file"  # Флаг для типа файла
            }
        )
        
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
    answers: list[str] = Form(...)
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
    
    # Создаем DataFrame
    df = pd.DataFrame(data, columns=["Вопрос", "Ответы"])
    
    # Сохраняем в Excel файл
    FILES_DIR = Path("uploaded_filesd_filesd_files")
    file_path = FILES_DIR / filename
    
    try:
        # Сохраняем в Excel
        with pd.ExcelWriter(file_path, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Тест')
        
        return JSONResponse(
            content={
                "success": True,
                "message": f"Файл {filename} успешно сохранен",
                "filename": filename
            }
        )
    except Exception as e:
        import traceback
        return JSONResponse(
            content={"error": f"Ошибка сохранения: {str(e)}\n{traceback.format_exc()}"},
            status_code=500
        )