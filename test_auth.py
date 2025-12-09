#!/usr/bin/env python3
"""
Тестовая версия приложения для проверки аутентификации
"""

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import sqlite3
import secrets
from session_manager import create_session, get_user_from_session

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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

def get_user_from_session_wrapper(request: Request):
    """Получает пользователя из сессии"""
    session_token = request.cookies.get("session_token")
    if session_token:
        return get_user_from_session(session_token)
    return None

@app.get("/", response_class=HTMLResponse)
def login_page(request: Request):
    """Страница входа"""
    # Если пользователь уже авторизован, перенаправляем на выбор файла
    if get_user_from_session_wrapper(request):
        return RedirectResponse(url="/select", status_code=303)
    
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    """Обработка входа"""
    try:
        user = get_user_by_login(username)
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
    """Страница выбора файла - тестовая"""
    login = get_user_from_session_wrapper(request)
    if not login:
        return RedirectResponse(url="/", status_code=303)
    
    # Простая тестовая страница, показывающая, что пользователь вошел
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Тест - Успешный вход</title>
        <meta charset="utf-8">
    </head>
    <body>
        <h1>Успешный вход!</h1>
        <p>Вы вошли как: {login}</p>
        <a href="/">Выйти</a>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)