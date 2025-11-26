from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
import random
import string
import pandas as pd
from io import StringIO
from database import init_db, get_db_connection
from models import UserCreate, UserUpdate

app = FastAPI(title="User Registration System")

# Инициализация базы данных
init_db()

# Настройка шаблонов и статических файлов
templates = Jinja2Templates(directory="templatesrg")
app.mount("/static", StaticFiles(directory="static"), name="static")

def generate_login(last_name: str, first_name: str, middle_name: str = None) -> str:
    """Генерация логина в формате фамилия+инициалы на английском"""
    # Транслитерация кириллицы в латиницу
    translit_map = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
        'ы': 'y', 'э': 'e', 'ю': 'yu', 'я': 'ya',
        'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'E',
        'Ж': 'Zh', 'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L', 'М': 'M',
        'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U',
        'Ф': 'F', 'Х': 'Kh', 'Ц': 'Ts', 'Ч': 'Ch', 'Ш': 'Sh', 'Щ': 'Shch',
        'Ы': 'Y', 'Э': 'E', 'Ю': 'Yu', 'Я': 'Ya'
    }
    
    def transliterate(text):
        return ''.join(translit_map.get(char, char) for char in text.lower())
    
    # Транслитерация фамилии
    last_name_en = transliterate(last_name)
    
    # Получение первой буквы имени
    first_initial = transliterate(first_name[0]) if first_name else ''
    
    # Получение первой буквы отчества (если есть)
    middle_initial = transliterate(middle_name[0]) if middle_name else ''
    
    login = f"{last_name_en}{first_initial}{middle_initial}"
    
    return login

def generate_password(length: int = 8) -> str:
    """Генерация случайного пароля"""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))

def user_exists(last_name: str, first_name: str, middle_name: str, user_type: str) -> bool:
    """Проверяет, существует ли пользователь с такими ФИО и типом"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id FROM users 
            WHERE last_name = ? AND first_name = ? AND middle_name = ? AND user_type = ?
        ''', (
            last_name,
            first_name, 
            middle_name or '',
            user_type
        ))
        existing_user = cursor.fetchone()
        return existing_user is not None

def save_user_to_db(user_data: dict) -> dict:
    """Сохранение пользователя в базу данных с обработкой дубликатов"""
    # Проверяем, существует ли пользователь
    if user_exists(
        user_data['last_name'],
        user_data['first_name'],
        user_data.get('middle_name'),
        user_data['user_type']
    ):
        return {"exists": True}
    
    login = user_data['login']
    password = user_data['password']
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Проверка уникальности логина
        cursor.execute("SELECT id FROM users WHERE login = ?", (login,))
        existing_login = cursor.fetchone()
        
        # Если логин уже существует, добавляем число
        counter = 1
        original_login = login
        while existing_login:
            login = f"{original_login}{counter}"
            cursor.execute("SELECT id FROM users WHERE login = ?", (login,))
            existing_login = cursor.fetchone()
            counter += 1
        
        # Сохранение пользователя в базу данных
        cursor.execute('''
            INSERT INTO users (user_type, last_name, first_name, middle_name, group_name, login, password)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_data['user_type'],
            user_data['last_name'],
            user_data['first_name'],
            user_data.get('middle_name'),
            user_data.get('group_name'),
            login,
            password
        ))
        conn.commit()
        user_id = cursor.lastrowid
        
        return {
            "id": user_id, 
            "login": login, 
            "password": password,
            "exists": False
        }

@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/register")
async def show_registration_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.post("/register")
async def register_user(
    request: Request,
    user_type: str = Form(...),
    last_name: str = Form(...),
    first_name: str = Form(...),
    middle_name: str = Form(None),
    group_name: str = Form(None)
):
    # Генерация логина и пароля
    login = generate_login(last_name, first_name, middle_name)
    password = generate_password()
    
    user_data = {
        'user_type': user_type,
        'last_name': last_name,
        'first_name': first_name,
        'middle_name': middle_name,
        'group_name': group_name,
        'login': login,
        'password': password
    }
    
    # Проверяем, существует ли пользователь
    if user_exists(last_name, first_name, middle_name, user_type):
        return templates.TemplateResponse("register.html", {
            "request": request,
            "error": f"Пользователь {last_name} {first_name} {middle_name or ''} уже существует в системе"
        })
    
    # Сохраняем пользователя
    result = save_user_to_db(user_data)
    
    return RedirectResponse(url="/users", status_code=303)

@app.get("/upload")
async def show_upload_form(request: Request):
    return templates.TemplateResponse("upload.html", {"request": request})

@app.post("/upload")
async def upload_users_file(request: Request, file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Файл не выбран")
    
    # Проверяем расширение файла
    if not file.filename.lower().endswith(('.csv', '.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Поддерживаются только CSV и Excel файлы")
    
    content = await file.read()
    
    try:
        if file.filename.lower().endswith('.csv'):
            # Обработка CSV файла
            csv_content = content.decode('utf-8')
            df = pd.read_csv(StringIO(csv_content))
        else:
            # Обработка Excel файла
            df = pd.read_excel(content)
        
        # Проверяем необходимые колонки
        required_columns = ['last_name', 'first_name', 'user_type']
        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            raise HTTPException(
                status_code=400, 
                detail=f"Отсутствуют обязательные колонки: {', '.join(missing_columns)}"
            )
        
        results = {
            'successful': 0,
            'failed': 0,
            'exists': 0,
            'errors': [],
            'existing_users': []
        }
        
        # Обрабатываем каждую строку
        for index, row in df.iterrows():
            try:
                # Подготовка данных
                user_data = {
                    'user_type': str(row['user_type']).strip().lower(),
                    'last_name': str(row['last_name']).strip(),
                    'first_name': str(row['first_name']).strip(),
                    'middle_name': str(row['middle_name']).strip() if 'middle_name' in df.columns and pd.notna(row.get('middle_name')) else None,
                    'group_name': str(row['group_name']).strip() if 'group_name' in df.columns and pd.notna(row.get('group_name')) else None
                }
                
                # Проверка типа пользователя
                if user_data['user_type'] not in ['teacher', 'student']:
                    raise ValueError("Тип пользователя должен быть 'teacher' или 'student'")
                
                # Проверка обязательных полей
                if not user_data['last_name'] or not user_data['first_name']:
                    raise ValueError("Фамилия и имя обязательны для заполнения")
                
                # Для студентов проверяем группу
                if user_data['user_type'] == 'student' and not user_data['group_name']:
                    raise ValueError("Для студента обязательно указание группы")
                
                # Проверяем, существует ли пользователь
                if user_exists(
                    user_data['last_name'],
                    user_data['first_name'],
                    user_data['middle_name'],
                    user_data['user_type']
                ):
                    results['exists'] += 1
                    results['existing_users'].append(
                        f"{user_data['last_name']} {user_data['first_name']} {user_data['middle_name'] or ''} - уже существует"
                    )
                    continue
                
                # Генерация логина и пароля
                user_data['login'] = generate_login(
                    user_data['last_name'], 
                    user_data['first_name'], 
                    user_data['middle_name']
                )
                user_data['password'] = generate_password()
                
                # Сохранение в базу
                result = save_user_to_db(user_data)
                
                if not result.get('exists'):
                    results['successful'] += 1
                
            except Exception as e:
                results['failed'] += 1
                results['errors'].append(f"Строка {index + 2}: {str(e)}")
        
        return templates.TemplateResponse("upload_result.html", {
            "request": request,
            "results": results,
            "total_processed": len(df)
        })
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка обработки файла: {str(e)}")

@app.get("/users")
async def list_users(
    request: Request, 
    sort_by: str = "newest", 
    user_type: str = "all",
    search: str = "",
    group_filter: str = ""
):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Базовый запрос
        query = "SELECT * FROM users WHERE 1=1"
        params = []
        
        # Фильтрация по типу пользователя
        if user_type != "all":
            query += " AND user_type = ?"
            params.append(user_type)
        
        # Фильтрация по группе
        if group_filter:
            query += " AND group_name = ?"
            params.append(group_filter)
        
        # Поиск
        if search:
            query += " AND (last_name LIKE ? OR first_name LIKE ? OR middle_name LIKE ? OR group_name LIKE ? OR login LIKE ?)"
            search_term = f"%{search}%"
            params.extend([search_term, search_term, search_term, search_term, search_term])
        
        # Сортировка
        if sort_by == "alphabet":
            query += " ORDER BY last_name, first_name, middle_name"
        elif sort_by == "newest":
            query += " ORDER BY created_at DESC"
        elif sort_by == "oldest":
            query += " ORDER BY created_at ASC"
        elif sort_by == "group":
            query += " ORDER BY group_name, last_name, first_name"
        
        cursor.execute(query, params)
        users = cursor.fetchall()
        
        # Получаем список всех групп для фильтра
        cursor.execute("SELECT DISTINCT group_name FROM users WHERE group_name IS NOT NULL AND group_name != '' ORDER BY group_name")
        groups = [row['group_name'] for row in cursor.fetchall()]
    
    # Разделение пользователей на преподавателей и студентов
    teachers = [dict(user) for user in users if user['user_type'] == 'teacher']
    students = [dict(user) for user in users if user['user_type'] == 'student']
    
    return templates.TemplateResponse("users_list.html", {
        "request": request,
        "teachers": teachers,
        "students": students,
        "sort_by": sort_by,
        "current_user_type": user_type,
        "search_query": search,
        "group_filter": group_filter,
        "available_groups": groups
    })

@app.get("/users/{user_id}/edit")
async def edit_user_form(request: Request, user_id: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
    
    return templates.TemplateResponse("edit_user.html", {
        "request": request,
        "user": dict(user)
    })

@app.post("/users/{user_id}/update")
async def update_user(user_id: int, request: Request):
    form_data = await request.form()
    
    update_data = UserUpdate(
        last_name=form_data.get("last_name"),
        first_name=form_data.get("first_name"),
        middle_name=form_data.get("middle_name"),
        group_name=form_data.get("group_name"),
        login=form_data.get("login"),
        password=form_data.get("password")
    )
    
    # Подготовка данных для обновления
    update_fields = []
    update_values = []
    
    for field, value in update_data.dict().items():
        if value is not None:
            update_fields.append(f"{field} = ?")
            update_values.append(value)
    
    if update_fields:
        update_values.append(user_id)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE users SET {', '.join(update_fields)} WHERE id = ?",
                update_values
            )
            conn.commit()
    
    return RedirectResponse(url="/users", status_code=303)

@app.post("/users/{user_id}/regenerate_password")
async def regenerate_password(user_id: int):
    new_password = generate_password()
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET password = ? WHERE id = ?",
            (new_password, user_id)
        )
        conn.commit()
    
    return RedirectResponse(url=f"/users/{user_id}/edit", status_code=303)

@app.post("/users/{user_id}/delete")
async def delete_user(user_id: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    
    return RedirectResponse(url="/users", status_code=303)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)